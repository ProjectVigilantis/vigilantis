"""T-7: 고객 설정(tenant_settings)이 원천인 알림 라우트. 재기동 없이 현재 설정을 쓴다.

- snapshot(db): 호출한 트랜잭션에서 읽은 현재 설정으로 RouteRegistry 를 만든다(발견 · claim 마다).
  소유는 tenants 표(한 계정 = 한 테넌트)다. 기존 규칙을 그대로 쓴다 — 다른 테넌트와 같은 실제
  목적지(Slack URL 해시 · SNS 토픽)는 모두 DESTINATION_SHARED, 비밀 누락은 SECRET_MISSING,
  소유 · 형식이 맞지 않는 참조는 DESTINATION_INVALID, 채널이 없거나 이메일이 READY 가 아니면
  CHANNEL_NOT_CONFIGURED 다. 다른 채널 · 다른 테넌트 · 파일 설정으로 대체하지 않는다.
- 관리 Slack 라우트의 destination 은 비밀 ARN 이다(URL 아님). URL 은 claim 뒤 발신기가 읽는다.
- 바인딩(notification_routes)은 설정을 바꾼 트랜잭션이 sync_tenant 로 같이 바꾼다. 기동 때는
  prepare 가 기존 routes 파일을 아직 정하지 않은 채널에만 가져온 뒤 전체 바인딩을 맞춘다.
  가져온 뒤에는 DB 가 원천이다 — 명시 해제(CLEARED)한 채널은 파일에 남아 있어도 다시 가져오지 않는다.
- 같은 목적지를 공유해 막힌 라우트도 바인딩 후보에는 남긴다(공유가 풀리면 재기동 없이 다시 쓴다).
"""

from __future__ import annotations

import hashlib
import logging
import os
from collections.abc import Iterable, Mapping

from db.repositories import notification_deliveries as deliveries_repo
from db.repositories import tenant_settings as settings_repo
from services.aws.settings_notifications import (
    secret_owner,
    topic_matches,
    valid_secret_version,
)
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .contracts import Route
from .routing import (
    DESTINATION_INVALID,
    DESTINATION_SHARED,
    SECRET_MISSING,
    RouteConfigError,
    RouteRegistry,
    _fingerprint,
    parse_sns_topic_arn,
    validate_webhook_url,
)

_logger = logging.getLogger("vigilantis.notifications")


def destination_hash(url: str) -> str:
    """Slack URL 의 SHA-256 hex — 같은 목적지 판정 · 같은 값 재저장 판정에만 쓴다."""
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def tenant_routes(
    tenant_id: str, account: str, record, environ: Mapping[str, str]
) -> tuple[list[tuple[Route, tuple[str, str]]], dict[str, str]]:
    """한 테넌트의 (라우트, 공유 판정 키) 목록과 채널별 차단 사유. 네트워크 없음."""
    routes: list[tuple[Route, tuple[str, str]]] = []
    blocked: dict[str, str] = {}
    if record is None:
        return routes, blocked
    if record.slack_source == "LEGACY_ENV":
        url = environ.get(record.slack_secret_ref) or None
        if url is None:
            blocked["SLACK"] = SECRET_MISSING
        else:
            try:
                validate_webhook_url(url)
            except RouteConfigError:
                blocked["SLACK"] = DESTINATION_INVALID
            else:
                fingerprint = _fingerprint(tenant_id, account, "SLACK", url)
                routes.append((
                    Route(tenant_id, account, "SLACK", fingerprint, url),
                    ("SLACK", destination_hash(url)),
                ))
    elif record.slack_source == "MANAGED":
        owner = secret_owner(record.slack_secret_ref)
        digest = record.slack_destination_hash
        if (
            owner is None
            or owner[1] != tenant_id
            or not valid_secret_version(record.slack_secret_version)
            or not isinstance(digest, str)
            or len(digest) != 64
        ):
            blocked["SLACK"] = DESTINATION_INVALID
        else:
            fingerprint = _fingerprint(tenant_id, account, "SLACK", f"sha256:{digest}")
            routes.append((
                Route(
                    tenant_id, account, "SLACK", fingerprint, record.slack_secret_ref,
                    region=owner[0], secret_version=record.slack_secret_version,
                    destination_hash=digest,
                ),
                ("SLACK", digest),
            ))
    if record.email_source == "LEGACY_TOPIC":
        arn = record.email_legacy_topic_arn
        try:
            region, _ = parse_sns_topic_arn(arn)
        except RouteConfigError:
            blocked["SNS"] = DESTINATION_INVALID
        else:
            fingerprint = _fingerprint(tenant_id, account, "SNS", arn)
            routes.append((Route(tenant_id, account, "SNS", fingerprint, arn, region), ("SNS", arn)))
    elif record.email_source == "MANAGED" and record.email_status == "READY":
        arn = record.email_topic_arn
        region = topic_matches(arn, tenant_id, str(record.email_generation))
        if region is None:
            blocked["SNS"] = DESTINATION_INVALID
        else:
            fingerprint = _fingerprint(tenant_id, account, "SNS", arn)
            routes.append((Route(tenant_id, account, "SNS", fingerprint, arn, region), ("SNS", arn)))
    return routes, blocked


