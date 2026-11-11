"""/api/v1/groups · 고객사 그룹 배치 (#572 2번 · 3번 · 4번) — 실제 PostgreSQL + 실제 세션.

지키는 것.
  ① 그룹 생성 · 삭제 · 고객사 넣고 빼기 · 직원 배정은 중앙 관리계정만 — 관리자 직원계정의 같은 요청은 403 이고 DB 가 그대로다.
     고객사(TENANT) 계정은 그룹 경로 전부 403 이다.
  ② 조회는 중앙 관리계정이 전체, 관리자 직원계정이 자기 담당 그룹만 받는다(이름순 · 고객사 수 포함).
  ③ 같은 이름은 409 GROUP_NAME_CONFLICT, 없는 그룹은 404 GROUP_NOT_FOUND.
  ④ 그룹을 지우면 그 고객사는 그룹 없음이 되고 직원 배정이 지워져 다음 요청부터 범위가 줄어든다.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from db import models
from db.repositories import auth as auth_repo
from db.repositories import platform_events as platform_events_repo
from schemas.api.auth import AccountType, PlatformRole, UserRole
from schemas.api.groups import GroupItem
from schemas.platform_events import (
    PlatformActorKind,
    PlatformEventAction,
    PlatformEventCategory,
    PlatformEventOutcome,
    PlatformEventTargetType,
)
from sqlalchemy import func, select

BASE = "/api/v1/groups"
TENANTS = "/api/v1/tenants"
STAFF = "/api/v1/staff"
MISSING = "00000000-0000-4000-8000-000000000000"
ITEM_KEYS = {"group_id", "name", "tenant_count", "created_at", "updated_at"}


@pytest.fixture()
def central(login_as):
    return login_as(UserRole.ADMIN, platform_role=PlatformRole.CENTRAL_ADMIN)


def _code(resp) -> str:
    return resp.json()["error"]["code"]


def _group_count(db) -> int:
    return db.scalar(select(func.count()).select_from(models.TenantGroup))


def _events(db, *, target_id=None):
    statement = select(models.PlatformEvent).where(
        models.PlatformEvent.action.in_([
            PlatformEventAction.TENANT_GROUP_CREATE,
            PlatformEventAction.TENANT_GROUP_DELETE,
            PlatformEventAction.TENANT_GROUP_CHANGE,
        ])
    )
    if target_id is not None:
        statement = statement.where(models.PlatformEvent.target_id == target_id)
    return list(db.scalars(
        statement.order_by(models.PlatformEvent.occurred_at, models.PlatformEvent.event_id)
    ))


def _event_ids(db, *, target_id=None):
    return [row.event_id for row in _events(db, target_id=target_id)]


def _assert_event(row, central, action, target, details, *, tenant=None):
    assert row.action == action
    assert row.category is PlatformEventCategory.TENANT_ADMIN
    assert row.outcome is PlatformEventOutcome.SUCCEEDED
    assert row.actor_kind is PlatformActorKind.USER
    assert row.actor_user_id == central.user_id
    assert row.actor_account_type is AccountType.PLATFORM
    assert row.actor_platform_role is PlatformRole.CENTRAL_ADMIN
    assert row.session_id == central.session_id
    assert row.tenant_id == tenant
    assert row.target_type is (
        PlatformEventTargetType.TENANT if tenant else PlatformEventTargetType.TENANT_GROUP
    )
    assert row.target_id == target
    assert row.details == details


def _tenant_group(db, tenant_id) -> str | None:
    db.expire_all()
    return db.get(models.Tenant, tenant_id).group_id


def _mine(items, *group_ids):
    """다른 테스트가 커밋해 남긴 그룹과 섞이지 않게 이 테스트의 그룹만 고른다(순서 유지)."""
    return [item for item in items if item["group_id"] in group_ids]


# ----- ① 권한 --------------------------------------------------------------------


def test_central_creates_lists_and_deletes_a_group(client_pg, db, central, make_group):
    # 다른 행동뿐 아니라 다른 그룹의 같은 행동도 이 요청의 기록 수에 섞이지 않아야 한다.
    other_group = make_group()
    unrelated = [
        platform_events_repo.record_platform_event(
            db, PlatformEventAction.SIGNUP_REQUEST_CREATE, platform_events_repo.ANONYMOUS,
            target_id=str(uuid.uuid4()),
        ),
        platform_events_repo.record_platform_event(
            db, PlatformEventAction.TENANT_GROUP_CREATE, platform_events_repo.person(central),
            target_id=other_group,
        ),
    ]
    unrelated_ids = [row.event_id for row in unrelated]
    db.commit()
    resp = client_pg.post(BASE, json={"name": "광주 지사"})
    assert resp.status_code == 201
    body = resp.json()
    assert set(body) == ITEM_KEYS
    GroupItem.model_validate(body)
    assert (body["name"], body["tenant_count"]) == ("광주 지사", 0)
    listed = client_pg.get(BASE)
    assert listed.status_code == 200
    assert _mine(listed.json()["items"], body["group_id"]) == [body]

    assert client_pg.delete(f"{BASE}/{body['group_id']}").status_code == 204
    assert db.get(models.TenantGroup, body["group_id"]) is None
    assert _mine(client_pg.get(BASE).json()["items"], body["group_id"]) == []
    created, deleted = _events(db, target_id=body["group_id"])
    _assert_event(created, central, "TENANT_GROUP_CREATE", body["group_id"], {})
    _assert_event(
        deleted, central, "TENANT_GROUP_DELETE", body["group_id"], {"tenant_count": 0, "staff_count": 0}
    )
    assert all(db.get(models.PlatformEvent, event_id) is not None for event_id in unrelated_ids)


def test_central_builds_a_group_scope_end_to_end(client_pg, db, central, make_auth_session, make_tenant):
    """중앙 관리계정이 그룹을 만들고 고객사를 넣고 직원에게 배정하면, 그 직원은 다음 요청부터 그 고객사만 다룬다."""
    inside, outside = make_tenant(), make_tenant()
    manager = make_auth_session(UserRole.ADMIN, platform_role=PlatformRole.TENANT_MANAGER)
    group_id = client_pg.post(BASE, json={"name": "부산 지사"}).json()["group_id"]
    placed = client_pg.put(f"{TENANTS}/{inside}/group", json={"group_id": group_id})
    assert placed.status_code == 200 and placed.json()["group_id"] == group_id
    assert client_pg.put(f"{STAFF}/{manager.user_id}/groups", json={"group_ids": [group_id]}).status_code == 204

    listed = client_pg.get(TENANTS, headers=manager.headers).json()["tenants"]
    assert [t["tenant_id"] for t in listed] == [inside]
    assert client_pg.get(f"{TENANTS}/{inside}", headers=manager.headers).status_code == 200
    assert client_pg.get(f"{TENANTS}/{outside}", headers=manager.headers).status_code == 403
    [group] = client_pg.get(BASE, headers=manager.headers).json()["items"]
    assert (group["group_id"], group["tenant_count"]) == (group_id, 1)


def test_manager_cannot_manage_groups_even_inside_its_scope(
    client_pg, db, make_auth_session, make_tenant, make_group
):
    tenant = make_tenant()
    group_id = make_group(tenant_ids=[tenant])
    other_group = make_group()
    manager = make_auth_session(
        UserRole.ADMIN, platform_role=PlatformRole.TENANT_MANAGER, assigned_tenant_ids=(tenant,)
    )
    before = (_group_count(db), _tenant_group(db, tenant), auth_repo.list_assigned_group_ids(db, user_id=manager.user_id))
    before_events = _event_ids(db)
    for method, url, body in (
        ("POST", BASE, {"name": "새 그룹"}),
        ("POST", BASE, {"name": ""}),  # 관문이 본문 검증보다 먼저다
        ("DELETE", f"{BASE}/{group_id}", None),
        ("PUT", f"{TENANTS}/{tenant}/group", {"group_id": other_group}),
        ("PUT", f"{TENANTS}/{tenant}/group", {"group_id": None}),
        ("PUT", f"{STAFF}/{manager.user_id}/groups", {"group_ids": [group_id, other_group]}),
    ):
        resp = client_pg.request(method, url, json=body, headers=manager.headers)
        assert (resp.status_code, _code(resp)) == (403, "AUTH_FORBIDDEN"), (method, url)
    db.expire_all()
    assert before == (
        _group_count(db), _tenant_group(db, tenant), auth_repo.list_assigned_group_ids(db, user_id=manager.user_id)
    )
    assert _event_ids(db) == before_events


@pytest.mark.parametrize("role", [UserRole.ADMIN, UserRole.VIEWER])
def test_tenant_accounts_are_forbidden_on_every_group_route(client_pg, db, login_as, make_tenant, make_group, role):
    group_id = make_group()
    login_as(role, tenant_id=make_tenant())
    count = _group_count(db)
    for method, url, body in (
        ("GET", BASE, None),
        ("POST", BASE, {"name": "새 그룹"}),
        ("DELETE", f"{BASE}/{group_id}", None),
    ):
        resp = client_pg.request(method, url, json=body)
        assert (resp.status_code, _code(resp)) == (403, "AUTH_FORBIDDEN"), (method, url)
    assert _group_count(db) == count


def test_unauthenticated_is_401(client_pg, make_group):
    group_id = make_group()
    anonymous = {"Cookie": ""}
    assert client_pg.get(BASE, headers=anonymous).status_code == 401
    assert client_pg.post(BASE, json={"name": "x"}, headers=anonymous).status_code == 401
    assert client_pg.delete(f"{BASE}/{group_id}", headers=anonymous).status_code == 401


def test_central_demoted_after_the_dependency_snapshot_writes_nothing(client_pg, db, central, make_group, monkeypatch):
    """의존성이 중앙 관리계정으로 본 뒤 잠금 안에서 강등이 먼저 커밋됐다 — 재판정이 403 이고 아무것도 쓰지 않는다."""
    group_id = make_group()
    original = auth_repo.lock_users_table

    def demote_then_lock(session):
        session.get(models.AppUser, central.user_id).platform_role = PlatformRole.TENANT_MANAGER
        session.flush()
        original(session)

    monkeypatch.setattr(auth_repo, "lock_users_table", demote_then_lock)
    count = _group_count(db)
    before_events = _event_ids(db)
    assert client_pg.post(BASE, json={"name": "새 그룹"}).status_code == 403
    assert client_pg.delete(f"{BASE}/{group_id}").status_code == 403
    assert _group_count(db) == count
    assert _event_ids(db) == before_events


# ----- ② 조회 --------------------------------------------------------------------


def test_manager_lists_only_its_assigned_groups_with_counts(
    client_pg, db, central, make_auth_session, make_tenant, make_group
):
    a, b, c = make_tenant(), make_tenant(), make_tenant()
    north = make_group("north", tenant_ids=[a, b])
    south = make_group("south", tenant_ids=[c])
    empty = make_group("empty")
    manager = make_auth_session(UserRole.ADMIN, platform_role=PlatformRole.TENANT_MANAGER)
    auth_repo.replace_assigned_groups(db, user_id=manager.user_id, group_ids=(north, empty), now=datetime.now(UTC))
    db.commit()

    mine = client_pg.get(BASE, headers=manager.headers).json()["items"]
    assert [(g["name"], g["tenant_count"]) for g in mine] == [("empty", 0), ("north", 2)]  # 이름순
    every = _mine(client_pg.get(BASE).json()["items"], north, south, empty)
    assert [(g["name"], g["tenant_count"]) for g in every] == [("empty", 0), ("north", 2), ("south", 1)]


def test_manager_without_groups_lists_nothing(client_pg, make_auth_session, make_group):
    make_group()
    manager = make_auth_session(UserRole.ADMIN, platform_role=PlatformRole.TENANT_MANAGER)
    assert client_pg.get(BASE, headers=manager.headers).json() == {"items": []}


# ----- ③ 거절 --------------------------------------------------------------------


def test_duplicate_name_is_409_and_keeps_one_group(client_pg, db, central):
    created = client_pg.post(BASE, json={"name": "광주 지사"})
    assert created.status_code == 201
    target_id = created.json()["group_id"]
    count = _group_count(db)
    before_events = _event_ids(db)
    resp = client_pg.post(BASE, json={"name": "광주 지사"})
    assert (resp.status_code, _code(resp)) == (409, "GROUP_NAME_CONFLICT")
    assert _group_count(db) == count
    (event,) = _events(db, target_id=target_id)
    assert event.action == "TENANT_GROUP_CREATE"
    assert _event_ids(db) == before_events


@pytest.mark.parametrize("body", [{}, {"name": ""}, {"name": "가" * 101}, {"name": "x", "tenant_ids": []}])
def test_create_outside_the_contract_is_422(client_pg, db, central, body):
    count = _group_count(db)
    resp = client_pg.post(BASE, json=body)
    assert (resp.status_code, _code(resp)) == (422, "REQUEST_VALIDATION_FAILED")
    assert _group_count(db) == count


@pytest.mark.parametrize("group_id", [MISSING, "not-a-uuid"])
def test_deleting_an_unknown_or_malformed_group_is_404(client_pg, db, central, group_id):
    before_events = _event_ids(db)
    resp = client_pg.delete(f"{BASE}/{group_id}")
    assert (resp.status_code, _code(resp)) == (404, "GROUP_NOT_FOUND")
    assert _event_ids(db) == before_events


def test_placing_a_tenant_rejects_unknown_group_and_tenant(client_pg, db, central, make_tenant, make_group):
    tenant = make_tenant()
    group_id = make_group()
    before_events = _event_ids(db)
    resp = client_pg.put(f"{TENANTS}/{tenant}/group", json={"group_id": MISSING})
    assert (resp.status_code, _code(resp)) == (404, "GROUP_NOT_FOUND")
    for unknown in (MISSING, "not-a-uuid"):
        resp = client_pg.put(f"{TENANTS}/{unknown}/group", json={"group_id": group_id})
        assert (resp.status_code, _code(resp)) == (404, "TENANT_NOT_FOUND")
    for body in ({}, {"group_id": "bad"}, {"group_id": None, "x": 1}):
        assert client_pg.put(f"{TENANTS}/{tenant}/group", json=body).status_code == 422
    assert _tenant_group(db, tenant) is None
    assert _event_ids(db) == before_events


def test_central_moves_a_tenant_between_groups_and_out(client_pg, db, central, make_tenant, make_group):
    tenant = make_tenant()
    first, second = make_group(), make_group()
    before = None
    for group_id in (first, second, None):
        resp = client_pg.put(f"{TENANTS}/{tenant}/group", json={"group_id": group_id})
        assert resp.status_code == 200 and resp.json()["group_id"] == group_id
        assert _tenant_group(db, tenant) == group_id
        assert client_pg.get(f"{TENANTS}/{tenant}").json()["group_id"] == group_id
        _assert_event(
            _events(db, target_id=tenant)[-1], central, "TENANT_GROUP_CHANGE", tenant,
            {"before": before, "after": group_id}, tenant=tenant,
        )
        before = group_id
    assert len(_events(db, target_id=tenant)) == 3


@pytest.mark.parametrize("assigned", [False, True])
def test_reassigning_the_same_group_returns_200_without_event(
    client_pg, db, central, make_tenant, make_group, assigned
):
    tenant = make_tenant()
    group = make_group(tenant_ids=[tenant]) if assigned else None
    before_events = _event_ids(db)
    response = client_pg.put(f"{TENANTS}/{tenant}/group", json={"group_id": group})
    assert response.status_code == 200
    assert response.json()["group_id"] == group
    assert _tenant_group(db, tenant) == group
    assert _event_ids(db) == before_events


# ----- ④ 삭제의 효과 ---------------------------------------------------------------


def test_deleting_a_group_ungroups_tenants_and_shrinks_manager_scope_on_the_next_request(
    client_pg, db, central, make_auth_session, make_tenant, make_group
):
    tenant = make_tenant()
    group_id = make_group(tenant_ids=[tenant])
    manager = make_auth_session(UserRole.ADMIN, platform_role=PlatformRole.TENANT_MANAGER)
    auth_repo.replace_assigned_groups(db, user_id=manager.user_id, group_ids=(group_id,), now=datetime.now(UTC))
    db.commit()
    assert [t["tenant_id"] for t in client_pg.get(TENANTS, headers=manager.headers).json()["tenants"]] == [tenant]

    assert client_pg.delete(f"{BASE}/{group_id}").status_code == 204

    # 고객사는 남고 그룹 없음(담당자 없음)이 된다 — 직원 배정도 함께 지워진다
    assert client_pg.get(f"{TENANTS}/{tenant}").json()["group_id"] is None
    assert auth_repo.list_assigned_group_ids(db, user_id=manager.user_id) == ()
    assert client_pg.get(f"{STAFF}/{manager.user_id}/groups").json() == {"group_ids": []}
    # 세션을 다시 만들지 않아도 다음 요청부터 범위에서 빠진다
    assert client_pg.get(TENANTS, headers=manager.headers).json()["tenants"] == []
    detail = client_pg.get(f"{TENANTS}/{tenant}", headers=manager.headers)
    assert (detail.status_code, _code(detail)) == (403, "AUTH_FORBIDDEN")
    assert client_pg.get("/api/v1/auth/me", headers=manager.headers).json()["assigned_tenant_ids"] == []
    (deleted,) = _events(db, target_id=group_id)
    _assert_event(
        deleted, central, "TENANT_GROUP_DELETE", group_id, {"tenant_count": 1, "staff_count": 1}
    )


@pytest.mark.parametrize("operation", ["create", "delete", "assign"])
def test_group_changes_and_events_roll_back_together(
    client_pg, db, central, make_tenant, make_group, make_auth_session, monkeypatch, operation
):
    tenant = make_tenant()
    group = make_group(tenant_ids=[tenant])
    other = make_group()
    manager = make_auth_session(UserRole.ADMIN, platform_role=PlatformRole.TENANT_MANAGER)
    auth_repo.replace_assigned_groups(
        db, user_id=manager.user_id, group_ids=(group,), now=datetime.now(UTC)
    )
    db.commit()
    initial_count = _group_count(db)
    before_events = _event_ids(db)
    original_commit = db.commit
    flushed = []

    def fail_after_event():
        rows = [row for row in _events(db) if row.event_id not in before_events]
        if rows:
            assert len(rows) == 1
            flushed.append(rows[0].event_id)
            raise RuntimeError("forced post-flush failure")
        original_commit()

    monkeypatch.setattr(db, "commit", fail_after_event)
    if operation == "create":
        response = client_pg.post(BASE, json={"name": "되돌릴 그룹"})
    elif operation == "delete":
        response = client_pg.delete(f"{BASE}/{group}")
    else:
        response = client_pg.put(f"{TENANTS}/{tenant}/group", json={"group_id": other})
    assert (response.status_code, _code(response)) == (500, "INTERNAL_ERROR")
    assert "forced post-flush failure" not in response.text
    assert len(flushed) == 1
    assert _event_ids(db) == before_events
    assert _group_count(db) == initial_count
    assert _tenant_group(db, tenant) == group
    assert auth_repo.list_assigned_group_ids(db, user_id=manager.user_id) == (group,)
