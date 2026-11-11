# ==============================================================================
# [파일 설명]
# 사용자 · 직원 관리의 활동 로그(platform_events, #589 B1) — 실제 PostgreSQL 행으로 확인한다.
#   - 호출 자리마다: 새 행 1개(action · outcome · 행위자 · 세션 · 영향받은 테넌트 · 대상 · details).
#   - 무변경 · 충돌 · 거절은 행이 없고, 이벤트 기록이 실패하면 업무 상태 · user_admin_events · 이벤트가 함께 되돌아간다.
# ==============================================================================

from __future__ import annotations

import json

import pytest
from auth.cognito import CognitoUser
from db import models
from db.repositories import platform_events as platform_events_repo
from schemas.api.auth import PlatformRole, UserRole
from schemas.platform_events import (
    PlatformActorKind,
    PlatformEventCategory,
    PlatformEventOutcome,
    PlatformEventTargetType,
)
from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError
from tests.test_user_admin_api import FORCE, FakeAdapter, use_adapter

USERS = "/api/v1/users"
STAFF = "/api/v1/staff"
CENTRAL = PlatformRole.CENTRAL_ADMIN
MANAGER = PlatformRole.TENANT_MANAGER


@pytest.fixture()
def tenant(make_tenant) -> str:
    return make_tenant()


@pytest.fixture()
def adapter(client_pg):
    fake = FakeAdapter()
    use_adapter(client_pg, fake)
    return fake


@pytest.fixture(params=["platform", "tenant"])
def admin(request, login_as, tenant):
    """같은 관리 권한의 두 행위자 — 중앙 관리계정 · 대상 테넌트의 고객사 ADMIN."""
    if request.param == "platform":
        return login_as(UserRole.ADMIN, platform_role=CENTRAL)
    return login_as(UserRole.ADMIN, tenant_id=tenant)


@pytest.fixture()
def central(login_as):
    return login_as(UserRole.ADMIN, platform_role=CENTRAL)


def platform_events(db, target_id):
    db.expire_all()
    return db.scalars(
        select(models.PlatformEvent)
        .where(models.PlatformEvent.target_id == target_id)
        .order_by(models.PlatformEvent.occurred_at, models.PlatformEvent.event_id)
    ).all()


def only_event(db, target_id):
    rows = platform_events(db, target_id)
    assert len(rows) == 1
    return rows[0]


def assert_event(row, action, actor, *, tenant_id, details):
    """한 행의 계약 칸 전부 — 행위자는 인가에 쓴 세션의 신원이고, 테넌트는 영향받은 대상의 것이다."""
    assert row.action == action
    assert row.category is PlatformEventCategory.USER_ADMIN
    assert row.outcome is PlatformEventOutcome.SUCCEEDED
    assert row.actor_kind is PlatformActorKind.USER
    assert row.actor_user_id == actor.user_id
    assert row.actor_account_type == actor.account_type
    assert row.actor_platform_role == actor.platform_role
    assert row.session_id == actor.session_id
    assert row.target_type is PlatformEventTargetType.USER
    assert row.tenant_id == tenant_id
    assert row.details == details


def user_events(db, target_id):
    db.expire_all()
    rows = db.scalars(
        select(models.UserAdminEvent).where(models.UserAdminEvent.target_user_id == target_id)
    ).all()
    return sorted(r.action.value for r in rows)


def counts(db):
    """업무 상태 · 두 이벤트 스트림 · 폐기 세션 — 롤백 · 무변경 비교용."""
    db.expire_all()
    return (
        db.execute(text("SELECT user_id, role::text, status::text, platform_role::text FROM app_users ORDER BY 1")).all(),
        db.execute(text("SELECT count(*) FROM user_admin_events")).scalar_one(),
        db.execute(text("SELECT count(*) FROM platform_events")).scalar_one(),
        db.execute(text("SELECT count(*) FROM auth_sessions WHERE revoked_at IS NOT NULL")).scalar_one(),
        db.execute(text("SELECT user_id, group_id FROM app_user_group_assignments ORDER BY 1, 2")).all(),
    )


def fail_after_recording(monkeypatch, *, on_call=1):
    """on_call 번째 이벤트 행을 실제로 쓴 뒤 실패시킨다 — 업무 쓰기 · user_admin_events · 앞서 쓴 행 · 이 행이 함께 되돌아가야 한다."""
    real = platform_events_repo.record_platform_event
    calls = []

    def record_then_fail(*args, **kwargs):
        row = real(*args, **kwargs)
        calls.append(row)
        if len(calls) == on_call:
            raise SQLAlchemyError("secret-detail postgres://user:pw@host")
        return row

    monkeypatch.setattr(platform_events_repo, "record_platform_event", record_then_fail)


