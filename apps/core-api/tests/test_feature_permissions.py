# ==============================================================================
# [파일 설명]
# 고객사 기능별 권한(FeaturePermission, #572 5번) — 실제 PostgreSQL.
#
#   - 판정 함수: effective_permissions 는 계정 종류 · 역할 · OPERATOR 부여분만 읽는다
#     (TENANT ADMIN 전부 · OPERATOR 부여분 · VIEWER 없음 · 플랫폼 관리 등급은 문의 등록 제외 셋).
#   - 진입점 관문: 조치 실행(EXECUTE_ACTION) · 인시던트 종료(RESOLVE_INCIDENT) · 문의 등록(CREATE_INQUIRY)은
#     권한이 없으면 403 AUTH_FORBIDDEN 이고 대상을 읽기 전이며 기록을 남기지 않는다.
#   - 쓰기 직전 재판정: 의존성이 통과시킨 뒤 회수된 권한으로는 users 잠금 안에서 쓰지 못한다.
#   - 조회 API 는 VIEW 로 막지 않는다 — VIEWER · 권한 없는 OPERATOR 도 자기 고객사 데이터를 읽는다.
#   - GET /auth/me 의 permissions 는 열거 선언 순서이고, 부여 · 회수는 재로그인 없이 다음 요청부터 보인다.
#   - 저장소: list/replace_feature_permissions, 사용자 삭제 시 부여분 cascade.
# ==============================================================================

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from auth import permissions as perms
from auth import service as auth_service
from db import models
from db.repositories import auth as auth_repo
from fastapi.testclient import TestClient
from schemas.api.auth import (
    AccountType,
    FeaturePermission,
    PlatformRole,
    SessionInfo,
    UserRole,
)
from schemas.api.tenants import TenantStatus
from sqlalchemy import text
from starlette.websockets import WebSocketDisconnect
from tests.test_incident_tenant_scope import (
    _change,
    _code,
    _effects,
    _incident,
    _key,
    _record_incident_reads,
)
from tests.test_ws_access import WS, _app, _wait_until

VIEW = FeaturePermission.VIEW
EXECUTE = FeaturePermission.EXECUTE_ACTION
RESOLVE = FeaturePermission.RESOLVE_INCIDENT
INQUIRY = FeaturePermission.CREATE_INQUIRY
ALL = tuple(FeaturePermission)
PLATFORM_SET = [VIEW.value, EXECUTE.value, RESOLVE.value]
INQUIRIES = "/api/v1/inquiries"


def _now() -> datetime:
    return datetime.now(UTC)


def _inquiry_body() -> dict:
    return {"category": "BUG", "title": "접속 장애", "body": "대시보드가 열리지 않습니다"}


def _inquiry_rows(db) -> tuple[int, int, int]:
    """문의 · 문의 이력 · 운영자 알림 — 거절된 등록은 어느 것도 남기지 않는다."""
    return tuple(
        db.execute(text(f"SELECT count(*) FROM {table}")).scalar_one()
        for table in ("inquiries", "inquiry_events", "notifications")
    )


@pytest.fixture()
def published(client_pg, monkeypatch) -> list:
    events: list = []
    monkeypatch.setattr(client_pg.app.state.realtime, "publish", events.append)
    return events


@pytest.fixture()
def tenant(make_tenant) -> str:
    return make_tenant()


@pytest.fixture()
def mine(db, make_incident, make_executable, tenant):
    """고객사 tenant 소속의 실행 · 종료 가능한 인시던트."""
    return _incident(db, make_incident, make_executable, tenant)


def _user(account_type, role, platform_role=None, granted=()) -> auth_service.CurrentUser:
    now = datetime.now(UTC)
    return auth_service.CurrentUser(
        user_id=str(uuid.uuid4()),
        email="u@example.com",
        role=role,
        account_type=account_type,
        tenant_id=str(uuid.uuid4()) if account_type is AccountType.TENANT else None,
        platform_role=platform_role,
        tenant_status=TenantStatus.ACTIVE if account_type is AccountType.TENANT else None,
        session=SessionInfo(absolute_expires_at=now, idle_expires_at=now),
        assigned_tenant_ids=(),
        granted_permissions=frozenset(granted),
    )


