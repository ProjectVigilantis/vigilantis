"""플랫폼 직원 관리 API (#492 PR 5) — 중앙 관리자 전용, 대상은 PLATFORM 뿐, 자기 변경 · 마지막 중앙 관리자 보호."""

from __future__ import annotations

import pytest
from db import models
from schemas.api.auth import PlatformRole, UserRole
from sqlalchemy import select, text

BASE = "/api/v1/staff"
CENTRAL = PlatformRole.CENTRAL_ADMIN
MANAGER = PlatformRole.TENANT_MANAGER
MISSING = "00000000-0000-4000-8000-000000000000"


@pytest.fixture()
def tenant(make_tenant) -> str:
    return make_tenant()


@pytest.fixture(autouse=True)
def central_session(login_as):
    """기본 platform_session 은 TENANT_MANAGER 다 — 이 파일의 행위자는 명시적으로 CENTRAL_ADMIN 으로 로그인한다."""
    return login_as(UserRole.ADMIN, platform_role=CENTRAL)


def staff(make_auth_session, platform_role=MANAGER):
    return make_auth_session(UserRole.ADMIN, platform_role=platform_role)


def assigned(db, user_id):
    from db.repositories import auth as auth_repo

    return sorted(auth_repo.list_assigned_tenant_ids(db, user_id=user_id))


def assign(client_pg, user_id, ids):
    return client_pg.put(f"{BASE}/{user_id}/tenants", json={"tenant_ids": ids})


def events(db, target_id):
    rows = db.scalars(
        select(models.UserAdminEvent)
        .where(models.UserAdminEvent.target_user_id == target_id)
        .order_by(models.UserAdminEvent.occurred_at, models.UserAdminEvent.event_id)
    ).all()
    return [(r.action.value, r.before_state, r.after_state) for r in rows]


def db_state(db, user_id):
    return db.execute(
        text("SELECT platform_role::text, status::text FROM app_users WHERE user_id = :u"),
        {"u": user_id},
    ).one()


REQUESTS = [
    ("GET", "", None),
    ("GET", "/{id}/events", None),
    ("PUT", "/{id}/role", {"platform_role": "CENTRAL_ADMIN"}),
    ("PUT", "/{id}/role", {"platform_role": "NOPE"}),
    ("PUT", "/{id}/status", {"status": "DISABLED"}),
    ("PUT", "/{id}/status", {}),
    ("GET", "/{id}/tenants", None),
    ("PUT", "/{id}/tenants", {"tenant_ids": []}),
    ("PUT", "/{id}/tenants", {"tenant_ids": ["bad"]}),
]


@pytest.mark.parametrize("actor_role", [MANAGER])
@pytest.mark.parametrize("method,path,body", REQUESTS)
def test_non_central_platform_actor_is_forbidden(
    client_pg, db, login_as, make_auth_session, actor_role, method, path, body
):
    target = staff(make_auth_session)
    login_as(UserRole.ADMIN, platform_role=actor_role)
    before = db_state(db, target.user_id)
    resp = client_pg.request(method, BASE + path.replace("{id}", target.user_id), json=body)
    assert resp.status_code == 403
    assert db_state(db, target.user_id) == before


@pytest.mark.parametrize("method,path,body", REQUESTS)
def test_tenant_admin_is_forbidden(client_pg, login_as, make_auth_session, tenant, method, path, body):
    target = staff(make_auth_session)
    login_as(UserRole.ADMIN, tenant_id=tenant)
    resp = client_pg.request(method, BASE + path.replace("{id}", target.user_id), json=body)
    assert resp.status_code == 403


def test_unauthenticated_is_401(client_pg):
    assert client_pg.get(BASE, headers={"Cookie": ""}).status_code == 401


def test_central_lists_only_platform_accounts(client_pg, db, make_auth_session, tenant):
    member = staff(make_auth_session, MANAGER)
    tenant_user = make_auth_session(UserRole.VIEWER, tenant_id=tenant)
    resp = client_pg.get(BASE)
    assert resp.status_code == 200
    items = {i["user_id"]: i for i in resp.json()["items"]}
    assert items[member.user_id]["platform_role"] == "TENANT_MANAGER"
    assert set(items[member.user_id]) == {"user_id", "email", "platform_role", "status", "last_login_at"}
    assert tenant_user.user_id not in items


