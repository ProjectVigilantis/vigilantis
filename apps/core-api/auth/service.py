# ==============================================================================
# [파일 설명]
# 로그인 완료 · 현재 사용자 · 활동 · 로그아웃 — 인증 서비스(트랜잭션 소유자). (#492 PR 1)
#
#   - 이 계층이 commit한다. 저장소(db.repositories.auth)와 sessions는 commit하지 않는다.
#   - 시각은 호출부가 now(UTC aware)로 넘긴다. 한도(초)도 인자다 — 설정을 읽지 않는다.
#   - 오류는 AuthError 하위 예외 — 코드 → HTTP 매핑은 auth.http 가 한다.
#   - 계정 종류(#492): 접근 권한의 원천은 account_type 하나다(role · tenant_id NULL 로 추론하지 않는다).
#     UNASSIGNED 는 로그인 완료가 403 ACCOUNT_NOT_PROVISIONED, 이미 있던 세션은 401 AUTH_REQUIRED 다.
#     TENANT 의 테넌트 상태는 로그인을 막지 않는다(정지 테넌트도 로그인 · 조회) — 변경 거절은 호출부 몫이다.
# ==============================================================================

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from db import models
from db.repositories import auth as auth_repo
from schemas.api.auth import AccountType, SessionInfo, UserRole
from schemas.api.errors import ErrorCode
from schemas.api.tenants import TenantStatus
from sqlalchemy.orm import Session

from . import sessions
from .cognito import AuthError, AuthFailedError
from .login_flow import VerifiedIdentity


class AccountNotProvisionedError(AuthError):
    code = ErrorCode.ACCOUNT_NOT_PROVISIONED


class AccountDisabledError(AuthError):
    code = ErrorCode.ACCOUNT_DISABLED


class AuthRequiredError(AuthError):
    code = ErrorCode.AUTH_REQUIRED


@dataclass(frozen=True)
class CurrentUser:
    user_id: str
    email: str
    role: UserRole
    #: PLATFORM 또는 TENANT — UNASSIGNED 는 세션 판정을 통과하지 않는다.
    account_type: AccountType
    #: TENANT 만 값이 있다(정규 UUID 문자열).
    tenant_id: str | None
    #: 판정 시점에 읽은 소속 테넌트 상태(내부 전용 — 공개 응답에 싣지 않는다). PLATFORM 은 None.
    tenant_status: TenantStatus | None
    session: SessionInfo

    @property
    def tenant_suspended(self) -> bool:
        return self.tenant_status is TenantStatus.SUSPENDED


def complete_login(
    db: Session,
    identity: VerifiedIdentity,
    *,
    previous_token: str | None,
    now: datetime,
    max_seconds: int,
    idle_seconds: int,
) -> sessions.IssuedSession:
    """로그인 완료 — 짧은 DB 변경 전체를 users 테이블 락 하나로 직렬화한다.

    같은 락을 관리자 서비스도 잡으므로 동시 로그인 · 비활성화 · 관리자 동작이 서로 끼어들지 않는다.
    락을 잡은 뒤에야 신원 · 상태 · 이메일 증명을 새로 읽는다. 한 사용자의 세션은 1개만 남는다.
    """
    try:
        auth_repo.lock_users_table(db)
        user = auth_repo.get_user_by_identity(
            db, issuer=identity.issuer, cognito_sub=identity.sub
        )
        # 소속 · 플랫폼 권한이 정해지지 않은 계정(UNASSIGNED)은 접근할 곳이 없다 — 세션을 만들지 않는다
        if user is None or user.account_type is AccountType.UNASSIGNED:
            raise AccountNotProvisionedError()
        if user.status is models.AppUserStatus.DISABLED:
            raise AccountDisabledError()
        # 이메일 증명은 앱이 로그인 흐름에서 이메일 코드 검증 성공을 받았을 때만 기록된다.
        if user.email_verified_at is None and not identity.email_proven:
            raise AuthFailedError()
        if identity.email_proven and user.email_verified_at is None:
            auth_repo.mark_email_verified(db, user_id=user.user_id, now=now)
        if previous_token:
            # 이전 쿠키 세션은 다른 사용자의 것이어도 교체한다.
            sessions.revoke_session(
                db,
                previous_token,
                now=now,
                reason=models.AuthSessionRevokeReason.SUPERSEDED,
            )
        sessions.revoke_user_sessions(
            db,
            user_id=user.user_id,
            now=now,
            reason=models.AuthSessionRevokeReason.SUPERSEDED,
        )
        issued = sessions.issue_session(
            db,
            user_id=user.user_id,
            mfa_step=models.AuthSessionMfaStep(identity.mfa_step.value),
            now=now,
            max_seconds=max_seconds,
            idle_seconds=idle_seconds,
        )
        db.commit()
    except Exception:
        db.rollback()
        raise
    return issued


def resolve_current_user(
    db: Session, token: str | None, *, now: datetime, idle_seconds: int, touch: bool
) -> CurrentUser:
    """유효한 세션 + ACTIVE 사용자 + PLATFORM · TENANT 계정만 통과한다. 그 밖은 전부 AuthRequiredError.

    touch=True 는 활동 기록이다 — DISABLED · UNASSIGNED 사용자면 기록을 되돌린다.
    TENANT 는 소속 테넌트의 현재 상태를 함께 읽는다(정지여도 통과한다).
    """
    if not token:
        raise AuthRequiredError()
    view = sessions.resolve_session(
        db, token, now=now, idle_seconds=idle_seconds, touch=touch
    )
    user = auth_repo.get_user(db, view.user_id) if view is not None else None
    if (
        user is None
        or user.status is not models.AppUserStatus.ACTIVE
        or user.account_type is AccountType.UNASSIGNED
    ):
        db.rollback()
        raise AuthRequiredError()
    tenant_status = None
    if user.account_type is AccountType.TENANT:
        # FK + CHECK(TENANT ⇔ tenant_id NOT NULL)가 행의 존재를 보장한다
        tenant_status = db.get(models.Tenant, user.tenant_id, populate_existing=True).status
    if touch:
        db.commit()
    return CurrentUser(
        user_id=user.user_id,
        email=user.email,
        role=user.role,
        account_type=user.account_type,
        tenant_id=user.tenant_id,
        tenant_status=tenant_status,
        session=view.info,
    )


def logout(db: Session, token: str | None, *, now: datetime) -> None:
    """멱등 — 토큰이 없거나 이미 폐기돼도 조용히 끝난다."""
    if not token:
        return
    sessions.revoke_session(db, token, now=now)
    db.commit()
