"""complete_login — 사용자당 세션 1개, 락 직렬화. 실제 PostgreSQL, 독립 트랜잭션."""

from __future__ import annotations

import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
from auth import service, sessions
from auth.login_flow import MfaStep, VerifiedIdentity
from db import models
from db.repositories import auth as auth_repo
from schemas.api.auth import AccountType, UserRole
from schemas.api.tenants import TenantStatus
from sqlalchemy import text
from sqlalchemy.orm import Session

T0 = datetime(2026, 10, 20, 9, 0, tzinfo=UTC)
MAX = 28800
IDLE = 1800
ISSUER = "https://issuer.example/pool"


def _identity(sub, proven=True):
    return VerifiedIdentity(
        issuer=ISSUER,
        sub=sub,
        email=f"{sub}@example.com",
        mfa_step=MfaStep.SOFTWARE_TOKEN_MFA,
        email_proven=proven,
    )


def _login(db, sub, previous=None, now=T0, proven=True):
    return service.complete_login(
        db,
        _identity(sub, proven),
        previous_token=previous,
        now=now,
        max_seconds=MAX,
        idle_seconds=IDLE,
    )


def _alive(db, token, now=T0 + timedelta(minutes=1)):
    return (
        sessions.resolve_session(db, token, now=now, idle_seconds=IDLE, touch=False)
        is not None
    )


def _reasons(db, user_id):
    return db.execute(
        text(
            "SELECT revoke_reason::text FROM auth_sessions WHERE user_id = :u "
            "ORDER BY created_at, session_id"
        ),
        {"u": user_id},
    ).scalars().all()


def _tenant_viewer(db, sub, tenant_id):
    return auth_repo.create_user(
        db,
        issuer=ISSUER,
        cognito_sub=sub,
        email=f"{sub}@example.com",
        role=UserRole.VIEWER,
        account_type=AccountType.TENANT,
        tenant_id=tenant_id,
        now=T0,
        email_verified_at=T0,
    )


@pytest.fixture()
def tenant_id(make_tenant):
    return make_tenant()


@pytest.fixture()
def user(db, tenant_id):
    return _tenant_viewer(db, "s1", tenant_id)


def test_second_login_from_another_browser_supersedes_the_first(db, user):
    first = _login(db, "s1")
    second = _login(db, "s1", now=T0 + timedelta(seconds=5))  # 쿠키 없는 다른 브라우저
    assert not _alive(db, first.token)
    assert _alive(db, second.token)
    assert sorted(r or "" for r in _reasons(db, user.user_id)) == ["", "SUPERSEDED"]


def test_previous_cookie_of_a_different_user_is_still_rotated(db, user, tenant_id):
    other = _tenant_viewer(db, "s2", tenant_id)
    other_session = _login(db, "s2")
    mine = _login(db, "s1", previous=other_session.token)
    assert not _alive(db, other_session.token)
    assert _alive(db, mine.token)
    assert _reasons(db, other.user_id) == ["SUPERSEDED"]


def test_disabled_user_is_rejected_and_nothing_is_changed(db, user):
    existing = _login(db, "s1")
    auth_repo.set_status(
        db, user_id=user.user_id, status=models.AppUserStatus.DISABLED, now=T0
    )
    with pytest.raises(service.AccountDisabledError):
        _login(db, "s1", previous=existing.token)
    assert db.execute(text("SELECT count(*) FROM auth_sessions")).scalar_one() == 1
    assert _reasons(db, user.user_id) == [None]


def test_unprovisioned_identity_is_rejected(db):
    with pytest.raises(service.AccountNotProvisionedError):
        _login(db, "nobody")


def test_unassigned_account_is_not_provisioned_and_nothing_is_changed(db, user):
    """UNASSIGNED 는 플랫폼 권한도 소속도 없다 — 로그인 완료가 거절되고 기존 세션 · 이메일 기록을 건드리지 않는다."""
    existing = _login(db, "s1")
    unassigned = auth_repo.create_user(
        db, issuer=ISSUER, cognito_sub="u1", email="u1@example.com", role=UserRole.ADMIN,
        account_type=AccountType.UNASSIGNED, tenant_id=None, now=T0, email_verified_at=None,
    )
    db.commit()
    with pytest.raises(service.AccountNotProvisionedError):
        _login(db, "u1", previous=existing.token)
    assert _alive(db, existing.token)
    assert _reasons(db, unassigned.user_id) == []
    db.refresh(unassigned)
    assert unassigned.email_verified_at is None


@pytest.mark.parametrize("tenant_status", [TenantStatus.ACTIVE, TenantStatus.SUSPENDED])
def test_tenant_user_logs_in_regardless_of_tenant_suspension(db, make_tenant, tenant_status):
    member = _tenant_viewer(db, "t1", make_tenant(tenant_status))
    issued = _login(db, "t1")
    current = service.resolve_current_user(
        db, issued.token, now=T0 + timedelta(minutes=1), idle_seconds=IDLE, touch=False
    )
    assert (current.user_id, current.account_type, current.tenant_id, current.tenant_status) == (
        member.user_id, AccountType.TENANT, member.tenant_id, tenant_status,
    )


