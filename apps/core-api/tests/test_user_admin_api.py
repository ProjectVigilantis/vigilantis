"""/api/v1/users — ADMIN 사용자 관리. 실제 PostgreSQL + 가짜 Cognito 어댑터(SDK 호출 기록).

user_admin_events 는 append-only(DELETE 거절 · FK RESTRICT)라 commit 시드를 지울 수 없다.
그래서 독립 commit 이 필요한 동시성 테스트는 이력을 남기지 않는 경로(거절 · 잠금 보유자의 직접 쓰기)만 쓴다.
"""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime

import pytest
from auth import admin_service, sessions
from auth.cognito import AuthProviderUnavailableError, CognitoUser, UserStateConflictError
from auth.http import get_cognito_adapter
from auth.service import AuthRequiredError
from config import get_auth_settings
from db import models
from db.repositories import auth as auth_repo
from exceptions import ApiError
from fastapi.routing import iter_route_contexts
from schemas.api.auth import UserRole
from schemas.api.errors import ErrorCode
from schemas.api.users import UserStatus
from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError

ISSUER = "https://cognito-idp.ap-northeast-2.amazonaws.com/ap-northeast-2_test"  # conftest TEST_AUTH_ISSUER
BASE = "/api/v1/users"
FORCE = "FORCE_CHANGE_PASSWORD"


class FakeAdapter:
    issuer = ISSUER

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.create_result = CognitoUser("new@example.com", "sub-new", "new@example.com", FORCE)
        self.create_error: Exception | None = None
        self.get_result: CognitoUser | None = None

    def admin_create_user(self, email, *, resend=False):
        self.calls.append(("create", email, resend))
        if self.create_error is not None:
            raise self.create_error
        return self.create_result

    def admin_get_user(self, username):
        self.calls.append(("get", username))
        return self.get_result


@pytest.fixture()
def adapter(client_pg):
    fake = FakeAdapter()
    client_pg.app.dependency_overrides[get_cognito_adapter] = lambda: fake
    return fake


@pytest.fixture()
def admin(login_as):
    return login_as(UserRole.ADMIN)


@pytest.fixture()
def target(make_auth_session):
    return make_auth_session(UserRole.VIEWER)


def user_row(db, user_id):
    return db.execute(
        text("SELECT email, role::text, status::text, email_verified_at, issuer, cognito_sub "
             "FROM app_users WHERE user_id = :u"),
        {"u": user_id},
    ).one_or_none()


def events(db, target_id):
    rows = db.scalars(
        select(models.UserAdminEvent)
        .where(models.UserAdminEvent.target_user_id == target_id)
        .order_by(models.UserAdminEvent.occurred_at, models.UserAdminEvent.event_id)
    ).all()
    return [(r.action.value, r.before_state, r.after_state, r.actor_user_id) for r in rows]


def snapshot(db):
    return (
        db.execute(text("SELECT user_id, role::text, status::text FROM app_users ORDER BY 1")).all(),
        db.execute(text("SELECT count(*) FROM user_admin_events")).scalar_one(),
        db.execute(text("SELECT count(*) FROM auth_sessions WHERE revoked_at IS NOT NULL")).scalar_one(),
    )


def me_status(client, seed) -> int:
    return client.get("/api/v1/auth/me", headers=seed.headers).status_code


# --- 비ADMIN: 모든 경로 403 · DB 불변 · 제공자 미호출 -------------------------------------

REQUESTS = [
    ("GET", "", None),
    ("GET", "/{id}/events", None),
    ("POST", "", {"email": "x@example.com", "role": "VIEWER", "recover_existing": False}),
    ("POST", "", {"email": "bad"}),  # 잘못된 본문도 403 이 먼저
    ("POST", "/{id}/invite/resend", None),
    ("PUT", "/{id}/role", {"role": "ADMIN"}),
    ("PUT", "/{id}/role", {"role": "NOPE"}),
    ("PUT", "/{id}/status", {"status": "DISABLED"}),
    ("PUT", "/{id}/status", {}),
    ("POST", "/{id}/sessions/revoke", None),
]


