"""/api/v1/users — ADMIN 사용자 관리. 실제 PostgreSQL + 가짜 Cognito 어댑터(SDK 호출 기록).

user_admin_events 는 append-only(DELETE 거절 · FK RESTRICT)라 commit 시드를 지울 수 없다.
그래서 독립 commit 이 필요한 동시성 테스트는 이력을 남기지 않는 경로(거절 · 잠금 보유자의 직접 쓰기)만 쓰거나,
이 파일 전용 격리 DB(throwaway_engine)를 쓴다.

관리 범위(#492): 행위자는 CENTRAL_ADMIN(모든 고객 테넌트) · 배정된 TENANT_MANAGER(배정 고객사만) ·
고객사 ADMIN(자기 테넌트) 세 종류이고, 같은 동작을 세 행위자로 돌린다(admin 픽스처). 대상은 실제 테넌트 행에
묶인 TENANT 계정이다. 범위 밖(미배정 포함) · PLATFORM · UNASSIGNED 대상은 없는 대상과 같은 404 이고
Cognito 를 부르지 않는다. 배정이 없는 TENANT_MANAGER 는 고객 데이터가 없다(전체가 아니다).
"""

from __future__ import annotations

import threading
import time
import uuid
from datetime import UTC, datetime

import pytest
from auth import admin_service, sessions
from auth.cognito import (
    AuthProviderUnavailableError,
    CognitoUser,
    UserStateConflictError,
)
from auth.service import AuthRequiredError
from config import get_auth_settings
from db import models
from db.repositories import auth as auth_repo
from db.repositories import tenants as tenants_repo
from exceptions import ApiError
from fastapi.routing import iter_route_contexts
from routers.users import get_cognito_adapter_factory
from schemas.api.auth import AccountType, PlatformRole, UserRole
from schemas.api.errors import ErrorCode
from schemas.api.tenants import TenantStatus
from schemas.api.users import UserStatus
from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError, SQLAlchemyError

ISSUER = "https://cognito-idp.ap-northeast-2.amazonaws.com/ap-northeast-2_test"  # conftest TEST_AUTH_ISSUER
BASE = "/api/v1/users"
FORCE = "FORCE_CHANGE_PASSWORD"
ITEM_KEYS = {"user_id", "email", "role", "account_type", "platform_role", "tenant_id", "status", "last_login_at"}


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


def use_adapter(client, fake) -> None:
    """초대 · 재발송의 지연 조립 의존성을 가짜 어댑터로 바꾼다 — 서비스가 사전 판정 뒤에 부른다."""
    client.app.dependency_overrides[get_cognito_adapter_factory] = lambda: (lambda: fake)


@pytest.fixture()
def adapter(client_pg):
    fake = FakeAdapter()
    use_adapter(client_pg, fake)
    return fake


@pytest.fixture()
def tenant(make_tenant) -> str:
    """대상 · 고객사 행위자가 속한 ACTIVE 테넌트."""
    return make_tenant()


def assign(db, manager, *tenant_ids: str) -> None:
    """TENANT_MANAGER 의 배정 고객사를 통째로 바꾸고 commit 한다(요청 세션이 보게)."""
    auth_repo.replace_assigned_tenants(
        db, user_id=manager.user_id, tenant_ids=tuple(tenant_ids), now=datetime.now(UTC)
    )
    db.commit()


@pytest.fixture()
def manager(login_as):
    """배정이 없는 TENANT_MANAGER — 테스트가 assign 으로 배정한다."""
    return login_as(UserRole.ADMIN, platform_role=PlatformRole.TENANT_MANAGER)


@pytest.fixture(params=["platform", "manager", "tenant"])
def admin(request, login_as, db, tenant):
    """같은 관리 권한의 세 행위자 — CENTRAL_ADMIN · 대상 테넌트가 배정된 TENANT_MANAGER · 대상 테넌트의 고객사 ADMIN."""
    if request.param == "platform":
        return login_as(UserRole.ADMIN, platform_role=PlatformRole.CENTRAL_ADMIN)
    if request.param == "manager":
        seed = login_as(UserRole.ADMIN, platform_role=PlatformRole.TENANT_MANAGER)
        assign(db, seed, tenant)
        return seed
    return login_as(UserRole.ADMIN, tenant_id=tenant)


@pytest.fixture()
def platform_admin(login_as):
    """기존 계정 연결(recover_existing)은 플랫폼 관리 권한자만 쓸 수 있다(전역 범위의 CENTRAL_ADMIN)."""
    return login_as(UserRole.ADMIN, platform_role=PlatformRole.CENTRAL_ADMIN)


@pytest.fixture()
def tenant_admin(login_as, tenant):
    """본인 변경 · 본인 세션 회수처럼 대상이 될 수 있는 행위자(고객사 ADMIN)가 필요한 경우."""
    return login_as(UserRole.ADMIN, tenant_id=tenant)


@pytest.fixture()
def target(make_auth_session, tenant):
    return make_auth_session(UserRole.VIEWER, tenant_id=tenant)


def user_row(db, user_id):
    return db.execute(
        text("SELECT email, role::text, status::text, email_verified_at, issuer, cognito_sub, "
             "account_type::text, tenant_id::text FROM app_users WHERE user_id = :u"),
        {"u": user_id},
    ).one_or_none()


def suspend(db, tenant_id):
    assert tenants_repo.suspend_tenant(db, tenant_id, reason="테스트 정지", now=datetime.now(UTC))
    db.commit()


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
        db.execute(text("SELECT tenant_id, status::text FROM tenants ORDER BY 1")).all(),
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


@pytest.mark.parametrize("tenant_status", [TenantStatus.ACTIVE, TenantStatus.SUSPENDED])
@pytest.mark.parametrize("role", [UserRole.OPERATOR, UserRole.VIEWER])
@pytest.mark.parametrize("method,path,body", REQUESTS)
def test_non_admin_is_forbidden_everywhere(
    client_pg, db, login_as, make_tenant, make_auth_session, adapter, tenant_status, role, method, path, body
):
    """고객사 OPERATOR · VIEWER(PLATFORM 은 ADMIN 고정이라 이 역할이 없다) — 자기 테넌트 대상에도 403 이 먼저다."""
    own = make_tenant(tenant_status)
    target = make_auth_session(UserRole.VIEWER, tenant_id=own)
    login_as(role, tenant_id=own)
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


def test_list_users_has_last_login_without_provider_calls(client_pg, db, admin, target, tenant, adapter):
    resp = client_pg.get(BASE)
    assert resp.status_code == 200
    items = {i["user_id"]: i for i in resp.json()["items"]}
    assert set(items[target.user_id]) == ITEM_KEYS
    assert items[target.user_id]["role"] == "VIEWER"
    assert items[target.user_id]["status"] == "ACTIVE"
    assert (items[target.user_id]["account_type"], items[target.user_id]["tenant_id"]) == ("TENANT", tenant)
    assert items[target.user_id]["last_login_at"] is not None
    assert adapter.calls == []


def test_list_scope_platform_sees_every_tenant_and_tenant_admin_only_its_own(
    client_pg, db, login_as, make_auth_session, make_tenant, tenant, target, adapter
):
    other_tenant = make_tenant()
    suspended_tenant = make_tenant(TenantStatus.SUSPENDED)
    other = make_auth_session(UserRole.OPERATOR, tenant_id=other_tenant)
    parked = make_auth_session(UserRole.VIEWER, tenant_id=suspended_tenant)
    hidden_platform = make_auth_session(UserRole.ADMIN)
    unassigned = make_auth_session(UserRole.ADMIN, account_type=AccountType.UNASSIGNED)

    platform = login_as(UserRole.ADMIN)
    seen = {i["user_id"]: (i["account_type"], i["tenant_id"]) for i in client_pg.get(BASE).json()["items"]}
    assert {target.user_id, other.user_id, parked.user_id} <= set(seen)
    assert {platform.user_id, hidden_platform.user_id, unassigned.user_id}.isdisjoint(seen)
    assert {kind for kind, _ in seen.values()} == {"TENANT"}

    own_admin = login_as(UserRole.ADMIN, tenant_id=tenant)
    own = {i["user_id"]: i["tenant_id"] for i in client_pg.get(BASE).json()["items"]}
    assert set(own) == {target.user_id, own_admin.user_id}
    assert set(own.values()) == {tenant}
    assert adapter.calls == []