def build_registry(
    rows: Iterable[tuple[str, str, object]], environ: Mapping[str, str]
) -> tuple[RouteRegistry, list[Route]]:
    """(현재 해석, 바인딩 후보). 후보는 공유 차단 전의 테넌트별 유효 라우트다."""
    owned: set[str] = set()
    declared: dict[tuple[str, str], set[str]] = {}
    keyed: list[tuple[Route, tuple[str, str]]] = []
    blocked: dict[tuple[str, str], str] = {}
    for tenant_id, account, record in rows:
        owned.add(account)
        routes, reasons = tenant_routes(tenant_id, account, record, environ)
        for channel, reason in reasons.items():
            blocked[(account, channel)] = reason
        for route, key in routes:
            declared.setdefault(key, set()).add(tenant_id)
            keyed.append((route, key))
    shared = {key for key, tenants in declared.items() if len(tenants) > 1}
    active = []
    for route, key in keyed:
        if key in shared:
            blocked[(route.aws_account_id, route.channel)] = DESTINATION_SHARED
        else:
            active.append(route)
    return RouteRegistry(tuple(active), blocked, frozenset(owned)), [route for route, _ in keyed]


def sync_tenant(
    db: Session, tenant_id: str, account: str, record, environ: Mapping[str, str] | None = None,
) -> None:
    """설정을 바꾼 트랜잭션 안에서 그 계정의 바인딩을 맞춘다. 호출자는 lock_notification_config 를 쥔다.

    바뀐 채널만 새 epoch · activated_at 을 받고, 없어진 채널은 지운다 — 기존 대기 · 재시도는 poll 전이라도
    claim 대조에서 ROUTE_CHANGED 로 막히고 새 목적지로 옮겨 보내지 않는다.
    """
    routes, _ = tenant_routes(tenant_id, account, record, os.environ if environ is None else environ)
    deliveries_repo.sync_account_routes(db, account, [route for route, _ in routes])


class SettingsRouteSource:
    """DeliveryDispatcher 의 라우트 원천(prepare · snapshot). legacy 는 기존 routes 파일 해석이다."""

    def __init__(
        self, legacy: RouteRegistry | None = None, environ: Mapping[str, str] | None = None,
    ) -> None:
        self._legacy = legacy
        self._environ = os.environ if environ is None else environ

    @property
    def legacy_routes(self) -> tuple[Route, ...]:
        """기동 검사 대상(파일 설정의 유효 라우트). 파일이 없으면 비어 있다."""
        return () if self._legacy is None else self._legacy.routes

    def snapshot(self, db: Session) -> RouteRegistry:
        registry, _ = build_registry(settings_repo.route_rows(db), self._environ)
        return registry

    def prepare(self, db: Session, *, skip: Iterable[Route] = ()) -> list[Route]:
        """기동 동기화 — 설정 쓰기와 직렬화하고, 파일 라우트를 가져온 뒤 바인딩 후보를 돌려준다.

        skip 은 고객이 지워져 경고로 넘긴 파일 라우트다. 가져오지 않고 바인딩 후보에도 넣지 않는다(그 계정의
        사건은 소유 고객이 없어 OWNERSHIP_MISSING 으로 막힌다). 다음 기동이 같은 선언을 다시 경고로 넘길 수
        있도록 증거만 notification_legacy_routes 에 남긴다 — 라우트 · 발송 권한은 되살리지 않는다.
        """
        skip = tuple(skip)
        settings_repo.lock_notification_config(db)
        settings_repo.lock_all(db)
        deliveries_repo.record_legacy_routes(db, skip)
        if self._legacy is not None:
            self._import(db, {(route.aws_account_id, route.channel) for route in skip})
        _, candidates = build_registry(settings_repo.route_rows(db), self._environ)
        return candidates

    def _import(self, db: Session, skipped: set[tuple[str, str]]) -> None:
        names: dict[tuple[str, str], set[str]] = {}
        for tenant, account, env_name, _ in self._legacy.declarations:
            if env_name is not None:
                names.setdefault((tenant, account), set()).add(env_name)
        now = db.scalar(select(func.now()))
        for route in self._legacy.routes:
            if (route.aws_account_id, route.channel) in skipped:
                continue
            record = settings_repo.lock_existing(db, route.tenant_id)
            created = record is None
            if created:
                record = settings_repo.new_record(route.tenant_id, updated_at=now)
            if route.channel == "SLACK" and record.slack_source is None:
                record.slack_source = "LEGACY_ENV"
                record.slack_secret_ref = min(names[(route.tenant_id, route.aws_account_id)])
            elif route.channel == "SNS" and record.email_source is None:
                record.email_source = "LEGACY_TOPIC"
                record.email_legacy_topic_arn = route.destination
            else:
                continue  # 이미 정한 채널(가져옴 · API 설정 · 명시 해제)은 덮어쓰지 않는다
            record.updated_at = now
            if created:
                db.add(record)
            db.flush()
            _logger.info("notification_route_imported", extra={
                "aws_account_id": route.aws_account_id, "channel": route.channel,
            })
