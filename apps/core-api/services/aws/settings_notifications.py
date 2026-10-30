# ==============================================================================
# [파일 설명]
# T-7 고객 설정의 플랫폼 소유 자원 — Slack webhook 비밀(Secrets Manager)과 이메일 SNS 토픽 · 구독.
#
#   - 자격은 플랫폼 태스크 역할뿐이다(services.aws.client 의 ECS 전용 세션). 고객 tenant_scope 문맥의
#     AssumeRole 자격을 쓰지 않는다. SDK 재시도 없음(호출당 1회) · 연결 · 응답 제한시간을 둔다.
#   - 클라이언트는 첫 호출 때 만든다 — 만들기만 해서는 AWS 를 부르지 않는다.
#   - 자원 이름은 schemas.settings_resources 의 접두 아래 결정적으로 만든다(배포 IAM 범위와 같은 원천).
#     삭제는 그 접두 아래 이름만 받는다 — 외부에서 만든 기존 토픽 · 비밀은 지우지 않는다.
#   - 오류는 SettingsProviderError(분류 · 고정 코드)로만 올린다. 예외 원문 · URL · 주소 · ARN 은
#     싣지 않는다.
#
# 쓰는 API(배포 정책과 맞춘다): Secrets Manager CreateSecret · GetSecretValue · DeleteSecret,
# SNS CreateTopic · Subscribe(email) · GetSubscriptionAttributes · ListSubscriptionsByTopic · DeleteTopic
# (발송 Publish 는 notifications.channels.SnsSender).
# ==============================================================================

from __future__ import annotations

import re
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from botocore.config import Config
from botocore.exceptions import (
    BotoCoreError,
    ClientError,
    ConnectTimeoutError,
    EndpointConnectionError,
    NoCredentialsError,
    PartialCredentialsError,
    ReadTimeoutError,
)
from notifications.routing import RouteConfigError, parse_sns_topic_arn
from schemas.settings_resources import SETTINGS_SECRET_PREFIX, SETTINGS_SNS_TOPIC_PREFIX

from services.aws import client as aws_client

Category = Literal["TRANSIENT", "PERMANENT", "UNKNOWN", "NOT_FOUND"]
SubscriptionState = Literal["PENDING", "CONFIRMED", "MISSING", "MISMATCH"]

_REGION = r"[a-z]{2}(?:-[a-z]+)+-[0-9]"
_HEX32 = r"[0-9a-f]{32}"
_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_SECRET_ARN_RE = re.compile(
    rf"arn:aws:secretsmanager:({_REGION}):([0-9]{{12}}):secret:"
    rf"({re.escape(SETTINGS_SECRET_PREFIX)}({_UUID})/slack-{_HEX32})-[A-Za-z0-9]{{6}}"
)
_TOPIC_ARN_RE = re.compile(
    rf"arn:aws:sns:({_REGION}):([0-9]{{12}}):({re.escape(SETTINGS_SNS_TOPIC_PREFIX)}"
    rf"({_HEX32})-({_HEX32}))"
)
_VERSION_RE = re.compile(r"[A-Za-z0-9-]{32,64}")

_NOT_FOUND = frozenset({"ResourceNotFoundException", "NotFound", "NotFoundException"})
# ListSubscriptionsByTopic 이 확인 전 구독의 SubscriptionArn 자리에 싣는 값
_PENDING_SENTINEL = "PendingConfirmation"
# 세대마다 토픽 하나 · 구독 하나라 한 쪽이면 충분하다. 넘으면 끝까지 보지 못한 것으로 보고 판정하지 않는다
_MAX_SUBSCRIPTION_PAGES = 20
_TRANSIENT = frozenset({
    "Throttled", "Throttling", "ThrottlingException", "TooManyRequestsException",
    "InternalError", "InternalFailure", "InternalServiceError", "ServiceUnavailable",
    "RequestTimeout", "KMSThrottling",
})


class SettingsProviderError(Exception):
    """분류와 고정 코드만 담는다(원문 없음)."""

    def __init__(self, category: Category, code: str) -> None:
        super().__init__(code)
        self.category = category
        self.code = code


@dataclass(frozen=True)
class SecretRef:
    arn: str
    version_id: str = ""

    def __repr__(self) -> str:
        return "SecretRef(<redacted>)"


# --- 이름 · 소유 판정(네트워크 없음) -------------------------------------------------------


def platform_region() -> str:
    """플랫폼 자원을 만드는 리전 — 플랫폼 기본 리전(AWS_REGION / AWS_REGIONS 첫 값)."""
    return aws_client.default_region()


def topic_name(tenant_id: str, generation: str) -> str:
    return f"{SETTINGS_SNS_TOPIC_PREFIX}{uuid.UUID(tenant_id).hex}-{uuid.UUID(generation).hex}"