def test_central_changes_role_and_records_event(client_pg, db, make_auth_session):
    target = staff(make_auth_session)
    for role in (CENTRAL, MANAGER):
        resp = client_pg.put(f"{BASE}/{target.user_id}/role", json={"platform_role": role.value})
        assert resp.status_code == 204
        assert db_state(db, target.user_id)[0] == role.value
    log = events(db, target.user_id)
    assert [e[0] for e in log] == ["PLATFORM_ROLE_CHANGED"] * 2
    assert log[0][1] == {"platform_role": "TENANT_MANAGER"}
    assert log[0][2] == {"platform_role": "CENTRAL_ADMIN"}


def test_same_role_is_409(client_pg, make_auth_session):
    target = staff(make_auth_session)
    resp = client_pg.put(f"{BASE}/{target.user_id}/role", json={"platform_role": "TENANT_MANAGER"})
    assert resp.status_code == 409


def test_staff_role_is_removed(client_pg, make_auth_session):
    target = staff(make_auth_session)
    assert client_pg.put(f"{BASE}/{target.user_id}/role", json={"platform_role": "STAFF"}).status_code == 422
    assert client_pg.put(f"{BASE}/{target.user_id}/role", json={"platform_role": "VIEWER"}).status_code == 422


def test_cannot_change_own_role_or_status(client_pg, db, central_session):
    me = central_session.user_id
    assert client_pg.put(f"{BASE}/{me}/role", json={"platform_role": "TENANT_MANAGER"}).status_code == 403
    assert client_pg.put(f"{BASE}/{me}/status", json={"status": "DISABLED"}).status_code == 403
    assert db_state(db, me) == ("CENTRAL_ADMIN", "ACTIVE")


def test_central_can_demote_and_disable_another_central(client_pg, db, make_auth_session):
    """복수 중앙 관리자 — 행위자(활성 중앙)가 남으므로 다른 중앙 관리자는 내릴 수 있다."""
    other = staff(make_auth_session, CENTRAL)
    assert client_pg.put(f"{BASE}/{other.user_id}/status", json={"status": "DISABLED"}).status_code == 204
    assert client_pg.put(f"{BASE}/{other.user_id}/role", json={"platform_role": "TENANT_MANAGER"}).status_code == 204
    assert db_state(db, other.user_id) == ("TENANT_MANAGER", "DISABLED")


def test_disable_revokes_sessions_and_management_immediately(client_pg, db, make_auth_session, tenant):
    target = staff(make_auth_session, MANAGER)
    users = "/api/v1/users"
    assert client_pg.get(users, headers=target.headers).status_code == 200
    resp = client_pg.put(f"{BASE}/{target.user_id}/status", json={"status": "DISABLED"})
    assert resp.status_code == 204
    assert db_state(db, target.user_id)[1] == "DISABLED"
    assert client_pg.get(users, headers=target.headers).status_code == 401
    assert events(db, target.user_id)[-1][0] == "STATUS_CHANGED"
    assert client_pg.put(f"{BASE}/{target.user_id}/status", json={"status": "ACTIVE"}).status_code == 204


def test_manager_cannot_use_staff_powers_even_with_assignment(client_pg, db, make_auth_session, tenant):
    target = staff(make_auth_session)
    assert assign(client_pg, target.user_id, [tenant]).status_code == 204
    resp = client_pg.get(BASE, headers=target.headers)
    assert resp.status_code == 403
    resp = client_pg.put(
        f"{BASE}/{target.user_id}/tenants", json={"tenant_ids": []}, headers=target.headers
    )
    assert resp.status_code == 403
    assert assigned(db, target.user_id) == [tenant]


def test_assign_get_replace_and_clear(client_pg, db, make_auth_session, make_tenant):
    target = staff(make_auth_session)
    a, b, c = make_tenant(), make_tenant(), make_tenant()
    assert client_pg.get(f"{BASE}/{target.user_id}/tenants").json() == {"tenant_ids": []}
    assert assign(client_pg, target.user_id, [b, a]).status_code == 204
    assert client_pg.get(f"{BASE}/{target.user_id}/tenants").json() == {"tenant_ids": sorted([a, b])}
    assert assign(client_pg, target.user_id, [c, a]).status_code == 204
    assert assigned(db, target.user_id) == sorted([a, c])
    assert assign(client_pg, target.user_id, []).status_code == 204
    assert client_pg.get(f"{BASE}/{target.user_id}/tenants").json() == {"tenant_ids": []}
    assert assigned(db, target.user_id) == []
    log = events(db, target.user_id)
    assert [e[0] for e in log] == ["TENANT_ASSIGNMENTS_CHANGED"] * 3
    assert log[0][1] == {"tenant_ids": []}
    assert log[0][2] == {"tenant_ids": sorted([a, b])}
    assert log[2][1] == {"tenant_ids": sorted([a, c])}
    assert log[2][2] == {"tenant_ids": []}


