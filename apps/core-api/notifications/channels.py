"""Slack incoming webhook / SNS topic 전송. 재시도·간격·DB 상태는 dispatcher 몫이다.

결과에는 안전한 범주·코드만 담는다. 예외 원문·응답 본문·URL·ARN은 결과/로그에 남기지 않는다.
"""

from __future__ import annotations

import http.client
import json
import re
import socket
import ssl
import threading
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import replace
from urllib.parse import urlsplit

from botocore.exceptions import (
    BotoCoreError,
    ClientError,
    ConnectTimeoutError,
    EndpointConnectionError,
    NoCredentialsError,
    PartialCredentialsError,
    ReadTimeoutError,
)
from notifications.contracts import EventSnapshot, Route, SendResult
from notifications.messages import SNS_SUBJECT, slack_payload, sns_body
from notifications.routing import validate_webhook_url
from notifications.settings_source import destination_hash
from services.aws.client import platform_sns_client
from services.aws.settings_notifications import SettingsProviderError

_SLACK_READ_LIMIT = 256
_SNS_BODY_MAX_BYTES = 262144
_MESSAGE_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,100}")
_RETRY_AFTER_RE = re.compile(r"[0-9]+")
# Retry-After 상한(1년). 넘으면 저장 가능한 시각을 보장할 수 없어 재시도하지 않는다.
MAX_RETRY_AFTER_SECONDS = 365 * 24 * 3600


def _fail(category: str, code: str, retry_after: float | None = None) -> SendResult:
    return SendResult(
        failure_category=category, error_code=code, retry_after_seconds=retry_after
    )


class SlackSender:
    def __init__(
        self,
        connect_timeout: float = 3.0,
        read_timeout: float = 5.0,
        *,
        connection_factory: Callable[..., http.client.HTTPSConnection] = (
            http.client.HTTPSConnection
        ),
    ) -> None:
        self._connect_timeout = connect_timeout
        self._read_timeout = read_timeout
        self._connection_factory = connection_factory

    def send(
        self, event: EventSnapshot, route: Route, public_base_url: str
    ) -> SendResult:
        body = json.dumps(
            slack_payload(event, route, public_base_url), ensure_ascii=False
        ).encode("utf-8")
        try:
            parts = urlsplit(validate_webhook_url(route.destination))
        except ValueError:
            return _fail("PERMANENT", "slack_invalid_destination")
        target = parts.path

        try:
            conn = self._connection_factory(
                parts.hostname, parts.port, timeout=self._connect_timeout
            )
        except Exception:
            return _fail("TRANSIENT", "slack_connect_failed")
        result = self._exchange(conn, target, body)
        try:
            conn.close()
        except Exception:
            pass  # 정리 실패가 이미 관측한 결과를 바꾸지 않는다.
        return result

    def _exchange(self, conn, target: str, body: bytes) -> SendResult:
        """예외를 밖으로 내지 않는다(원문에 웹훅 URL이 담길 수 있다)."""
        try:
            conn.connect()
        except socket.timeout:
            return _fail("TRANSIENT", "slack_connect_timeout")
        except ssl.SSLError:
            return _fail("TRANSIENT", "slack_tls_error")
        except Exception:
            return _fail("TRANSIENT", "slack_connect_failed")
        # 연결 이후의 실패는 이미 전송됐을 수 있어 비전달을 단정하지 않는다.
        try:
            conn.sock.settimeout(self._read_timeout)
            conn.request(
                "POST",
                target,
                body,
                {
                    "Content-Type": "application/json; charset=utf-8",
                    "Connection": "close",
                },
            )
            response = conn.getresponse()
            status = response.status
            if status == 429:
                # 본문은 필요 없다. 읽다 실패해 알려진 Retry-After를 잃지 않는다.
                return self._rate_limited(response.getheader("Retry-After"))
            data = response.read(_SLACK_READ_LIMIT + 1)
        except socket.timeout:
            return _fail("UNKNOWN", "slack_read_timeout")
        except Exception:
            return _fail("UNKNOWN", "slack_io_error")
        return self._classify(status, data)

    @staticmethod
    def _rate_limited(header: str | None) -> SendResult:
        value = (header or "").strip()
        if not _RETRY_AFTER_RE.fullmatch(value):
            return _fail("TRANSIENT", "slack_http_429")
        digits = value.lstrip("0") or "0"
        if len(digits) > 12 or int(digits) > MAX_RETRY_AFTER_SECONDS:
            # 일찍 재시도하지 않도록 더 이상 보내지 않는다.
            return _fail("PERMANENT", "slack_retry_after_out_of_range")
        return _fail("TRANSIENT", "slack_http_429", float(int(digits)))

    @staticmethod
    def _classify(status: int, data: bytes) -> SendResult:
        if status == 200:
            if data == b"ok":
                return SendResult(receipt_kind="SLACK_POSTED")
            return _fail("UNKNOWN", "slack_unexpected_body")
        if 300 <= status < 400:
            return _fail("PERMANENT", "slack_redirect_refused")
        if 400 <= status < 500:
            return _fail("PERMANENT", f"slack_http_{status}")
        if status >= 500:
            return _fail("UNKNOWN", f"slack_http_{status}" if status < 600 else "slack_http_5xx")
        return _fail("UNKNOWN", "slack_unexpected_status")

    def close(self) -> None:
        return None