def test_events_limit_bounds_and_missing_target(client_pg, db, admin, target):
    for bad in (0, 101):
        assert client_pg.get(f"{BASE}/{target.user_id}/events", params={"limit": bad}).status_code == 422
    assert client_pg.get(f"{BASE}/missing/events").status_code == 404
    assert client_pg.get(f"{BASE}/missing/events").json()["error"]["code"] == "USER_NOT_FOUND"
    for role in ("OPERATOR", "VIEWER", "OPERATOR"):
        assert client_pg.put(f"{BASE}/{target.user_id}/role", json={"role": role}).status_code == 204
    full = client_pg.get(f"{BASE}/{target.user_id}/events").json()["items"]
    assert len(full) == 3
    assert len(client_pg.get(f"{BASE}/{target.user_id}/events", params={"limit": 2}).json()["items"]) == 2


# --- 초대 ----------------------------------------------------------------------


def test_invite_creates_active_unverified_tenant_user_and_audit(client_pg, db, admin, tenant, adapter):
    resp = client_pg.post(BASE, json={"email": "new@example.com", "role": "OPERATOR", "tenant_id": tenant})
    assert resp.status_code == 201
    body = resp.json()
    assert set(body) == ITEM_KEYS
    assert (body["email"], body["role"], body["status"], body["last_login_at"]) == (
        "new@example.com", "OPERATOR", "ACTIVE", None,
    )
    assert (body["account_type"], body["tenant_id"]) == ("TENANT", tenant)
    assert adapter.calls == [("create", "new@example.com", False)]
    row = user_row(db, body["user_id"])
    assert (row.role, row.status, row.email_verified_at, row.issuer, row.cognito_sub) == (
        "OPERATOR", "ACTIVE", None, ISSUER, "sub-new",
    )
    assert (row.account_type, row.tenant_id) == ("TENANT", tenant)
    assert events(db, body["user_id"]) == [
        ("USER_INVITED", {},
         {"role": "OPERATOR", "status": "ACTIVE", "recover_existing": False, "tenant_id": tenant},
         admin.user_id)
    ]
    shown = client_pg.get(f"{BASE}/{body['user_id']}/events").json()["items"][0]
    assert set(shown) == {"event_id", "actor_user_id", "target_user_id", "action",
                          "before_state", "after_state", "occurred_at"}
    assert shown["after_state"]["tenant_id"] == tenant


def test_platform_invite_requires_explicit_tenant(client_pg, db, adapter):
    """client_pg 기본 세션은 PLATFORM ADMIN — 소속을 추론할 곳이 없어 tenant_id 가 없으면 422, SDK 미호출."""
    before = snapshot(db)
    resp = client_pg.post(BASE, json={"email": "a@example.com", "role": "VIEWER"})
    assert (resp.status_code, resp.json()["error"]["code"]) == (422, "REQUEST_VALIDATION_FAILED")
    assert adapter.calls == []
    assert snapshot(db) == before


@pytest.mark.parametrize("field", [{"account_type": "PLATFORM"}, {"account_type": "TENANT"}])
def test_invite_cannot_choose_account_type(client_pg, db, admin, tenant, adapter, field):
    before = snapshot(db)
    resp = client_pg.post(BASE, json={"email": "a@example.com", "role": "ADMIN", "tenant_id": tenant, **field})
    assert (resp.status_code, resp.json()["error"]["code"]) == (422, "REQUEST_VALIDATION_FAILED")
    assert adapter.calls == []
    assert snapshot(db) == before


@pytest.mark.parametrize("state", [TenantStatus.PENDING_CONNECTION, TenantStatus.SUSPENDED])
def test_platform_invite_into_non_active_tenant_is_409_before_sdk(client_pg, db, make_tenant, adapter, state):
    before_tenant = make_tenant(state)
    before = snapshot(db)
    resp = client_pg.post(BASE, json={"email": "a@example.com", "role": "ADMIN", "tenant_id": before_tenant})
    assert (resp.status_code, resp.json()["error"]["code"]) == (409, "TENANT_STATE_CONFLICT")
    assert adapter.calls == []
    assert snapshot(db) == before


def test_platform_invite_into_unknown_tenant_is_404_before_sdk(client_pg, db, adapter):
    before = snapshot(db)
    resp = client_pg.post(BASE, json={"email": "a@example.com", "role": "ADMIN", "tenant_id": str(uuid.uuid4())})
    assert (resp.status_code, resp.json()["error"]["code"]) == (404, "TENANT_NOT_FOUND")
    assert adapter.calls == []
    assert snapshot(db) == before


def test_tenant_admin_invite_is_bound_to_its_own_tenant(client_pg, db, tenant_admin, tenant, make_tenant, adapter):
    # 생략 → 자기 테넌트
    implicit = client_pg.post(BASE, json={"email": "new@example.com", "role": "VIEWER"})
    assert implicit.status_code == 201
    assert (implicit.json()["account_type"], implicit.json()["tenant_id"]) == ("TENANT", tenant)
    # 다른 테넌트(있든 없든)는 같은 404 — 다른 고객의 존재 · 상태를 드러내지 않는다. SDK 미호출
    adapter.calls.clear()
    before = snapshot(db)
    for foreign in (make_tenant(), make_tenant(TenantStatus.SUSPENDED), str(uuid.uuid4())):
        resp = client_pg.post(BASE, json={"email": "x@example.com", "role": "ADMIN", "tenant_id": foreign})
        assert (resp.status_code, resp.json()["error"]["code"]) == (404, "TENANT_NOT_FOUND")
    assert adapter.calls == []
    assert snapshot(db)[:3] == before[:3]
    # 자기 테넌트를 명시해도 된다
    adapter.create_result = CognitoUser("own@example.com", "sub-own", "own@example.com", FORCE)
    explicit = client_pg.post(BASE, json={"email": "own@example.com", "role": "OPERATOR", "tenant_id": tenant})
    assert (explicit.status_code, explicit.json()["tenant_id"]) == (201, tenant)


def test_invite_duplicate_is_409_without_db_write(client_pg, db, admin, tenant, adapter):
    adapter.create_error = UserStateConflictError()
    before = snapshot(db)
    count = db.execute(text("SELECT count(*) FROM app_users")).scalar_one()
    resp = client_pg.post(BASE, json={"email": "dup@example.com", "role": "VIEWER", "tenant_id": tenant})
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "USER_STATE_CONFLICT"
    assert adapter.calls == [("create", "dup@example.com", False)]  # 자동 연결(get) 없음
    assert snapshot(db) == before
    assert db.execute(text("SELECT count(*) FROM app_users")).scalar_one() == count


def test_invite_provider_failure_leaves_no_db_row(client_pg, db, admin, tenant, adapter):
    adapter.create_error = AuthProviderUnavailableError()
    before = snapshot(db)
    body = {"email": "a@example.com", "role": "VIEWER", "tenant_id": tenant}
    assert client_pg.post(BASE, json=body).status_code == 503
    assert snapshot(db) == before


def test_invite_db_failure_is_sanitized_500_and_no_orphan_row(client_pg, db, admin, tenant, adapter, monkeypatch):
    def boom(*args, **kwargs):
        raise SQLAlchemyError("secret-detail postgres://user:pw@host")

    monkeypatch.setattr(auth_repo, "add_user_event", boom)
    count = db.execute(text("SELECT count(*) FROM app_users")).scalar_one()
    resp = client_pg.post(BASE, json={"email": "a@example.com", "role": "VIEWER", "tenant_id": tenant})
    assert resp.status_code == 500
    assert resp.json()["error"]["code"] == "INTERNAL_ERROR"
    assert "secret-detail" not in resp.text
    assert db.execute(text("SELECT count(*) FROM app_users")).scalar_one() == count
    assert adapter.calls == [("create", "a@example.com", False)]  # 제공자 사용자는 지우지 않는다


def test_tenant_admin_recover_existing_is_403_without_sdk_or_db_change(client_pg, db, tenant_admin, tenant, adapter):
    adapter.get_result = CognitoUser("old@example.com", "sub-old", "old@example.com", "CONFIRMED")
    before = snapshot(db)
    resp = client_pg.post(BASE, json={
        "email": "old@example.com", "role": "VIEWER", "tenant_id": tenant, "recover_existing": True,
    })
    assert (resp.status_code, resp.json()["error"]["code"]) == (403, "AUTH_FORBIDDEN")
    assert adapter.calls == []
    assert snapshot(db) == before


