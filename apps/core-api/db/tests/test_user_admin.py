"""사용자 관리 저장소 · 감사 이력 append-only — 실제 PostgreSQL."""

from datetime import UTC, datetime, timedelta

import pytest
from db import models
from db.repositories import auth as auth_repo
from pydantic import ValidationError
from schemas.api.auth import UserRole
from schemas.api.users import UserEventAction
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

ISSUER = "https://cognito-idp.ap-northeast-2.amazonaws.com/pool-a"
NOW = datetime(2026, 10, 20, 9, 0, tzinfo=UTC)
REASONS = models.AuthSessionRevokeReason


def _user(db, sub, role=UserRole.VIEWER, status=models.AppUserStatus.ACTIVE):
    return auth_repo.create_user(
        db,
        issuer=ISSUER,
        cognito_sub=sub,
        email=f"{sub}@example.com",
        role=role,
        status=status,
        now=NOW,
    )


def _session(db, user, token_hash, created_at, revoked=False):
    row = auth_repo.create_session(
        db,
        token_hash=token_hash,
        user_id=user.user_id,
        mfa_step=models.AuthSessionMfaStep.SOFTWARE_TOKEN_MFA,
        now=created_at,
        absolute_expires_at=created_at + timedelta(hours=8),
    )
    if revoked:
        auth_repo.revoke_session(
            db, token_hash=token_hash, reason=REASONS.LOGOUT, now=created_at
        )
    return row


def _event(db, actor, target, **overrides):
    values = {
        "actor_user_id": actor.user_id,
        "target_user_id": target.user_id,
        "action": UserEventAction.ROLE_CHANGED,
        "before_state": {"role": "VIEWER"},
        "after_state": {"role": "ADMIN"},
        "now": NOW,
    }
    values.update(overrides)
    return auth_repo.add_user_event(db, **values)


def test_enum_labels_include_new_revoke_reasons(db):
    labels = set(
        db.execute(
            text(
                "SELECT enumlabel FROM pg_enum e JOIN pg_type t ON t.oid = e.enumtypid "
                "WHERE t.typname = 'auth_session_revoke_reason'"
            )
        ).scalars()
    )
    assert labels == {
        "LOGOUT",
        "SUPERSEDED",
        "ADMIN_REVOKED",
        "ACCOUNT_DISABLED",
        "PASSWORD_RESET",
    }


def test_list_users_with_last_login_uses_newest_session_even_if_revoked(db):
    never = _user(db, "never")
    user = _user(db, "someone")
    _session(db, user, "1" * 64, NOW + timedelta(hours=1))
    _session(db, user, "2" * 64, NOW + timedelta(hours=3), revoked=True)
    rows = dict(
        (u.user_id, login) for u, login in auth_repo.list_users_with_last_login(db)
    )
    assert rows[never.user_id] is None
    assert rows[user.user_id] == NOW + timedelta(hours=3)


def test_count_active_admins_ignores_disabled_and_other_roles(db):
    before = auth_repo.count_active_admins(db)
    _user(db, "a1", role=UserRole.ADMIN)
    _user(db, "a2", role=UserRole.ADMIN, status=models.AppUserStatus.DISABLED)
    _user(db, "o1", role=UserRole.OPERATOR)
    assert auth_repo.count_active_admins(db) == before + 1


def test_set_role_updates_and_reports_missing(db):
    user = _user(db, "r1")
    later = NOW + timedelta(minutes=1)
    assert auth_repo.set_role(db, user_id=user.user_id, role=UserRole.ADMIN, now=later)
    assert auth_repo.get_user(db, user.user_id).role is UserRole.ADMIN
    assert auth_repo.get_user(db, user.user_id).updated_at == later
    assert not auth_repo.set_role(
        db,
        user_id="00000000-0000-0000-0000-000000000000",
        role=UserRole.ADMIN,
        now=later,
    )