def test_forbidden_requests_cover_every_registered_users_route(client):
    """REQUESTS 가 실제 등록된 /api/v1/users (메서드, 경로) 전부를 덮는다 — 새 경로·메서드가 빠지면 실패."""
    registered = {
        (method, route.path)
        for route in iter_route_contexts(client.app.routes)
        if route.path == BASE or route.path.startswith(BASE + "/")
        for method in route.methods or ()
    }
    covered = {(method, BASE + path.replace("{id}", "{user_id}")) for method, path, _ in REQUESTS}
    assert covered == registered


@pytest.mark.parametrize("role", [UserRole.OPERATOR, UserRole.VIEWER])
@pytest.mark.parametrize("method,path,body", REQUESTS)
def test_non_admin_is_forbidden_everywhere(client_pg, db, login_as, target, adapter, role, method, path, body):
    login_as(role)
    before = snapshot(db)
    resp = client_pg.request(method, BASE + path.replace("{id}", target.user_id), json=body)
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "AUTH_FORBIDDEN"
    assert snapshot(db) == before
    assert adapter.calls == []


def test_unauthenticated_is_401(client_pg):
    resp = client_pg.get(BASE, headers={"Cookie": ""})
    assert resp.status_code == 401


# --- 조회 ----------------------------------------------------------------------


def test_list_users_has_last_login_without_provider_calls(client_pg, db, admin, target, adapter):
    resp = client_pg.get(BASE)
    assert resp.status_code == 200
    items = {i["user_id"]: i for i in resp.json()["items"]}
    assert set(items[target.user_id]) == {"user_id", "email", "role", "status", "last_login_at"}
    assert items[target.user_id]["role"] == "VIEWER"
    assert items[target.user_id]["status"] == "ACTIVE"
    assert items[target.user_id]["last_login_at"] is not None
    assert adapter.calls == []


def test_events_limit_bounds_and_missing_target(client_pg, db, admin, target):
    for bad in (0, 101):
        assert client_pg.get(f"{BASE}/{target.user_id}/events", params={"limit": bad}).status_code == 422
    assert client_pg.get(f"{BASE}/missing/events").status_code == 404
    assert client_pg.get(f"{BASE}/missing/events").json()["error"]["code"] == "USER_NOT_FOUND"
    for role in ("OPERATOR", "ADMIN", "VIEWER"):
        assert client_pg.put(f"{BASE}/{target.user_id}/role", json={"role": role}).status_code in (204, 409)
    full = client_pg.get(f"{BASE}/{target.user_id}/events").json()["items"]
    assert len(full) == 3
    assert len(client_pg.get(f"{BASE}/{target.user_id}/events", params={"limit": 2}).json()["items"]) == 2


# --- 초대 ----------------------------------------------------------------------


def test_invite_creates_active_unverified_user_and_audit(client_pg, db, admin, adapter):
    resp = client_pg.post(BASE, json={"email": "new@example.com", "role": "OPERATOR"})
    assert resp.status_code == 201
    body = resp.json()
    assert set(body) == {"user_id", "email", "role", "status", "last_login_at"}
    assert (body["email"], body["role"], body["status"], body["last_login_at"]) == (
        "new@example.com", "OPERATOR", "ACTIVE", None,
    )
    assert adapter.calls == [("create", "new@example.com", False)]
    row = user_row(db, body["user_id"])
    assert (row.role, row.status, row.email_verified_at, row.issuer, row.cognito_sub) == (
        "OPERATOR", "ACTIVE", None, ISSUER, "sub-new",
    )
    assert events(db, body["user_id"]) == [
        ("USER_INVITED", {}, {"role": "OPERATOR", "status": "ACTIVE", "recover_existing": False}, admin.user_id)
    ]
    shown = client_pg.get(f"{BASE}/{body['user_id']}/events").json()["items"][0]
    assert set(shown) == {"event_id", "actor_user_id", "target_user_id", "action",
                          "before_state", "after_state", "occurred_at"}