def test_suspended_tenant_admin_recover_existing_keeps_409_precedence(client_pg, db, make_tenant, login_as, adapter):
    parked = make_tenant(TenantStatus.SUSPENDED)
    login_as(UserRole.ADMIN, tenant_id=parked)
    resp = client_pg.post(BASE, json={
        "email": "old@example.com", "role": "VIEWER", "tenant_id": parked, "recover_existing": True,
    })
    assert (resp.status_code, resp.json()["error"]["code"]) == (409, "TENANT_SUSPENDED")
    assert adapter.calls == []


def test_tenant_admin_ordinary_invite_is_still_allowed(client_pg, db, tenant_admin, tenant, adapter):
    resp = client_pg.post(BASE, json={"email": "new@example.com", "role": "VIEWER", "tenant_id": tenant})
    assert resp.status_code == 201
    assert adapter.calls == [("create", "new@example.com", False)]


def test_recover_existing_links_provider_user(client_pg, db, platform_admin, tenant, adapter):
    adapter.get_result = CognitoUser("old@example.com", "sub-old", "old@example.com", "CONFIRMED")
    resp = client_pg.post(BASE, json={
        "email": "old@example.com", "role": "VIEWER", "tenant_id": tenant, "recover_existing": True,
    })
    assert resp.status_code == 201
    assert adapter.calls == [("get", "old@example.com")]
    uid = resp.json()["user_id"]
    assert (user_row(db, uid).cognito_sub, user_row(db, uid).tenant_id) == ("sub-old", tenant)
    assert events(db, uid)[0][2] == {
        "role": "VIEWER", "status": "ACTIVE", "recover_existing": True, "tenant_id": tenant,
    }


def test_recover_existing_missing_provider_user_is_404(client_pg, db, platform_admin, tenant, adapter):
    before = snapshot(db)
    resp = client_pg.post(BASE, json={
        "email": "none@example.com", "role": "VIEWER", "tenant_id": tenant, "recover_existing": True,
    })
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "USER_NOT_FOUND"
    assert snapshot(db) == before


@pytest.mark.parametrize("existing", ["tenant_member", "platform", "unassigned"])
def test_recover_existing_already_provisioned_is_409_and_membership_never_moves(
    client_pg, db, platform_admin, tenant, adapter, target, make_auth_session, existing
):
    """이미 앱에 있는 신원은 어떤 계정이든 다시 연결하지 않는다 — 소속 · 계정 종류를 옮기는 길이 없다."""
    seed = {
        "tenant_member": target,
        "platform": make_auth_session(UserRole.ADMIN),
        "unassigned": make_auth_session(UserRole.ADMIN, account_type=AccountType.UNASSIGNED),
    }[existing]
    before_row = user_row(db, seed.user_id)
    adapter.get_result = CognitoUser("t@example.com", before_row.cognito_sub, "t@example.com", "CONFIRMED")
    count = db.execute(text("SELECT count(*) FROM app_users")).scalar_one()
    resp = client_pg.post(BASE, json={
        "email": "t@example.com", "role": "ADMIN", "tenant_id": tenant, "recover_existing": True,
    })
    assert (resp.status_code, resp.json()["error"]["code"]) == (409, "USER_STATE_CONFLICT")
    assert db.execute(text("SELECT count(*) FROM app_users")).scalar_one() == count
    assert user_row(db, seed.user_id) == before_row


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


@pytest.mark.parametrize("state", [TenantStatus.PENDING_CONNECTION, TenantStatus.SUSPENDED])
def test_platform_resend_into_non_active_tenant_is_409_before_sdk(
    client_pg, db, make_tenant, make_auth_session, adapter, state
):
    """PLATFORM 은 정지 테넌트의 사용자도 관리하지만, 그 테넌트로 초대 메일은 다시 보내지 않는다."""
    parked = make_auth_session(UserRole.VIEWER, tenant_id=make_tenant(state))
    adapter.get_result = _provider_for(db, parked)
    before = snapshot(db)
    resp = client_pg.post(f"{BASE}/{parked.user_id}/invite/resend")
    assert (resp.status_code, resp.json()["error"]["code"]) == (409, "TENANT_STATE_CONFLICT")
    assert adapter.calls == []
    assert snapshot(db) == before


# --- SDK 호출 경계 — 호출 순간 요청 세션에 열린 DB 트랜잭션이 없다 -------------------------------------
#
#   라우터에 주입되는 세션이 이 db 픽스처 하나라 서비스가 쓰는 세션 상태를 그대로 본다. 초대 · 복구 · 재발송이
#   SDK 호출 전에 읽기 트랜잭션을 끝내고, 그 뒤 다시 열지 않았다는 직접 관측이다(호출 순서와는 별개).


class BoundaryAdapter(FakeAdapter):
    """SDK 호출마다 요청 세션의 트랜잭션 상태를 기록한다 — (호출 이름, 열려 있었나)."""

    def __init__(self, session) -> None:
        super().__init__()
        self._session = session
        self.open_at_call: list[tuple[str, bool]] = []

    def admin_create_user(self, email, *, resend=False):
        self.open_at_call.append(("create", self._session.in_transaction()))
        return super().admin_create_user(email, resend=resend)

    def admin_get_user(self, username):
        self.open_at_call.append(("get", self._session.in_transaction()))
        return super().admin_get_user(username)


@pytest.fixture()
def boundary_adapter(client_pg, db):
    fake = BoundaryAdapter(db)
    use_adapter(client_pg, fake)
    return fake


def test_invite_calls_provider_with_no_open_transaction(client_pg, db, admin, tenant, boundary_adapter):
    assert not db.in_transaction()  # 시드 commit 뒤 — 이후의 열림은 요청이 연 것이다
    resp = client_pg.post(BASE, json={"email": "new@example.com", "role": "OPERATOR", "tenant_id": tenant})
    assert resp.status_code == 201
    assert boundary_adapter.open_at_call == [("create", False)]


def test_recover_existing_calls_provider_with_no_open_transaction(client_pg, db, platform_admin, tenant, boundary_adapter):
    boundary_adapter.get_result = CognitoUser("old@example.com", "sub-old", "old@example.com", "CONFIRMED")
    resp = client_pg.post(BASE, json={
        "email": "old@example.com", "role": "VIEWER", "tenant_id": tenant, "recover_existing": True,
    })
    assert resp.status_code == 201
    assert boundary_adapter.open_at_call == [("get", False)]


def test_resend_invite_calls_provider_with_no_open_transaction(client_pg, db, admin, target, boundary_adapter):
    boundary_adapter.get_result = _provider_for(db, target)
    resp = client_pg.post(f"{BASE}/{target.user_id}/invite/resend")
    assert resp.status_code == 204
    assert boundary_adapter.open_at_call == [("get", False), ("create", False)]


# --- SDK 뒤 재확인 — 제공자 결과는 이미 있었고 DB 는 쓰지 않는다(성공으로 보고하지 않는다) ---------------
#
#   대상 테넌트가 SDK 호출 사이에 ACTIVE 가 아니게 되면 PLATFORM 행위자는 409 TENANT_STATE_CONFLICT,
#   고객사 ADMIN 행위자는 자기 테넌트가 정지된 것이라 행위자 재판정에서 409 TENANT_SUSPENDED 다.
#   초대 메일 · 재발송 메일은 이미 나갔다(Cognito 결과는 되돌리지 않는다).


class SuspendingAdapter(FakeAdapter):
    """Cognito 호출이 성공한 직후 같은 테스트 DB 에서 대상 테넌트를 정지한다."""

    def __init__(self, db, tenant_id) -> None:
        super().__init__()
        self._db = db
        self._tenant_id = tenant_id

    def admin_create_user(self, email, *, resend=False):
        result = super().admin_create_user(email, resend=resend)
        suspend(self._db, self._tenant_id)
        return result


@pytest.fixture()
def suspending_adapter(client_pg, db, tenant):
    fake = SuspendingAdapter(db, tenant)
    use_adapter(client_pg, fake)
    return fake


