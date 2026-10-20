# ==============================================================================
# [파일 설명]
# 서버 세션 서비스 — 발급 · 조회(resolve) · 폐기. (#492 PR 1)
#
#   - 설정을 읽지 않고 commit하지 않는다. 시각(now)과 한도(초)는 호출부가 넘긴다.
#   - 원본 토큰은 발급 응답으로만 나가고 DB에는 SHA-256 hex만 저장한다.
#   - 만료 기준 2개: 절대(발급 + max) · 유휴(마지막 활동 + idle). 먼저 오는 쪽에서 무효다.
#   - resolve(touch=False)는 읽기 전용이다(GET /me · 자동 폴링이 유휴를 늘리면 안 된다).
#     touch=True는 사용자 활동이 있을 때만 쓴다.
# ==============================================================================

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta

from db import models
from db.repositories import auth as auth_repo
from schemas.api.auth import SessionInfo
from sqlalchemy.orm import Session


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class SessionView:
    session_id: str
    user_id: str
    mfa_step: models.AuthSessionMfaStep
    info: SessionInfo


@dataclass(frozen=True)
class IssuedSession:
    """token은 쿠키에만 싣는다 — 로그·응답 본문에 넣지 않는다."""

    token: str
    view: SessionView


def _view(row: models.AuthSession, last_activity_at: datetime, idle_seconds: int) -> SessionView:
    absolute = row.absolute_expires_at
    idle = min(last_activity_at + timedelta(seconds=idle_seconds), absolute)
    return SessionView(
        session_id=row.session_id,
        user_id=row.user_id,
        mfa_step=row.mfa_step,
        info=SessionInfo(absolute_expires_at=absolute, idle_expires_at=idle),
    )


def issue_session(
    db: Session,
    *,
    user_id: str,
    mfa_step: models.AuthSessionMfaStep,
    now: datetime,
    max_seconds: int,
    idle_seconds: int,
) -> IssuedSession:
    token = secrets.token_urlsafe(32)
    row = auth_repo.create_session(
        db,
        token_hash=hash_token(token),
        user_id=user_id,
        mfa_step=mfa_step,
        now=now,
        absolute_expires_at=now + timedelta(seconds=max_seconds),
    )
    return IssuedSession(token=token, view=_view(row, now, idle_seconds))


def resolve_session(
    db: Session,
    token: str,
    *,
    now: datetime,
    idle_seconds: int,
    touch: bool,
) -> SessionView | None:
    """유효한 세션이면 SessionView, 아니면 None. 만료·폐기 경로는 아무것도 쓰지 않는다."""
    row = auth_repo.get_session_by_token_hash(db, hash_token(token))
    if row is None or row.revoked_at is not None:
        return None
    if now >= row.absolute_expires_at:
        return None
    last_activity_at = row.last_activity_at
    if now >= last_activity_at + timedelta(seconds=idle_seconds):
        return None
    if touch:
        touched = auth_repo.touch_session(db, session_id=row.session_id, now=now)
        if touched is None:
            return None
        last_activity_at = touched
    return _view(row, last_activity_at, idle_seconds)


def revoke_session(
    db: Session,
    token: str,
    *,
    now: datetime,
    reason: models.AuthSessionRevokeReason = models.AuthSessionRevokeReason.LOGOUT,
) -> bool:
    """멱등. 이번 호출이 실제로 폐기했으면 True."""
    return auth_repo.revoke_session(
        db, token_hash=hash_token(token), reason=reason, now=now
    )