def test_invite_duplicate_is_409_without_db_write(client_pg, db, admin, adapter):
    adapter.create_error = UserStateConflictError()
    before = snapshot(db)
    count = db.execute(text("SELECT count(*) FROM app_users")).scalar_one()
    resp = client_pg.post(BASE, json={"email": "dup@example.com", "role": "VIEWER"})
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "USER_STATE_CONFLICT"
    assert adapter.calls == [("create", "dup@example.com", False)]  # 자동 연결(get) 없음
    assert snapshot(db) == before
    assert db.execute(text("SELECT count(*) FROM app_users")).scalar_one() == count


def test_invite_provider_failure_leaves_no_db_row(client_pg, db, admin, adapter):
    adapter.create_error = AuthProviderUnavailableError()
    before = snapshot(db)
    assert client_pg.post(BASE, json={"email": "a@example.com", "role": "VIEWER"}).status_code == 503
    assert snapshot(db) == before


def test_invite_db_failure_is_sanitized_500_and_no_orphan_row(client_pg, db, admin, adapter, monkeypatch):
    def boom(*args, **kwargs):
        raise SQLAlchemyError("secret-detail postgres://user:pw@host")

    monkeypatch.setattr(auth_repo, "add_user_event", boom)
    count = db.execute(text("SELECT count(*) FROM app_users")).scalar_one()
    resp = client_pg.post(BASE, json={"email": "a@example.com", "role": "VIEWER"})
    assert resp.status_code == 500
    assert resp.json()["error"]["code"] == "INTERNAL_ERROR"
    assert "secret-detail" not in resp.text
    assert db.execute(text("SELECT count(*) FROM app_users")).scalar_one() == count
    assert adapter.calls == [("create", "a@example.com", False)]  # 제공자 사용자는 지우지 않는다


def test_recover_existing_links_provider_user(client_pg, db, admin, adapter):
    adapter.get_result = CognitoUser("old@example.com", "sub-old", "old@example.com", "CONFIRMED")
    resp = client_pg.post(BASE, json={"email": "old@example.com", "role": "VIEWER", "recover_existing": True})
    assert resp.status_code == 201
    assert adapter.calls == [("get", "old@example.com")]
    uid = resp.json()["user_id"]
    assert user_row(db, uid).cognito_sub == "sub-old"
    assert events(db, uid)[0][2] == {"role": "VIEWER", "status": "ACTIVE", "recover_existing": True}


def test_recover_existing_missing_provider_user_is_404(client_pg, db, admin, adapter):
    before = snapshot(db)
    resp = client_pg.post(BASE, json={"email": "none@example.com", "role": "VIEWER", "recover_existing": True})
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "USER_NOT_FOUND"
    assert snapshot(db) == before


def test_recover_existing_already_provisioned_is_409(client_pg, db, admin, adapter, target):
    sub = user_row(db, target.user_id).cognito_sub
    adapter.get_result = CognitoUser("t@example.com", sub, "t@example.com", "CONFIRMED")
    count = db.execute(text("SELECT count(*) FROM app_users")).scalar_one()
    resp = client_pg.post(BASE, json={"email": "t@example.com", "role": "ADMIN", "recover_existing": True})
    assert resp.status_code == 409
    assert db.execute(text("SELECT count(*) FROM app_users")).scalar_one() == count


# --- 재발송 --------------------------------------------------------------------


def _provider_for(db, target, status=FORCE, sub=None):
    row = user_row(db, target.user_id)
    return CognitoUser(row.email, sub or row.cognito_sub, row.email, status)


def test_resend_invite_resets_temp_password_and_audits(client_pg, db, admin, target, adapter):
    adapter.get_result = _provider_for(db, target)
    email = user_row(db, target.user_id).email
    resp = client_pg.post(f"{BASE}/{target.user_id}/invite/resend")
    assert resp.status_code == 204
    assert adapter.calls == [("get", email), ("create", email, True)]
    assert events(db, target.user_id) == [("INVITE_RESENT", {}, {}, admin.user_id)]