def _post_sdk_code(admin) -> tuple[int, str]:
    if admin.account_type is AccountType.PLATFORM:
        return 409, "TENANT_STATE_CONFLICT"
    return 409, "TENANT_SUSPENDED"


def test_invite_rechecks_tenant_after_sdk_and_writes_nothing(client_pg, db, admin, tenant, suspending_adapter):
    count = db.execute(text("SELECT count(*) FROM app_users")).scalar_one()
    resp = client_pg.post(BASE, json={"email": "late@example.com", "role": "VIEWER", "tenant_id": tenant})
    assert (resp.status_code, resp.json()["error"]["code"]) == _post_sdk_code(admin)
    assert suspending_adapter.calls == [("create", "late@example.com", False)]  # 제공자 결과는 남는다
    assert db.execute(text("SELECT count(*) FROM app_users")).scalar_one() == count
    assert db.execute(text("SELECT count(*) FROM user_admin_events")).scalar_one() == 0


def test_resend_rechecks_tenant_after_sdk_and_writes_nothing(client_pg, db, admin, target, suspending_adapter):
    suspending_adapter.get_result = _provider_for(db, target)
    email = user_row(db, target.user_id).email
    resp = client_pg.post(f"{BASE}/{target.user_id}/invite/resend")
    assert (resp.status_code, resp.json()["error"]["code"]) == _post_sdk_code(admin)
    assert suspending_adapter.calls == [("get", email), ("create", email, True)]  # 재발송 메일은 나갔다
    assert events(db, target.user_id) == []


# --- 역할 ----------------------------------------------------------------------


def test_change_role_updates_and_audits(client_pg, db, admin, target):
    assert client_pg.put(f"{BASE}/{target.user_id}/role", json={"role": "OPERATOR"}).status_code == 204
    assert user_row(db, target.user_id).role == "OPERATOR"
    assert events(db, target.user_id) == [
        ("ROLE_CHANGED", {"role": "VIEWER"}, {"role": "OPERATOR"}, admin.user_id)
    ]


def test_change_role_same_value_self_and_missing(client_pg, db, tenant_admin, target):
    same = client_pg.put(f"{BASE}/{target.user_id}/role", json={"role": "VIEWER"})
    assert (same.status_code, same.json()["error"]["code"]) == (409, "USER_STATE_CONFLICT")
    selfie = client_pg.put(f"{BASE}/{tenant_admin.user_id}/role", json={"role": "VIEWER"})
    assert (selfie.status_code, selfie.json()["error"]["code"]) == (403, "AUTH_FORBIDDEN")
    assert client_pg.put(f"{BASE}/nope/role", json={"role": "VIEWER"}).status_code == 404
    assert user_row(db, tenant_admin.user_id).role == "ADMIN"
    assert events(db, target.user_id) == [] and events(db, tenant_admin.user_id) == []


def test_tenant_admin_may_demote_another_admin_of_its_tenant(client_pg, db, tenant_admin, tenant, make_auth_session):
    other = make_auth_session(UserRole.ADMIN, tenant_id=tenant)
    assert client_pg.put(f"{BASE}/{other.user_id}/role", json={"role": "VIEWER"}).status_code == 204
    assert user_row(db, other.user_id).role == "VIEWER"


# --- 마지막 활성 ADMIN — 테넌트별 -------------------------------------------------------


def test_last_active_admin_is_counted_per_tenant(client_pg, db, tenant, make_tenant, make_auth_session):
    """PLATFORM 행위자는 어느 테넌트의 ADMIN 수에도 들지 않는다 — 그 테넌트의 유일한 ADMIN 은 내리지 못한다.

    다른 테넌트 · PLATFORM 의 활성 ADMIN 이 몇이든 이 테넌트의 마지막 한 명을 지킨다(전역 집계면 통과해 버린다).
    """
    only = make_auth_session(UserRole.ADMIN, tenant_id=tenant)
    elsewhere = make_tenant()
    make_auth_session(UserRole.ADMIN, tenant_id=elsewhere)
    make_auth_session(UserRole.ADMIN, tenant_id=elsewhere)
    make_auth_session(UserRole.ADMIN)  # 다른 PLATFORM ADMIN
    for path, body in (("role", {"role": "VIEWER"}), ("status", {"status": "DISABLED"})):
        resp = client_pg.put(f"{BASE}/{only.user_id}/{path}", json=body)
        assert (resp.status_code, resp.json()["error"]["code"]) == (409, "USER_STATE_CONFLICT"), path
    assert user_row(db, only.user_id)[1:3] == ("ADMIN", "ACTIVE")
    assert events(db, only.user_id) == []
    assert me_status(client_pg, only) == 200

    second = make_auth_session(UserRole.ADMIN, tenant_id=tenant)
    assert client_pg.put(f"{BASE}/{only.user_id}/role", json={"role": "VIEWER"}).status_code == 204
    resp = client_pg.put(f"{BASE}/{second.user_id}/status", json={"status": "DISABLED"})
    assert (resp.status_code, resp.json()["error"]["code"]) == (409, "USER_STATE_CONFLICT")
    # 이미 비활성 · 비ADMIN 대상은 남은 수를 줄이지 않는다
    assert client_pg.put(f"{BASE}/{only.user_id}/status", json={"status": "DISABLED"}).status_code == 204


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


def test_status_same_value_self_missing(client_pg, db, tenant_admin, target):
    same = client_pg.put(f"{BASE}/{target.user_id}/status", json={"status": "ACTIVE"})
    assert (same.status_code, same.json()["error"]["code"]) == (409, "USER_STATE_CONFLICT")
    assert client_pg.put(f"{BASE}/{tenant_admin.user_id}/status", json={"status": "DISABLED"}).status_code == 403
    assert client_pg.put(f"{BASE}/nope/status", json={"status": "DISABLED"}).status_code == 404
    assert me_status(client_pg, tenant_admin) == 200


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


def test_revoke_own_sessions_is_allowed_then_401(client_pg, db, tenant_admin):
    assert client_pg.post(f"{BASE}/{tenant_admin.user_id}/sessions/revoke").status_code == 204
    assert events(db, tenant_admin.user_id)[0][:3] == ("SESSIONS_REVOKED", {}, {"revoked_session_count": 1})
    assert client_pg.get(BASE).status_code == 401


# --- 관리 범위 전수: 범위 밖 · PLATFORM · UNASSIGNED 대상은 없는 대상과 같다 ---------------------

TARGET_OPS = [
    ("GET", "/{id}/events", None),
    ("POST", "/{id}/invite/resend", None),
    ("PUT", "/{id}/role", {"role": "VIEWER"}),  # PLATFORM 대상에 닿으면 DB CHECK(ADMIN 고정)가 500 으로 터진다
    ("PUT", "/{id}/status", {"status": "DISABLED"}),
    ("POST", "/{id}/sessions/revoke", None),
]


def test_target_ops_cover_every_user_scoped_route(client):
    registered = {
        (method, route.path)
        for route in iter_route_contexts(client.app.routes)
        if route.path.startswith(BASE + "/{user_id}")
        for method in route.methods or ()
    }
    assert {(m, BASE + p.replace("{id}", "{user_id}")) for m, p, _ in TARGET_OPS} == registered


HIDDEN = [
    ("platform", "other_platform"),
    ("platform", "self"),
    ("platform", "unassigned"),
    ("platform", "missing"),
    ("tenant", "other_platform"),
    ("tenant", "unassigned"),
    ("tenant", "other_tenant"),
    ("tenant", "suspended_tenant"),
    ("tenant", "missing"),
]


