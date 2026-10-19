# ==============================================================================
# [파일 설명]
# 로그인 완료 · 현재 사용자 · 활동 · 로그아웃 — 인증 서비스(트랜잭션 소유자). (#492 PR 1)
#
#   - 이 계층이 commit한다. 저장소(db.repositories.auth)와 sessions는 commit하지 않는다.
#   - 시각은 호출부가 now(UTC aware)로 넘긴다. 한도(초)도 인자다 — 설정을 읽지 않는다.
#   - 오류는 AuthError 하위 예외 — 코드 → HTTP 매핑은 auth.http 가 한다.
# ==============================================================================

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from db import models
from db.repositories import auth as auth_repo
from schemas.api.auth import SessionInfo, UserRole
from schemas.api.errors import ErrorCode
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
    session: SessionInfo


def complete_login(
    db: Session,
    identity: VerifiedIdentity,
    *,
    previous_token: str | None,
    now: datetime,
    max_seconds: int,
    idle_seconds: int,
) -> sessions.IssuedSession:
    user = auth_repo.get_user_by_identity(
        db, issuer=identity.issuer, cognito_sub=identity.sub
    )
    if user is None:
        raise AccountNotProvisionedError()
    if user.status is models.AppUserStatus.DISABLED:
        raise AccountDisabledError()
    # 이메일 증명은 앱이 로그인 흐름에서 이메일 코드 검증 성공을 받았을 때만 기록된다.
    if user.email_verified_at is None and not identity.email_proven:
        raise AuthFailedError()
    try:
        if identity.email_proven and user.email_verified_at is None:
            auth_repo.mark_email_verified(db, user_id=user.user_id, now=now)
        if previous_token:
            sessions.revoke_session(
                db,
                previous_token,
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
    """유효한 세션 + ACTIVE 사용자만 통과한다. 그 밖은 전부 AuthRequiredError.

    touch=True 는 활동 기록이다 — DISABLED 사용자면 기록을 되돌린다.
    """
    if not token:
        raise AuthRequiredError()
    view = sessions.resolve_session(
        db, token, now=now, idle_seconds=idle_seconds, touch=touch
    )
    user = auth_repo.get_user(db, view.user_id) if view is not None else None
    if user is None or user.status is not models.AppUserStatus.ACTIVE:
        db.rollback()
        raise AuthRequiredError()
    if touch:
        db.commit()
    return CurrentUser(
        user_id=user.user_id, email=user.email, role=user.role, session=view.info
    )


def logout(db: Session, token: str | None, *, now: datetime) -> None:
    """멱등 — 토큰이 없거나 이미 폐기돼도 조용히 끝난다."""
    if not token:
        return
    sessions.revoke_session(db, token, now=now)
    db.commit()
