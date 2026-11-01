# ==============================================================================
# [파일 설명]
# TOTP 분실 복구 저장소 — 복구 작업 · 사용자 인증 세대 · 복구 차단 전이 · 신원 변경 울타리. (#515)
#
#   - commit하지 않는다. 트랜잭션은 auth.totp_recovery 가 소유한다.
#   - 전이는 조건부 UPDATE 다 — 기대한 세대 · 상태가 아니면 아무것도 바꾸지 않고 False/None 을 돌려준다.
#   - 시각은 호출부가 now로 넘긴다(시계를 읽지 않는다). 시각은 기록용이며 판정에 쓰지 않는다.
# ==============================================================================

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from .. import models

_PENDING = models.TotpRecoveryStatus.PROVIDER_PENDING
_STATE = models.MfaRecoveryState
_MUTATION = models.TotpProviderMutation


def get_operation(db: Session, operation_id: str) -> models.TotpRecoveryOperation | None:
    return db.get(models.TotpRecoveryOperation, operation_id, populate_existing=True)


def create_operation(
    db: Session,
    *,
    operation_id: str,
    target_user_id: str,
    actor_user_id: str | None,
    generation: int,
    revoked_session_count: int,
    now: datetime,
) -> models.TotpRecoveryOperation:
    """PROVIDER_PENDING 작업 1건. 같은 operation_id · (대상, 세대) 중복은 flush 에서 DB 가 거절한다."""
    row = models.TotpRecoveryOperation(
        operation_id=operation_id,
        target_user_id=target_user_id,
        actor_user_id=actor_user_id,
        identity_verified=True,
        generation=generation,
        status=_PENDING,
        revoked_session_count=revoked_session_count,
        created_at=now,
        finalized_at=None,
    )
    db.add(row)
    db.flush()
    return row


def finalize_operation(
    db: Session, *, operation_id: str, status: models.TotpRecoveryStatus, now: datetime
) -> bool:
    """PROVIDER_PENDING 인 작업만 결과로 닫는다. 이미 닫혔거나 없으면 False."""
    result = db.execute(
        update(models.TotpRecoveryOperation)
        .where(
            models.TotpRecoveryOperation.operation_id == operation_id,
            models.TotpRecoveryOperation.status == _PENDING,
        )
        .values(status=status, finalized_at=now)
        .execution_options(synchronize_session=False)
    )
    return result.rowcount == 1


def fence_user(db: Session, *, user_id: str, from_generation: int, now: datetime) -> int | None:
    """복구 차단 — 세대 +1 · RESET_PENDING · 이메일 증명 제거. 세대가 from_generation 이 아니면 None."""
    return db.execute(
        update(models.AppUser)
        .where(
            models.AppUser.user_id == user_id,
            models.AppUser.auth_generation == from_generation,
        )
        .values(
            auth_generation=models.AppUser.auth_generation + 1,
            mfa_recovery_state=_STATE.RESET_PENDING,
            email_verified_at=None,
            updated_at=now,
        )
        .returning(models.AppUser.auth_generation)
        .execution_options(synchronize_session=False)
    ).scalar_one_or_none()


def require_enrollment(db: Session, *, user_id: str, generation: int, now: datetime) -> bool:
    """제공자 등록 없음이 확인된 차단 → ENROLLMENT_REQUIRED. 그 세대의 RESET_PENDING 이 아니면 False."""
    result = db.execute(
        update(models.AppUser)
        .where(
            models.AppUser.user_id == user_id,
            models.AppUser.auth_generation == generation,
            models.AppUser.mfa_recovery_state == _STATE.RESET_PENDING,
        )
        .values(mfa_recovery_state=_STATE.ENROLLMENT_REQUIRED, updated_at=now)
        .execution_options(synchronize_session=False)
    )
    return result.rowcount == 1


def record_enrollment(db: Session, *, user_id: str, generation: int, now: datetime) -> bool:
    """그 세대의 새 TOTP 등록 성공 → NONE. 세대가 다르거나 RESET_PENDING 이면 False."""
    result = db.execute(
        update(models.AppUser)
        .where(
            models.AppUser.user_id == user_id,
            models.AppUser.auth_generation == generation,
            models.AppUser.mfa_recovery_state.in_(
                [_STATE.NONE, _STATE.ENROLLMENT_REQUIRED]
            ),
        )
        .values(mfa_recovery_state=_STATE.NONE, updated_at=now)
        .execution_options(synchronize_session=False)
    )
    return result.rowcount == 1


def list_operations(db: Session, *, target_user_id: str) -> list[models.TotpRecoveryOperation]:
    """대상의 복구 작업, 세대 순."""
    return list(
        db.scalars(
            select(models.TotpRecoveryOperation)
            .where(models.TotpRecoveryOperation.target_user_id == target_user_id)
            .order_by(models.TotpRecoveryOperation.generation)
        )
    )


# --- 신원 변경 울타리 ------------------------------------------------------------------


def claim_mutation(
    db: Session,
    *,
    issuer: str,
    cognito_sub: str,
    kind: models.TotpProviderMutationKind,
    operation_id: str | None,
    now: datetime,
) -> str | None:
    """신원의 울타리를 연다 — 새 mutation_id. 그 신원에 풀리지 않은 울타리가 이미 있으면 None(아무것도 쓰지 않는다)."""
    return db.execute(
        insert(_MUTATION)
        .values(
            mutation_id=str(uuid.uuid4()),
            issuer=issuer,
            cognito_sub=cognito_sub,
            kind=kind,
            operation_id=operation_id,
            started_at=now,
        )
        .on_conflict_do_nothing(
            index_elements=[_MUTATION.issuer, _MUTATION.cognito_sub],
            index_where=_MUTATION.resolution.is_(None),
        )
        .returning(_MUTATION.mutation_id)
    ).scalar_one_or_none()


def get_unresolved_mutation(
    db: Session, *, issuer: str, cognito_sub: str
) -> models.TotpProviderMutation | None:
    return db.scalar(
        select(_MUTATION)
        .where(
            _MUTATION.issuer == issuer,
            _MUTATION.cognito_sub == cognito_sub,
            _MUTATION.resolution.is_(None),
        )
        .execution_options(populate_existing=True)
    )


def holds_mutation(db: Session, *, mutation_id: str) -> bool:
    """그 울타리가 아직 풀리지 않았는지 — 행을 잠근다(같은 트랜잭션의 정리 · 풀림과 겹치지 않게)."""
    return (
        db.scalar(
            select(_MUTATION.mutation_id)
            .where(_MUTATION.mutation_id == mutation_id, _MUTATION.resolution.is_(None))
            .with_for_update()
        )
        is not None
    )


def resolve_mutation(
    db: Session,
    *,
    mutation_id: str,
    resolution: models.TotpProviderMutationResolution,
    now: datetime,
    reconciled_by: str | None = None,
) -> bool:
    """풀리지 않은 울타리만 푼다. 이미 풀렸거나 없으면 False."""
    result = db.execute(
        update(_MUTATION)
        .where(_MUTATION.mutation_id == mutation_id, _MUTATION.resolution.is_(None))
        .values(resolution=resolution, resolved_at=now, reconciled_by=reconciled_by)
        .execution_options(synchronize_session=False)
    )
    return result.rowcount == 1