@pytest.mark.parametrize(("actor_kind", "hidden"), HIDDEN)
@pytest.mark.parametrize("method,path,body", TARGET_OPS)
def test_targets_outside_scope_are_404_like_missing_without_provider_or_db_change(
    client_pg, db, login_as, make_auth_session, make_tenant, tenant, adapter, actor_kind, hidden, method, path, body
):
    actor = login_as(UserRole.ADMIN) if actor_kind == "platform" else login_as(UserRole.ADMIN, tenant_id=tenant)
    target_id = {
        "other_platform": lambda: make_auth_session(UserRole.ADMIN).user_id,
        "self": lambda: actor.user_id,
        "unassigned": lambda: make_auth_session(UserRole.ADMIN, account_type=AccountType.UNASSIGNED).user_id,
        "other_tenant": lambda: make_auth_session(UserRole.ADMIN, tenant_id=make_tenant()).user_id,
        "suspended_tenant": lambda: make_auth_session(
            UserRole.ADMIN, tenant_id=make_tenant(TenantStatus.SUSPENDED)
        ).user_id,
        "missing": lambda: MISSING_UUID,
    }[hidden]()
    # 범위 판정이 빠지면 재발송까지 진행될 응답을 준비해 둔다 — 그래도 SDK 를 부르지 않아야 한다
    adapter.get_result = CognitoUser("x@example.com", "unused", "x@example.com", FORCE)
    missing = client_pg.request(method, BASE + path.replace("{id}", MISSING_UUID), json=body)
    before = snapshot(db)

    resp = client_pg.request(method, BASE + path.replace("{id}", target_id), json=body)

    assert (resp.status_code, resp.json()["error"]["code"]) == (404, "USER_NOT_FOUND")
    assert resp.json()["error"]["message"] == missing.json()["error"]["message"]
    assert adapter.calls == []
    assert snapshot(db) == before


@pytest.mark.parametrize("method,path,body", TARGET_OPS)
def test_platform_manages_tenant_users_of_any_tenant(
    client_pg, db, make_auth_session, make_tenant, adapter, method, path, body
):
    """PLATFORM 의 관리 권한은 테넌트를 가리지 않는다(정지 테넌트 사용자의 정리 포함 — 재발송만 ACTIVE 필요)."""
    member = make_auth_session(UserRole.OPERATOR, tenant_id=make_tenant())
    adapter.get_result = _provider_for(db, member)
    resp = client_pg.request(method, BASE + path.replace("{id}", member.user_id), json=body)
    assert resp.status_code == (200 if method == "GET" else 204)


@pytest.mark.parametrize(("method", "path", "body"), [op for op in TARGET_OPS if op[1] != "/{id}/invite/resend"])
def test_platform_may_clean_up_users_of_a_suspended_tenant(client_pg, db, make_auth_session, make_tenant, method, path, body):
    member = make_auth_session(UserRole.OPERATOR, tenant_id=make_tenant(TenantStatus.SUSPENDED))
    resp = client_pg.request(method, BASE + path.replace("{id}", member.user_id), json=body)
    assert resp.status_code == (200 if method == "GET" else 204)


# --- 정지 테넌트의 고객사 ADMIN — 조회 · 로그인은 되고 변경은 409 TENANT_SUSPENDED ----------------


def test_suspended_tenant_admin_reads_safe_history_but_cannot_change_anything(
    client_pg, db, login_as, make_auth_session, make_tenant, adapter
):
    parked = make_tenant(TenantStatus.SUSPENDED)
    member = make_auth_session(UserRole.VIEWER, tenant_id=parked)
    # 정지 뒤에도 PLATFORM 은 정리할 수 있다 — 그 이력을 고객사 ADMIN 이 읽는다
    platform = login_as(UserRole.ADMIN)
    assert client_pg.put(f"{BASE}/{member.user_id}/role", json={"role": "OPERATOR"}).status_code == 204

    actor = login_as(UserRole.ADMIN, tenant_id=parked)
    me = client_pg.get("/api/v1/auth/me")
    assert (me.status_code, me.json()["account_type"], me.json()["tenant_id"]) == (200, "TENANT", parked)
    listed = {i["user_id"] for i in client_pg.get(BASE).json()["items"]}
    assert listed == {member.user_id, actor.user_id}
    history = client_pg.get(f"{BASE}/{member.user_id}/events")
    assert history.status_code == 200
    [event] = history.json()["items"]
    shown = {k: v for k, v in event["after_state"].items() if v is not None}
    assert (event["action"], event["actor_user_id"], shown) == (
        "ROLE_CHANGED", platform.user_id, {"role": "OPERATOR"},
    )
    # 이력 상태는 닫힌 목록뿐이다(이메일 · 토큰 · Cognito 원본 없음)
    assert set(event["before_state"]) | set(event["after_state"]) <= {
        "role", "platform_role", "status", "revoked_session_count", "recover_existing", "tenant_id", "tenant_ids",
    }
    # 고객 사용자 이벤트에는 배정 ID 가 없다(직렬화된 null 만 허용)
    assert event["before_state"].get("tenant_ids") is None and event["after_state"].get("tenant_ids") is None

    adapter.get_result = _provider_for(db, member)
    before = snapshot(db)
    for method, path, body in [
        ("POST", "", {"email": "x@example.com", "role": "VIEWER"}),
        ("POST", "", {"email": "x@example.com", "role": "VIEWER", "tenant_id": parked}),
        *TARGET_OPS[1:],
    ]:
        resp = client_pg.request(method, BASE + path.replace("{id}", member.user_id), json=body)
        assert (resp.status_code, resp.json()["error"]["code"]) == (409, "TENANT_SUSPENDED"), (method, path)
    assert adapter.calls == []
    assert snapshot(db) == before
    assert client_pg.post("/api/v1/auth/activity").status_code == 204


# --- 설정 누락 우선순위 --------------------------------------------------------------


def _without_cognito(client) -> None:
    base = get_auth_settings()
    client.app.dependency_overrides[get_auth_settings] = lambda: base.model_copy(
        update={"COGNITO_USER_POOL_ID": None}
    )


# Cognito 어댑터는 지연 조립이라, 사전 판정(행위자 · 대상 · 테넌트)의 거절이 설정 누락 503 보다 먼저다.
# 가짜 어댑터를 끼우지 않는다 — 판정을 통과한 요청만 실제 조립 경로에서 503 을 받는다.
PREFLIGHT_WITHOUT_COGNITO = [
    ("platform", "invite_without_tenant", (422, "REQUEST_VALIDATION_FAILED")),
    ("platform", "invite_unknown_tenant", (404, "TENANT_NOT_FOUND")),
    ("platform", "invite_suspended_tenant", (409, "TENANT_STATE_CONFLICT")),
    ("platform", "resend_platform_target", (404, "USER_NOT_FOUND")),
    ("platform", "resend_suspended_tenant_member", (409, "TENANT_STATE_CONFLICT")),
    ("tenant", "invite_foreign_tenant", (404, "TENANT_NOT_FOUND")),
    ("tenant", "resend_other_tenant_member", (404, "USER_NOT_FOUND")),
    ("suspended_tenant", "invite_own_tenant", (409, "TENANT_SUSPENDED")),
    ("suspended_tenant", "resend_own_member", (409, "TENANT_SUSPENDED")),
    ("platform", "invite_valid", (503, "AUTH_PROVIDER_UNAVAILABLE")),
    ("tenant", "resend_valid", (503, "AUTH_PROVIDER_UNAVAILABLE")),
]


@pytest.mark.parametrize(("actor_kind", "case", "expected"), PREFLIGHT_WITHOUT_COGNITO)
def test_missing_cognito_config_never_masks_preflight_rejections(
    client_pg, db, login_as, make_auth_session, make_tenant, tenant, actor_kind, case, expected
):
    _without_cognito(client_pg)
    parked = make_tenant(TenantStatus.SUSPENDED)
    own = parked if actor_kind == "suspended_tenant" else tenant
    if actor_kind == "platform":
        login_as(UserRole.ADMIN)
    else:
        login_as(UserRole.ADMIN, tenant_id=own)
    member = {
        "resend_platform_target": lambda: make_auth_session(UserRole.ADMIN),
        "resend_suspended_tenant_member": lambda: make_auth_session(UserRole.VIEWER, tenant_id=parked),
        "resend_other_tenant_member": lambda: make_auth_session(UserRole.VIEWER, tenant_id=make_tenant()),
        "resend_own_member": lambda: make_auth_session(UserRole.VIEWER, tenant_id=own),
        "resend_valid": lambda: make_auth_session(UserRole.VIEWER, tenant_id=own),
    }.get(case, lambda: None)()
    invite = {
        "invite_without_tenant": {},
        "invite_unknown_tenant": {"tenant_id": str(uuid.uuid4())},
        "invite_suspended_tenant": {"tenant_id": parked},
        "invite_foreign_tenant": {"tenant_id": make_tenant()},
        "invite_own_tenant": {},
        "invite_valid": {"tenant_id": tenant},
    }.get(case)
    before = snapshot(db)

    if invite is not None:
        resp = client_pg.post(BASE, json={"email": "a@example.com", "role": "VIEWER", **invite})
    else:
        resp = client_pg.post(f"{BASE}/{member.user_id}/invite/resend")

    assert (resp.status_code, resp.json()["error"]["code"]) == expected
    assert snapshot(db) == before