# --- 판정 함수 ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("user", "expected"),
    [
        # ADMIN 은 부여분과 무관하게 전부다
        (_user(AccountType.TENANT, UserRole.ADMIN), set(ALL)),
        (_user(AccountType.TENANT, UserRole.OPERATOR, granted=ALL), set(ALL)),
        (_user(AccountType.TENANT, UserRole.OPERATOR, granted=(VIEW, INQUIRY)), {VIEW, INQUIRY}),
        (_user(AccountType.TENANT, UserRole.OPERATOR), set()),
        # VIEWER 는 남은 부여 행이 있어도 없다 — 역할이 근거다
        (_user(AccountType.TENANT, UserRole.VIEWER, granted=ALL), set()),
        (_user(AccountType.PLATFORM, UserRole.ADMIN, PlatformRole.CENTRAL_ADMIN), {VIEW, EXECUTE, RESOLVE}),
        (_user(AccountType.PLATFORM, UserRole.ADMIN, PlatformRole.TENANT_MANAGER), {VIEW, EXECUTE, RESOLVE}),
        # 등급 없는 PLATFORM · 미지정 계정은 닫힌다
        (_user(AccountType.PLATFORM, UserRole.ADMIN), set()),
        (_user(AccountType.UNASSIGNED, UserRole.ADMIN), set()),
    ],
    ids=[
        "tenant-admin", "operator-all", "operator-subset", "operator-none", "viewer-ignores-grants",
        "central", "manager", "platform-without-level", "unassigned",
    ],
)
def test_effective_permissions_reads_account_role_and_grants_only(user, expected):
    assert perms.effective_permissions(user) == frozenset(expected)


# --- GET /auth/me ---------------------------------------------------------------------


def _me_permissions(client) -> list[str]:
    response = client.get("/api/v1/auth/me")
    assert response.status_code == 200, response.text
    return response.json()["permissions"]


@pytest.mark.parametrize(
    ("role", "granted", "expected"),
    [
        (UserRole.ADMIN, None, [p.value for p in ALL]),
        (UserRole.OPERATOR, (INQUIRY, VIEW), [VIEW.value, INQUIRY.value]),  # 입력 순서가 아니라 열거 순서
        (UserRole.OPERATOR, ALL, [p.value for p in ALL]),
        (UserRole.OPERATOR, (), []),
        (UserRole.VIEWER, None, []),
    ],
    ids=["tenant-admin", "operator-subset", "operator-all", "operator-none", "viewer"],
)
def test_me_lists_the_effective_permissions_of_a_tenant_account(
    client_pg, login_as, tenant, role, granted, expected
):
    kwargs = {} if granted is None else {"permissions": granted}
    login_as(role, tenant_id=tenant, **kwargs)
    assert _me_permissions(client_pg) == expected


@pytest.mark.parametrize("platform_role", [PlatformRole.CENTRAL_ADMIN, PlatformRole.TENANT_MANAGER])
def test_me_lists_execute_and_resolve_but_not_inquiry_for_platform_accounts(
    client_pg, login_as, platform_role
):
    login_as(UserRole.ADMIN, platform_role=platform_role)
    assert _me_permissions(client_pg) == PLATFORM_SET


def test_grant_change_is_visible_on_the_next_me_without_relogin(client_pg, db, login_as, tenant):
    seed = login_as(UserRole.OPERATOR, tenant_id=tenant, permissions=())
    assert _me_permissions(client_pg) == []

    auth_repo.replace_feature_permissions(
        db, user_id=seed.user_id, permissions=(VIEW, INQUIRY), now=_now()
    )
    db.commit()
    assert _me_permissions(client_pg) == [VIEW.value, INQUIRY.value]

    auth_repo.replace_feature_permissions(db, user_id=seed.user_id, permissions=(), now=_now())
    db.commit()
    assert _me_permissions(client_pg) == []


# --- 조치 실행 · 인시던트 종료 ----------------------------------------------------------

CALLS = pytest.mark.parametrize(
    ("call", "feature"), [("execute", EXECUTE), ("resolve", RESOLVE)]
)
OK = {"execute": 202, "resolve": 200}


def _others(feature):
    return tuple(p for p in ALL if p is not feature)