def test_assignment_event_failure_rolls_back_replacement(
    client_pg, db, make_auth_session, make_tenant, monkeypatch
):
    from db.repositories import auth as auth_repo
    from sqlalchemy.exc import SQLAlchemyError

    target = staff(make_auth_session)
    kept, other = make_tenant(), make_tenant()
    assert assign(client_pg, target.user_id, [kept]).status_code == 204
    events_before = len(events(db, target.user_id))
    replaced = []
    real_replace = auth_repo.replace_assigned_tenants

    def spy_replace(*args, **kwargs):
        real_replace(*args, **kwargs)
        replaced.append(kwargs["tenant_ids"])

    def boom(*args, **kwargs):
        raise SQLAlchemyError("secret-detail postgres://user:pw@host")

    with monkeypatch.context() as patch:
        patch.setattr(auth_repo, "replace_assigned_tenants", spy_replace)
        patch.setattr(auth_repo, "add_user_event", boom)
        resp = assign(client_pg, target.user_id, [other])
    assert replaced == [(other,)]  # 교체가 일어난 뒤 이력 추가에서 실패했다
    assert resp.status_code == 500
    assert resp.json()["error"]["code"] == "INTERNAL_ERROR"
    assert "secret-detail" not in resp.text
    db.expire_all()
    assert assigned(db, target.user_id) == [kept]
    assert len(events(db, target.user_id)) == events_before


def test_same_assignment_is_409_without_event(client_pg, db, make_auth_session, tenant):
    target = staff(make_auth_session)
    assert assign(client_pg, target.user_id, []).status_code == 409
    assert assign(client_pg, target.user_id, [tenant]).status_code == 204
    assert assign(client_pg, target.user_id, [tenant]).status_code == 409
    assert len(events(db, target.user_id)) == 1


def test_duplicate_unknown_and_malformed_ids_change_nothing(client_pg, db, make_auth_session, tenant):
    target = staff(make_auth_session)
    assert assign(client_pg, target.user_id, [tenant]).status_code == 204
    assert assign(client_pg, target.user_id, [tenant, tenant]).status_code == 422
    assert assign(client_pg, target.user_id, [tenant.upper(), tenant]).status_code == 422
    assert assign(client_pg, target.user_id, ["not-a-uuid"]).status_code == 422
    resp = client_pg.put(f"{BASE}/{target.user_id}/tenants", json={"tenant_ids": [], "extra": 1})
    assert resp.status_code == 422
    resp = assign(client_pg, target.user_id, [tenant, MISSING])
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "TENANT_NOT_FOUND"
    assert assigned(db, target.user_id) == [tenant]
    assert len(events(db, target.user_id)) == 1


def test_assignment_allowed_for_suspended_customer_without_changing_accounts(
    client_pg, db, make_auth_session, make_tenant
):
    from db.models import TenantStatus

    suspended = make_tenant(TenantStatus.SUSPENDED)
    target = staff(make_auth_session)
    before = db.execute(
        text("SELECT account_type::text, tenant_id FROM app_users WHERE user_id = :u"),
        {"u": target.user_id},
    ).one()
    assert assign(client_pg, target.user_id, [suspended]).status_code == 204
    assert assigned(db, target.user_id) == [suspended]
    assert db.execute(
        text("SELECT account_type::text, tenant_id FROM app_users WHERE user_id = :u"),
        {"u": target.user_id},
    ).one() == before


def test_central_and_non_platform_targets_cannot_be_assigned(client_pg, db, make_auth_session, tenant):
    central = staff(make_auth_session, CENTRAL)
    assert assign(client_pg, central.user_id, [tenant]).status_code == 409
    assert assigned(db, central.user_id) == []
    customer = make_auth_session(UserRole.VIEWER, tenant_id=tenant)
    for uid in (customer.user_id, MISSING, "not-a-uuid"):
        for resp in (assign(client_pg, uid, [tenant]), client_pg.get(f"{BASE}/{uid}/tenants")):
            assert resp.status_code == 404
            assert resp.json()["error"]["code"] == "USER_NOT_FOUND"
    assert assigned(db, customer.user_id) == []


