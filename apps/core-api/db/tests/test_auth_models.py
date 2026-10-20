"""app_users · auth_sessions 제약과 auth 저장소 — 실제 PostgreSQL."""

from datetime import UTC, datetime, timedelta

import pytest
from db import models
from db.repositories import auth as auth_repo
from schemas.api.auth import UserRole
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

ISSUER = "https://cognito-idp.ap-northeast-2.amazonaws.com/pool-a"
NOW = datetime(2026, 10, 19, 9, 0, tzinfo=UTC)
HASH = "a" * 64


def _user(db, sub="sub-1", email="a@example.com", issuer=ISSUER):
    return auth_repo.create_user(
        db,
        issuer=issuer,
        cognito_sub=sub,
        email=email,
        role=UserRole.VIEWER,
        now=NOW,
    )


def _session_kwargs(user, **overrides):
    values = {
        "token_hash": HASH,
        "user_id": user.user_id,
        "mfa_step": models.AuthSessionMfaStep.SOFTWARE_TOKEN_MFA,
        "created_at": NOW,
        "last_activity_at": NOW,
        "absolute_expires_at": NOW + timedelta(hours=8),
    }
    values.update(overrides)
    return values


def _rejected(db, row):
    with pytest.raises(IntegrityError), db.begin_nested():
        db.add(row)
        db.flush()


def test_identity_is_issuer_and_sub_and_email_is_not_unique(db):
    _user(db, sub="sub-1", email="same@example.com")
    _user(db, sub="sub-2", email="same@example.com")  # 같은 email, 다른 sub — 허용
    _user(db, sub="sub-1", issuer=ISSUER + "-other")  # 같은 sub, 다른 issuer — 허용
    with pytest.raises(IntegrityError), db.begin_nested():
        _user(db, sub="sub-1", email="other@example.com")


def test_get_user_by_identity_never_matches_by_email(db):
    created = _user(db, sub="sub-1", email="x@example.com")
    assert (
        auth_repo.get_user_by_identity(db, issuer=ISSUER, cognito_sub="sub-1").user_id
        == created.user_id
    )
    assert auth_repo.get_user_by_identity(db, issuer=ISSUER, cognito_sub="x@example.com") is None
    assert auth_repo.get_user_by_identity(db, issuer="other", cognito_sub="sub-1") is None


def test_mark_email_verified_once_and_set_status(db):
    user = _user(db)
    later = NOW + timedelta(minutes=5)
    assert auth_repo.mark_email_verified(db, user_id=user.user_id, now=later) is True
    assert auth_repo.mark_email_verified(db, user_id=user.user_id, now=later + timedelta(hours=1)) is False
    assert auth_repo.set_status(
        db, user_id=user.user_id, status=models.AppUserStatus.DISABLED, now=later
    )
    db.refresh(user)
    assert user.email_verified_at == later
    assert user.status is models.AppUserStatus.DISABLED
    assert user.updated_at == later


def test_enums_store_values(db):
    user = _user(db)
    assert db.execute(text("SELECT role::text, status::text FROM app_users")).one() == (
        "VIEWER",
        "ACTIVE",
    )
    db.add(models.AuthSession(**_session_kwargs(user)))
    db.flush()
    assert db.execute(text("SELECT mfa_step::text FROM auth_sessions")).scalar_one() == (
        "SOFTWARE_TOKEN_MFA"
    )


def test_token_hash_is_unique(db):
    user = _user(db)
    db.add(models.AuthSession(**_session_kwargs(user)))
    db.flush()
    _rejected(db, models.AuthSession(**_session_kwargs(user)))


def test_session_requires_existing_user_and_user_delete_is_restricted(db):
    _rejected(
        db,
        models.AuthSession(
            **_session_kwargs(models.AppUser(user_id="00000000-0000-0000-0000-000000000000"))
        ),
    )
    user = _user(db)
    db.add(models.AuthSession(**_session_kwargs(user)))
    db.flush()
    with pytest.raises(IntegrityError), db.begin_nested():
        db.execute(text("DELETE FROM app_users WHERE user_id = :u"), {"u": user.user_id})


@pytest.mark.parametrize(
    "overrides",
    [
        {"revoked_at": NOW},  # reason 없이
        {"revoke_reason": models.AuthSessionRevokeReason.LOGOUT},  # 시각 없이
        {"absolute_expires_at": NOW},  # created_at과 같음
        {"absolute_expires_at": NOW - timedelta(seconds=1)},
        {"last_activity_at": NOW - timedelta(seconds=1)},  # created_at 이전
        {"last_activity_at": NOW + timedelta(hours=8, seconds=1)},  # absolute 이후
    ],
)
def test_check_constraints_reject_bad_rows(db, overrides):
    user = _user(db)
    _rejected(db, models.AuthSession(**_session_kwargs(user, **overrides)))


def test_check_constraints_accept_boundaries(db):
    user = _user(db)
    db.add(
        models.AuthSession(
            **_session_kwargs(
                user,
                last_activity_at=NOW + timedelta(hours=8),
                revoked_at=NOW,
                revoke_reason=models.AuthSessionRevokeReason.LOGOUT,
            )
        )
    )
    db.flush()
    assert db.scalar(select(models.AuthSession.revoked_at)) == NOW
