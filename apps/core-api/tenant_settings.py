# ==============================================================================
# [파일 설명]
# /api/v1/tenants/{tenant_id}/settings 의 판정 · 트랜잭션 · 응답 조립 (T-7 · 계약: schemas.api.settings)
#
#   - 범위: auth.access 하나 — CENTRAL_ADMIN 전체, TENANT_MANAGER 배정 고객, 고객사 계정은 자기 고객.
#     쓰기 역할은 access.require_admin(고객사 ADMIN · 테넌트 관리 등급)이다. 범위 밖 · 없는 · 형식이 틀린
#     ID 는 같은 404 TENANT_NOT_FOUND, 정지 고객 쓰기는 409 TENANT_SUSPENDED 다.
#     쓰기는 ACTIVE 에만 허용하며, 연결 전 고객은 409 TENANT_STATE_CONFLICT 로 거절한다.
#   - 쓰기: ① 짧은 사전 판정(잠금 없음) → ② 새 Slack URL 이면 준비 기록 commit 뒤 Secrets Manager 에 새
#     비밀을 만든다(트랜잭션 밖) → ③ app_users 잠금 안에서 세션 · 역할 · 범위 · 고객 상태를 다시 판정하고
#     설정 · 알림 바인딩 · 정리 대기열을 한 commit 으로 바꾼다. ③이 거절 · 실패하면 준비 비밀은 바로 정리
#     차례가 된다(중단돼도 준비 기록의 유예 뒤 정리된다). DB 잠금 안에서 AWS 를 부르지 않는다.
#   - NOTIFICATION_ENABLED=false 이면 이 경로도 AWS 를 부르지 않는다 — 새 Slack URL 저장은 500
#     INTERNAL_ERROR 로 거절하고(저장 안 함), 이메일 · 해제 · 보호 태그는 DB 목표만 바꾼다(관측 · 정리는 켠 뒤).
#   - Slack URL 은 SecretStr 에서 비밀 생성 직전에만 꺼낸다. 응답 · 로그 · 오류에 URL · ARN · 주소를 싣지 않는다.
#   - 이메일: 새 주소 · SETUP_FAILED 의 같은 주소는 새 세대(SETUP_PENDING)이고 이전 세대 토픽은 정리 대기다.
#     그 밖 상태의 같은 주소는 세대 · 진행을 그대로 둔다. 연결 진행은 알림 런타임이 관측한다.
#   - updated_at 은 저장 값이 실제로 바뀐 때만 바뀐다. 감사 기록(T-2)은 남기지 않는다.
# ==============================================================================

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass

from auth import access, admin_service, service
from db import models
from db.repositories import auth as auth_repo
from db.repositories import tenant_settings as settings_repo
from exceptions import ApiError
from identifiers import canonical_id
from notifications.settings_source import destination_hash, sync_tenant
from schemas.api.errors import ErrorCode
from schemas.api.settings import (
    EmailNotificationStatus,
    NotificationSettings,
    ProtectedTagRule,
    TenantSettingsUpdateRequest,
)
from schemas.api.settings import TenantSettings as TenantSettingsResponse
from schemas.api.tenants import TenantStatus
from services.aws.settings_notifications import (
    SecretRef,
    SettingsProviderError,
    SettingsResources,
    platform_region,
    secret_name,
    secret_owner,
    topic_name,
)
from services.rule_engine import default_protected_tags
from sqlalchemy import func, select
from sqlalchemy.orm import Session

logger = logging.getLogger("vigilantis.tenant_settings")

#: 준비 비밀의 정리 유예 — 저장 요청이 그 안에 끝나지 않으면 저장을 거절하고 비밀을 지운다.
STAGING_GRACE_SECONDS = 900
#: 은퇴 자원 정리 여유(진행 중 전송 · 관측 임대가 끝난 뒤)
RETIRE_MARGIN_SECONDS = 60


@dataclass(frozen=True)
class WriteOptions:
    notifications_enabled: bool
    #: 은퇴한 Slack 비밀을 지우기까지의 지연 — 이미 claim 한 전송이 값을 읽을 수 있게 발송 임대보다 길다.
    retire_secret_delay_seconds: float


@dataclass
class _Staged:
    cleanup_id: str
    tenant_id: str
    region: str
    name: str
    ref: SecretRef | None = None