@pytest.mark.parametrize("case", ["no_provider_user", "other_sub", "confirmed"])
def test_resend_invite_state_mismatch_is_409_without_resend(client_pg, db, admin, target, adapter, case):
    adapter.get_result = {
        "no_provider_user": None,
        "other_sub": _provider_for(db, target, sub="someone-else"),
        "confirmed": _provider_for(db, target, status="CONFIRMED"),
    }[case]
    resp = client_pg.post(f"{BASE}/{target.user_id}/invite/resend")
    assert resp.status_code == 409
    assert [c for c in adapter.calls if c[0] == "create"] == []
    assert events(db, target.user_id) == []


def test_resend_missing_target_is_404_without_provider_call(client_pg, db, admin, adapter):
    assert client_pg.post(f"{BASE}/nope/invite/resend").status_code == 404
    assert adapter.calls == []


# --- 역할 ----------------------------------------------------------------------


def test_change_role_updates_and_audits(client_pg, db, admin, target):
    assert client_pg.put(f"{BASE}/{target.user_id}/role", json={"role": "OPERATOR"}).status_code == 204
    assert user_row(db, target.user_id).role == "OPERATOR"
    assert events(db, target.user_id) == [
        ("ROLE_CHANGED", {"role": "VIEWER"}, {"role": "OPERATOR"}, admin.user_id)
    ]


def test_change_role_same_value_self_and_missing(client_pg, db, admin, target):
    same = client_pg.put(f"{BASE}/{target.user_id}/role", json={"role": "VIEWER"})
    assert (same.status_code, same.json()["error"]["code"]) == (409, "USER_STATE_CONFLICT")
    selfie = client_pg.put(f"{BASE}/{admin.user_id}/role", json={"role": "VIEWER"})
    assert (selfie.status_code, selfie.json()["error"]["code"]) == (403, "AUTH_FORBIDDEN")
    assert client_pg.put(f"{BASE}/nope/role", json={"role": "VIEWER"}).status_code == 404
    assert user_row(db, admin.user_id).role == "ADMIN"
    assert events(db, target.user_id) == [] and events(db, admin.user_id) == []


def test_demote_other_admin_ok_but_last_active_admin_is_409(client_pg, db, admin, make_auth_session, monkeypatch):
    other = make_auth_session(UserRole.ADMIN)
    # 행위자가 활성 ADMIN 이고 본인 변경이 막혀 있어, 실제 요청으로는 count 가 1 이 될 수 없다 — 집계 값을 고정해 방어선을 확인한다
    monkeypatch.setattr(auth_repo, "count_active_admins", lambda db: 1)
    resp = client_pg.put(f"{BASE}/{other.user_id}/role", json={"role": "VIEWER"})
    assert (resp.status_code, resp.json()["error"]["code"]) == (409, "USER_STATE_CONFLICT")
    assert user_row(db, other.user_id).role == "ADMIN"
    monkeypatch.undo()
    assert client_pg.put(f"{BASE}/{other.user_id}/role", json={"role": "VIEWER"}).status_code == 204


# --- 상태 ----------------------------------------------------------------------


def test_disable_revokes_sessions_and_reenable_does_not_resurrect(client_pg, db, admin, target):
    assert me_status(client_pg, target) == 200
    assert client_pg.put(f"{BASE}/{target.user_id}/status", json={"status": "DISABLED"}).status_code == 204
    assert user_row(db, target.user_id).status == "DISABLED"
    reasons = db.execute(
        text("SELECT revoke_reason::text FROM auth_sessions WHERE user_id = :u"), {"u": target.user_id}
    ).scalars().all()
    assert reasons == ["ACCOUNT_DISABLED"]
    assert me_status(client_pg, target) == 401
    assert client_pg.put(f"{BASE}/{target.user_id}/status", json={"status": "ACTIVE"}).status_code == 204
    assert user_row(db, target.user_id).status == "ACTIVE"
    assert me_status(client_pg, target) == 401  # 폐기된 세션은 되살아나지 않는다
    assert [e[:3] for e in events(db, target.user_id)] == [
        ("STATUS_CHANGED", {"status": "ACTIVE"}, {"status": "DISABLED"}),
        ("STATUS_CHANGED", {"status": "DISABLED"}, {"status": "ACTIVE"}),
    ]