def test_get_user_reads_fresh_values_after_external_change(db):
    user = _user(db, "fresh")
    db.execute(
        text("UPDATE app_users SET status = 'DISABLED' WHERE user_id = :u"),
        {"u": user.user_id},
    )
    assert auth_repo.get_user(db, user.user_id).status is models.AppUserStatus.DISABLED


def test_revoke_user_sessions_revokes_every_unrevoked_including_expired(db):
    user = _user(db, "rv")
    other = _user(db, "other")
    _session(db, user, "a" * 64, NOW)  # 만료 시각이 지난 세션도 대상
    _session(db, user, "b" * 64, NOW + timedelta(hours=1))
    _session(db, user, "c" * 64, NOW + timedelta(hours=2), revoked=True)
    _session(db, other, "d" * 64, NOW)
    later = NOW + timedelta(days=2)
    assert (
        auth_repo.revoke_user_sessions(
            db, user_id=user.user_id, reason=REASONS.ADMIN_REVOKED, now=later
        )
        == 2
    )
    assert (
        auth_repo.revoke_user_sessions(
            db, user_id=user.user_id, reason=REASONS.ADMIN_REVOKED, now=later
        )
        == 0
    )
    rows = db.execute(
        text(
            "SELECT user_id, revoke_reason::text FROM auth_sessions "
            "ORDER BY token_hash"
        )
    ).all()
    assert [r[1] for r in rows if str(r[0]) == user.user_id] == [
        "ADMIN_REVOKED",
        "ADMIN_REVOKED",
        "LOGOUT",
    ]
    assert [r[1] for r in rows if str(r[0]) == other.user_id] == [None]


def test_events_list_newest_first_for_target_only(db):
    actor = _user(db, "actor")
    target = _user(db, "target")
    elsewhere = _user(db, "elsewhere")
    first = _event(db, actor, target, now=NOW)
    second = _event(
        db,
        actor,
        target,
        action=UserEventAction.SESSIONS_REVOKED,
        before_state={},
        after_state={"revoked_session_count": 2},
        now=NOW + timedelta(minutes=1),
    )
    _event(db, actor, elsewhere)
    events = auth_repo.list_user_events(db, user_id=target.user_id, limit=10)
    assert [e.event_id for e in events] == [second.event_id, first.event_id]
    assert events[0].after_state == {"revoked_session_count": 2}
    assert len(auth_repo.list_user_events(db, user_id=target.user_id, limit=1)) == 1


@pytest.mark.parametrize(
    "state",
    [
        {"password": "hunter2"},
        {"role": "ADMIN", "refresh_token": "abc"},
        {"role": "SUPERUSER"},
        {"revoked_session_count": -1},
    ],
)
def test_event_payload_outside_allowlist_is_rejected_and_not_stored(db, state):
    actor = _user(db, "p-actor")
    target = _user(db, "p-target")
    for key in ("before_state", "after_state"):
        with pytest.raises(ValidationError):
            _event(db, actor, target, **{key: state})
    assert auth_repo.list_user_events(db, user_id=target.user_id, limit=10) == []


def test_audit_rows_reject_update_and_delete_but_insert_is_allowed(db):
    actor = _user(db, "t-actor")
    target = _user(db, "t-target")
    event = _event(db, actor, target)
    with pytest.raises(DBAPIError, match="append-only"), db.begin_nested():
        db.execute(
            text("UPDATE user_admin_events SET action = 'USER_INVITED'")
        )
    with pytest.raises(DBAPIError, match="append-only"), db.begin_nested():
        db.execute(text("DELETE FROM user_admin_events"))
    _event(db, actor, target)  # INSERT는 계속 허용
    count = db.execute(text("SELECT count(*) FROM user_admin_events")).scalar_one()
    assert count == 2
    assert event.before_state == {"role": "VIEWER"}


def test_users_referenced_by_audit_cannot_be_deleted(db):
    actor = _user(db, "d-actor")
    target = _user(db, "d-target")
    _event(db, actor, target)
    for user in (actor, target):
        with pytest.raises(DBAPIError), db.begin_nested():
            db.execute(
                text("DELETE FROM app_users WHERE user_id = :u"), {"u": user.user_id}
            )
