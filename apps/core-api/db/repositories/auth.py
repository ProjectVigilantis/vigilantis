# ==============================================================================
# [파일 설명]
# 인증 저장소 — app_users · app_user_tenant_assignments · auth_sessions. (#492 PR 1 · PR 5)
#
#   - commit하지 않는다. 트랜잭션은 호출부(Workflow)가 소유한다. 생성은 flush까지 한다.
#   - 사용자 신원은 (issuer, cognito_sub)뿐이다. email로 조회·연결하는 경로를 두지 않는다.
#   - 세션은 token_hash(SHA-256 hex)로만 찾는다. 원본 토큰은 이 계층에 들어오지 않는다.
#   - 시각은 호출부가 now로 넘긴다(시계를 읽지 않는다).
# ==============================================================================

from __future__ import annotations

from datetime import datetime

from schemas.api.auth import AccountType, PlatformRole, UserRole
from schemas.api.users import UserEventAction, UserEventState
from sqlalchemy import delete, func, insert, select, text, update
from sqlalchemy.orm import Session

from .. import models

# --- 사용자 ---------------------------------------------------------------------


def get_user_by_identity(
    db: Session, *, issuer: str, cognito_sub: str
) -> models.AppUser | None:
    # populate_existing — 락을 잡은 뒤 identity map의 낡은 값을 읽지 않게 항상 DB 값으로 덮는다.
    return db.scalars(
        select(models.AppUser)
        .where(
            models.AppUser.issuer == issuer,
            models.AppUser.cognito_sub == cognito_sub,
        )
        .execution_options(populate_existing=True)
    ).one_or_none()


def get_user(db: Session, user_id: str) -> models.AppUser | None:
    return db.get(models.AppUser, user_id, populate_existing=True)


def _with_last_login(
    db: Session, *conditions
) -> list[tuple[models.AppUser, datetime | None]]:
    """조건에 맞는 계정 + 마지막 로그인 = 세션 created_at 최댓값(폐기·만료 세션 포함). 생성순."""
    last_login = func.max(models.AuthSession.created_at)
    rows = db.execute(
        select(models.AppUser, last_login)
        .outerjoin(
            models.AuthSession, models.AuthSession.user_id == models.AppUser.user_id
        )
        .where(*conditions)
        .group_by(models.AppUser.user_id)
        .order_by(models.AppUser.created_at, models.AppUser.user_id)
        .execution_options(populate_existing=True)
    ).all()
    return [(user, login) for user, login in rows]


def list_users_with_last_login(
    db: Session, *, tenant_ids: tuple[str, ...] | None
) -> list[tuple[models.AppUser, datetime | None]]:
    """고객(TENANT) 계정 + 마지막 로그인.

    tenant_ids 가 None 이면 모든 고객 테넌트, 튜플이면 그 테넌트들만(빈 튜플 = 아무도 없음).
    PLATFORM·UNASSIGNED 는 나오지 않는다.
    """
    conditions = [models.AppUser.account_type == AccountType.TENANT]
    if tenant_ids is not None:
        conditions.append(models.AppUser.tenant_id.in_(tenant_ids))
    return _with_last_login(db, *conditions)


def list_platform_users_with_last_login(
    db: Session,
) -> list[tuple[models.AppUser, datetime | None]]:
    """플랫폼(PLATFORM) 계정 전부(모든 등급 · 상태) + 마지막 로그인. TENANT·UNASSIGNED 는 나오지 않는다."""
    return _with_last_login(db, models.AppUser.account_type == AccountType.PLATFORM)


def count_active_central_admins(db: Session) -> int:
    """ACTIVE 인 PLATFORM CENTRAL_ADMIN 수 — role · account_type 만으로 세지 않는다."""
    return (
        db.scalar(
            select(func.count())
            .select_from(models.AppUser)
            .where(
                models.AppUser.account_type == AccountType.PLATFORM,
                models.AppUser.platform_role == PlatformRole.CENTRAL_ADMIN,
                models.AppUser.status == models.AppUserStatus.ACTIVE,
            )
        )
        or 0
    )


def count_active_admins(db: Session, *, tenant_id: str) -> int:
    """그 테넌트 안의 ACTIVE TENANT ADMIN 수."""
    return (
        db.scalar(
            select(func.count())
            .select_from(models.AppUser)
            .where(
                models.AppUser.account_type == AccountType.TENANT,
                models.AppUser.tenant_id == tenant_id,
                models.AppUser.role == UserRole.ADMIN,
                models.AppUser.status == models.AppUserStatus.ACTIVE,
            )
        )
        or 0
    )