def test_status_same_value_self_missing(client_pg, db, admin, target):
    same = client_pg.put(f"{BASE}/{target.user_id}/status", json={"status": "ACTIVE"})
    assert (same.status_code, same.json()["error"]["code"]) == (409, "USER_STATE_CONFLICT")
    assert client_pg.put(f"{BASE}/{admin.user_id}/status", json={"status": "DISABLED"}).status_code == 403
    assert client_pg.put(f"{BASE}/nope/status", json={"status": "DISABLED"}).status_code == 404
    assert me_status(client_pg, admin) == 200


def test_disable_last_active_admin_is_409(client_pg, db, admin, make_auth_session, monkeypatch):
    other = make_auth_session(UserRole.ADMIN)
    monkeypatch.setattr(auth_repo, "count_active_admins", lambda db: 1)
    resp = client_pg.put(f"{BASE}/{other.user_id}/status", json={"status": "DISABLED"})
    assert resp.status_code == 409
    assert user_row(db, other.user_id).status == "ACTIVE"
    assert me_status(client_pg, other) == 200


# --- 세션 폐기 -----------------------------------------------------------------


def test_revoke_sessions_counts_and_audits(client_pg, db, admin, target):
    sessions.issue_session(
        db, user_id=target.user_id, mfa_step=models.AuthSessionMfaStep.SOFTWARE_TOKEN_MFA,
        now=datetime.now(UTC), max_seconds=3600, idle_seconds=1800,
    )
    db.commit()
    assert client_pg.post(f"{BASE}/{target.user_id}/sessions/revoke").status_code == 204
    reasons = db.execute(
        text("SELECT revoke_reason::text FROM auth_sessions WHERE user_id = :u"), {"u": target.user_id}
    ).scalars().all()
    assert reasons == ["ADMIN_REVOKED", "ADMIN_REVOKED"]
    assert me_status(client_pg, target) == 401
    assert events(db, target.user_id) == [
        ("SESSIONS_REVOKED", {}, {"revoked_session_count": 2}, admin.user_id)
    ]
    assert client_pg.post(f"{BASE}/nope/sessions/revoke").status_code == 404


def test_revoke_own_sessions_is_allowed_then_401(client_pg, db, admin):
    assert client_pg.post(f"{BASE}/{admin.user_id}/sessions/revoke").status_code == 204
    assert events(db, admin.user_id)[0][:3] == ("SESSIONS_REVOKED", {}, {"revoked_session_count": 1})
    assert client_pg.get(BASE).status_code == 401


# --- 설정 누락 우선순위 --------------------------------------------------------------


def test_missing_cognito_config_403_before_503_and_ordinary_routes_work(client_pg, db, login_as, target):
    base = get_auth_settings()
    client_pg.app.dependency_overrides[get_auth_settings] = lambda: base.model_copy(
        update={"COGNITO_USER_POOL_ID": None}
    )
    body = {"email": "a@example.com", "role": "VIEWER"}
    login_as(UserRole.OPERATOR)
    assert client_pg.post(BASE, json=body).status_code == 403
    assert client_pg.post(f"{BASE}/{target.user_id}/invite/resend").status_code == 403
    login_as(UserRole.ADMIN)
    for resp in (client_pg.post(BASE, json=body), client_pg.post(f"{BASE}/{target.user_id}/invite/resend")):
        assert resp.status_code == 503
        assert resp.json()["error"]["code"] == "AUTH_PROVIDER_UNAVAILABLE"
    assert client_pg.get(BASE).status_code == 200
    assert client_pg.put(f"{BASE}/{target.user_id}/role", json={"role": "OPERATOR"}).status_code == 204
    assert client_pg.post(f"{BASE}/{target.user_id}/sessions/revoke").status_code == 204