def test_missing_cognito_config_403_before_503_and_ordinary_routes_work(client_pg, db, login_as, tenant, target):
    _without_cognito(client_pg)
    body = {"email": "a@example.com", "role": "VIEWER", "tenant_id": tenant}
    login_as(UserRole.OPERATOR, tenant_id=tenant)
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
                # 같은 서버의 다른 테스트 프로세스(다른 DB)의 잠금 대기를 이 쓰기로 오인하지 않는다
                waiting = probe.execute(
                    text("SELECT count(*) FROM pg_stat_activity "
                         "WHERE datname = current_database() AND wait_event_type = 'Lock' "
                         "AND query LIKE 'LOCK TABLE app_users%'")
                ).scalar_one()
                probe.rollback()
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
        ("demote", ErrorCode.AUTH_FORBIDDEN),
        ("suspend_tenant", ErrorCode.TENANT_SUSPENDED),
    ],
)
def test_stale_actor_is_rechecked_under_lock_and_nothing_is_written(
    pg_engine, committed_auth, change, expected
):
    tenant_id = committed_auth.make_tenant()
    actor_seed = committed_auth.make(UserRole.ADMIN, tenant_id=tenant_id)
    victim = committed_auth.make(UserRole.VIEWER, tenant_id=tenant_id)

    def holder_change(db):
        now = datetime.now(UTC)
        if change == "revoke":
            sessions.revoke_user_sessions(
                db, user_id=actor_seed.user_id, now=now, reason=models.AuthSessionRevokeReason.ADMIN_REVOKED
            )
        elif change == "disable":
            auth_repo.set_status(db, user_id=actor_seed.user_id, status=models.AppUserStatus.DISABLED, now=now)
        elif change == "demote":
            auth_repo.set_role(db, user_id=actor_seed.user_id, role=UserRole.VIEWER, now=now)
        else:
            assert tenants_repo.suspend_tenant(db, tenant_id, reason="잠금 대기 중 정지", now=now)

    error = _run_blocked(
        pg_engine,
        committed_auth,
        lambda db: admin_service.change_role(db, _actor(actor_seed), victim.user_id, UserRole.OPERATOR),
        holder_change,
    )
    if isinstance(expected, ErrorCode):
        assert isinstance(error, ApiError) and error.code is expected
    else:
        assert isinstance(error, expected)
    assert _admin_state(committed_auth, victim)[0] == "VIEWER"
    with committed_auth.sessions() as db:
        assert db.execute(
            text("SELECT count(*) FROM user_admin_events WHERE target_user_id = :u"), {"u": victim.user_id}
        ).scalar_one() == 0