class _StagingRequired(Exception):
    """사전 판정 뒤 Slack 값이 바뀌었다 — 새 비밀을 만든 뒤 다시 저장한다."""


# --- 조회 --------------------------------------------------------------------------


def _target(db: Session, user: service.CurrentUser, tenant_id: str, *, lock: bool = False) -> models.Tenant:
    """범위 안의 고객 행. 형식 오류 · 범위 밖 · 없음은 같은 404 다(DB 캐스트 오류를 내지 않는다)."""
    stored = canonical_id(tenant_id)
    if stored is None or not access.can_access_tenant(user, stored):
        raise ApiError(ErrorCode.TENANT_NOT_FOUND)
    statement = (
        select(models.Tenant)
        .where(models.Tenant.tenant_id == stored)
        .execution_options(populate_existing=True)
    )
    if lock:
        statement = statement.with_for_update(read=True)  # commit 까지 정지 전이를 막는다
    tenant = db.scalars(statement).first()
    if tenant is None:
        raise ApiError(ErrorCode.TENANT_NOT_FOUND)
    return tenant


def _response(tenant: models.Tenant, record: models.TenantSettingsRecord | None) -> TenantSettingsResponse:
    stored_tags = None if record is None else record.protected_tags
    tags = default_protected_tags() if stored_tags is None else stored_tags
    managed = record is not None and record.email_source == "MANAGED"
    return TenantSettingsResponse(
        tenant_id=tenant.tenant_id,
        protected_tags=[ProtectedTagRule(key=rule["key"], values=rule["values"]) for rule in tags],
        notifications=NotificationSettings(
            slack_configured=record is not None and record.slack_source in ("LEGACY_ENV", "MANAGED"),
            # 가져온 외부 SNS 토픽이 아직 발송 중인지(ARN · 수신자는 알리지 않는다). 이메일 상태와 별개다
            legacy_sns_configured=record is not None and record.email_source == "LEGACY_TOPIC",
            email_address=record.email_address if managed else None,
            email_status=(
                EmailNotificationStatus(record.email_status) if managed
                else EmailNotificationStatus.UNSET
            ),
        ),
        updated_at=tenant.created_at if record is None else record.updated_at,
    )


def read_settings(db: Session, user: service.CurrentUser, tenant_id: str) -> TenantSettingsResponse:
    """범위 안이면 역할과 관계없이 읽는다(고객사 OPERATOR · VIEWER 포함). 저장된 값이 없으면 기본 정책이다."""
    try:
        tenant = _target(db, user, tenant_id)
        return _response(tenant, settings_repo.get(db, tenant.tenant_id))
    finally:
        db.rollback()


# --- 쓰기 --------------------------------------------------------------------------


def _resolve_writer(db: Session, actor: admin_service.Actor) -> service.CurrentUser:
    user = service.resolve_current_user(
        db, actor.token, now=actor.clock(), idle_seconds=actor.idle_seconds, touch=False
    )
    access.require_admin(user)
    return user


def _writable_target(db: Session, user: service.CurrentUser, tenant_id: str, *, lock: bool) -> models.Tenant:
    tenant = _target(db, user, tenant_id, lock=lock)
    if tenant.status is TenantStatus.SUSPENDED:
        raise ApiError(ErrorCode.TENANT_SUSPENDED)
    if tenant.status is not TenantStatus.ACTIVE:
        raise ApiError(ErrorCode.TENANT_STATE_CONFLICT)
    return tenant


def _preliminary(db: Session, actor: admin_service.Actor, tenant_id: str) -> tuple[str, str | None]:
    """잠금 없는 사전 판정 — (정규 tenant_id, 현재 관리 Slack URL 해시). 결과와 관계없이 rollback 한다."""
    try:
        tenant = _writable_target(db, _resolve_writer(db, actor), tenant_id, lock=False)
        record = settings_repo.get(db, tenant.tenant_id)
        current = (
            record.slack_destination_hash
            if record is not None and record.slack_source == "MANAGED" else None
        )
        return tenant.tenant_id, current
    finally:
        db.rollback()