# --- 동시성 · 낡은 행위자 (독립 commit 세션) -------------------------------------------


def test_actor_repr_does_not_expose_session_token():
    actor = admin_service.Actor(token="secret-token-xyz", idle_seconds=1800, clock=lambda: None)
    assert "secret-token-xyz" not in repr(actor) and "secret-token-xyz" not in str(actor)


def _actor(seed):
    return admin_service.Actor(token=seed.token, idle_seconds=1800, clock=lambda: datetime.now(UTC))


def _run_blocked(pg_engine, committed_auth, fn, holder_change):
    """잠금 보유자가 users 를 잠근 채 holder_change 를 쓰는 동안 fn 을 다른 스레드에서 막아 둔 뒤 풀어 결과를 돌려준다."""
    outcome: dict = {}

    def work():
        with committed_auth.sessions() as db:
            try:
                fn(db)
                outcome["error"] = None
            except BaseException as exc:  # noqa: BLE001 — 결과 비교용
                outcome["error"] = exc

    with committed_auth.sessions() as holder:
        auth_repo.lock_users_table(holder)
        thread = threading.Thread(target=work)
        thread.start()
        deadline = time.monotonic() + 10
        with pg_engine.connect() as probe:
            while time.monotonic() < deadline:
                waiting = probe.execute(
                    text("SELECT count(*) FROM pg_stat_activity "
                         "WHERE wait_event_type = 'Lock' AND query LIKE 'LOCK TABLE app_users%'")
                ).scalar_one()
                if waiting:
                    break
                time.sleep(0.05)
            else:
                pytest.fail("쓰기가 users 잠금에서 기다리지 않았다")
        holder_change(holder)
        holder.commit()
    thread.join(10)
    assert not thread.is_alive()
    return outcome["error"]


def _admin_state(committed_auth, seed):
    with committed_auth.sessions() as db:
        return db.execute(
            text("SELECT role::text, status::text FROM app_users WHERE user_id = :u"), {"u": seed.user_id}
        ).one()


@pytest.mark.parametrize(
    "change,expected",
    [
        ("revoke", AuthRequiredError),
        ("disable", AuthRequiredError),
        ("demote", ApiError),
    ],
)
def test_stale_actor_is_rechecked_under_lock_and_nothing_is_written(
    pg_engine, committed_auth, change, expected
):
    actor_seed = committed_auth.make(UserRole.ADMIN)
    victim = committed_auth.make(UserRole.VIEWER)

    def holder_change(db):
        now = datetime.now(UTC)
        if change == "revoke":
            sessions.revoke_user_sessions(
                db, user_id=actor_seed.user_id, now=now, reason=models.AuthSessionRevokeReason.ADMIN_REVOKED
            )
        elif change == "disable":
            auth_repo.set_status(db, user_id=actor_seed.user_id, status=models.AppUserStatus.DISABLED, now=now)
        else:
            auth_repo.set_role(db, user_id=actor_seed.user_id, role=UserRole.VIEWER, now=now)

    error = _run_blocked(
        pg_engine,
        committed_auth,
        lambda db: admin_service.change_role(db, _actor(actor_seed), victim.user_id, UserRole.OPERATOR),
        holder_change,
    )
    assert isinstance(error, expected)
    if change == "demote":
        assert error.code is ErrorCode.AUTH_FORBIDDEN
    assert _admin_state(committed_auth, victim)[0] == "VIEWER"
    with committed_auth.sessions() as db:
        assert db.execute(
            text("SELECT count(*) FROM user_admin_events WHERE target_user_id = :u"), {"u": victim.user_id}
        ).scalar_one() == 0


