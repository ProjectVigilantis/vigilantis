"""서버 세션 서비스 — 실제 PostgreSQL. 원본 토큰은 어떤 단정 메시지에도 싣지 않는다."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from auth import sessions
from db import models
from db.repositories import auth as auth_repo
from schemas.api.auth import UserRole
from sqlalchemy import text

T0 = datetime(2026, 10, 19, 9, 0, tzinfo=UTC)
MAX = 28800  # 8h
IDLE = 1800  # 30m


@pytest.fixture()
def user(db):
    return auth_repo.create_user(
        db,
        issuer="https://issuer.example/pool",
        cognito_sub="sub-1",
        email="u@example.com",
        role=UserRole.OPERATOR,
        now=T0,
    )


def _issue(db, user):
    return sessions.issue_session(
        db,
        user_id=user.user_id,
        mfa_step=models.AuthSessionMfaStep.SOFTWARE_TOKEN_MFA,
        now=T0,
        max_seconds=MAX,
        idle_seconds=IDLE,
    )


def _resolve(db, token, at, touch=False):
    return sessions.resolve_session(db, token, now=at, idle_seconds=IDLE, touch=touch)


def _last_activity(db, token):
    return db.execute(
        text("SELECT last_activity_at FROM auth_sessions WHERE token_hash = :h"),
        {"h": sessions.hash_token(token)},
    ).scalar_one()


def test_issue_stores_only_the_hash(db, user):
    issued = _issue(db, user)
    rows = db.execute(text("SELECT to_jsonb(s)::text FROM auth_sessions s")).scalars().all()
    assert len(rows) == 1
    assert (issued.token in rows[0]) is False
    assert (sessions.hash_token(issued.token) in rows[0]) is True
    assert len(sessions.hash_token(issued.token)) == 64
    assert issued.view.info.absolute_expires_at == T0 + timedelta(seconds=MAX)
    assert issued.view.info.idle_expires_at == T0 + timedelta(seconds=IDLE)


def test_tokens_are_unique_and_unknown_token_does_not_resolve(db, user):
    assert _issue(db, user).token != _issue(db, user).token
    assert _resolve(db, "not-a-real-token", T0) is None


def test_idle_boundary(db, user):
    token = _issue(db, user).token
    assert _resolve(db, token, T0 + timedelta(minutes=29, seconds=59)) is not None
    assert _resolve(db, token, T0 + timedelta(minutes=30)) is None


def test_absolute_boundary_even_with_regular_activity(db, user):
    token = _issue(db, user).token
    at = T0
    while at + timedelta(minutes=20) < T0 + timedelta(seconds=MAX):
        at += timedelta(minutes=20)
        assert _resolve(db, token, at, touch=True) is not None
    absolute = T0 + timedelta(seconds=MAX)
    assert _resolve(db, token, absolute - timedelta(seconds=1)) is not None
    assert _resolve(db, token, absolute) is None
    assert _resolve(db, token, absolute, touch=True) is None


def test_touch_false_never_extends(db, user):
    token = _issue(db, user).token
    for minutes in (5, 10, 20, 29):
        view = _resolve(db, token, T0 + timedelta(minutes=minutes), touch=False)
        assert view is not None
        assert view.info.idle_expires_at == T0 + timedelta(seconds=IDLE)
        assert _last_activity(db, token) == T0
    assert _resolve(db, token, T0 + timedelta(minutes=30)) is None


def test_touch_true_extends_idle_but_not_past_absolute(db, user):
    token = _issue(db, user).token
    at = T0 + timedelta(minutes=20)
    view = _resolve(db, token, at, touch=True)
    assert view.info.idle_expires_at == at + timedelta(seconds=IDLE)
    assert _last_activity(db, token) == at
    assert _resolve(db, token, T0 + timedelta(minutes=49)) is not None
    assert _resolve(db, token, T0 + timedelta(minutes=50)) is None

    near_end = T0 + timedelta(seconds=MAX - 60)
    token2 = _issue(db, user).token
    # 활동을 이어 near_end 직전까지 간다
    at = T0
    while at + timedelta(minutes=25) < near_end:
        at += timedelta(minutes=25)
        _resolve(db, token2, at, touch=True)
    view = _resolve(db, token2, near_end, touch=True)
    assert view.info.idle_expires_at == view.info.absolute_expires_at


def test_stale_now_never_moves_activity_backwards(db, user):
    token = _issue(db, user).token
    _resolve(db, token, T0 + timedelta(minutes=10), touch=True)
    _resolve(db, token, T0 + timedelta(minutes=5), touch=True)
    assert _last_activity(db, token) == T0 + timedelta(minutes=10)


def test_expired_paths_do_not_write(db, user):
    token = _issue(db, user).token
    before = db.execute(text("SELECT to_jsonb(s)::text FROM auth_sessions s")).scalar_one()
    assert _resolve(db, token, T0 + timedelta(minutes=31), touch=True) is None
    assert _resolve(db, token, T0 + timedelta(hours=9), touch=True) is None
    after = db.execute(text("SELECT to_jsonb(s)::text FROM auth_sessions s")).scalar_one()
    assert before == after


def test_revoke_then_resolve_none_and_second_revoke_harmless(db, user):
    token = _issue(db, user).token
    assert sessions.revoke_session(db, token, now=T0 + timedelta(minutes=1)) is True
    assert _resolve(db, token, T0 + timedelta(minutes=2)) is None
    assert _resolve(db, token, T0 + timedelta(minutes=2), touch=True) is None
    assert sessions.revoke_session(db, token, now=T0 + timedelta(minutes=9)) is False
    assert sessions.revoke_session(db, "unknown-token", now=T0) is False
    revoked_at, reason = db.execute(
        text("SELECT revoked_at, revoke_reason::text FROM auth_sessions")
    ).one()
    assert (revoked_at, reason) == (T0 + timedelta(minutes=1), "LOGOUT")
