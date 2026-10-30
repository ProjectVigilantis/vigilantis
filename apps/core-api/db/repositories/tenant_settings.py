# ==============================================================================
# [파일 설명]
# T-7 고객 설정 저장소 — tenant_settings · tenant_settings_cleanups. commit하지 않는다.
#
#   - 트랜잭션은 호출부(tenant_settings Workflow · 알림 런타임)가 소유한다. 네트워크 호출이 없다.
#   - 알림 바인딩(notification_routes)을 바꾸는 쓰기는 lock_notification_config 로 직렬화한다
#     (API 저장 · 이메일 관측 반영 · 기동 동기화가 서로의 바인딩을 덮어쓰지 않게).
#   - 이메일 관측 반영은 세대(email_generation) · 임대 토큰 CAS 다. 바뀐 세대의 늦은 결과는 0행이다.
#   - 정리 대기열의 claim 은 not_before 를 미래로 미는 임대이고, 결과는 claim 때의 attempts 로 막는다.
# ==============================================================================

from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import delete, func, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from .. import models

_S = models.TenantSettingsRecord
_C = models.TenantSettingsCleanup
_T = models.Tenant
_NO_SYNC = {"synchronize_session": False}
# 알림 설정 · 바인딩 쓰기의 트랜잭션 advisory lock 키(임의의 고정 값)
_CONFIG_LOCK_KEY = 7_493_700_000_001
#: 이메일 관측을 계속하는 상태(SETUP_FAILED 는 사용자 재시도 전까지 멈춘다)
EMAIL_ACTIVE_STATUSES = ("SETUP_PENDING", "CONFIRMATION_PENDING", "READY")


def _after(seconds: float):
    return func.now() + timedelta(seconds=seconds)


def _canonical(values: Iterable[object]) -> list[str]:
    result = set()
    for value in values:
        try:
            result.add(str(uuid.UUID(str(value))))
        except ValueError:
            continue  # UUID 가 아니면 저장된 행도 없다 — DB 캐스트 오류를 내지 않는다
    return sorted(result)


# --- 규칙 판정이 읽는 보호 태그 정책 -------------------------------------------------


def load_protected_tag_policies(
    db: Session, tenant_ids: Iterable[object]
) -> dict[str, list[dict] | None]:
    """테넌트별 저장 정책을 한 번의 조회로 읽는다. 키는 정규 UUID 문자열이다.

    행이 없는 테넌트는 결과에서 빠지고, None 은 저장된 정책 없음(기본 정책), [] 는 태그 보호 없음이다.
    규칙은 호출마다 새 dict · list 다. commit · 네트워크 호출이 없다.
    """
    ids = _canonical(tenant_ids)
    if not ids:
        return {}
    rows = db.execute(select(_S.tenant_id, _S.protected_tags).where(_S.tenant_id.in_(ids))).all()
    return {
        str(tenant_id): None if tags is None else [
            {"key": rule["key"], "values": list(rule["values"])} for rule in tags
        ]
        for tenant_id, tags in rows
    }


# --- 행 · 잠금 ----------------------------------------------------------------------


def lock_notification_config(db: Session) -> None:
    """알림 설정 · 바인딩 쓰기를 트랜잭션 끝까지 직렬화한다."""
    db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _CONFIG_LOCK_KEY})


def get(db: Session, tenant_id: str) -> _S | None:
    """현재 값(identity map 을 DB 값으로 덮어 읽는다). tenant_id 는 정규 UUID 문자열이어야 한다."""
    return db.scalars(
        select(_S).where(_S.tenant_id == tenant_id).execution_options(populate_existing=True)
    ).first()