def _stage_record(db: Session, tenant_id: str) -> _Staged:
    """비밀을 만들기 전에 정리 기록을 commit 한다 — 그 뒤 어디서 멈춰도 비밀이 남지 않는다."""
    region = platform_region()
    staging_id = str(uuid.uuid4())
    name = secret_name(tenant_id, staging_id)
    try:
        cleanup_id = settings_repo.queue_cleanup(
            db, tenant_id=tenant_id, resource_kind="SECRET", purpose="STAGING",
            region=region, resource_name=name, delay_seconds=STAGING_GRACE_SECONDS,
        )
        db.commit()
    except BaseException:
        db.rollback()
        raise
    return _Staged(cleanup_id=cleanup_id, tenant_id=tenant_id, region=region, name=name)


def _create_secret(resources: SettingsResources, staged: _Staged, url: str) -> None:
    request_token = staged.name.rsplit("-", 1)[1]  # 준비 ID(hex 32자) — 같은 이름의 요청 재전송 식별
    try:
        staged.ref = resources.create_slack_secret(
            region=staged.region, name=staged.name, url=url, request_token=request_token,
        )
    except SettingsProviderError as exc:
        logger.warning("tenant_settings_secret_stage_failed", extra={
            "category": exc.category, "error_code": exc.code,
        })
        raise ApiError(ErrorCode.INTERNAL_ERROR) from None


def _expedite(db: Session, staged: _Staged) -> None:
    try:
        settings_repo.expedite_staging(db, staged.cleanup_id)
        db.commit()
    except Exception:  # noqa: BLE001 — 실패해도 유예 뒤 정리된다
        db.rollback()
        logger.warning("tenant_settings_staging_release_failed")


def _retire_secret(db: Session, record: models.TenantSettingsRecord, options: WriteOptions) -> None:
    owner = secret_owner(record.slack_secret_ref)
    if owner is None:
        return  # 관리 형식이 아니면 지우지 않는다(라우팅도 DESTINATION_INVALID 로 막는다)
    settings_repo.queue_cleanup(
        db, tenant_id=record.tenant_id, resource_kind="SECRET", purpose="RETIRED",
        region=owner[0], resource_name=record.slack_secret_ref,
        delay_seconds=options.retire_secret_delay_seconds,
    )


def _retire_generation(db: Session, record: models.TenantSettingsRecord) -> None:
    """현재 이메일 세대의 관리 토픽을 정리 대기에 넣는다. 한 번도 관측을 시작하지 않은 세대는 만든 토픽이 없다."""
    if record.email_source != "MANAGED":
        return  # 외부에서 만든 기존 토픽(LEGACY_TOPIC)은 지우지 않는다
    if (
        record.email_topic_arn is None
        and record.email_lease_token is None
        and record.email_checked_at is None
    ):
        return
    settings_repo.queue_cleanup(
        db, tenant_id=record.tenant_id, resource_kind="SNS_TOPIC", purpose="RETIRED",
        region=record.email_region,
        resource_name=record.email_topic_arn
        or topic_name(record.tenant_id, str(record.email_generation)),
        not_before=settings_repo.retire_after_lease(record, RETIRE_MARGIN_SECONDS),
    )


def _clear_email(record: models.TenantSettingsRecord, source: str | None) -> None:
    record.email_source = source
    for name in (
        "email_legacy_topic_arn", "email_address", "email_generation", "email_region",
        "email_status", "email_topic_arn", "email_subscription_arn", "email_failure_code",
        "email_next_check_at", "email_lease_token", "email_lease_expires_at", "email_checked_at",
    ):
        setattr(record, name, None)
    record.email_attempts = 0


def _new_generation(record: models.TenantSettingsRecord, address: str) -> None:
    _clear_email(record, "MANAGED")
    record.email_address = address
    record.email_generation = str(uuid.uuid4())
    record.email_region = platform_region()
    record.email_status = "SETUP_PENDING"
    record.email_next_check_at = func.now()


def _apply_slack(
    db: Session, record, url: str | None, staged: _Staged | None, options: WriteOptions,
) -> tuple[bool, bool]:
    """(바뀜, 준비 비밀 사용)."""
    if url is None:
        if record.slack_source == "CLEARED":
            return False, False
        if record.slack_source == "MANAGED":
            _retire_secret(db, record, options)
        record.slack_source = "CLEARED"
        record.slack_secret_ref = record.slack_secret_version = record.slack_destination_hash = None
        return True, False
    digest = destination_hash(url)
    if record.slack_source == "MANAGED" and record.slack_destination_hash == digest:
        return False, False  # 같은 값 — 현재 비밀을 그대로 쓴다
    if staged is None or staged.ref is None:
        raise _StagingRequired
    if not settings_repo.consume_staging(db, staged.cleanup_id):
        logger.warning("tenant_settings_staging_reclaimed")
        raise ApiError(ErrorCode.INTERNAL_ERROR)
    if record.slack_source == "MANAGED":
        _retire_secret(db, record, options)
    record.slack_source = "MANAGED"
    record.slack_secret_ref = staged.ref.arn
    record.slack_secret_version = staged.ref.version_id
    record.slack_destination_hash = digest
    return True, True