@CALLS
@pytest.mark.parametrize("who", ["admin", "operator-all", "operator-only-feature", "operator-feature-and-view"])
def test_allowed_tenant_accounts_run_the_change(
    client_pg, db, login_as, tenant, mine, published, call, feature, who
):
    if who == "admin":
        login_as(UserRole.ADMIN, tenant_id=tenant)
    else:
        granted = {
            "operator-all": ALL,
            "operator-only-feature": (feature,),
            "operator-feature-and-view": (VIEW, feature),
        }[who]
        login_as(UserRole.OPERATOR, tenant_id=tenant, permissions=granted)

    response = _change(client_pg, mine, call)

    assert response.status_code == OK[call], response.text


@CALLS
@pytest.mark.parametrize(
    ("role", "permissions"),
    [
        (UserRole.OPERATOR, "none"),
        (UserRole.OPERATOR, "others"),  # 다른 기능 권한만으로는 열리지 않는다
        (UserRole.VIEWER, None),
    ],
    ids=["operator-none", "operator-other-features", "viewer"],
)
def test_missing_permission_is_403_before_the_target_is_read_and_leaves_no_trace(
    client_pg, db, login_as, tenant, mine, published, monkeypatch, call, feature, role, permissions
):
    kwargs = {}
    if permissions == "none":
        kwargs["permissions"] = ()
    elif permissions == "others":
        kwargs["permissions"] = _others(feature)
    login_as(role, tenant_id=tenant, **kwargs)
    reads = _record_incident_reads(monkeypatch)
    before = _effects(db, mine)

    # 본인 소속 · 없는 ID 모두 대상을 읽기 전에 같은 403 이다
    for target in (mine, SimpleNamespace(incident_id=str(uuid.uuid4()))):
        response = _change(client_pg, target, call)
        assert (response.status_code, _code(response)) == (403, "AUTH_FORBIDDEN")

    assert reads == []
    assert _effects(db, mine) == before
    assert published == []


@CALLS
def test_revoked_grant_after_the_dependency_cannot_authorize_the_write(
    client_pg, db, login_as, tenant, mine, published, monkeypatch, call, feature
):
    """의존성이 통과시킨 뒤 users 잠금 직전에 회수되면 Workflow 가 403 으로 거절하고 실행 · 감사 기록이 없다."""
    seed = login_as(UserRole.OPERATOR, tenant_id=tenant, permissions=ALL)
    revoked: list[str] = []
    real_lock = auth_repo.lock_users_table

    def revoke_then_lock(session):
        if not revoked:
            auth_repo.replace_feature_permissions(
                session, user_id=seed.user_id, permissions=_others(feature), now=_now()
            )
            revoked.append(seed.user_id)
        real_lock(session)

    monkeypatch.setattr(auth_repo, "lock_users_table", revoke_then_lock)
    before = _effects(db, mine)

    response = _change(client_pg, mine, call)

    assert revoked == [seed.user_id]  # 의존성을 통과해 쓰기 직전 잠금까지 왔다
    assert (response.status_code, _code(response)) == (403, "AUTH_FORBIDDEN")
    assert _effects(db, mine) == before
    assert published == []


@CALLS
def test_grant_and_revoke_apply_to_the_next_request_without_relogin(
    client_pg, db, login_as, tenant, mine, published, call, feature
):
    seed = login_as(UserRole.OPERATOR, tenant_id=tenant, permissions=_others(feature))
    assert _change(client_pg, mine, call).status_code == 403

    auth_repo.replace_feature_permissions(
        db, user_id=seed.user_id, permissions=(VIEW, feature), now=_now()
    )
    db.commit()
    assert _change(client_pg, mine, call).status_code == OK[call]

    # 회수 뒤에는 같은 세션으로 다시 403 — 종료된 인시던트라도 권한 거절이 상태 판정보다 먼저다
    auth_repo.replace_feature_permissions(db, user_id=seed.user_id, permissions=(), now=_now())
    db.commit()
    again = _change(client_pg, mine, call)
    assert (again.status_code, _code(again)) == (403, "AUTH_FORBIDDEN")


def test_platform_grades_keep_executing_and_resolving(client_pg, db, login_as, tenant, make_incident, make_executable):
    manager = login_as(
        UserRole.ADMIN, platform_role=PlatformRole.TENANT_MANAGER, assigned_tenant_ids=(tenant,)
    )
    for call in ("execute", "resolve"):
        target = _incident(db, make_incident, make_executable, tenant)
        assert _change(client_pg, target, call).status_code == OK[call], (manager.role, call)