def test_role_change_clears_assignments_and_never_resurrects(client_pg, db, make_auth_session, tenant):
    target = staff(make_auth_session)
    assert assign(client_pg, target.user_id, [tenant]).status_code == 204
    assert client_pg.put(f"{BASE}/{target.user_id}/role", json={"platform_role": "CENTRAL_ADMIN"}).status_code == 204
    assert assigned(db, target.user_id) == []
    assert client_pg.put(f"{BASE}/{target.user_id}/role", json={"platform_role": "TENANT_MANAGER"}).status_code == 204
    assert client_pg.get(f"{BASE}/{target.user_id}/tenants").json() == {"tenant_ids": []}
    log = events(db, target.user_id)
    assert sorted(e[0] for e in log) == ["PLATFORM_ROLE_CHANGED"] * 2 + ["TENANT_ASSIGNMENTS_CHANGED"] * 2
    cleared = [e for e in log if e[0] == "TENANT_ASSIGNMENTS_CHANGED" and e[1] == {"tenant_ids": [tenant]}]
    assert [e[2] for e in cleared] == [{"tenant_ids": []}]


def test_assignment_by_demoted_actor_is_rejected_and_writes_nothing(
    client_pg, db, login_as, make_auth_session, tenant
):
    target = staff(make_auth_session)
    actor = login_as(UserRole.ADMIN, platform_role=CENTRAL)
    db.execute(
        text("UPDATE app_users SET platform_role = 'TENANT_MANAGER' WHERE user_id = :u"),
        {"u": actor.user_id},
    )
    assert assign(client_pg, target.user_id, [tenant]).status_code == 403
    assert assigned(db, target.user_id) == []
    assert events(db, target.user_id) == []


@pytest.mark.parametrize("kind", ["tenant", "unknown", "malformed"])
def test_non_platform_or_missing_targets_are_opaque_404(client_pg, db, make_auth_session, tenant, kind):
    ids = {
        "tenant": make_auth_session(UserRole.VIEWER, tenant_id=tenant).user_id,
        "unknown": "00000000-0000-4000-8000-000000000000",
        "malformed": "not-a-uuid",
    }
    uid = ids[kind]
    for method, path, body in (
        ("PUT", "/role", {"platform_role": "CENTRAL_ADMIN"}),
        ("PUT", "/status", {"status": "DISABLED"}),
        ("GET", "/events", None),
    ):
        resp = client_pg.request(method, f"{BASE}/{uid}{path}", json=body)
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "USER_NOT_FOUND"


def test_customer_users_endpoints_never_reach_platform_targets(client_pg, make_auth_session):
    target = staff(make_auth_session)
    for method, path, body in (
        ("PUT", "/role", {"role": "VIEWER"}),
        ("PUT", "/status", {"status": "DISABLED"}),
        ("POST", "/sessions/revoke", None),
        ("GET", "/events", None),
    ):
        assert client_pg.request(method, f"/api/v1/users/{target.user_id}{path}", json=body).status_code == 404


def test_staff_schemas_are_closed_and_validate_enums():
    from datetime import UTC, datetime

    from pydantic import ValidationError
    from schemas.api.staff import (
        StaffItem,
        StaffResponse,
        StaffRoleRequest,
        StaffStatusRequest,
        StaffTenantsRequest,
    )

    for bad in ({"tenant_ids": [MISSING, MISSING]}, {"tenant_ids": ["x"]}, {"tenant_ids": [], "a": 1}, {}):
        with pytest.raises(ValidationError):
            StaffTenantsRequest(**bad)
    assert StaffTenantsRequest(tenant_ids=[]).tenant_ids == []
    assert StaffRoleRequest(platform_role="TENANT_MANAGER").platform_role is MANAGER
    assert StaffStatusRequest(status="DISABLED").status.value == "DISABLED"
    for bad in ({"platform_role": "STAFF"}, {"platform_role": "TENANT_MANAGER", "role": "ADMIN"}, {}):
        with pytest.raises(ValidationError):
            StaffRoleRequest(**bad)
    for bad in ({"status": "PENDING"}, {"status": "ACTIVE", "extra": 1}, {}):
        with pytest.raises(ValidationError):
            StaffStatusRequest(**bad)
    item = {
        "user_id": "u1", "email": "a@example.com", "platform_role": "TENANT_MANAGER",
        "status": "ACTIVE", "last_login_at": datetime.now(UTC),
    }
    assert StaffResponse(items=[item]).items[0].platform_role is MANAGER
    for bad in ({**item, "tenant_id": None}, {**item, "platform_role": "NOPE"}, {**item, "email": "bad"}):
        with pytest.raises(ValidationError):
            StaffItem(**bad)


def test_no_creation_route_exists(client_pg):
    assert client_pg.post(BASE, json={"email": "a@example.com"}).status_code in (404, 405)