# --- 고객 사용자 관리 ------------------------------------------------------------------


@pytest.fixture()
def target(make_auth_session, tenant):
    return make_auth_session(UserRole.VIEWER, tenant_id=tenant)


def test_role_change_records_event_in_target_tenant(client_pg, db, admin, target, tenant):
    resp = client_pg.put(f"{USERS}/{target.user_id}/role", json={"role": "OPERATOR"})
    assert resp.status_code == 204
    row = only_event(db, target.user_id)
    assert_event(row, "USER_ROLE_CHANGE", admin, tenant_id=tenant, details={"before": "VIEWER", "after": "OPERATOR"})
    assert user_events(db, target.user_id) == ["ROLE_CHANGED"]


def test_same_role_and_out_of_scope_role_change_record_nothing(client_pg, db, admin, target, make_auth_session):
    """같은 역할은 409, 관리 범위 밖(PLATFORM) 대상은 두 행위자 모두 같은 404 — 어느 쪽도 이벤트가 없다."""
    outside = make_auth_session(UserRole.ADMIN)
    assert client_pg.put(f"{USERS}/{target.user_id}/role", json={"role": "VIEWER"}).status_code == 409
    assert client_pg.put(f"{USERS}/{outside.user_id}/role", json={"role": "VIEWER"}).status_code == 404
    assert platform_events(db, target.user_id) == [] and platform_events(db, outside.user_id) == []


def test_status_change_records_each_transition_and_skips_same_status(client_pg, db, admin, target, tenant):
    path = f"{USERS}/{target.user_id}/status"
    assert client_pg.put(path, json={"status": "DISABLED"}).status_code == 204
    assert client_pg.put(path, json={"status": "DISABLED"}).status_code == 409
    assert client_pg.put(path, json={"status": "ACTIVE"}).status_code == 204
    first, second = platform_events(db, target.user_id)
    assert_event(first, "USER_STATUS_CHANGE", admin, tenant_id=tenant, details={"before": "ACTIVE", "after": "DISABLED"})
    assert_event(second, "USER_STATUS_CHANGE", admin, tenant_id=tenant, details={"before": "DISABLED", "after": "ACTIVE"})
    assert user_events(db, target.user_id) == ["STATUS_CHANGED"] * 2


def test_sessions_revoke_records_event_only_when_sessions_were_revoked(client_pg, db, admin, target, tenant):
    path = f"{USERS}/{target.user_id}/sessions/revoke"
    assert client_pg.post(path).status_code == 204
    assert_event(only_event(db, target.user_id), "USER_SESSIONS_REVOKE", admin, tenant_id=tenant, details={})
    assert client_pg.post(path).status_code == 204  # 살아 있는 세션이 없다 — 폐기한 것이 없다
    assert len(platform_events(db, target.user_id)) == 1
    assert user_events(db, target.user_id) == ["SESSIONS_REVOKED"] * 2


def test_invite_records_event_for_the_new_user_without_identity_or_permissions(client_pg, db, admin, tenant, adapter):
    adapter.create_result = CognitoUser("pe-new@example.com", "sub-pe-new", "pe-new@example.com", FORCE)
    resp = client_pg.post(USERS, json={"email": "pe-new@example.com", "role": "VIEWER", "tenant_id": tenant})
    assert resp.status_code == 201
    new_id = resp.json()["user_id"]
    row = only_event(db, new_id)
    assert_event(row, "USER_INVITE", admin, tenant_id=tenant, details={})
    assert "pe-new@example.com" not in str(row.details) + str(row.target_id)
    assert user_events(db, new_id) == ["USER_INVITED"]


def test_invite_conflict_records_nothing(client_pg, db, admin, tenant, adapter, target):
    row = db.get(models.AppUser, target.user_id)
    adapter.create_result = CognitoUser(row.email, row.cognito_sub, row.email, FORCE)
    before = counts(db)
    resp = client_pg.post(USERS, json={"email": row.email, "role": "VIEWER", "tenant_id": tenant})
    assert resp.status_code == 409
    assert counts(db) == before