def test_disabled_user_of_suspended_tenant_is_still_disabled(db, make_tenant):
    member = _tenant_viewer(db, "t2", make_tenant(TenantStatus.SUSPENDED))
    auth_repo.set_status(db, user_id=member.user_id, status=models.AppUserStatus.DISABLED, now=T0)
    with pytest.raises(service.AccountDisabledError):
        _login(db, "t2")


def test_revoke_user_sessions_facade_revokes_all_for_user_only(db, user):
    first = _login(db, "s1")
    n = sessions.revoke_user_sessions(
        db,
        user_id=user.user_id,
        now=T0 + timedelta(minutes=2),
        reason=models.AuthSessionRevokeReason.ADMIN_REVOKED,
    )
    assert n == 1
    assert not _alive(db, first.token)
    assert _reasons(db, user.user_id) == ["ADMIN_REVOKED"]


# --- 동시성: 서로 다른 커넥션 · 커밋된 데이터 ----------------------------------------


@pytest.fixture()
def committed_user(pg_engine):
    sub = f"c-{uuid.uuid4().hex[:10]}"
    with Session(pg_engine) as seed:
        row = auth_repo.create_user(
            seed,
            issuer=ISSUER,
            cognito_sub=sub,
            email=f"{sub}@example.com",
            role=UserRole.ADMIN,
            account_type=AccountType.PLATFORM,
            tenant_id=None,
            now=T0,
            email_verified_at=T0,
        )
        seed.commit()
        user_id = row.user_id
    yield sub, user_id
    with Session(pg_engine) as cleanup:
        cleanup.execute(
            text("DELETE FROM auth_sessions WHERE user_id = :u"), {"u": user_id}
        )
        cleanup.execute(text("DELETE FROM app_users WHERE user_id = :u"), {"u": user_id})
        cleanup.commit()


def test_concurrent_logins_leave_exactly_one_live_session(pg_engine, committed_user):
    sub, user_id = committed_user
    workers = 4
    barrier = threading.Barrier(workers)

    def run(i):
        with Session(pg_engine) as own:
            barrier.wait(timeout=10)
            return _login(own, sub, now=T0 + timedelta(seconds=i)).token

    with ThreadPoolExecutor(workers) as pool:
        tokens = list(pool.map(run, range(workers)))

    with Session(pg_engine) as check:
        live = [t for t in tokens if _alive(check, t)]
        reasons = _reasons(check, user_id)
    assert len(live) == 1
    assert sorted(r or "" for r in reasons) == [""] + ["SUPERSEDED"] * (workers - 1)


def _wait_until_blocked_on_users_lock(pg_engine, timeout=10.0):
    deadline = time.monotonic() + timeout
    with pg_engine.connect() as probe:
        while time.monotonic() < deadline:
            waiting = probe.execute(
                text(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE datname = current_database() AND wait_event_type = 'Lock' "
                    "AND query LIKE 'LOCK TABLE app_users%'"
                )
            ).scalar_one()
            probe.rollback()
            if waiting:
                return
            time.sleep(0.02)  # 폴링 간격일 뿐 — 정확성은 pg_stat_activity 관측이 보장한다
    raise AssertionError("login never blocked on the users table lock")


def test_login_waiting_on_lock_sees_a_disable_committed_by_the_lock_holder(
    pg_engine, committed_user
):
    sub, user_id = committed_user
    # executor가 바깥, admin 세션이 안쪽: 어서션 실패 시에도 executor가 스레드를
    # 기다리기 전에 admin 세션이 닫혀(롤백) 락이 풀려야 교착 없이 unwind 된다.
    with ThreadPoolExecutor(1) as pool, Session(pg_engine) as admin:
        auth_repo.lock_users_table(admin)  # 관리자 동작이 락을 쥔 상태
        login = pool.submit(
            lambda: _login_in_own_session(pg_engine, sub, T0 + timedelta(seconds=1))
        )
        _wait_until_blocked_on_users_lock(pg_engine)
        assert not login.done()
        auth_repo.set_status(
            admin,
            user_id=user_id,
            status=models.AppUserStatus.DISABLED,
            now=T0 + timedelta(seconds=2),
        )
        admin.commit()
        with pytest.raises(service.AccountDisabledError):
            login.result(timeout=10)
    with Session(pg_engine) as check:
        assert (
            check.execute(
                text("SELECT count(*) FROM auth_sessions WHERE user_id = :u"),
                {"u": user_id},
            ).scalar_one()
            == 0
        )


def _login_in_own_session(pg_engine, sub, now):
    with Session(pg_engine) as own:
        return _login(own, sub, now=now)