def lock_existing(db: Session, tenant_id: str) -> _S | None:
    """설정 행을 잠근다. 없으면 None — 새 행은 호출부가 바뀐 값이 있을 때만 new_record 로 더한다."""
    return db.scalars(
        select(_S).where(_S.tenant_id == tenant_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).first()


def new_record(tenant_id: str, *, updated_at: datetime) -> _S:
    """정책 · 채널이 모두 미정인 새 행(아직 세션에 넣지 않는다)."""
    return _S(tenant_id=tenant_id, updated_at=updated_at, email_attempts=0)


def lock_all(db: Session) -> list[_S]:
    """기동 동기화용 — 모든 설정 행을 tenant_id 순으로 잠근다."""
    return list(db.scalars(
        select(_S).order_by(_S.tenant_id).with_for_update()
        .execution_options(populate_existing=True)
    ))


def route_rows(db: Session) -> list[tuple[str, str, _S | None]]:
    """알림 라우트 원천 — 모든 테넌트의 (tenant_id, 계정, 설정 행 또는 None)."""
    return [
        (str(tenant_id), account, record)
        for tenant_id, account, record in db.execute(
            select(_T.tenant_id, _T.aws_account_id, _S)
            .outerjoin(_S, _S.tenant_id == _T.tenant_id)
            .order_by(_T.tenant_id)
            .execution_options(populate_existing=True)
        ).all()
    ]


# --- 이메일 관측(백그라운드) ----------------------------------------------------------


@dataclass(frozen=True)
class EmailClaim:
    tenant_id: str
    generation: str
    token: str
    address: str = ""  # repr 에 주소를 싣지 않도록 비교 · 표시에서 뺀다
    region: str = ""
    status: str = ""
    topic_arn: str | None = None
    subscription_arn: str | None = None
    attempts: int = 0

    def __repr__(self) -> str:
        return f"EmailClaim(tenant_id={self.tenant_id!r}, status={self.status!r})"


def _email_due():
    # 임대 시각은 문장 실행 시점의 DB 시계(clock_timestamp)다 — 트랜잭션 시작 시각(now())이 아니다
    return (
        _S.email_source == "MANAGED",
        _S.email_status.in_(EMAIL_ACTIVE_STATUSES),
        _S.email_next_check_at <= func.clock_timestamp(),
        or_(
            _S.email_lease_token.is_(None),
            _S.email_lease_expires_at <= func.clock_timestamp(),
        ),
    )


def due_email_tenants(db: Session, limit: int) -> list[str]:
    """관측할 차례인 테넌트(임대 없거나 만료). 잠근 행은 건너뛴다."""
    return [
        str(tenant_id) for tenant_id in db.scalars(
            select(_S.tenant_id)
            .where(*_email_due())
            .order_by(_S.email_next_check_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
    ]


def claim_email(db: Session, tenant_id: str, token: str, lease_seconds: float) -> EmailClaim | None:
    """현재 세대 관측 1단계의 임대. 조건이 바뀌었으면 None. 호출자는 SDK 호출 직전에 잡는다."""
    row = db.execute(
        update(_S)
        .where(_S.tenant_id == tenant_id, *_email_due())
        .values(
            email_lease_token=token,
            email_lease_expires_at=func.clock_timestamp() + timedelta(seconds=lease_seconds),
        )
        .returning(
            _S.email_generation, _S.email_address, _S.email_region, _S.email_status,
            _S.email_topic_arn, _S.email_subscription_arn, _S.email_attempts,
        )
        .execution_options(**_NO_SYNC)
    ).first()
    if row is None:
        return None
    return EmailClaim(
        tenant_id=tenant_id, generation=str(row[0]), token=token, address=row[1], region=row[2],
        status=row[3], topic_arn=row[4], subscription_arn=row[5], attempts=row[6],
    )


def lock_email_claim(db: Session, claim: EmailClaim) -> _S | None:
    """관측 결과를 반영하기 전 — 임대 · 세대가 그대로인 행만 잠가 돌려준다(아니면 None)."""
    return db.scalars(
        select(_S)
        .where(
            _S.tenant_id == claim.tenant_id,
            _S.email_source == "MANAGED",
            _S.email_generation == claim.generation,
            _S.email_lease_token == claim.token,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    ).first()


def lease_alive(db: Session, record: _S) -> bool:
    """잠근 행의 임대가 지금(잠금 대기 뒤의 DB 시계) 유효한가. 만료면 결과를 반영하지 않는다."""
    expires = record.email_lease_expires_at
    return expires is not None and bool(db.scalar(select(func.clock_timestamp() < expires)))


def release_email_lease(record: _S) -> None:
    record.email_lease_token = None
    record.email_lease_expires_at = None


# --- 정리 대기열 ----------------------------------------------------------------------


@dataclass(frozen=True)
class CleanupClaim:
    cleanup_id: str
    tenant_id: str
    resource_kind: str
    purpose: str
    region: str
    resource_name: str
    attempts: int


def queue_cleanup(
    db: Session,
    *,
    tenant_id: str,
    resource_kind: str,
    purpose: str,
    region: str,
    resource_name: str,
    delay_seconds: float = 0,
    not_before=None,
) -> str | None:
    """정리 1건을 넣는다. 같은 자원이 이미 있으면 그대로 두고 None 이다."""
    cleanup_id = str(uuid.uuid4())
    inserted = db.execute(
        insert(_C)
        .values(
            cleanup_id=cleanup_id, tenant_id=tenant_id, resource_kind=resource_kind,
            purpose=purpose, region=region, resource_name=resource_name,
            not_before=not_before if not_before is not None else _after(delay_seconds),
        )
        .on_conflict_do_nothing(index_elements=[_C.resource_kind, _C.region, _C.resource_name])
        .returning(_C.cleanup_id)
    ).first()
    return None if inserted is None else str(inserted[0])


def retire_after_lease(record: _S, margin_seconds: float):
    """은퇴할 세대의 정리 시각 — 진행 중 관측 임대가 끝난 뒤(+여유)."""
    base = (
        func.greatest(func.now(), record.email_lease_expires_at)
        if record.email_lease_expires_at is not None else func.now()
    )
    return base + timedelta(seconds=margin_seconds)


def consume_staging(db: Session, cleanup_id: str) -> bool:
    """저장 commit 이 준비 비밀을 사용 중으로 바꾼다. 정리 worker 가 먼저 잡았으면 False."""
    result = db.execute(
        delete(_C)
        .where(_C.cleanup_id == cleanup_id, _C.purpose == "STAGING", _C.attempts == 0)
        .execution_options(**_NO_SYNC)
    )
    return result.rowcount == 1


def expedite_staging(db: Session, cleanup_id: str) -> None:
    """쓰지 않게 된 준비 비밀을 바로 정리 차례로 올린다."""
    db.execute(
        update(_C)
        .where(_C.cleanup_id == cleanup_id, _C.purpose == "STAGING", _C.attempts == 0)
        .values(not_before=func.now())
        .execution_options(**_NO_SYNC)
    )


def claim_cleanups(db: Session, limit: int, lease_seconds: float) -> list[CleanupClaim]:
    """차례가 된 정리를 임대한다(not_before 를 DB 시계 기준 미래로). 호출자는 삭제 직전에 잡는다."""
    rows = list(db.scalars(
        select(_C)
        .where(_C.not_before <= func.clock_timestamp())
        .order_by(_C.not_before, _C.cleanup_id)
        .limit(limit)
        .with_for_update(skip_locked=True)
    ))
    claims = []
    for row in rows:
        db.execute(
            update(_C)
            .where(_C.cleanup_id == row.cleanup_id)
            .values(
                attempts=_C.attempts + 1,
                not_before=func.clock_timestamp() + timedelta(seconds=lease_seconds),
            )
            .execution_options(**_NO_SYNC)
        )
        claims.append(CleanupClaim(
            cleanup_id=str(row.cleanup_id), tenant_id=str(row.tenant_id),
            resource_kind=row.resource_kind, purpose=row.purpose, region=row.region,
            resource_name=row.resource_name, attempts=row.attempts + 1,
        ))
    return claims


def finish_cleanup(db: Session, claim: CleanupClaim) -> bool:
    result = db.execute(
        delete(_C)
        .where(_C.cleanup_id == claim.cleanup_id, _C.attempts == claim.attempts)
        .execution_options(**_NO_SYNC)
    )
    return result.rowcount == 1


def retry_cleanup(db: Session, claim: CleanupClaim, *, delay_seconds: float, error_code: str) -> None:
    db.execute(
        update(_C)
        .where(_C.cleanup_id == claim.cleanup_id, _C.attempts == claim.attempts)
        .values(not_before=_after(delay_seconds), last_error_code=error_code[:64])
        .execution_options(**_NO_SYNC)
    )