# --- 문의 등록 ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("role", "granted"),
    [
        (UserRole.ADMIN, None),
        (UserRole.OPERATOR, ALL),
        (UserRole.OPERATOR, (INQUIRY,)),
        (UserRole.OPERATOR, (VIEW, INQUIRY)),
    ],
    ids=["admin", "operator-all", "operator-only-inquiry", "operator-view-and-inquiry"],
)
def test_allowed_tenant_accounts_submit_an_inquiry(client_pg, db, login_as, tenant, role, granted):
    kwargs = {} if granted is None else {"permissions": granted}
    seed = login_as(role, tenant_id=tenant, **kwargs)

    response = client_pg.post(INQUIRIES, json=_inquiry_body())

    assert response.status_code == 201, response.text
    assert response.json()["tenant_id"] == tenant
    assert _inquiry_rows(db) == (1, 1, 1)
    assert db.execute(text("SELECT author_user_id::text FROM inquiries")).scalar_one() == seed.user_id


@pytest.mark.parametrize(
    ("role", "granted"),
    [
        (UserRole.OPERATOR, ()),
        (UserRole.OPERATOR, (VIEW, EXECUTE, RESOLVE)),  # 문의 권한만 빠진 경우
        (UserRole.VIEWER, None),
    ],
    ids=["operator-none", "operator-without-inquiry", "viewer"],
)
def test_missing_inquiry_permission_is_403_and_leaves_no_row_event_or_notification(
    client_pg, db, login_as, tenant, role, granted
):
    kwargs = {} if granted is None else {"permissions": granted}
    login_as(role, tenant_id=tenant, **kwargs)
    before = _inquiry_rows(db)

    response = client_pg.post(INQUIRIES, json=_inquiry_body())

    assert (response.status_code, _code(response)) == (403, "AUTH_FORBIDDEN")
    assert _inquiry_rows(db) == before == (0, 0, 0)
    # 잘못된 본문도 권한 판정이 먼저다
    assert client_pg.post(INQUIRIES, json={"bad": 1}).status_code == 403


@pytest.mark.parametrize(
    ("role", "granted", "status"),
    [
        (UserRole.ADMIN, None, 201),
        (UserRole.OPERATOR, (INQUIRY,), 201),
        (UserRole.OPERATOR, (), 403),
        (UserRole.VIEWER, None, 403),
    ],
    ids=["admin", "operator-with-grant", "operator-none", "viewer"],
)
def test_suspended_tenant_inquiry_follows_the_same_permission(
    client_pg, db, login_as, make_tenant, role, granted, status
):
    kwargs = {} if granted is None else {"permissions": granted}
    login_as(role, tenant_id=make_tenant(TenantStatus.SUSPENDED), **kwargs)

    assert client_pg.post(INQUIRIES, json=_inquiry_body()).status_code == status


def test_inquiry_grant_revoked_after_the_dependency_is_403_and_leaves_nothing(
    client_pg, db, login_as, tenant, monkeypatch
):
    seed = login_as(UserRole.OPERATOR, tenant_id=tenant, permissions=ALL)
    revoked: list[str] = []
    real_lock = auth_repo.lock_users_table

    def revoke_then_lock(session):
        if not revoked:
            auth_repo.replace_feature_permissions(
                session, user_id=seed.user_id, permissions=(VIEW,), now=_now()
            )
            revoked.append(seed.user_id)
        real_lock(session)

    monkeypatch.setattr(auth_repo, "lock_users_table", revoke_then_lock)

    response = client_pg.post(INQUIRIES, json=_inquiry_body())

    assert revoked == [seed.user_id]
    assert (response.status_code, _code(response)) == (403, "AUTH_FORBIDDEN")
    assert _inquiry_rows(db) == (0, 0, 0)


# --- 조회 API 는 VIEW 로 막지 않는다 ---------------------------------------------------------