def test_invite_actor_revoked_during_sdk_writes_nothing_but_provider_effect_remains(committed_auth):
    actor_seed = committed_auth.make(UserRole.ADMIN)

    class RevokingAdapter(FakeAdapter):
        def admin_create_user(self, email, *, resend=False):
            result = super().admin_create_user(email, resend=resend)
            with committed_auth.sessions() as other:
                sessions.revoke_user_sessions(
                    other, user_id=actor_seed.user_id, now=datetime.now(UTC),
                    reason=models.AuthSessionRevokeReason.ADMIN_REVOKED,
                )
                other.commit()
            return result

    fake = RevokingAdapter()
    with committed_auth.sessions() as db:
        with pytest.raises(AuthRequiredError):
            admin_service.invite_user(
                db, _actor(actor_seed), fake, email="late@example.com", role=UserRole.VIEWER,
                recover_existing=False,
            )
    assert fake.calls == [("create", "late@example.com", False)]
    with committed_auth.sessions() as db:
        assert db.execute(
            text("SELECT count(*) FROM app_users WHERE cognito_sub = 'sub-new'")
        ).scalar_one() == 0


# --- 서로를 동시에 강등 · 비활성화 (격리 DB · 실제 서비스 · 실제 집계) ------------------------


@pytest.fixture()
def throwaway_engine(pg_engine):
    """이력이 append-only 라 commit 데이터를 지울 수 없다 — 이 테스트 전용 DB 를 만들고 정확히 그 DB 만 지운다."""
    import os
    import uuid as uuid_module

    from alembic import command
    from alembic.config import Config
    from config import get_settings
    from sqlalchemy import create_engine

    from pathlib import Path

    CORE_API = Path(__file__).resolve().parents[1]
    ADMIN_URL = pg_engine.url.set(database="postgres").render_as_string(hide_password=False)
    name = f"vigilantis_test_{uuid_module.uuid4().hex}"
    admin = create_engine(ADMIN_URL, isolation_level="AUTOCOMMIT")
    previous = os.environ.get("DATABASE_URL")
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    engine = None
    try:
        url = ADMIN_URL.rsplit("/", 1)[0] + "/" + name
        os.environ["DATABASE_URL"] = url
        get_settings.cache_clear()
        command.upgrade(Config(str(CORE_API / "alembic.ini")), "head")
        engine = create_engine(url)
        yield engine
    finally:
        if engine is not None:
            engine.dispose()
        if previous is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous
        get_settings.cache_clear()
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