def test_resend_records_event_and_skips_state_conflict(client_pg, db, admin, target, tenant, adapter):
    row = db.get(models.AppUser, target.user_id)
    adapter.get_result = CognitoUser(row.email, row.cognito_sub, row.email, "CONFIRMED")
    before = counts(db)
    assert client_pg.post(f"{USERS}/{target.user_id}/invite/resend").status_code == 409
    assert counts(db) == before
    adapter.get_result = CognitoUser(row.email, row.cognito_sub, row.email, FORCE)
    assert client_pg.post(f"{USERS}/{target.user_id}/invite/resend").status_code == 204
    assert_event(only_event(db, target.user_id), "USER_INVITE_RESEND", admin, tenant_id=tenant, details={})
    assert user_events(db, target.user_id) == ["INVITE_RESENT"]


USER_WRITES = {
    "role": lambda c, t, tenant: c.put(f"{USERS}/{t.user_id}/role", json={"role": "OPERATOR"}),
    "status": lambda c, t, tenant: c.put(f"{USERS}/{t.user_id}/status", json={"status": "DISABLED"}),
    "sessions": lambda c, t, tenant: c.post(f"{USERS}/{t.user_id}/sessions/revoke"),
    "invite": lambda c, t, tenant: c.post(
        USERS, json={"email": "pe-new@example.com", "role": "VIEWER", "tenant_id": tenant}
    ),
    "resend": lambda c, t, tenant: c.post(f"{USERS}/{t.user_id}/invite/resend"),
}


@pytest.mark.parametrize("write", USER_WRITES)
def test_user_event_failure_rolls_back_business_state_and_both_events(
    client_pg, db, admin, target, tenant, adapter, monkeypatch, write
):
    row = db.get(models.AppUser, target.user_id)
    adapter.get_result = CognitoUser(row.email, row.cognito_sub, row.email, FORCE)
    adapter.create_result = CognitoUser("pe-new@example.com", "sub-pe-new", "pe-new@example.com", FORCE)
    before = counts(db)
    fail_after_recording(monkeypatch)
    resp = USER_WRITES[write](client_pg, target, tenant)
    assert (resp.status_code, resp.json()["error"]["code"]) == (500, "INTERNAL_ERROR")
    assert "secret-detail" not in resp.text
    assert counts(db) == before


# --- 직원 관리 ---------------------------------------------------------------------------


@pytest.fixture()
def staff_target(make_auth_session):
    return make_auth_session(UserRole.ADMIN, platform_role=MANAGER)


def test_staff_invite_records_event_without_tenant_or_identity(client_pg, db, central, adapter):
    adapter.create_result = CognitoUser("pe-staff@example.com", "sub-pe-staff", "pe-staff@example.com", FORCE)
    resp = client_pg.post(STAFF, json={"email": "pe-staff@example.com"})
    assert resp.status_code == 201
    new_id = resp.json()["user_id"]
    assert_event(only_event(db, new_id), "USER_INVITE", central, tenant_id=None, details={})
    assert user_events(db, new_id) == ["USER_INVITED"]


def test_staff_invite_conflict_records_nothing(client_pg, db, central, adapter, staff_target):
    row = db.get(models.AppUser, staff_target.user_id)
    adapter.create_result = CognitoUser(row.email, row.cognito_sub, row.email, FORCE)
    before = counts(db)
    assert client_pg.post(STAFF, json={"email": row.email}).status_code == 409
    assert counts(db) == before


def test_staff_platform_role_change_records_event_and_skips_same_role(client_pg, db, central, staff_target):
    path = f"{STAFF}/{staff_target.user_id}/role"
    assert client_pg.put(path, json={"platform_role": "TENANT_MANAGER"}).status_code == 409
    assert platform_events(db, staff_target.user_id) == []
    assert client_pg.put(path, json={"platform_role": "CENTRAL_ADMIN"}).status_code == 204
    assert_event(
        only_event(db, staff_target.user_id), "USER_PLATFORM_ROLE_CHANGE", central, tenant_id=None,
        details={"before": "TENANT_MANAGER", "after": "CENTRAL_ADMIN"},
    )


