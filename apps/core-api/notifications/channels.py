"""Slack incoming webhook / SNS topic 전송. 재시도·간격·DB 상태는 dispatcher 몫이다.

결과에는 안전한 범주·코드만 담는다. 예외 원문·응답 본문·URL·ARN은 결과/로그에 남기지 않는다.
"""

from __future__ import annotations

import http.client
import json
import re
import socket
import ssl
from collections.abc import Callable
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
from services.aws.client import platform_sns_client

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