class ManagedSlackSender:
    """T-7 관리 비밀 참조(secret_version 이 있는 라우트)의 Slack 전송. 그 밖의 라우트는 그대로 넘긴다.

    URL 은 claim 이 끝난 뒤(DB 트랜잭션 밖) 불변 참조(ARN · VersionId)로 읽고, 라우트의 destination_hash 와
    같을 때만 보낸다. 같은 참조의 값은 바뀌지 않으므로 프로세스 안에 제한된 개수만 캐시한다.
    읽기 실패는 아직 아무것도 보내지 않은 것이라 PERMANENT · TRANSIENT 로만 나눈다(UNKNOWN 아님).
    """

    def __init__(
        self, inner: SlackSender, read_secret: Callable[[str, str], str], *, cache_size: int = 256,
    ) -> None:
        self._inner = inner
        self._read_secret = read_secret
        self._cache: OrderedDict[tuple[str, str], str] = OrderedDict()
        self._cache_size = cache_size
        self._lock = threading.Lock()

    def send(
        self, event: EventSnapshot, route: Route, public_base_url: str
    ) -> SendResult:
        if route.secret_version is None:
            return self._inner.send(event, route, public_base_url)
        try:
            url = self._resolve(route.destination, route.secret_version)
        except SettingsProviderError as exc:
            if exc.category in ("PERMANENT", "NOT_FOUND"):
                return _fail("PERMANENT", "slack_secret_unavailable")
            return _fail("TRANSIENT", "slack_secret_lookup_failed")
        except Exception:
            return _fail("TRANSIENT", "slack_secret_lookup_failed")
        if route.destination_hash is None or destination_hash(url) != route.destination_hash:
            return _fail("PERMANENT", "slack_secret_mismatch")
        resolved = replace(route, destination=url, secret_version=None, destination_hash=None)
        return self._inner.send(event, resolved, public_base_url)

    def _resolve(self, arn: str, version: str) -> str:
        key = (arn, version)
        with self._lock:
            cached = self._cache.get(key)
            if cached is not None:
                self._cache.move_to_end(key)
                return cached
        value = self._read_secret(arn, version)
        with self._lock:
            self._cache[key] = value
            while len(self._cache) > self._cache_size:
                self._cache.popitem(last=False)
        return value

    def close(self) -> None:
        with self._lock:
            self._cache.clear()
        self._inner.close()


_SNS_PERMANENT = frozenset({
    "InvalidParameter", "InvalidParameterValue", "ValidationError", "NotFound",
    "AuthorizationError", "AccessDenied", "AccessDeniedException", "InvalidSecurity",
    "EndpointDisabled", "KMSAccessDenied", "KMSDisabled", "KMSInvalidState",
    "KMSNotFound", "KMSOptInRequired", "InvalidClientTokenId", "SignatureDoesNotMatch",
    "ExpiredToken",
})
_SNS_TRANSIENT = frozenset({
    "Throttled", "Throttling", "ThrottlingException", "KMSThrottling",
})
_SNS_UNKNOWN = frozenset({
    "InternalError", "InternalFailure", "ServiceUnavailable", "RequestTimeout",
})


def _sns_error(exc: Exception) -> SendResult:
    if isinstance(exc, ClientError):
        code = exc.response.get("Error", {}).get("Code")
        if code in _SNS_PERMANENT:
            return _fail("PERMANENT", f"sns_{code}")
        if code in _SNS_TRANSIENT:
            return _fail("TRANSIENT", f"sns_{code}")
        if code in _SNS_UNKNOWN:
            return _fail("UNKNOWN", f"sns_{code}")
        return _fail("UNKNOWN", "sns_unrecognized_error")
    if isinstance(exc, (ConnectTimeoutError, EndpointConnectionError)):
        return _fail("TRANSIENT", "sns_connect_failed")
    if isinstance(exc, ReadTimeoutError):
        return _fail("UNKNOWN", "sns_read_timeout")
    if isinstance(exc, (NoCredentialsError, PartialCredentialsError)):
        return _fail("PERMANENT", "sns_credentials_unavailable")
    if isinstance(exc, BotoCoreError):
        return _fail("UNKNOWN", "sns_sdk_error")
    return _fail("UNKNOWN", "sns_unexpected_error")


class SnsSender:
    def __init__(
        self,
        client_factory: Callable[..., object] = platform_sns_client,
        connect_timeout: float = 3.0,
        read_timeout: float = 5.0,
    ) -> None:
        self._client_factory = client_factory
        self._connect_timeout = connect_timeout
        self._read_timeout = read_timeout
        self._clients: dict[str, object] = {}

    def send(
        self, event: EventSnapshot, route: Route, public_base_url: str
    ) -> SendResult:
        region = route.region
        if not region:
            return _fail("PERMANENT", "sns_region_missing")
        message = sns_body(event, route, public_base_url)
        if len(message.encode("utf-8")) > _SNS_BODY_MAX_BYTES:
            return _fail("PERMANENT", "sns_message_too_large")
        try:
            client = self._clients.get(region)
            if client is None:
                client = self._client_factory(
                    region,
                    connect_timeout=self._connect_timeout,
                    read_timeout=self._read_timeout,
                )
                self._clients[region] = client
            response = client.publish(
                TopicArn=route.destination, Subject=SNS_SUBJECT, Message=message
            )
        except Exception as exc:
            return _sns_error(exc)
        message_id = response.get("MessageId") if isinstance(response, dict) else None
        if not isinstance(message_id, str) or not _MESSAGE_ID_RE.fullmatch(message_id):
            # 요청은 수락됐을 수 있으므로 비전달로 단정하지 않는다.
            return _fail("UNKNOWN", "sns_invalid_message_id")
        return SendResult(receipt_kind="SNS_ACCEPTED", receipt_ref=message_id)

    def close(self) -> None:
        clients, self._clients = self._clients, {}
        for client in clients.values():
            close = getattr(client, "close", None)
            if close is not None:
                try:
                    close()
                except Exception:
                    pass