def test_staff_role_change_that_clears_groups_records_both_events(client_pg, db, central, staff_target, make_group):
    group = make_group()
    assert client_pg.put(f"{STAFF}/{staff_target.user_id}/groups", json={"group_ids": [group]}).status_code == 204
    assert client_pg.put(
        f"{STAFF}/{staff_target.user_id}/role", json={"platform_role": "CENTRAL_ADMIN"}
    ).status_code == 204
    rows = platform_events(db, staff_target.user_id)
    assert len(rows) == 3
    expected = [
        ("USER_GROUP_ASSIGNMENTS_CHANGE", {"before": [], "after": [group]}),
        ("USER_GROUP_ASSIGNMENTS_CHANGE", {"before": [group], "after": []}),
        ("USER_PLATFORM_ROLE_CHANGE", {"before": "TENANT_MANAGER", "after": "CENTRAL_ADMIN"}),
    ]
    def normalized(pairs):  # JSONB 의 키 순서와 무관하게 비교한다
        return sorted((action, json.dumps(details, sort_keys=True)) for action, details in pairs)

    assert normalized((r.action, r.details) for r in rows) == normalized(expected)
    for row in rows:
        assert_event(row, row.action, central, tenant_id=None, details=row.details)


def test_staff_group_replace_records_before_after_and_skips_no_change(client_pg, db, central, staff_target, make_group):
    a, b = make_group(), make_group()
    path = f"{STAFF}/{staff_target.user_id}/groups"
    assert client_pg.put(path, json={"group_ids": []}).status_code == 409
    assert client_pg.put(path, json={"group_ids": [b, a]}).status_code == 204
    assert client_pg.put(path, json={"group_ids": [a, b]}).status_code == 409
    assert client_pg.put(path, json={"group_ids": [a, a]}).status_code == 422
    assert client_pg.put(path, json={"group_ids": ["00000000-0000-4000-8000-000000000000"]}).status_code == 404
    assert client_pg.put(path, json={"group_ids": []}).status_code == 204
    first, second = platform_events(db, staff_target.user_id)
    ids = sorted([a, b])
    assert_event(first, "USER_GROUP_ASSIGNMENTS_CHANGE", central, tenant_id=None, details={"before": [], "after": ids})
    assert_event(second, "USER_GROUP_ASSIGNMENTS_CHANGE", central, tenant_id=None, details={"before": ids, "after": []})
    assert user_events(db, staff_target.user_id) == ["GROUP_ASSIGNMENTS_CHANGED"] * 2


def test_group_clear_event_failure_after_role_event_rolls_back_everything(
    client_pg, db, central, staff_target, make_group, monkeypatch
):
    """담당 그룹이 있는 직원의 등급 변경 — 두 번째(그룹 비움) 이벤트를 쓴 뒤 실패해도 첫 이벤트 · 등급 · 배정 · 이력이 모두 되돌아간다."""
    group = make_group()
    assert client_pg.put(f"{STAFF}/{staff_target.user_id}/groups", json={"group_ids": [group]}).status_code == 204
    before = counts(db)
    fail_after_recording(monkeypatch, on_call=2)
    resp = client_pg.put(f"{STAFF}/{staff_target.user_id}/role", json={"platform_role": "CENTRAL_ADMIN"})
    assert (resp.status_code, resp.json()["error"]["code"]) == (500, "INTERNAL_ERROR")
    assert counts(db) == before
    assert len(platform_events(db, staff_target.user_id)) == 1  # 사전 배정 이벤트만 남는다


STAFF_WRITES = {
    "invite": lambda c, t, g: c.post(STAFF, json={"email": "pe-staff@example.com"}),
    "role": lambda c, t, g: c.put(f"{STAFF}/{t.user_id}/role", json={"platform_role": "CENTRAL_ADMIN"}),
    "groups": lambda c, t, g: c.put(f"{STAFF}/{t.user_id}/groups", json={"group_ids": [g]}),
}


@pytest.mark.parametrize("write", STAFF_WRITES)
def test_staff_event_failure_rolls_back_business_state_and_both_events(
    client_pg, db, central, adapter, staff_target, make_group, monkeypatch, write
):
    group = make_group()
    row = db.get(models.AppUser, staff_target.user_id)
    adapter.get_result = CognitoUser(row.email, row.cognito_sub, row.email, FORCE)
    adapter.create_result = CognitoUser("pe-staff@example.com", "sub-pe-staff", "pe-staff@example.com", FORCE)
    before = counts(db)
    fail_after_recording(monkeypatch)
    resp = STAFF_WRITES[write](client_pg, staff_target, group)
    assert (resp.status_code, resp.json()["error"]["code"]) == (500, "INTERNAL_ERROR")
    assert counts(db) == before