def _apply_email(db: Session, record, address: str | None) -> bool:
    if address is None:
        if record.email_source == "CLEARED":
            return False
        _retire_generation(db, record)
        _clear_email(record, "CLEARED")
        return True
    if (
        record.email_source == "MANAGED"
        and record.email_address == address
        and record.email_status != "SETUP_FAILED"
    ):
        return False  # 같은 주소 — 세대 · 진행 유지
    _retire_generation(db, record)
    _new_generation(record, address)
    return True


def _apply(
    db: Session,
    actor: admin_service.Actor,
    tenant_id: str,
    body: TenantSettingsUpdateRequest,
    url: str | None,
    staged: _Staged | None,
    options: WriteOptions,
) -> tuple[TenantSettingsResponse, bool]:
    """잠금 안 재판정 → 변경 → 바인딩 · 정리 → 응답 → commit. 예외는 전부 rollback 이다."""
    try:
        auth_repo.lock_users_table(db)
        db.expire_all()
        user = _resolve_writer(db, actor)
        tenant = _writable_target(db, user, tenant_id, lock=True)
        settings_repo.lock_notification_config(db)
        record = settings_repo.lock_existing(db, tenant.tenant_id)
        created = record is None
        if created:
            record = settings_repo.new_record(tenant.tenant_id, updated_at=tenant.created_at)
        changed_tags = False
        if "protected_tags" in body.model_fields_set:
            tags = [rule.model_dump() for rule in body.protected_tags]
            changed_tags = record.protected_tags != tags
            if changed_tags:
                record.protected_tags = tags
        changed_channels, used = False, False
        notifications = body.notifications
        fields = set() if notifications is None else notifications.model_fields_set
        if "slack_webhook_url" in fields:
            changed_channels, used = _apply_slack(db, record, url, staged, options)
        if "email_address" in fields:
            changed_channels = _apply_email(db, record, notifications.email_address) or changed_channels
        if changed_tags or changed_channels:
            record.updated_at = actor.clock()
            if created:
                db.add(record)
            db.flush()
            if changed_channels:
                sync_tenant(db, tenant.tenant_id, tenant.aws_account_id, record)
        response = _response(tenant, record)
        db.commit()
    except BaseException:
        db.rollback()
        raise
    logger.info("tenant_settings_updated", extra={
        "tenant_id": tenant.tenant_id,
        "protected_tags_changed": changed_tags,
        "notifications_changed": changed_channels,
    })
    return response, used


def update_settings(
    db: Session,
    actor: admin_service.Actor,
    tenant_id: str,
    body: TenantSettingsUpdateRequest,
    *,
    resources: Callable[[], SettingsResources],
    options: WriteOptions,
) -> TenantSettingsResponse:
    notifications = body.notifications
    secret = None
    if notifications is not None and "slack_webhook_url" in notifications.model_fields_set:
        secret = notifications.slack_webhook_url
    url = None if secret is None else secret.get_secret_value()
    staged: _Staged | None = None
    used = False
    try:
        force = False
        while True:
            stored_id, current = _preliminary(db, actor, tenant_id)
            if url is not None and staged is None and (force or current != destination_hash(url)):
                if not options.notifications_enabled:
                    # 알림이 꺼진 배포는 설정 자원 AWS 호출을 하지 않는다 — URL 을 저장할 곳이 없다
                    logger.warning("tenant_settings_secret_store_disabled")
                    raise ApiError(ErrorCode.INTERNAL_ERROR)
                staged = _stage_record(db, stored_id)
                _create_secret(resources(), staged, url)
            try:
                response, used = _apply(db, actor, stored_id, body, url, staged, options)
            except _StagingRequired:
                if force:
                    raise ApiError(ErrorCode.INTERNAL_ERROR) from None
                force = True
                continue
            return response
    finally:
        if staged is not None and not used:
            _expedite(db, staged)