@pytest.mark.parametrize("first,second", [("role", "role"), ("status", "status"), ("role", "status"), ("status", "role")])
def test_two_admins_cannot_remove_each_other_concurrently(throwaway_engine, first, second):
    from sqlalchemy.orm import sessionmaker

    factory = sessionmaker(bind=throwaway_engine, autoflush=False, expire_on_commit=False)
    now = datetime.now(UTC)
    seeds = []
    with factory() as db:
        for n in range(2):
            user = auth_repo.create_user(
                db, issuer=ISSUER, cognito_sub=f"race-{n}", email=f"race{n}@example.com",
                role=UserRole.ADMIN, now=now, email_verified_at=now,
            )
            issued = sessions.issue_session(
                db, user_id=user.user_id, mfa_step=models.AuthSessionMfaStep.SOFTWARE_TOKEN_MFA,
                now=now, max_seconds=3600, idle_seconds=1800,
            )
            seeds.append((user.user_id, issued.token))
        db.commit()
    assert auth_repo.count_active_admins(factory()) == 2

    barrier = threading.Barrier(2)
    results: list = [None, None]

    def attempt(index: int, kind: str) -> None:
        actor_id, token = seeds[index]
        victim_id = seeds[1 - index][0]
        actor = admin_service.Actor(token=token, idle_seconds=1800, clock=lambda: datetime.now(UTC))
        with factory() as db:
            barrier.wait(10)
            try:
                if kind == "role":
                    admin_service.change_role(db, actor, victim_id, UserRole.VIEWER)
                else:
                    admin_service.change_status(db, actor, victim_id, UserStatus.DISABLED)
                results[index] = None
            except BaseException as exc:  # noqa: BLE001 — 결과 비교용
                results[index] = exc

    threads = [threading.Thread(target=attempt, args=(i, k)) for i, k in enumerate((first, second))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(20)
        assert not t.is_alive()

    winners = [r for r in results if r is None]
    losers = [r for r in results if r is not None]
    assert len(winners) == 1 and len(losers) == 1, results
    loser = losers[0]
    assert isinstance(loser, AuthRequiredError) or (
        isinstance(loser, ApiError) and loser.code is ErrorCode.AUTH_FORBIDDEN
    ), loser
    with factory() as db:
        assert auth_repo.count_active_admins(db) == 1
        assert db.execute(text("SELECT count(*) FROM user_admin_events")).scalar_one() == 1


# --- 정규형이 아닌 UUID 표기(urn:uuid: · 대문자 · 중괄호) ---------------------------------------

MISSING_UUID = "00000000-0000-0000-0000-000000000001"


def _forms(user_id):
    return [f"urn:uuid:{user_id}", str(user_id).upper(), "{" + str(user_id) + "}", str(user_id).replace("-", "")]


def test_noncanonical_missing_target_is_404_not_500(client_pg, db, admin, adapter):
    ref = f"urn:uuid:{MISSING_UUID}"
    assert client_pg.get(f"{BASE}/{ref}/events").status_code == 404
    assert client_pg.put(f"{BASE}/{ref}/role", json={"role": "VIEWER"}).status_code == 404
    assert client_pg.put(f"{BASE}/{ref}/status", json={"status": "DISABLED"}).status_code == 404
    assert client_pg.post(f"{BASE}/{ref}/sessions/revoke").status_code == 404
    assert client_pg.post(f"{BASE}/{ref}/invite/resend").status_code == 404
    assert adapter.calls == []


def test_noncanonical_existing_target_behaves_canonically(client_pg, db, admin, target):
    urn, upper = _forms(target.user_id)[:2]
    assert client_pg.put(f"{BASE}/{urn}/role", json={"role": "OPERATOR"}).status_code == 204
    assert client_pg.put(f"{BASE}/{upper}/status", json={"status": "DISABLED"}).status_code == 204
    assert client_pg.post(f"{BASE}/{urn}/sessions/revoke").status_code == 204
    row = user_row(db, target.user_id)
    assert (row.role, row.status) == ("OPERATOR", "DISABLED")
    assert [e[0] for e in events(db, target.user_id)] == ["ROLE_CHANGED", "STATUS_CHANGED", "SESSIONS_REVOKED"]
    assert all(e[3] == admin.user_id for e in events(db, target.user_id))
    history = client_pg.get(f"{BASE}/{urn}/events").json()["items"]
    assert len(history) == 3 and {i["target_user_id"] for i in history} == {target.user_id}


def test_noncanonical_self_is_still_403_and_last_admin_still_409(
    client_pg, db, admin, make_auth_session, monkeypatch
):
    for form in _forms(admin.user_id):
        assert client_pg.put(f"{BASE}/{form}/role", json={"role": "VIEWER"}).status_code == 403
        assert client_pg.put(f"{BASE}/{form}/status", json={"status": "DISABLED"}).status_code == 403
    assert user_row(db, admin.user_id)[1:3] == ("ADMIN", "ACTIVE")
    other = make_auth_session(UserRole.ADMIN)
    monkeypatch.setattr(auth_repo, "count_active_admins", lambda db: 1)
    urn = f"urn:uuid:{other.user_id}"
    assert client_pg.put(f"{BASE}/{urn}/role", json={"role": "VIEWER"}).status_code == 409
    assert client_pg.put(f"{BASE}/{urn}/status", json={"status": "DISABLED"}).status_code == 409
    assert events(db, other.user_id) == []