def count_users(db: Session) -> int:
    return db.scalar(select(func.count()).select_from(models.AppUser)) or 0


def lock_users_table(db: Session) -> None:
    """부트스트랩의 "사용자가 없을 때만 생성"을 원자적으로 만든다 — 트랜잭션 끝까지 유지된다."""
    db.execute(text("LOCK TABLE app_users IN EXCLUSIVE MODE"))


def create_user(
    db: Session,
    *,
    issuer: str,
    cognito_sub: str,
    email: str,
    role: UserRole,
    account_type: AccountType,
    tenant_id: str | None,
    now: datetime,
    status: models.AppUserStatus = models.AppUserStatus.ACTIVE,
    email_verified_at: datetime | None = None,
    platform_role: PlatformRole | None = None,
) -> models.AppUser:
    """새 사용자 1건. (issuer, sub) 중복·account_type↔tenant_id·platform_role 불일치는 DB가 flush에서 거절한다.

    등급을 주지 않은 PLATFORM 계정은 TENANT_MANAGER 이고 배정이 없다 — 고객사 범위를 기본값으로 주지 않는다.
    """
    if platform_role is None and account_type is AccountType.PLATFORM:
        platform_role = PlatformRole.TENANT_MANAGER
    row = models.AppUser(
        issuer=issuer,
        cognito_sub=cognito_sub,
        email=email,
        role=role,
        account_type=account_type,
        platform_role=platform_role,
        tenant_id=tenant_id,
        status=status,
        email_verified_at=email_verified_at,
        created_at=now,
        updated_at=now,
    )
    db.add(row)
    db.flush()
    return row


def mark_email_verified(db: Session, *, user_id: str, now: datetime) -> bool:
    """이메일 검증 시각을 처음 한 번만 기록한다. 이미 검증됐거나 없는 사용자면 False."""
    result = db.execute(
        update(models.AppUser)
        .where(
            models.AppUser.user_id == user_id,
            models.AppUser.email_verified_at.is_(None),
        )
        .values(email_verified_at=now, updated_at=now)
    )
    return result.rowcount == 1


def set_status(
    db: Session, *, user_id: str, status: models.AppUserStatus, now: datetime
) -> bool:
    result = db.execute(
        update(models.AppUser)
        .where(models.AppUser.user_id == user_id)
        .values(status=status, updated_at=now)
    )
    return result.rowcount == 1


def set_role(db: Session, *, user_id: str, role: UserRole, now: datetime) -> bool:
    result = db.execute(
        update(models.AppUser)
        .where(models.AppUser.user_id == user_id)
        .values(role=role, updated_at=now)
    )
    return result.rowcount == 1


def set_platform_role(
    db: Session, *, user_id: str, platform_role: PlatformRole, now: datetime
) -> bool:
    """PLATFORM 계정의 등급만 바꾼다. 없거나 PLATFORM 이 아니면 False — 계정 종류 · 소속은 바꾸지 않는다."""
    result = db.execute(
        update(models.AppUser)
        .where(
            models.AppUser.user_id == user_id,
            models.AppUser.account_type == AccountType.PLATFORM,
        )
        .values(platform_role=platform_role, updated_at=now)
    )
    return result.rowcount == 1


# --- 담당 고객사 배정 ------------------------------------------------------------


def list_assigned_tenant_ids(db: Session, *, user_id: str) -> tuple[str, ...]:
    """사용자에게 배정된 고객사 ID(정규 UUID 문자열), tenant_id 오름차순. 등급은 보지 않는다 — 호출부가 가린다."""
    assignment = models.AppUserTenantAssignment
    return tuple(
        db.scalars(
            select(assignment.tenant_id)
            .where(assignment.user_id == user_id)
            .order_by(assignment.tenant_id)
        )
    )


def replace_assigned_tenants(
    db: Session, *, user_id: str, tenant_ids: tuple[str, ...], now: datetime
) -> None:
    """사용자의 배정을 tenant_ids 로 통째로 바꾼다(빈 튜플 = 전부 회수). 사용자 updated_at 을 now 로 올린다.

    commit 하지 않는다. 호출부가 users 락을 쥐고 대상 등급 · 고객사 존재 · 중복을 먼저 확인한다 —
    없는 사용자 · 고객사와 중복 ID 는 FK · PK 가 거절한다.
    """
    assignment = models.AppUserTenantAssignment
    db.execute(delete(assignment).where(assignment.user_id == user_id))
    if tenant_ids:
        db.execute(
            insert(assignment),
            [{"user_id": user_id, "tenant_id": tenant_id} for tenant_id in tenant_ids],
        )
    db.execute(
        update(models.AppUser).where(models.AppUser.user_id == user_id).values(updated_at=now)
    )