def test_invite_actor_revoked_during_sdk_writes_nothing_but_provider_effect_remains(committed_auth):
    tenant_id = committed_auth.make_tenant()
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
    with committed_auth.sessions() as db, pytest.raises(AuthRequiredError):
        admin_service.invite_user(
            db, _actor(actor_seed), lambda: fake, email="late@example.com", role=UserRole.VIEWER,
            tenant_id=uuid.UUID(tenant_id), recover_existing=False,
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
    from pathlib import Path

    from alembic import command
    from alembic.config import Config
    from config import get_settings
    from sqlalchemy import create_engine

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


def _seed_user(db, n, *, account_type, tenant_id=None, role=UserRole.ADMIN, platform_role=None):
    """PLATFORM 계정의 platform_role 은 명시한다(기본 CENTRAL_ADMIN — 전역 범위의 고객 사용자 관리 행위자)."""
    now = datetime.now(UTC)
    user = auth_repo.create_user(
        db, issuer=ISSUER, cognito_sub=f"race-{n}", email=f"race{n}@example.com",
        role=role, account_type=account_type, tenant_id=tenant_id, now=now, email_verified_at=now,
    )
    if account_type is AccountType.PLATFORM:
        assert auth_repo.set_platform_role(
            db, user_id=user.user_id, platform_role=platform_role or PlatformRole.CENTRAL_ADMIN, now=now
        )
    issued = sessions.issue_session(
        db, user_id=user.user_id, mfa_step=models.AuthSessionMfaStep.SOFTWARE_TOKEN_MFA,
        now=now, max_seconds=3600, idle_seconds=1800,
    )
    return user.user_id, issued.token


def _race(factory, attempts):
    """attempts[i] = (token, fn(db, actor)) 을 장벽으로 동시에 출발시켜 예외(성공이면 None) 목록을 돌려준다."""
    barrier = threading.Barrier(len(attempts))
    results: list = [None] * len(attempts)

    def run(index: int) -> None:
        token, fn = attempts[index]
        actor = admin_service.Actor(token=token, idle_seconds=1800, clock=lambda: datetime.now(UTC))
        with factory() as db:
            barrier.wait(10)
            try:
                fn(db, actor)
                results[index] = None
            except BaseException as exc:  # noqa: BLE001 — 결과 비교용
                results[index] = exc

    threads = [threading.Thread(target=run, args=(i,)) for i in range(len(attempts))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(20)
        assert not t.is_alive()
    return results


def _removal(kind, victim_id):
    if kind == "role":
        return lambda db, actor: admin_service.change_role(db, actor, victim_id, UserRole.VIEWER)
    return lambda db, actor: admin_service.change_status(db, actor, victim_id, UserStatus.DISABLED)


PAIRS = [("role", "role"), ("status", "status"), ("role", "status"), ("status", "role")]


@pytest.mark.parametrize("first,second", PAIRS)
def test_two_tenant_admins_cannot_remove_each_other_concurrently(throwaway_engine, seed_tenant, first, second):
    from sqlalchemy.orm import sessionmaker

    factory = sessionmaker(bind=throwaway_engine, autoflush=False, expire_on_commit=False)
    with factory() as db:
        tenant_id = seed_tenant(db)
        seeds = [_seed_user(db, n, account_type=AccountType.TENANT, tenant_id=tenant_id) for n in range(2)]
        db.commit()
    with factory() as db:
        assert auth_repo.count_active_admins(db, tenant_id=tenant_id) == 2

    results = _race(factory, [
        (seeds[0][1], _removal(first, seeds[1][0])),
        (seeds[1][1], _removal(second, seeds[0][0])),
    ])

    winners = [r for r in results if r is None]
    losers = [r for r in results if r is not None]
    assert len(winners) == 1 and len(losers) == 1, results
    loser = losers[0]
    assert isinstance(loser, AuthRequiredError) or (
        isinstance(loser, ApiError) and loser.code is ErrorCode.AUTH_FORBIDDEN
    ), loser
    with factory() as db:
        assert auth_repo.count_active_admins(db, tenant_id=tenant_id) == 1
        assert db.execute(text("SELECT count(*) FROM user_admin_events")).scalar_one() == 1


@pytest.mark.parametrize("first,second", PAIRS)
def test_two_platform_actors_cannot_remove_a_tenants_last_two_admins_concurrently(
    throwaway_engine, seed_tenant, first, second
):
    """PLATFORM 행위자는 집계에 들지 않으므로 행위자 재판정이 아니라 테넌트별 집계(잠금 안)만이 마지막 한 명을 지킨다."""
    from sqlalchemy.orm import sessionmaker

    factory = sessionmaker(bind=throwaway_engine, autoflush=False, expire_on_commit=False)
    with factory() as db:
        tenant_id = seed_tenant(db)
        victims = [_seed_user(db, n, account_type=AccountType.TENANT, tenant_id=tenant_id)[0] for n in range(2)]
        actors = [_seed_user(db, 10 + n, account_type=AccountType.PLATFORM)[1] for n in range(2)]
        db.commit()

    results = _race(factory, [
        (actors[0], _removal(first, victims[0])),
        (actors[1], _removal(second, victims[1])),
    ])

    losers = [r for r in results if r is not None]
    assert results.count(None) == 1 and len(losers) == 1, results
    assert isinstance(losers[0], ApiError) and losers[0].code is ErrorCode.USER_STATE_CONFLICT, losers[0]
    with factory() as db:
        assert auth_repo.count_active_admins(db, tenant_id=tenant_id) == 1
        assert db.execute(text("SELECT count(*) FROM user_admin_events")).scalar_one() == 1


def _staff_removal(kind, victim_id):
    from auth import staff_service

    if kind == "role":
        return lambda db, actor: staff_service.change_platform_role(
            db, actor, victim_id, PlatformRole.TENANT_MANAGER
        )
    return lambda db, actor: staff_service.change_status(db, actor, victim_id, UserStatus.DISABLED)


@pytest.mark.parametrize("first,second", PAIRS)
def test_two_central_admins_cannot_remove_each_other_concurrently(throwaway_engine, monkeypatch, first, second):
    """결정적 경합 — A 가 users 잠금 안에서 commit 직전에 멈춰 있는 동안 B 가 A 를 내리려다 그 잠금에서 대기한다
    (B 의 backend pid 가 Lock 대기인 것을 확인한 뒤 A 를 commit). 대기하던 B 는 재판정에서 권한을 잃고 쓰지 못한다."""
    from sqlalchemy.orm import sessionmaker

    central = PlatformRole.CENTRAL_ADMIN
    factory = sessionmaker(bind=throwaway_engine, autoflush=False, expire_on_commit=False)
    with factory() as db:
        (a_id, a_token), (b_id, b_token) = [
            _seed_user(db, n, account_type=AccountType.PLATFORM, platform_role=central) for n in range(2)
        ]
        db.commit()
    with factory() as db:
        assert auth_repo.count_active_central_admins(db) == 2

    def actor(token):
        return admin_service.Actor(token=token, idle_seconds=1800, clock=lambda: datetime.now(UTC))

    outcome: dict = {}

    def run_b():
        with factory() as db:
            # 같은 트랜잭션·연결에서 서비스가 users 잠금을 잡는다 — 이 pid 만이 대기 판정 대상이다
            outcome["pid"] = db.execute(text("SELECT pg_backend_pid()")).scalar_one()
            try:
                _staff_removal(second, a_id)(db, actor(b_token))
                outcome["b"] = None
            except BaseException as exc:  # noqa: BLE001
                outcome["b"] = exc

    thread = threading.Thread(target=run_b)
    real_add_event = auth_repo.add_user_event
    state = {"started": False}

    def add_event_with_waiting_peer(db, **kwargs):
        if not state["started"]:  # A 의 이력 쓰기 — 잠금을 쥔 채 commit 전
            state["started"] = True
            thread.start()
            deadline = time.monotonic() + 10
            while True:
                pid = outcome.get("pid")
                if pid is not None:
                    with factory() as probe:
                        waiting = probe.execute(
                            text("SELECT count(*) FROM pg_stat_activity "
                                 "WHERE pid = :pid AND wait_event_type = 'Lock'"),
                            {"pid": pid},
                        ).scalar_one()
                    if waiting == 1:
                        break
                assert time.monotonic() < deadline, "B 가 잠금에서 대기하지 않았다"
                time.sleep(0.05)
        return real_add_event(db, **kwargs)

    monkeypatch.setattr(auth_repo, "add_user_event", add_event_with_waiting_peer)
    with factory() as db:
        _staff_removal(first, b_id)(db, actor(a_token))  # A 성공
    thread.join(20)
    assert not thread.is_alive()

    loser = outcome["b"]
    assert isinstance(loser, AuthRequiredError) or (
        isinstance(loser, ApiError) and loser.code is ErrorCode.AUTH_FORBIDDEN
    ), loser
    with factory() as db:
        assert auth_repo.count_active_central_admins(db) == 1
        assert db.execute(text("SELECT count(*) FROM user_admin_events")).scalar_one() == 1
        row = db.get(models.AppUser, a_id)
        assert row.platform_role is central and row.status is models.AppUserStatus.ACTIVE


@pytest.mark.parametrize("operation", ["invite", "resend"])
def test_tenant_recheck_holds_the_tenant_row_until_commit(throwaway_engine, seed_tenant, monkeypatch, operation):
    """잠금 안의 재확인은 테넌트 행을 commit 까지 잡는다 — 그 사이 다른 트랜잭션의 정지(UPDATE)는 기다려야 한다.

    정지가 끼어들 수 있으면 「쓰기 직전 ACTIVE 확인」이 commit 시점의 상태를 보장하지 못한다.
    """
    from sqlalchemy.orm import sessionmaker

    factory = sessionmaker(bind=throwaway_engine, autoflush=False, expire_on_commit=False)
    with factory() as db:
        tenant_id = seed_tenant(db)
        _, platform_token = _seed_user(db, 0, account_type=AccountType.PLATFORM)
        member_id, _ = _seed_user(db, 1, account_type=AccountType.TENANT, tenant_id=tenant_id, role=UserRole.VIEWER)
        member_sub = db.get(models.AppUser, member_id).cognito_sub
        member_email = db.get(models.AppUser, member_id).email
        db.commit()

    probe: dict = {}
    real_add_event = auth_repo.add_user_event

    def add_event_while_probing(db, **kwargs):
        # 이 시점의 쓰기 트랜잭션은 재확인을 마쳤고 아직 commit 전이다
        with factory() as other:
            other.execute(text("SET LOCAL lock_timeout = '300ms'"))
            try:
                tenants_repo.suspend_tenant(other, tenant_id, reason="끼어든 정지", now=datetime.now(UTC))
                probe["blocked"] = False
            except OperationalError:
                probe["blocked"] = True
            other.rollback()
        return real_add_event(db, **kwargs)

    monkeypatch.setattr(auth_repo, "add_user_event", add_event_while_probing)
    fake = FakeAdapter()
    fake.get_result = CognitoUser(member_email, member_sub, member_email, FORCE)
    actor = admin_service.Actor(token=platform_token, idle_seconds=1800, clock=lambda: datetime.now(UTC))
    with factory() as db:
        if operation == "invite":
            admin_service.invite_user(
                db, actor, lambda: fake, email="new@example.com", role=UserRole.VIEWER,
                tenant_id=uuid.UUID(tenant_id), recover_existing=False,
            )
        else:
            admin_service.resend_invite(db, actor, lambda: fake, member_id)
    assert probe == {"blocked": True}
    with factory() as db:
        assert db.get(models.Tenant, tenant_id).status is TenantStatus.ACTIVE


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


def test_noncanonical_self_is_still_403(client_pg, db, tenant_admin):
    for form in _forms(tenant_admin.user_id):
        assert client_pg.put(f"{BASE}/{form}/role", json={"role": "VIEWER"}).status_code == 403
        assert client_pg.put(f"{BASE}/{form}/status", json={"status": "DISABLED"}).status_code == 403
    assert user_row(db, tenant_admin.user_id)[1:3] == ("ADMIN", "ACTIVE")


def test_noncanonical_last_admin_is_still_409(client_pg, db, make_auth_session, tenant):
    only = make_auth_session(UserRole.ADMIN, tenant_id=tenant)  # PLATFORM 행위자(client_pg 기본)가 내리려 한다
    urn = f"urn:uuid:{only.user_id}"
    assert client_pg.put(f"{BASE}/{urn}/role", json={"role": "VIEWER"}).status_code == 409
    assert client_pg.put(f"{BASE}/{urn}/status", json={"status": "DISABLED"}).status_code == 409
    assert events(db, only.user_id) == []


@pytest.mark.parametrize("form", range(4))
def test_noncanonical_hidden_target_is_still_404(client_pg, db, make_auth_session, adapter, form):
    hidden = make_auth_session(UserRole.ADMIN)  # 다른 PLATFORM 계정
    ref = _forms(hidden.user_id)[form]
    assert client_pg.put(f"{BASE}/{ref}/role", json={"role": "VIEWER"}).status_code == 404
    assert client_pg.post(f"{BASE}/{ref}/invite/resend").status_code == 404
    assert adapter.calls == []


# --- 배정 고객사 범위(#492): CENTRAL_ADMIN 전역, TENANT_MANAGER 는 배정된 고객사만 -----------------

ID_ROUTES = [
    ("GET", "/{id}/events", None),
    ("POST", "/{id}/invite/resend", None),
    ("PUT", "/{id}/role", {"role": "OPERATOR"}),
    ("PUT", "/{id}/status", {"status": "DISABLED"}),
    ("POST", "/{id}/sessions/revoke", None),
]


@pytest.mark.parametrize("assigned_elsewhere", [False, True])
def test_unassigned_manager_has_no_customer_data(
    client_pg, db, manager, make_tenant, target, tenant, adapter, assigned_elsewhere
):
    """배정이 없거나 다른 고객사만 배정된 매니저 — 목록은 비고(전체가 아니다), 대상 · 초대는 Cognito 전에 거절된다."""
    if assigned_elsewhere:
        assign(db, manager, make_tenant())
    before = snapshot(db)
    listed = client_pg.get(BASE)
    assert (listed.status_code, listed.json()["items"]) == (200, [])
    for method, path, body in ID_ROUTES:
        resp = client_pg.request(method, BASE + path.replace("{id}", target.user_id), json=body)
        assert (resp.status_code, resp.json()["error"]["code"]) == (404, "USER_NOT_FOUND"), (method, path)
    for recover in (False, True):
        resp = client_pg.post(BASE, json={
            "email": "x@example.com", "role": "VIEWER", "tenant_id": tenant, "recover_existing": recover,
        })
        assert (resp.status_code, resp.json()["error"]["code"]) == (404, "TENANT_NOT_FOUND")
    omitted = client_pg.post(BASE, json={"email": "x@example.com", "role": "VIEWER"})
    assert (omitted.status_code, omitted.json()["error"]["code"]) == (422, "REQUEST_VALIDATION_FAILED")
    assert adapter.calls == []
    assert snapshot(db) == before


def test_manager_is_limited_to_assigned_customers(
    client_pg, db, manager, make_tenant, make_auth_session, tenant, target, adapter
):
    other_tenant = make_tenant()
    other = make_auth_session(UserRole.VIEWER, tenant_id=other_tenant)
    assign(db, manager, tenant)

    seen = {i["user_id"]: i["tenant_id"] for i in client_pg.get(BASE).json()["items"]}
    assert seen == {target.user_id: tenant}

    # 배정 대상은 동작한다
    assert client_pg.get(f"{BASE}/{target.user_id}/events").status_code == 200
    assert client_pg.put(f"{BASE}/{target.user_id}/role", json={"role": "OPERATOR"}).status_code == 204
    assert client_pg.put(f"{BASE}/{target.user_id}/status", json={"status": "DISABLED"}).status_code == 204
    assert client_pg.post(f"{BASE}/{target.user_id}/sessions/revoke").status_code == 204
    invited = client_pg.post(BASE, json={"email": "new@example.com", "role": "VIEWER", "tenant_id": tenant})
    assert invited.status_code == 201

    # 미배정 고객사 · 없는 고객사는 거절, 대상은 없는 대상과 같다
    before = snapshot(db)
    adapter.calls.clear()
    for method, path, body in ID_ROUTES:
        resp = client_pg.request(method, BASE + path.replace("{id}", other.user_id), json=body)
        assert (resp.status_code, resp.json()["error"]["code"]) == (404, "USER_NOT_FOUND"), (method, path)
    for foreign in (other_tenant, str(uuid.uuid4())):
        for recover in (False, True):
            resp = client_pg.post(BASE, json={
                "email": "x@example.com", "role": "ADMIN", "tenant_id": foreign, "recover_existing": recover,
            })
            assert (resp.status_code, resp.json()["error"]["code"]) == (404, "TENANT_NOT_FOUND")
    assert adapter.calls == []
    assert snapshot(db) == before


def test_manager_scope_follows_assignment_replacement(client_pg, db, manager, make_tenant, make_auth_session, tenant, target):
    other_tenant = make_tenant()
    other = make_auth_session(UserRole.VIEWER, tenant_id=other_tenant)
    assign(db, manager, tenant, other_tenant)
    assert {i["user_id"] for i in client_pg.get(BASE).json()["items"]} == {target.user_id, other.user_id}
    assign(db, manager, other_tenant)
    assert {i["user_id"] for i in client_pg.get(BASE).json()["items"]} == {other.user_id}
    assert client_pg.get(f"{BASE}/{target.user_id}/events").status_code == 404


def test_central_admin_reaches_every_customer_without_assignment(client_pg, db, login_as, make_tenant, make_auth_session):
    login_as(UserRole.ADMIN, platform_role=PlatformRole.CENTRAL_ADMIN)
    first, second = (make_auth_session(UserRole.VIEWER, tenant_id=make_tenant()) for _ in range(2))
    assert {first.user_id, second.user_id} <= {i["user_id"] for i in client_pg.get(BASE).json()["items"]}
    assert client_pg.get(f"{BASE}/{second.user_id}/events").status_code == 200


def test_manager_may_recover_existing_in_assigned_tenant(client_pg, db, manager, tenant, adapter):
    assign(db, manager, tenant)
    adapter.get_result = CognitoUser("old@example.com", "sub-old", "old@example.com", "CONFIRMED")
    resp = client_pg.post(BASE, json={
        "email": "old@example.com", "role": "VIEWER", "tenant_id": tenant, "recover_existing": True,
    })
    assert resp.status_code == 201
    assert adapter.calls == [("get", "old@example.com")]


class RevokingAdapter(FakeAdapter):
    """제공자 호출이 끝난 직후 같은 테스트 DB 에서 매니저의 배정을 모두 회수한다."""

    def __init__(self, db, manager) -> None:
        super().__init__()
        self._db = db
        self._manager = manager

    def _revoke(self) -> None:
        assign(self._db, self._manager)

    def admin_create_user(self, email, *, resend=False):
        result = super().admin_create_user(email, resend=resend)
        self._revoke()
        return result

    def admin_get_user(self, username):
        result = super().admin_get_user(username)
        self._revoke()
        return result


@pytest.mark.parametrize("recover", [False, True])
def test_invite_rechecks_assignment_after_sdk_and_writes_nothing(
    client_pg, db, manager, tenant, recover
):
    assign(db, manager, tenant)
    fake = RevokingAdapter(db, manager)
    fake.get_result = CognitoUser("old@example.com", "sub-old", "old@example.com", "CONFIRMED")
    use_adapter(client_pg, fake)
    count = db.execute(text("SELECT count(*) FROM app_users")).scalar_one()
    resp = client_pg.post(BASE, json={
        "email": "old@example.com", "role": "VIEWER", "tenant_id": tenant, "recover_existing": recover,
    })
    assert (resp.status_code, resp.json()["error"]["code"]) == (404, "TENANT_NOT_FOUND")
    assert len(fake.calls) == 1  # 제공자 결과는 이미 있었다 — DB 만 쓰지 않는다
    assert db.execute(text("SELECT count(*) FROM app_users")).scalar_one() == count
    assert db.execute(text("SELECT count(*) FROM user_admin_events")).scalar_one() == 0


def test_resend_rechecks_assignment_after_sdk_and_writes_nothing(client_pg, db, manager, tenant, target):
    assign(db, manager, tenant)
    fake = RevokingAdapter(db, manager)
    fake.get_result = _provider_for(db, target)
    use_adapter(client_pg, fake)
    email = user_row(db, target.user_id).email
    resp = client_pg.post(f"{BASE}/{target.user_id}/invite/resend")
    assert (resp.status_code, resp.json()["error"]["code"]) == (404, "USER_NOT_FOUND")
    assert fake.calls == [("get", email), ("create", email, True)]  # 재발송 메일은 나갔다
    assert events(db, target.user_id) == []


def test_tenant_admin_recover_existing_stays_forbidden(client_pg, db, tenant_admin, tenant, adapter):
    resp = client_pg.post(BASE, json={
        "email": "old@example.com", "role": "VIEWER", "tenant_id": tenant, "recover_existing": True,
    })
    assert (resp.status_code, resp.json()["error"]["code"]) == (403, "AUTH_FORBIDDEN")
    assert adapter.calls == []


def test_central_admin_may_recover_existing(client_pg, login_as, tenant, adapter):
    login_as(UserRole.ADMIN, platform_role=PlatformRole.CENTRAL_ADMIN)
    adapter.get_result = CognitoUser("old@example.com", "sub-old", "old@example.com", "CONFIRMED")
    resp = client_pg.post(BASE, json={
        "email": "old@example.com", "role": "VIEWER", "tenant_id": tenant, "recover_existing": True,
    })
    assert resp.status_code == 201