def secret_name(tenant_id: str, staging_id: str) -> str:
    return f"{SETTINGS_SECRET_PREFIX}{uuid.UUID(tenant_id)}/slack-{uuid.UUID(staging_id).hex}"


def secret_owner(arn: object) -> tuple[str, str] | None:
    """플랫폼 관리 Slack 비밀 ARN → (리전, 소유 tenant_id). 형식 밖이면 None."""
    match = _SECRET_ARN_RE.fullmatch(arn) if isinstance(arn, str) else None
    return None if match is None else (match.group(1), match.group(4))


def topic_matches(arn: object, tenant_id: str, generation: str) -> str | None:
    """그 테넌트 · 세대의 관리 토픽 ARN 이면 리전, 아니면 None."""
    match = _TOPIC_ARN_RE.fullmatch(arn) if isinstance(arn, str) else None
    if match is None or match.group(3) != topic_name(tenant_id, generation):
        return None
    return match.group(1)


def valid_secret_version(value: object) -> bool:
    return isinstance(value, str) and _VERSION_RE.fullmatch(value) is not None


# --- SDK -------------------------------------------------------------------------------


def platform_client(service: str, region: str, *, connect_timeout: float, read_timeout: float):
    """플랫폼 태스크 역할 자격의 클라이언트(platform_sns_client 와 같은 규칙, SDK 재시도 없음)."""
    if service == "sns":
        return aws_client.platform_sns_client(
            region, connect_timeout=connect_timeout, read_timeout=read_timeout
        )
    config = Config(
        retries={"total_max_attempts": 1, "mode": "standard"},
        user_agent_extra="vigilantis/0.1",
    )
    kwargs = aws_client._client_kwargs(
        config,
        aws_client.endpoint_url(),
        region,
        {"connect_timeout": connect_timeout, "read_timeout": read_timeout},
    )
    return aws_client._ecs_only_session().create_client(service, **kwargs)


def _classify(service: str, exc: BaseException) -> SettingsProviderError:
    if isinstance(exc, SettingsProviderError):
        return exc
    if isinstance(exc, ClientError):
        code = exc.response.get("Error", {}).get("Code")
        if code in _NOT_FOUND:
            return SettingsProviderError("NOT_FOUND", f"{service}_not_found")
        if code in _TRANSIENT:
            return SettingsProviderError("TRANSIENT", f"{service}_{code}"[:64])
        if isinstance(code, str) and re.fullmatch(r"[A-Za-z]{1,48}", code):
            return SettingsProviderError("PERMANENT", f"{service}_{code}"[:64])
        return SettingsProviderError("PERMANENT", f"{service}_unrecognized_error")
    if isinstance(exc, (ConnectTimeoutError, EndpointConnectionError)):
        return SettingsProviderError("TRANSIENT", f"{service}_connect_failed")
    if isinstance(exc, ReadTimeoutError):
        return SettingsProviderError("UNKNOWN", f"{service}_read_timeout")
    if isinstance(exc, (NoCredentialsError, PartialCredentialsError)):
        return SettingsProviderError("PERMANENT", f"{service}_credentials_unavailable")
    if isinstance(exc, BotoCoreError):
        return SettingsProviderError("UNKNOWN", f"{service}_sdk_error")
    return SettingsProviderError("UNKNOWN", f"{service}_unexpected_error")