# --- 사용자 관리 이력 ------------------------------------------------------------


def add_user_event(
    db: Session,
    *,
    actor_user_id: str,
    target_user_id: str,
    action: UserEventAction,
    before_state: dict,
    after_state: dict,
    now: datetime,
) -> models.UserAdminEvent:
    """이력 1건 append. 상태는 UserEventState(닫힌 목록)로 검증한 값만 저장한다 — 모르는 키는 거절."""
    row = models.UserAdminEvent(
        actor_user_id=actor_user_id,
        target_user_id=target_user_id,
        action=action,
        before_state=UserEventState.model_validate(before_state).model_dump(
            mode="json", exclude_none=True
        ),
        after_state=UserEventState.model_validate(after_state).model_dump(
            mode="json", exclude_none=True
        ),
        occurred_at=now,
    )
    db.add(row)
    db.flush()
    return row


def list_user_events(
    db: Session, *, user_id: str, limit: int
) -> list[models.UserAdminEvent]:
    """대상 사용자의 이력, 최신순."""
    return list(
        db.scalars(
            select(models.UserAdminEvent)
            .where(models.UserAdminEvent.target_user_id == user_id)
            .order_by(
                models.UserAdminEvent.occurred_at.desc(),
                models.UserAdminEvent.event_id.desc(),
            )
            .limit(limit)
        )
    )


# --- 세션 -----------------------------------------------------------------------


def create_session(
    db: Session,
    *,
    token_hash: str,
    user_id: str,
    mfa_step: models.AuthSessionMfaStep,
    now: datetime,
    absolute_expires_at: datetime,
) -> models.AuthSession:
    row = models.AuthSession(
        token_hash=token_hash,
        user_id=user_id,
        mfa_step=mfa_step,
        created_at=now,
        last_activity_at=now,
        absolute_expires_at=absolute_expires_at,
    )
    db.add(row)
    db.flush()
    return row


def get_session_by_token_hash(
    db: Session, token_hash: str
) -> models.AuthSession | None:
    return db.scalars(
        select(models.AuthSession).where(models.AuthSession.token_hash == token_hash)
    ).one_or_none()


def touch_session(db: Session, *, session_id: str, now: datetime) -> datetime | None:
    """last_activity_at을 now로 올린다(뒤로 가지 않음). 폐기된 세션이면 None, 아니면 새 값."""
    return db.execute(
        update(models.AuthSession)
        .where(
            models.AuthSession.session_id == session_id,
            models.AuthSession.revoked_at.is_(None),
        )
        .values(
            last_activity_at=func.greatest(models.AuthSession.last_activity_at, now)
        )
        .returning(models.AuthSession.last_activity_at)
        .execution_options(synchronize_session=False)
    ).scalar_one_or_none()


def revoke_session(
    db: Session,
    *,
    token_hash: str,
    reason: models.AuthSessionRevokeReason,
    now: datetime,
) -> bool:
    """아직 살아 있는 세션만 폐기한다. 없거나 이미 폐기됐으면 False(멱등)."""
    result = db.execute(
        update(models.AuthSession)
        .where(
            models.AuthSession.token_hash == token_hash,
            models.AuthSession.revoked_at.is_(None),
        )
        .values(revoked_at=now, revoke_reason=reason)
        .execution_options(synchronize_session=False)
    )
    return result.rowcount == 1


def revoke_user_sessions(
    db: Session,
    *,
    user_id: str,
    reason: models.AuthSessionRevokeReason,
    now: datetime,
) -> int:
    """사용자의 아직 폐기되지 않은 세션 전부(만료된 것 포함)를 폐기한다. 폐기한 개수."""
    result = db.execute(
        update(models.AuthSession)
        .where(
            models.AuthSession.user_id == user_id,
            models.AuthSession.revoked_at.is_(None),
        )
        .values(revoked_at=now, revoke_reason=reason)
        .execution_options(synchronize_session=False)
    )
    return result.rowcount