@pytest.mark.parametrize("role", [UserRole.VIEWER, UserRole.OPERATOR])
def test_read_apis_are_not_gated_by_view(client_pg, db, login_as, tenant, mine, role):
    """VIEWER · 권한 없는 OPERATOR 도 자기 고객사의 조회 API 를 그대로 본다(대시보드가 같은 API 를 쓴다)."""
    kwargs = {"permissions": ()} if role is UserRole.OPERATOR else {}
    seed = login_as(role, tenant_id=tenant, **kwargs)
    assert _me_permissions(client_pg) == []

    listed = client_pg.get("/api/v1/incidents")
    assert listed.status_code == 200
    assert [item["incident_id"] for item in listed.json()["items"]] == [mine.incident_id]
    assert client_pg.get(f"/api/v1/incidents/{mine.incident_id}").status_code == 200
    for url in ("/api/v1/assets", "/api/v1/metrics/timeseries", INQUIRIES, "/api/v1/audit-events"):
        response = client_pg.get(url)
        assert response.status_code == 200, (url, response.text)
    assert seed.tenant_id == tenant


@pytest.mark.parametrize("role", [UserRole.VIEWER, UserRole.OPERATOR])
def test_websocket_is_not_gated_by_view(committed_auth, allowed_origin, role):
    tenant_id = committed_auth.make_tenant()
    kwargs = {"permissions": ()} if role is UserRole.OPERATOR else {}
    seed = committed_auth.make(role, tenant_id=tenant_id, **kwargs)
    with TestClient(_app(committed_auth)) as client:
        manager = client.app.state.realtime
        headers = {"Cookie": seed.cookie, "Origin": allowed_origin}
        try:
            with client.websocket_connect(WS, headers=headers):
                assert manager.connection_count == 1
        except WebSocketDisconnect as closed:  # pragma: no cover - 실패 메시지용
            pytest.fail(f"VIEW 없는 {role.value} 의 WebSocket 연결이 닫혔다: {closed.code}")
        _wait_until(lambda: manager.connection_count == 0)


# --- 저장소 ---------------------------------------------------------------------------


def _operator(make_auth_session, tenant_id, **kwargs):
    return make_auth_session(UserRole.OPERATOR, tenant_id=tenant_id, **kwargs)


def test_replace_feature_permissions_overwrites_returns_frozenset_and_bumps_updated_at(
    db, make_auth_session, tenant
):
    seed = _operator(make_auth_session, tenant, permissions=())
    assert auth_repo.list_feature_permissions(db, user_id=seed.user_id) == frozenset()
    row = db.get(models.AppUser, seed.user_id)
    before = row.updated_at
    later = before + timedelta(minutes=5)

    auth_repo.replace_feature_permissions(
        db, user_id=seed.user_id, permissions=(INQUIRY, VIEW), now=later
    )
    db.flush()
    db.refresh(row)

    assert auth_repo.list_feature_permissions(db, user_id=seed.user_id) == frozenset({VIEW, INQUIRY})
    assert row.updated_at == later

    auth_repo.replace_feature_permissions(db, user_id=seed.user_id, permissions=(EXECUTE,), now=later)
    db.flush()
    assert auth_repo.list_feature_permissions(db, user_id=seed.user_id) == frozenset({EXECUTE})

    auth_repo.replace_feature_permissions(db, user_id=seed.user_id, permissions=(), now=later)
    db.flush()
    assert auth_repo.list_feature_permissions(db, user_id=seed.user_id) == frozenset()


def test_permissions_are_per_user(db, make_auth_session, tenant):
    a = _operator(make_auth_session, tenant, permissions=(VIEW,))
    b = _operator(make_auth_session, tenant, permissions=(INQUIRY,))
    auth_repo.replace_feature_permissions(db, user_id=a.user_id, permissions=(), now=_now())
    db.flush()
    assert auth_repo.list_feature_permissions(db, user_id=a.user_id) == frozenset()
    assert auth_repo.list_feature_permissions(db, user_id=b.user_id) == frozenset({INQUIRY})


def test_deleting_a_user_cascades_its_permission_rows(db, make_auth_session, tenant):
    seed = _operator(make_auth_session, tenant, permissions=ALL)
    count = "SELECT count(*) FROM app_user_feature_permissions WHERE user_id = :u"
    assert db.execute(text(count), {"u": seed.user_id}).scalar_one() == len(ALL)

    db.execute(text("DELETE FROM auth_sessions WHERE user_id = :u"), {"u": seed.user_id})
    db.execute(text("DELETE FROM app_users WHERE user_id = :u"), {"u": seed.user_id})

    assert db.execute(text(count), {"u": seed.user_id}).scalar_one() == 0