class SettingsResources:
    """설정 자원 공급자 경계. 스레드 안전(클라이언트 캐시만 잠근다)."""

    def __init__(
        self,
        client_factory: Callable[..., object] = platform_client,
        *,
        connect_timeout: float = 3.0,
        read_timeout: float = 5.0,
    ) -> None:
        self._factory = client_factory
        self._connect_timeout = connect_timeout
        self._read_timeout = read_timeout
        self._clients: dict[tuple[str, str], object] = {}
        self._lock = threading.Lock()

    def _client(self, service: str, region: str):
        key = (service, region)
        with self._lock:
            client = self._clients.get(key)
            if client is None:
                client = self._factory(
                    service, region,
                    connect_timeout=self._connect_timeout, read_timeout=self._read_timeout,
                )
                self._clients[key] = client
        return client

    def _call(self, service: str, region: str, operation: str, **params):
        try:
            return getattr(self._client(service, region), operation)(**params)
        except Exception as exc:  # noqa: BLE001 — 분류 코드만 남긴다
            raise _classify(service, exc) from None

    # --- Slack webhook 비밀 ---------------------------------------------------------

    def create_slack_secret(self, *, region: str, name: str, url: str, request_token: str) -> SecretRef:
        """새 비밀 1개(불변 참조). 기존 비밀은 바꾸지 않는다 — 교체는 새 비밀 + 이전 비밀 정리다."""
        if not name.startswith(SETTINGS_SECRET_PREFIX):
            raise SettingsProviderError("PERMANENT", "secretsmanager_name_not_owned")
        response = self._call(
            "secretsmanager", region, "create_secret",
            Name=name, SecretString=url, ClientRequestToken=request_token,
            Description="Vigilantis tenant Slack webhook",
        )
        arn = response.get("ARN") if isinstance(response, dict) else None
        version = response.get("VersionId") if isinstance(response, dict) else None
        match = _SECRET_ARN_RE.fullmatch(arn) if isinstance(arn, str) else None
        if (
            match is None
            or match.group(1) != region
            or match.group(3) != name
            or not valid_secret_version(version)
        ):
            raise SettingsProviderError("UNKNOWN", "secretsmanager_invalid_response")
        return SecretRef(arn, version)

    def read_slack_secret(self, arn: str, version_id: str) -> str:
        owner = secret_owner(arn)
        if owner is None:
            raise SettingsProviderError("PERMANENT", "secretsmanager_ref_not_owned")
        response = self._call(
            "secretsmanager", owner[0], "get_secret_value", SecretId=arn, VersionId=version_id
        )
        value = response.get("SecretString") if isinstance(response, dict) else None
        if not isinstance(value, str):
            raise SettingsProviderError("PERMANENT", "secretsmanager_invalid_value")
        return value

    def delete_secret(self, *, region: str, name: str) -> None:
        """지운다(복구 대기 없음). 없으면 이미 끝난 것이다. 관리 접두 밖은 거절한다."""
        owned = name.startswith(SETTINGS_SECRET_PREFIX) or secret_owner(name) is not None
        if not owned:
            raise SettingsProviderError("PERMANENT", "secretsmanager_name_not_owned")
        try:
            self._call(
                "secretsmanager", region, "delete_secret",
                SecretId=name, ForceDeleteWithoutRecovery=True,
            )
        except SettingsProviderError as exc:
            if exc.category != "NOT_FOUND":
                raise

    # --- 이메일 SNS ----------------------------------------------------------------

    def create_topic(self, *, region: str, name: str) -> str:
        """결정적 이름의 토픽(같은 이름이면 같은 ARN — 멱등)."""
        if not name.startswith(SETTINGS_SNS_TOPIC_PREFIX):
            raise SettingsProviderError("PERMANENT", "sns_name_not_owned")
        response = self._call("sns", region, "create_topic", Name=name)
        arn = response.get("TopicArn") if isinstance(response, dict) else None
        # 응답 검증은 알림 라우팅의 표준 토픽 ARN 해석(parse_sns_topic_arn)을 같이 쓴다 — ARN 을 조립하지 않는다
        try:
            arn_region, _ = parse_sns_topic_arn(arn)
        except RouteConfigError:
            raise SettingsProviderError("UNKNOWN", "sns_invalid_response") from None
        if arn_region != region or not arn.endswith(f":{name}"):
            raise SettingsProviderError("UNKNOWN", "sns_invalid_response")
        return arn

    def subscribe_email(self, *, topic_arn: str, address: str) -> str:
        """확인 대기 구독도 실제 구독 ARN 을 받는다(ReturnSubscriptionArn). 확인은 수신자가 한다."""
        response = self._call(
            "sns", _arn_region(topic_arn), "subscribe",
            TopicArn=topic_arn, Protocol="email", Endpoint=address, ReturnSubscriptionArn=True,
        )
        arn = response.get("SubscriptionArn") if isinstance(response, dict) else None
        if not isinstance(arn, str) or not arn.startswith(topic_arn + ":") or len(arn) > 512:
            raise SettingsProviderError("UNKNOWN", "sns_invalid_response")
        return arn

    def subscription_state(
        self, *, subscription_arn: str, topic_arn: str, address: str
    ) -> SubscriptionState:
        """확인 대기 · 확인 · 없음(해지 · 만료) · 다른 대상(이 세대의 구독이 아님).

        - 구독 속성 조회가 NotFound 면 MISSING 이다.
        - PendingConfirmation "true" 면 PENDING. 확인된 이메일 구독의 응답은 이 속성을 싣지 않을 수 있어
          "false" · 없음을 확인으로 단정하지 않는다 — 토픽의 구독 목록(모든 쪽)에 저장한 구독 ARN 이 같은
          토픽 · email · 주소로 있을 때만 CONFIRMED 다. 목록의 확인 대기 표시(PendingConfirmation)는 PENDING.
        - 형식이 어긋나거나 판정이 갈리는 응답은 확인으로 보지 않고 UNKNOWN 오류다(상태를 바꾸지 않고 다시 본다).
        """
        region = _arn_region(topic_arn)
        try:
            response = self._call(
                "sns", region, "get_subscription_attributes", SubscriptionArn=subscription_arn,
            )
        except SettingsProviderError as exc:
            if exc.category == "NOT_FOUND":
                return "MISSING"
            raise
        attributes = response.get("Attributes") if isinstance(response, dict) else None
        if not isinstance(attributes, dict):
            raise SettingsProviderError("UNKNOWN", "sns_invalid_response")
        described = [attributes.get(key) for key in ("TopicArn", "Protocol", "Endpoint")]
        if not all(isinstance(value, str) for value in described):
            raise SettingsProviderError("UNKNOWN", "sns_invalid_response")
        if not _same_subscription(*described, topic_arn=topic_arn, address=address) or (
            attributes.get("SubscriptionArn", subscription_arn) != subscription_arn
        ):
            return "MISMATCH"
        pending = attributes.get("PendingConfirmation")
        if pending == "true":
            return "PENDING"
        if pending not in (None, "false"):
            raise SettingsProviderError("UNKNOWN", "sns_invalid_response")
        return self._listed_state(region, subscription_arn, topic_arn, address)

    def _listed_state(
        self, region: str, subscription_arn: str, topic_arn: str, address: str
    ) -> SubscriptionState:
        """토픽의 구독 목록(ListSubscriptionsByTopic) 전체에서 저장한 구독의 확인 여부를 본다."""
        exact = pending = other = False
        token = None
        seen: set[str] = set()
        for _ in range(_MAX_SUBSCRIPTION_PAGES):
            params = {"TopicArn": topic_arn} if token is None else {"TopicArn": topic_arn, "NextToken": token}
            response = self._call("sns", region, "list_subscriptions_by_topic", **params)
            entries = response.get("Subscriptions") if isinstance(response, dict) else None
            if not isinstance(entries, list):
                raise SettingsProviderError("UNKNOWN", "sns_invalid_response")
            for entry in entries:
                fields = [
                    entry.get(key) if isinstance(entry, dict) else None
                    for key in ("SubscriptionArn", "TopicArn", "Protocol", "Endpoint")
                ]
                if not all(isinstance(value, str) for value in fields) or fields[1] != topic_arn:
                    raise SettingsProviderError("UNKNOWN", "sns_invalid_response")
                arn, topic, protocol, endpoint = fields
                same = _same_subscription(topic, protocol, endpoint, topic_arn=topic_arn, address=address)
                if arn == subscription_arn:
                    if not same:
                        return "MISMATCH"
                    exact = True
                elif same:
                    pending = pending or arn == _PENDING_SENTINEL
                    other = other or arn != _PENDING_SENTINEL
            token = response.get("NextToken")
            if token is None:
                break
            if not isinstance(token, str) or not token or token in seen:
                raise SettingsProviderError("UNKNOWN", "sns_invalid_response")
            seen.add(token)
        else:
            raise SettingsProviderError("UNKNOWN", "sns_subscriptions_incomplete")
        if exact and not (pending or other):
            return "CONFIRMED"
        if pending and not (exact or other):
            return "PENDING"
        # 속성은 있는데 목록에 없거나, 같은 주소의 다른 구독과 섞였다 — 확인으로 보지 않는다
        raise SettingsProviderError("UNKNOWN", "sns_subscription_ambiguous")

    def delete_topic(self, *, region: str, name: str) -> None:
        """관리 토픽만 지운다(구독 · 확인 대기 구독도 함께 사라진다). 없는 토픽도 성공이다.

        name 이 ARN 이 아니면(만든 결과를 저장하기 전에 은퇴한 세대) 같은 이름 CreateTopic 으로 ARN 을 얻는다.
        """
        if name.startswith("arn:"):
            match = _TOPIC_ARN_RE.fullmatch(name)
            if match is None or match.group(1) != region:
                raise SettingsProviderError("PERMANENT", "sns_name_not_owned")
            arn = name
        else:
            arn = self.create_topic(region=region, name=name)
        try:
            self._call("sns", region, "delete_topic", TopicArn=arn)
        except SettingsProviderError as exc:
            if exc.category != "NOT_FOUND":
                raise

    def close(self) -> None:
        with self._lock:
            clients, self._clients = self._clients, {}
        for client in clients.values():
            close = getattr(client, "close", None)
            if close is not None:
                try:
                    close()
                except Exception:  # noqa: BLE001 — 정리 실패는 결과를 바꾸지 않는다
                    pass


def _same_subscription(topic: str, protocol: str, endpoint: str, *, topic_arn: str, address: str) -> bool:
    return topic == topic_arn and protocol == "email" and endpoint.casefold() == address.casefold()


def _arn_region(arn: str) -> str:
    parts = arn.split(":")
    if len(parts) < 6 or not re.fullmatch(_REGION, parts[3]):
        raise SettingsProviderError("PERMANENT", "sns_arn_invalid")
    return parts[3]
