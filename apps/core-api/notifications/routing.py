"""운영자 알림 라우트 설정 해석. 설정만 해석하고 발송·DB 활성화(epoch/backfill)는 하지 않는다.

설정 파일(비밀 아님, 최대 64 KiB):
    {"routes": [{"tenant_id": "customer-a", "aws_account_id": "111111111111",
                 "slack_webhook_env": "NOTIFICATION_SLACK_A",
                 "sns_topic_arn": "arn:aws:sns:ap-northeast-2:999999999999:customer-a"}]}
`slack_webhook_env`/`sns_topic_arn` 은 둘 다 생략 가능(소유만 선언 → 이벤트별 CHANNEL_NOT_CONFIGURED 차단).
`{"routes": []}` 도 허용(모든 이벤트 OWNERSHIP_MISSING 차단). 웹훅 URL 자체는 환경변수에만 둔다.

공개 인터페이스
- ``RouteRegistry.from_file(path, environ=None)``; 테스트/호출자용 팩토리 ``RouteRegistry.from_mapping(data, environ)``
  (파일과 같은 검증). 구조 오류는 시작 시 ``RouteConfigError``(ValueError) 로 실패하며 메시지는 고정 문자열이다.
- ``registry.routes``: 유효한 바인딩 ``Route`` 튜플. ``registry.resolve(event, channel) -> RouteDecision``:
  개별 누락·충돌은 예외가 아니라 ``blocked_reason`` 코드로 차단(대체 라우트 없음).
- ``validate_webhook_url(url) -> str`` (검증된 URL 그대로; 아니면 ``RouteConfigError``), ``validate_public_base_url(raw) -> str`` (origin).
- ``parse_sns_topic_arn(arn) -> (region, account_id)``.

소유권은 설정의 (tenant_id, aws_account_id) 로만 정해진다. ARN 의 계정은 일관성 검사에만 쓰며 소유권/계정을
추정하지 않는다. SNS 플랫폼 토픽 계정은 고객 계정과 달라도 된다.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

from .contracts import CHANNELS, Channel, EventSnapshot, Route, RouteDecision

MAX_CONFIG_BYTES = 64 * 1024
MAX_ROUTES = 1000
MAX_TENANT_ID_LENGTH = 128

ACCOUNT_MISSING = "ACCOUNT_MISSING"
ACCOUNT_INVALID = "ACCOUNT_INVALID"
TARGET_ARN_INVALID = "TARGET_ARN_INVALID"
ARN_ACCOUNT_MISMATCH = "ARN_ACCOUNT_MISMATCH"
OWNERSHIP_MISSING = "OWNERSHIP_MISSING"
OWNERSHIP_CONFLICT = "OWNERSHIP_CONFLICT"
CHANNEL_NOT_CONFIGURED = "CHANNEL_NOT_CONFIGURED"
SECRET_MISSING = "SECRET_MISSING"
DESTINATION_INVALID = "DESTINATION_INVALID"
DESTINATION_CONFLICT = "DESTINATION_CONFLICT"
DESTINATION_SHARED = "DESTINATION_SHARED"

_ACCOUNT_RE = re.compile(r"[0-9]{12}")
_ENV_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}")
_WEBHOOK_RE = re.compile(r"https://hooks\.slack\.com/services/[A-Za-z0-9]+/[A-Za-z0-9]+/[A-Za-z0-9]+")
_SNS_ARN_RE = re.compile(
    r"arn:aws:sns:([a-z]{2}(?:-[a-z]+)+-[0-9]):([0-9]{12}):([A-Za-z0-9_-]{1,256})"
)  # 이름에 '.' 불가 → ".fifo" 토픽은 자동 제외
_HOST_RE = re.compile(r"(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}")
_ALLOWED_KEYS = {"tenant_id", "aws_account_id", "slack_webhook_env", "sns_topic_arn"}


class RouteConfigError(ValueError):
    """고정 문구만 담는다. 설정/웹훅/원본 예외 내용을 싣지 않는다."""


def _fail(message: str) -> RouteConfigError:
    return RouteConfigError(message)


def validate_webhook_url(url: object) -> str:
    """엄격한 Slack 웹훅이면 그 URL(엄격 패턴이라 정규형과 동일)을 반환. 아니면 URL 을 담지 않은 RouteConfigError."""
    if not isinstance(url, str) or _WEBHOOK_RE.fullmatch(url) is None:
        raise _fail("invalid slack webhook url")
    return url


def parse_sns_topic_arn(arn: object) -> tuple[str, str]:
    """표준(non-FIFO) SNS 토픽 ARN → (region, account_id)."""
    match = _SNS_ARN_RE.fullmatch(arn) if isinstance(arn, str) else None
    if match is None:
        raise _fail("invalid sns topic arn")
    return match.group(1), match.group(2)


def validate_public_base_url(raw: object) -> str:
    """origin 만 허용(https, 명시적 DNS 호스트, 선택 포트). 경로는 비거나 '/' 만; 정규화된 origin 반환.

    사용처는 origin 뒤에 ``/incidents/<id>`` 를 붙인다. 인증정보·쿼리·프래그먼트·토큰 경로는 거부한다.
    """
    if not isinstance(raw, str) or not raw.isascii() or not raw.isprintable() or " " in raw:
        raise _fail("invalid public base url")
    try:
        parts = urlsplit(raw)
        port = parts.port
    except ValueError:
        raise _fail("invalid public base url") from None
    host = parts.hostname
    if (
        parts.scheme != "https"
        or host is None
        or _HOST_RE.fullmatch(host) is None
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
        or parts.path not in ("", "/")
        or "?" in raw
        or "#" in raw
        or "@" in raw
    ):
        raise _fail("invalid public base url")
    return f"https://{host}" if port is None else f"https://{host}:{port}"


def _fingerprint(tenant_id: str, account: str, channel: str, destination: str) -> str:
    payload = json.dumps(["v1", tenant_id, account, channel, destination], separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _fail("invalid route settings")
        result[key] = value
    return result


class RouteRegistry:
    def __init__(self, routes: tuple[Route, ...], blocked: Mapping[tuple[str, str], str], owned: frozenset[str]):
        self._by_key = {(r.aws_account_id, r.channel): r for r in routes}
        self._blocked = dict(blocked)  # (account, channel) -> reason; "" channel = whole account
        self._owned = owned
        self.routes = tuple(sorted(routes, key=lambda r: (r.aws_account_id, r.channel, r.tenant_id)))

    @classmethod
    def from_file(cls, path: str, environ: Mapping[str, str] | None = None) -> RouteRegistry:
        try:
            with open(path, "rb") as handle:
                raw = handle.read(MAX_CONFIG_BYTES + 1)
        except (OSError, TypeError, ValueError):
            raise _fail("route settings unreadable") from None
        if len(raw) > MAX_CONFIG_BYTES:
            raise _fail("route settings too large")
        try:
            data = json.loads(raw.decode("utf-8"), object_pairs_hook=_no_duplicate_keys)
        except (ValueError, RecursionError):
            raise _fail("invalid route settings") from None
        return cls.from_mapping(data, environ)

    @classmethod
    def from_mapping(cls, data: object, environ: Mapping[str, str] | None = None) -> RouteRegistry:
        """파일과 동일한 검증을 거치는 팩토리(테스트/호출자 편의)."""
        env = os.environ if environ is None else environ
        if not isinstance(data, dict) or set(data) != {"routes"} or not isinstance(data["routes"], list):
            raise _fail("invalid route settings")
        entries = data["routes"]
        if len(entries) > MAX_ROUTES:
            raise _fail("invalid route settings")

        owners: dict[str, set[str]] = {}
        slack: dict[tuple[str, str], set[str]] = {}
        sns: dict[tuple[str, str], set[str]] = {}
        # 선언 도메인 전체: 소유/목적지 충돌·시크릿 누락 등으로 막힌 항목이 선언한 유효 목적지도 포함한다.
        # (유효 라우트만으로 공유를 판정하면, 막힌 항목의 목적지로 건강한 다른 tenant 가 보내게 된다.)
        declared: dict[tuple[str, str], set[str]] = {}
        for entry in entries:
            tenant, account, slack_env, arn = cls._entry(entry)
            owners.setdefault(account, set()).add(tenant)
            if slack_env is not None:
                slack.setdefault((tenant, account), set()).add(slack_env)
                url = env.get(slack_env) or None
                try:
                    validate_webhook_url(url)
                except RouteConfigError:
                    pass
                else:
                    declared.setdefault(("SLACK", url), set()).add(tenant)
            if arn is not None:
                sns.setdefault((tenant, account), set()).add(arn)
                try:
                    parse_sns_topic_arn(arn)
                except RouteConfigError:
                    pass
                else:
                    declared.setdefault(("SNS", arn), set()).add(tenant)

        routes: list[Route] = []
        blocked: dict[tuple[str, str], str] = {}
        for account, tenants in owners.items():
            if len(tenants) > 1:
                blocked[(account, "")] = OWNERSHIP_CONFLICT
        for (tenant, account), names in slack.items():
            if len(owners[account]) > 1:
                continue
            values = {env.get(name) or None for name in names}
            if len(names) > 1 and (None in values or len(values) > 1):
                blocked[(account, "SLACK")] = DESTINATION_CONFLICT
            elif None in values:
                blocked[(account, "SLACK")] = SECRET_MISSING
            else:
                (url,) = values
                try:
                    validate_webhook_url(url)
                except RouteConfigError:
                    blocked[(account, "SLACK")] = DESTINATION_INVALID
                else:
                    routes.append(Route(tenant, account, "SLACK", _fingerprint(tenant, account, "SLACK", url), url))
        for (tenant, account), arns in sns.items():
            if len(owners[account]) > 1:
                continue
            if len(arns) > 1:
                blocked[(account, "SNS")] = DESTINATION_CONFLICT
                continue
            (arn,) = arns
            region, _ = parse_sns_topic_arn(arn)
            routes.append(Route(tenant, account, "SNS", _fingerprint(tenant, account, "SNS", arn), arn, region))
        # 서로 다른 tenant 가 같은 실제 목적지를 공유하면 전부 fail-closed (같은 tenant 의 다중 계정은 허용).
        shared = {key for key, tenants in declared.items() if len(tenants) > 1}
        if shared:
            for r in routes:
                if (r.channel, r.destination) in shared:
                    blocked[(r.aws_account_id, r.channel)] = DESTINATION_SHARED
            routes = [r for r in routes if (r.channel, r.destination) not in shared]
        return cls(tuple(routes), blocked, frozenset(owners))

    @staticmethod
    def _entry(entry: object) -> tuple[str, str, str | None, str | None]:
        if not isinstance(entry, dict) or not set(entry) <= _ALLOWED_KEYS:
            raise _fail("invalid route settings")
        tenant = entry.get("tenant_id")
        account = entry.get("aws_account_id")
        slack_env = entry.get("slack_webhook_env")
        arn = entry.get("sns_topic_arn")
        if (
            not isinstance(tenant, str)
            or not 1 <= len(tenant) <= MAX_TENANT_ID_LENGTH
            or not tenant.strip()
            or not tenant.isprintable()
            or not isinstance(account, str)
            or _ACCOUNT_RE.fullmatch(account) is None
        ):
            raise _fail("invalid route settings")
        if slack_env is not None and (not isinstance(slack_env, str) or _ENV_NAME_RE.fullmatch(slack_env) is None):
            raise _fail("invalid route settings")
        if arn is not None:
            parse_sns_topic_arn(arn)
        return tenant, account, slack_env, arn

    def resolve(self, event: EventSnapshot, channel: Channel) -> RouteDecision:
        if channel not in CHANNELS:
            raise ValueError("unknown channel")
        account = event.aws_account_id
        if account is None or account == "":
            return RouteDecision(None, ACCOUNT_MISSING)
        if _ACCOUNT_RE.fullmatch(account) is None:
            return RouteDecision(None, ACCOUNT_INVALID)
        parts = event.target_arn.split(":") if isinstance(event.target_arn, str) else []
        if len(parts) < 6 or parts[0] != "arn" or _ACCOUNT_RE.fullmatch(parts[4]) is None:
            return RouteDecision(None, TARGET_ARN_INVALID)
        if parts[4] != account:
            return RouteDecision(None, ARN_ACCOUNT_MISMATCH)
        if account not in self._owned:
            return RouteDecision(None, OWNERSHIP_MISSING)
        reason = self._blocked.get((account, "")) or self._blocked.get((account, channel))
        if reason:
            return RouteDecision(None, reason)
        route = self._by_key.get((account, channel))
        if route is None:
            return RouteDecision(None, CHANNEL_NOT_CONFIGURED)
        return RouteDecision(route)
