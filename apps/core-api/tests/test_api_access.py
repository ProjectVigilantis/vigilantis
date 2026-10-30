# ==============================================================================
# [파일 설명]
# /api/v1/* 접근 보호 — 실제 PostgreSQL 세션. (#492 PR 2)
#
#   - 경로 전수: 실제 앱의 경로를 열거해 공개 목록 밖은 전부 미인증 401임을 본다. 공개 목록은
#     실제 경로와 정확히 대조한다 — 새 경로가 조용히 공개되거나 목록이 낡지 않게.
#   - 역할: VIEWER 의 조치 실행 · 종료는 403 AUTH_FORBIDDEN 이고 Workflow 에 닿지 않는다(DB 무변화).
#   - 업무 범위(#492 · #533): 인시던트 · 조치 · 자산은 요청자의 고객사 범위(auth.access.tenant_scope_of)로 열린다 —
#     CENTRAL_ADMIN 은 전체, TENANT_MANAGER 는 배정 고객사(배정 없음 = 빈 목록 · 404), 고객사(TENANT) 계정은 자기
#     소속. 범위 밖 · 소속 없는 MVP 기록은 목록에서 빠지고 상세 · 실행 · 종료가 404 다. 상태 변경의 역할 검사(403)는
#     대상을 읽기 전이고, 정지는 요청자가 아니라 대상 인시던트의 고객사로 Workflow 가 본다. 메트릭은 플랫폼 관리
#     등급만 받고, 그 밖의 업무 경로도 고객사 계정에는 403 이다(경로 전수). 고객사 계정의 /api/v1/auth ·
#     /api/v1/users 는 그대로 열린다.
#   - 고객사 범위 판정: tenant_scope_ids · can_access_tenant · tenant_scope_of 는 명시 등급 · 배정 · 소속만 읽고
#     닫힌 쪽으로 실패한다.
#   - 세션: 유휴 · 최대 수명 만료, 회수(LOGOUT · SUPERSEDED), 비활성 · UNASSIGNED 사용자는 401.
#   - Origin: 상태 변경 요청은 세션 조회 전에 403 AUTH_ORIGIN_REJECTED.
#   - 보호 API 요청은 유휴 시간을 늘리지 않는다. 접근 로그에 user_id · role 만 남는다.
#   - 세션 토큰은 헤더로만 싣고 assert 메시지에 싣지 않는다.
# ==============================================================================

from __future__ import annotations

import dataclasses
import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
import workflows
from auth import access
from auth import service as auth_service
from auth import sessions as auth_sessions
from config import AuthSettings, Settings, get_auth_settings, get_settings
from db import models
from db.repositories import auth as auth_repo
from exceptions import ApiError
from fastapi.routing import iter_route_contexts
from fastapi.testclient import TestClient
from schemas.api.auth import AccountType, PlatformRole, SessionInfo, UserRole
from schemas.api.errors import ErrorCode
from schemas.api.tenants import TenantStatus
from schemas.candidates import CandidateStatus
from schemas.incidents import AgentInvocationStatus
from sqlalchemy import text
from starlette.websockets import WebSocketDisconnect
from tenancy import ALL_TENANTS, TenantScope

EXECUTE = "/api/v1/actions/execute"
FOREIGN_ORIGIN = "https://evil.example.com"

# 세션 없이 열리는 HTTP 경로 전부. 늘리려면 여기와 auth/README.md 정책을 함께 고친다.
PUBLIC_ROUTES = {
    ("GET", "/health"),
    ("POST", "/api/v1/signup-requests"),
    ("POST", "/api/v1/auth/login"),
    ("POST", "/api/v1/auth/login/new-password"),
    ("POST", "/api/v1/auth/login/totp-setup"),
    ("POST", "/api/v1/auth/login/totp-setup/verify"),
    ("POST", "/api/v1/auth/login/totp"),
    ("POST", "/api/v1/auth/login/email-verification"),
    ("POST", "/api/v1/auth/password-reset"),
    ("POST", "/api/v1/auth/password-reset/confirm"),
    # 멱등 로그아웃(PR 1) — 세션이 없으면 지울 것이 없어 204다. 상태를 바꾸지 않는다
    ("POST", "/api/v1/auth/logout"),
}
WS_ROUTES = {"/api/v1/ws"}


def _routes(app) -> tuple[set[tuple[str, str]], set[str]]:
    """실제 앱의 (메서드, 경로) · WebSocket 경로. 그 밖의 경로 종류가 생기면 여기서 멈춘다."""
    http, ws = set(), set()
    for route in iter_route_contexts(app.routes):
        if route.methods:
            http.update((method, route.path) for method in route.methods)
        else:
            path = route.starlette_route.path
            assert type(route.original_route).__name__ == "APIWebSocketRoute", path
            ws.add(path)
    return http, ws


def _configured_auth(origin: str) -> AuthSettings:
    """Cognito 가 구성된 것처럼 — /me · /activity 가 503 이 아니라 세션 판정까지 간다."""
    return AuthSettings(
        _env_file=None,
        COGNITO_REGION="ap-northeast-2",
        COGNITO_USER_POOL_ID="ap-northeast-2_pool",
        COGNITO_CLIENT_ID="client-id",
        COGNITO_CLIENT_SECRET="client-secret",
        AUTH_ALLOWED_ORIGINS=origin,
    )


def _execution_count(db) -> int:
    return db.execute(text("SELECT count(*) FROM action_executions")).scalar_one()


def _last_activity(db, session_id: str) -> datetime:
    return db.execute(
        text("SELECT last_activity_at FROM auth_sessions WHERE session_id = :s"), {"s": session_id}
    ).scalar_one()


def _spy(monkeypatch, module, name: str) -> list:
    calls: list = []
    real = getattr(module, name)

    def spy(*args, **kwargs):
        calls.append(name)
        return real(*args, **kwargs)

    monkeypatch.setattr(module, name, spy)
    return calls


# --- (a) 경로 전수 ---------------------------------------------------------------------


def test_every_route_outside_public_allowlist_requires_a_session(client_pg_anon, allowed_origin):
    app = client_pg_anon.app
    app.dependency_overrides[get_auth_settings] = lambda: _configured_auth(allowed_origin)
    http, ws = _routes(app)

    assert PUBLIC_ROUTES <= http, "공개 목록에 없는 경로가 남았다 — 목록을 실제 경로와 맞춘다"
    assert ws == WS_ROUTES
    protected = sorted(http - PUBLIC_ROUTES)
    assert ("POST", EXECUTE) in protected

    leaks = []
    for method, path in protected:
        url = path.replace("{incident_id}", str(uuid.uuid4())).replace("{user_id}", str(uuid.uuid4()))
        response = client_pg_anon.request(
            method, url, headers={"Origin": allowed_origin}, json={} if method == "POST" else None
        )
        code = response.json().get("error", {}).get("code") if response.content else None
        if (response.status_code, code) != (401, "AUTH_REQUIRED"):
            leaks.append((method, path, response.status_code))
    assert leaks == []

    for path in sorted(ws):
        with (
            pytest.raises(WebSocketDisconnect) as closed,
            client_pg_anon.websocket_connect(path, headers={"Origin": allowed_origin}),
        ):
            pass
        assert closed.value.code == 1008


# --- (b) 역할 ---------------------------------------------------------------------------


@pytest.fixture()
def resolvable(db, make_incident, make_candidate, seed_summary_lines):
    """종료할 수 있는 승인 대기 인시던트(요약 3줄 · 분석 완료 · 실행 가능 제안 1건).
    commit해 거절 경로의 rollback 과 무관하게 둔다."""
    incident = make_incident(db, summary_lines=seed_summary_lines)
    incident.agent_invocation_status = AgentInvocationStatus.SUCCEEDED
    make_candidate(db, incident)
    db.commit()
    return incident


def test_platform_admin_may_execute(client_pg, db, platform_session, make_executable, monkeypatch):
    assert platform_session.account_type is AccountType.PLATFORM
    incident, candidate = make_executable(db)
    db.commit()
    calls = _spy(monkeypatch, workflows, "reserve_execution")

    response = client_pg.post(EXECUTE, json={
        "incident_id": incident.incident_id,
        "runbook_id": candidate.runbook_id.value,
        "idempotency_key": str(uuid.uuid4()),
    })

    assert response.status_code == 202
    assert calls == ["reserve_execution"]
    db.refresh(candidate)
    assert candidate.status is CandidateStatus.CLAIMED


def test_platform_admin_may_resolve(client_pg, db, resolvable, monkeypatch):
    calls = _spy(monkeypatch, workflows, "resolve_incident")

    response = client_pg.post(
        f"/api/v1/incidents/{resolvable.incident_id}/resolve", json={"resolution": "NO_FURTHER_ACTION"}
    )

    assert response.status_code == 200
    assert response.json()["status"] == "RESOLVED"
    assert calls == ["resolve_incident"]


def test_platform_admin_can_read(client_pg, resolvable):
    assert client_pg.get("/api/v1/incidents").status_code == 200
    assert client_pg.get(f"/api/v1/incidents/{resolvable.incident_id}").status_code == 200
    assert client_pg.get("/api/v1/assets").status_code == 200


def _mutations(incident_id: str, runbook_id: str) -> list[tuple[str, dict]]:
    return [
        (EXECUTE, {
            "incident_id": incident_id,
            "runbook_id": runbook_id,
            "idempotency_key": str(uuid.uuid4()),
        }),
        (f"/api/v1/incidents/{incident_id}/resolve", {"resolution": "NO_FURTHER_ACTION"}),
    ]


def _business_state(db, incident, candidate):
    db.expire_all()
    return (
        _execution_count(db),
        db.get(models.Incident, incident.incident_id).status,
        db.get(models.Incident, incident.incident_id).resolved_at,
        db.get(models.RunbookCandidate, candidate.candidate_id).status,
    )


# 상태 변경의 판정 순서 — 역할(VIEWER 403 · 대상을 읽기 전) → 범위(404). 대상은 소속 없는 MVP 기록이라
# 고객사 계정의 범위 밖이다. 요청자 소속 고객사가 정지돼 있어도 409 가 아니다 — 정지는 요청자가 아니라
# 대상 인시던트의 고객사로 보고, 범위 밖 대상에는 그 판정까지 가지 않는다.
@pytest.mark.parametrize(("tenant_status", "role", "expected"), [
    (TenantStatus.ACTIVE, UserRole.ADMIN, (404, "INCIDENT_NOT_FOUND")),
    (TenantStatus.ACTIVE, UserRole.OPERATOR, (404, "INCIDENT_NOT_FOUND")),
    (TenantStatus.ACTIVE, UserRole.VIEWER, (403, "AUTH_FORBIDDEN")),
    (TenantStatus.SUSPENDED, UserRole.ADMIN, (404, "INCIDENT_NOT_FOUND")),
    (TenantStatus.SUSPENDED, UserRole.OPERATOR, (404, "INCIDENT_NOT_FOUND")),
    (TenantStatus.SUSPENDED, UserRole.VIEWER, (403, "AUTH_FORBIDDEN")),
])
def test_tenant_accounts_cannot_change_unowned_records_and_nothing_changes(
    client_pg, login_as, make_tenant, db, make_executable, seed_summary_lines, monkeypatch,
    tenant_status, role, expected,
):
    login_as(role, tenant_id=make_tenant(tenant_status))
    incident, candidate = make_executable(db)
    incident.summary_lines = list(seed_summary_lines)
    incident.agent_invocation_status = AgentInvocationStatus.SUCCEEDED
    db.commit()
    before = _business_state(db, incident, candidate)
    reserves = _spy(monkeypatch, workflows, "reserve_execution")
    resolves = _spy(monkeypatch, workflows, "resolve_incident")

    for url, body in _mutations(incident.incident_id, candidate.runbook_id.value):
        response = client_pg.post(url, json=body)
        assert (response.status_code, response.json()["error"]["code"]) == expected, url

    if expected[0] == 403:
        assert (reserves, resolves) == ([], [])  # 역할 거절은 Workflow 에 닿지 않는다
    assert _business_state(db, incident, candidate) == before


@pytest.mark.parametrize("tenant_status", [TenantStatus.ACTIVE, TenantStatus.SUSPENDED])
@pytest.mark.parametrize("role", list(UserRole))
def test_tenant_accounts_read_only_their_scope_while_metrics_stay_platform_only(
    client_pg, login_as, make_tenant, resolvable, tenant_status, role
):
    """정지 고객사를 포함한 고객사 계정은 자기 범위를 읽는다. 소속 없는 MVP 기록은 목록에서 빠지고 상세는
    404 다. 메트릭은 플랫폼 관리 등급만 받아 고객사 계정은 403 이다."""
    login_as(role, tenant_id=make_tenant(tenant_status))

    listed = client_pg.get("/api/v1/incidents")
    assert listed.status_code == 200
    assert resolvable.incident_id not in {item["incident_id"] for item in listed.json()["items"]}
    detail = client_pg.get(f"/api/v1/incidents/{resolvable.incident_id}")
    assert (detail.status_code, detail.json()["error"]["code"]) == (404, "INCIDENT_NOT_FOUND")
    assert client_pg.get("/api/v1/assets").status_code == 200
    metrics = client_pg.get("/api/v1/metrics/timeseries")
    assert (metrics.status_code, metrics.json()["error"]["code"]) == (403, "AUTH_FORBIDDEN")


# 고객사 범위로 걸러지는 업무 경로 — 고객사 계정 · 담당자에게도 열린다. 그 밖의 업무 경로는 플랫폼 전용이다.
TENANT_SCOPED_ROUTES = {
    ("GET", "/api/v1/incidents"),
    ("GET", "/api/v1/incidents/{incident_id}"),
    ("POST", "/api/v1/incidents/{incident_id}/resolve"),
    ("POST", EXECUTE),
    ("GET", "/api/v1/assets"),
    ("GET", "/api/v1/audit-events"),
}


def test_business_routes_outside_tenant_scope_stay_platform_only(client_pg, login_as, make_tenant):
    """경로 전수 — 인증 · 사용자 관리와 범위로 걸러지는 업무 경로 밖은 고객사 ADMIN 에게 403 이다.

    새 업무 경로가 범위 판정이나 플랫폼 관문 없이 붙으면 여기서 걸린다(포함 시점 의존성이 빠진 경우 포함).
    범위로 걸러지는 경로는 고객사 계정에게 열리고, 범위 밖(여기서는 없는) 인시던트는 404 다.
    """
    seed = login_as(UserRole.ADMIN, tenant_id=make_tenant())
    http, _ = _routes(client_pg.app)
    business = {
        (method, path)
        for method, path in http - PUBLIC_ROUTES
        if not path.startswith(("/api/v1/auth/", "/api/v1/users"))
    }
    assert TENANT_SCOPED_ROUTES <= business
    assert ("GET", "/api/v1/metrics/timeseries") in business

    bodies = _valid_bodies(str(uuid.uuid4()))
    leaks = []
    for method, path in sorted(business - TENANT_SCOPED_ROUTES):
        response = _send(client_pg, method, path, {}, bodies.get((method, path)))
        if (response.status_code, _error_code(response)) != (403, "AUTH_FORBIDDEN"):
            leaks.append((method, path, response.status_code))
    assert leaks == []

    unknown = str(uuid.uuid4())
    bodies = _valid_bodies(unknown)
    answers = {}
    for method, path in sorted(TENANT_SCOPED_ROUTES):
        response = client_pg.request(
            method, path.replace("{incident_id}", unknown), json=bodies.get((method, path))
        )
        answers[(method, path)] = (response.status_code, _error_code(response))
    assert answers == {
        ("GET", "/api/v1/incidents"): (200, None),
        ("GET", "/api/v1/assets"): (200, None),
        ("GET", "/api/v1/audit-events"): (200, None),
        ("GET", "/api/v1/incidents/{incident_id}"): (404, "INCIDENT_NOT_FOUND"),
        ("POST", "/api/v1/incidents/{incident_id}/resolve"): (404, "INCIDENT_NOT_FOUND"),
        ("POST", EXECUTE): (404, "INCIDENT_NOT_FOUND"),
    }

    me = client_pg.get("/api/v1/auth/me")
    assert me.status_code == 200
    assert (me.json()["account_type"], me.json()["tenant_id"]) == ("TENANT", seed.tenant_id)
    assert client_pg.get("/api/v1/users").status_code == 200


# --- 판정 함수(등급 · 역할 · 고객사 범위 규칙) -----------------------------------------------


def _user(
    account_type, role, tenant_status=None, platform_role=None, assigned_tenant_ids=(), tenant_id=None
) -> auth_service.CurrentUser:
    now = datetime.now(UTC)
    if account_type is AccountType.TENANT and tenant_id is None:
        tenant_id = str(uuid.uuid4())
    return auth_service.CurrentUser(
        user_id=str(uuid.uuid4()),
        email="u@example.com",
        role=role,
        account_type=account_type,
        tenant_id=tenant_id,
        platform_role=platform_role,
        tenant_status=tenant_status,
        session=SessionInfo(absolute_expires_at=now, idle_expires_at=now),
        assigned_tenant_ids=tuple(assigned_tenant_ids),
    )


@pytest.mark.parametrize(("user", "expected"), [
    (_user(AccountType.PLATFORM, UserRole.ADMIN, platform_role=PlatformRole.CENTRAL_ADMIN), None),
    (_user(AccountType.PLATFORM, UserRole.ADMIN, platform_role=PlatformRole.TENANT_MANAGER), None),
    # 호환 값 role=ADMIN 만으로는 플랫폼 조치 권한이 없다 — 명시 등급이 있어야 한다
    (_user(AccountType.PLATFORM, UserRole.ADMIN), ErrorCode.AUTH_FORBIDDEN),
    (_user(AccountType.TENANT, UserRole.ADMIN, TenantStatus.ACTIVE), None),
    (_user(AccountType.TENANT, UserRole.OPERATOR, TenantStatus.ACTIVE), None),
    (_user(AccountType.TENANT, UserRole.VIEWER, TenantStatus.ACTIVE), ErrorCode.AUTH_FORBIDDEN),
    # 정지는 요청자 소속이 아니라 대상 인시던트의 고객사로 Workflow 가 본다 — 여기서는 등급 · 역할만
    (_user(AccountType.TENANT, UserRole.OPERATOR, TenantStatus.SUSPENDED), None),
    (_user(AccountType.TENANT, UserRole.VIEWER, TenantStatus.SUSPENDED), ErrorCode.AUTH_FORBIDDEN),
    (_user(AccountType.UNASSIGNED, UserRole.ADMIN), ErrorCode.AUTH_FORBIDDEN),
], ids=[
    "central", "manager", "platform-without-level", "tenant-admin", "tenant-operator", "tenant-viewer",
    "suspended-operator", "suspended-viewer", "unassigned-admin",
])
def test_action_role_rule_reads_grade_and_role_only(user, expected):
    if expected is None:
        assert access.require_action_role(user) is user
    else:
        with pytest.raises(ApiError) as rejected:
            access.require_action_role(user)
        assert rejected.value.code is expected


@pytest.mark.parametrize(("user", "allowed"), [
    (_user(AccountType.PLATFORM, UserRole.ADMIN), True),
    (_user(AccountType.TENANT, UserRole.ADMIN, TenantStatus.ACTIVE), False),
    (_user(AccountType.TENANT, UserRole.ADMIN, TenantStatus.SUSPENDED), False),
], ids=["platform", "tenant-admin", "suspended-tenant-admin"])
def test_platform_account_rule_reads_account_type_not_role(user, allowed):
    if allowed:
        assert access.require_platform_account(user) is user
    else:
        with pytest.raises(ApiError) as rejected:
            access.require_platform_account(user)
        assert rejected.value.code is ErrorCode.AUTH_FORBIDDEN


@pytest.mark.parametrize(
    ("account_type", "role", "platform_role", "management", "central", "users"),
    [
        (AccountType.PLATFORM, UserRole.ADMIN, PlatformRole.CENTRAL_ADMIN, True, True, True),
        (AccountType.PLATFORM, UserRole.ADMIN, PlatformRole.TENANT_MANAGER, True, False, True),
        (AccountType.PLATFORM, UserRole.ADMIN, None, False, False, False),
        (AccountType.TENANT, UserRole.ADMIN, None, False, False, True),
        (AccountType.TENANT, UserRole.VIEWER, None, False, False, False),
        (AccountType.TENANT, UserRole.VIEWER, PlatformRole.CENTRAL_ADMIN, False, False, False),
        (AccountType.UNASSIGNED, UserRole.ADMIN, PlatformRole.CENTRAL_ADMIN, False, False, False),
    ],
)
def test_management_guards_use_explicit_platform_level(
    account_type, role, platform_role, management, central, users
):
    user = _user(account_type, role, platform_role=platform_role)
    for guard, allowed in (
        (access.require_tenant_management, management),
        (access.require_central_admin, central),
        (access.require_admin, users),
    ):
        if allowed:
            assert guard(user) is user
        else:
            with pytest.raises(ApiError) as rejected:
                guard(user)
            assert rejected.value.code is ErrorCode.AUTH_FORBIDDEN


_T1, _T2, _T3 = sorted(str(uuid.uuid4()) for _ in range(3))


@pytest.mark.parametrize(("user", "scope"), [
    # 전체(None)는 명시 CENTRAL_ADMIN 뿐이다 — 남은 배정 행이 있어도 범위는 전체다
    (_user(AccountType.PLATFORM, UserRole.ADMIN, platform_role=PlatformRole.CENTRAL_ADMIN), None),
    (_user(AccountType.PLATFORM, UserRole.ADMIN, platform_role=PlatformRole.TENANT_MANAGER,
           assigned_tenant_ids=(_T1, _T2)), (_T1, _T2)),
    # 배정 없는 담당자는 고객사가 없다(전체로 읽히지 않는다)
    (_user(AccountType.PLATFORM, UserRole.ADMIN, platform_role=PlatformRole.TENANT_MANAGER), ()),
    # 소속은 자기 고객사 하나 — 배정이 아니다
    (_user(AccountType.TENANT, UserRole.ADMIN, TenantStatus.ACTIVE, tenant_id=_T3), (_T3,)),
    # 등급 없는 PLATFORM · 미지정 · 배정만 있는 비담당자는 닫힌다(role=ADMIN · 소속 NULL 로 추론하지 않는다)
    (_user(AccountType.PLATFORM, UserRole.ADMIN), ()),
    (_user(AccountType.UNASSIGNED, UserRole.ADMIN), ()),
    (_user(AccountType.TENANT, UserRole.ADMIN, TenantStatus.ACTIVE, tenant_id=_T3,
           assigned_tenant_ids=(_T1,)), (_T3,)),
], ids=["central", "manager", "unassigned-manager", "tenant", "platform-without-level", "unassigned",
        "tenant-ignores-assignment-rows"])
def test_tenant_scope_is_explicit_and_fails_closed(user, scope):
    assert access.tenant_scope_ids(user) == scope
    for tenant_id in (_T1, _T2, _T3):
        expected = scope is None or tenant_id in scope
        assert access.can_access_tenant(user, tenant_id) is expected
        # 정규형이 아닌 표기도 같은 고객사로 본다
        assert access.can_access_tenant(user, tenant_id.upper()) is expected
    assert access.can_access_tenant(user, "not-a-uuid") is False


def test_tenant_scope_of_carries_explicit_scope_ids_never_account_type():
    central = _user(AccountType.PLATFORM, UserRole.ADMIN, platform_role=PlatformRole.CENTRAL_ADMIN)
    manager = _user(AccountType.PLATFORM, UserRole.ADMIN, platform_role=PlatformRole.TENANT_MANAGER,
                    assigned_tenant_ids=(_T1, _T2))
    unassigned_manager = _user(AccountType.PLATFORM, UserRole.ADMIN, platform_role=PlatformRole.TENANT_MANAGER)
    customer = _user(AccountType.TENANT, UserRole.OPERATOR, TenantStatus.SUSPENDED, tenant_id=_T3)

    assert access.tenant_scope_of(central) == ALL_TENANTS
    assert access.tenant_scope_of(manager) == TenantScope(tenant_ids=(_T1, _T2))
    assert access.tenant_scope_of(customer) == TenantScope(tenant_ids=(_T3,))
    # 전체는 명시 CENTRAL_ADMIN 뿐 — 담당 없는 담당자 · 등급 없는 PLATFORM · 소속 없는 고객사 계정은 빈 범위다
    for closed in (
        unassigned_manager,
        _user(AccountType.PLATFORM, UserRole.ADMIN),
        dataclasses.replace(customer, tenant_id=None),
    ):
        scope = access.tenant_scope_of(closed)
        assert scope == TenantScope(tenant_ids=())
        assert scope.is_platform is False
        assert not any(scope.covers(tenant_id) for tenant_id in (_T1, _T2, _T3, None))
    # 라우트 의존성은 같은 판정이다
    assert access.require_tenant_scope(manager) == access.tenant_scope_of(manager)


@pytest.mark.parametrize(("user", "allowed"), [
    (_user(AccountType.PLATFORM, UserRole.ADMIN, platform_role=PlatformRole.CENTRAL_ADMIN), True),
    (_user(AccountType.PLATFORM, UserRole.ADMIN, platform_role=PlatformRole.TENANT_MANAGER), True),
    (_user(AccountType.PLATFORM, UserRole.ADMIN), False),
    (_user(AccountType.TENANT, UserRole.ADMIN, TenantStatus.ACTIVE), False),
    (_user(AccountType.TENANT, UserRole.ADMIN, TenantStatus.SUSPENDED), False),
    (_user(AccountType.UNASSIGNED, UserRole.ADMIN), False),
], ids=["central", "unassigned-manager", "platform-without-level", "tenant-admin", "suspended-tenant-admin",
        "unassigned"])
def test_metrics_gate_admits_platform_management_grades_only(user, allowed):
    """메트릭 자격은 등급만 본다(배정 없는 담당자도 통과 — 범위는 라우트가 빈 범위로 건다)."""
    if allowed:
        assert access.require_business_access(user) is user
    else:
        with pytest.raises(ApiError) as rejected:
            access.require_business_access(user)
        assert rejected.value.code is ErrorCode.AUTH_FORBIDDEN


# --- (b2) 담당자 범위 — 배정된 고객사만 ------------------------------------------------


def _scoped_records(db, make_incident, make_candidate, seed_summary_lines, owners: dict) -> dict:
    """키 → (종료 · 실행 가능한 인시던트, EXECUTABLE 후보). owners 는 키 → 소속 고객사(None = 소속 없는 MVP 기록)."""
    records = {}
    for key, tenant_id in owners.items():
        incident = make_incident(db, summary_lines=seed_summary_lines)
        incident.tenant_id = tenant_id
        incident.agent_invocation_status = AgentInvocationStatus.SUCCEEDED
        records[key] = (incident, make_candidate(db, incident))
    db.commit()
    return records


@pytest.mark.parametrize("actor", ["central", "manager", "unassigned-manager", "tenant-viewer"])
def test_incident_reads_follow_explicit_scope(
    client_pg, login_as, make_tenant, db, make_incident, make_candidate, seed_summary_lines, actor
):
    """같은 기록을 행위자마다 다르게 본다 — 전체는 중앙 관리자뿐, 담당자는 배정만, 배정이 없으면 빈 목록 · 404."""
    tenant_a, tenant_b = make_tenant(), make_tenant()
    records = _scoped_records(
        db, make_incident, make_candidate, seed_summary_lines, {"a": tenant_a, "b": tenant_b, "none": None}
    )
    if actor == "manager":
        login_as(UserRole.ADMIN, platform_role=PlatformRole.TENANT_MANAGER, assigned_tenant_ids=(tenant_a,))
    elif actor == "unassigned-manager":
        login_as(UserRole.ADMIN, platform_role=PlatformRole.TENANT_MANAGER)
    elif actor == "tenant-viewer":
        login_as(UserRole.VIEWER, tenant_id=tenant_a)
    visible = {
        "central": {"a", "b", "none"},  # client_pg 기본 세션(CENTRAL_ADMIN)
        "manager": {"a"},
        "unassigned-manager": set(),
        "tenant-viewer": {"a"},
    }[actor]

    listed = client_pg.get("/api/v1/incidents")
    assert listed.status_code == 200
    listed_ids = {item["incident_id"] for item in listed.json()["items"]}
    assert {key for key, (incident, _) in records.items() if incident.incident_id in listed_ids} == visible
    for key, (incident, _) in records.items():
        response = client_pg.get(f"/api/v1/incidents/{incident.incident_id}")
        expected = (200, None) if key in visible else (404, "INCIDENT_NOT_FOUND")
        assert (response.status_code, _error_code(response)) == expected, key
    assert client_pg.get("/api/v1/assets").status_code == 200


@pytest.mark.parametrize("assigned", [True, False], ids=["assigned-manager", "unassigned-manager"])
def test_manager_changes_reach_only_assigned_records(
    client_pg, login_as, make_tenant, db, make_incident, make_candidate, seed_summary_lines, assigned
):
    """담당자의 실행 · 종료는 배정 고객사의 인시던트에만 닿는다. 범위 밖(다른 고객사 · 소속 없음)은 404 이고
    아무것도 바뀌지 않는다. 배정이 없으면 모든 대상이 범위 밖이다."""
    tenant_a, tenant_b = make_tenant(), make_tenant()
    records = _scoped_records(
        db, make_incident, make_candidate, seed_summary_lines,
        {"a-execute": tenant_a, "a-resolve": tenant_a, "b": tenant_b, "none": None},
    )
    login_as(
        UserRole.ADMIN, platform_role=PlatformRole.TENANT_MANAGER,
        assigned_tenant_ids=(tenant_a,) if assigned else (),
    )
    in_scope = {"a-execute", "a-resolve"} if assigned else set()

    for key in sorted(set(records) - in_scope):
        incident, candidate = records[key]
        before = _business_state(db, incident, candidate)
        for url, body in _mutations(incident.incident_id, candidate.runbook_id.value):
            response = client_pg.post(url, json=body)
            assert (response.status_code, _error_code(response)) == (404, "INCIDENT_NOT_FOUND"), (key, url)
        assert _business_state(db, incident, candidate) == before

    if assigned:
        incident, candidate = records["a-execute"]
        execute_url, execute_body = _mutations(incident.incident_id, candidate.runbook_id.value)[0]
        assert client_pg.post(execute_url, json=execute_body).status_code == 202
        incident, candidate = records["a-resolve"]
        resolve_url, resolve_body = _mutations(incident.incident_id, candidate.runbook_id.value)[1]
        resolved = client_pg.post(resolve_url, json=resolve_body)
        assert (resolved.status_code, resolved.json()["status"]) == (200, "RESOLVED")


def test_manager_scope_revocation_applies_to_the_next_request(
    client_pg, login_as, make_tenant, db, make_incident, make_candidate, seed_summary_lines
):
    """배정 회수는 세션을 다시 만들지 않아도 다음 요청부터 범위에서 빠진다(판정마다 DB 를 새로 읽는다)."""
    tenant_a = make_tenant()
    incident, _ = _scoped_records(db, make_incident, make_candidate, seed_summary_lines, {"a": tenant_a})["a"]
    seed = login_as(UserRole.ADMIN, platform_role=PlatformRole.TENANT_MANAGER, assigned_tenant_ids=(tenant_a,))
    detail = f"/api/v1/incidents/{incident.incident_id}"
    assert client_pg.get(detail).status_code == 200

    auth_repo.replace_assigned_tenants(db, user_id=seed.user_id, tenant_ids=(), now=datetime.now(UTC))
    db.commit()

    response = client_pg.get(detail)
    assert (response.status_code, _error_code(response)) == (404, "INCIDENT_NOT_FOUND")
    listed = client_pg.get("/api/v1/incidents")
    assert incident.incident_id not in {item["incident_id"] for item in listed.json()["items"]}


# --- (c) 무효 세션 ----------------------------------------------------------------------


def _idle_expired(make, db, settings):
    # 마지막 활동 + 유휴 한도가 지났다(최대 수명은 남았다)
    return make(now=datetime.now(UTC) - timedelta(seconds=settings.AUTH_SESSION_IDLE_SECONDS + 1))


def _absolute_expired(make, db, settings):
    # 최근 활동이 있어도 최대 수명이 지나면 끝이다 — 활동 시각은 수명 끝 1분 전(유휴 한도 안)
    seed = make(now=datetime.now(UTC) - timedelta(seconds=settings.AUTH_SESSION_MAX_SECONDS + 10))
    db.execute(
        text(
            "UPDATE auth_sessions SET last_activity_at = absolute_expires_at - interval '60 seconds'"
            " WHERE session_id = :s"
        ),
        {"s": seed.session_id},
    )
    return seed


def _revoked(reason):
    def _make(make, db, settings):
        seed = make()
        auth_sessions.revoke_session(db, seed.token, now=datetime.now(UTC), reason=reason)
        return seed

    return _make


def _disabled_user(make, db, settings):
    seed = make()
    auth_repo.set_status(
        db, user_id=seed.user_id, status=models.AppUserStatus.DISABLED, now=datetime.now(UTC)
    )
    return seed


def _unassigned_user(make, db, settings):
    # 소속 · 플랫폼 권한이 정해지지 않은 계정의 기존 세션 — 세션 행은 유효해도 접근할 곳이 없다
    return make(UserRole.ADMIN, account_type=AccountType.UNASSIGNED)


@pytest.mark.parametrize("invalid_seed", [
    _idle_expired,
    _absolute_expired,
    _revoked(models.AuthSessionRevokeReason.LOGOUT),
    _revoked(models.AuthSessionRevokeReason.SUPERSEDED),
    _disabled_user,
    _unassigned_user,
], ids=["idle", "absolute", "logout", "superseded", "disabled", "unassigned"])
def test_invalid_session_is_401(client_pg_anon, db, make_auth_session, invalid_seed):
    seed = invalid_seed(make_auth_session, db, get_auth_settings())
    db.commit()

    for response in (
        client_pg_anon.get("/api/v1/incidents", headers=seed.headers),
        client_pg_anon.get("/api/v1/users", headers=seed.headers),
        client_pg_anon.get("/api/v1/auth/me", headers=seed.headers),
        client_pg_anon.post(EXECUTE, headers=seed.headers, json={
            "incident_id": str(uuid.uuid4()),
            "runbook_id": "RUNBOOK_NACL_ADD_DENY",
            "idempotency_key": str(uuid.uuid4()),
        }),
    ):
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "AUTH_REQUIRED"


def test_unknown_token_is_401(client_pg_anon):
    response = client_pg_anon.get(
        "/api/v1/incidents", headers={"Cookie": "__Host-vg_session=not-a-session"}
    )
    assert response.status_code == 401


# --- (d) Origin ------------------------------------------------------------------------


@pytest.mark.parametrize("origin", [None, FOREIGN_ORIGIN, "null"])
def test_bad_origin_is_rejected_before_session_lookup(
    client_pg_anon, db, platform_session, resolvable, monkeypatch, origin
):
    lookups = _spy(monkeypatch, auth_service, "resolve_current_user")
    reserves = _spy(monkeypatch, workflows, "reserve_execution")
    resolves = _spy(monkeypatch, workflows, "resolve_incident")
    before = _execution_count(db)
    headers = {"Cookie": platform_session.cookie}
    if origin is not None:
        headers["Origin"] = origin

    for url, body in (
        (EXECUTE, {
            "incident_id": resolvable.incident_id,
            "runbook_id": "RUNBOOK_NACL_ADD_DENY",
            "idempotency_key": str(uuid.uuid4()),
        }),
        (f"/api/v1/incidents/{resolvable.incident_id}/resolve", {"resolution": "NO_FURTHER_ACTION"}),
    ):
        response = client_pg_anon.post(url, headers=headers, json=body)
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "AUTH_ORIGIN_REJECTED"

    assert (lookups, reserves, resolves) == ([], [], [])
    assert _execution_count(db) == before
    db.refresh(resolvable)
    assert resolvable.resolved_at is None


SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def _state_changing_protected(app) -> set[tuple[str, str]]:
    """실제 앱의 보호 경로 중 상태 변경 (메서드, 경로) — 공개 목록은 Origin 계약이 따로라 요구하지 않는다."""
    http, _ = _routes(app)
    return {(m, p) for m, p in http - PUBLIC_ROUTES if m not in SAFE_METHODS}


def _valid_bodies(incident_id: str) -> dict[tuple[str, str], dict | None]:
    """_state_changing_protected 의 모든 경로에 대한 유효한 요청 본문 — 문법 · 스키마 오류로 막히지 않게."""
    user = "/api/v1/users/{user_id}"
    return {
        ("POST", EXECUTE): {
            "incident_id": incident_id,
            "runbook_id": "RUNBOOK_NACL_ADD_DENY",
            "idempotency_key": str(uuid.uuid4()),
        },
        ("POST", "/api/v1/incidents/{incident_id}/resolve"): {"resolution": "NO_FURTHER_ACTION"},
        ("POST", "/api/v1/auth/activity"): None,
        ("POST", "/api/v1/users"): {
            "email": "invitee@example.com", "role": "VIEWER", "tenant_id": str(uuid.uuid4()),
            "recover_existing": False,
        },
        ("POST", f"{user}/invite/resend"): None,
        ("PUT", f"{user}/role"): {"role": "VIEWER"},
        ("PUT", f"{user}/status"): {"status": "DISABLED"},
        ("POST", f"{user}/sessions/revoke"): None,
        ("PUT", "/api/v1/staff/{user_id}/role"): {"platform_role": "TENANT_MANAGER"},
        ("PUT", "/api/v1/staff/{user_id}/status"): {"status": "DISABLED"},
        ("PUT", "/api/v1/staff/{user_id}/tenants"): {"tenant_ids": []},
        ("POST", "/api/v1/tenants"): {
            "name": "학원", "aws_account_id": "123456789012", "regions": ["ap-northeast-2"],
        },
        ("PATCH", "/api/v1/tenants/{tenant_id}"): {"name": "x"},
        ("DELETE", "/api/v1/tenants/{tenant_id}"): None,
        ("POST", "/api/v1/tenants/{tenant_id}/suspend"): {"reason": "해지"},
        ("PUT", "/api/v1/signup-requests/{request_id}/status"): {"status": "APPROVED"},
        ("POST", "/api/v1/notifications/read"): {"notification_ids": []},
    }


def _send(client, method, path, headers, body):
    url = (
        path.replace("{incident_id}", str(uuid.uuid4()))
        .replace("{user_id}", str(uuid.uuid4()))
        .replace("{tenant_id}", str(uuid.uuid4()))
        .replace("{request_id}", str(uuid.uuid4()))
    )
    return client.request(method, url, headers=headers, json=body)


def _error_code(response) -> str | None:
    return response.json().get("error", {}).get("code") if response.content else None


def test_origin_bodies_cover_every_state_changing_protected_route(client_pg_anon):
    """본문 표가 실제 상태 변경 보호 경로와 정확히 같다 — 새 경로가 Origin 전수에서 빠지지 않는다."""
    routes = _state_changing_protected(client_pg_anon.app)
    assert set(_valid_bodies("x")) == routes
    assert {("POST", "/api/v1/users"), ("PUT", "/api/v1/users/{user_id}/role")} <= routes


@pytest.mark.parametrize("origin", [None, FOREIGN_ORIGIN, "null"])
def test_bad_origin_is_rejected_on_every_state_changing_protected_route(
    client_pg_anon, platform_session, monkeypatch, origin
):
    """유효한 관리자 세션 · 유효한 본문이어도 Origin 만 틀리면 403 AUTH_ORIGIN_REJECTED 이고 세션 조회 전에 끝난다.

    세션 조회 0회가 다른 403(역할 · 관문) · 401 로 우연히 통과하는 것을 막는다. 문법이 깨진 JSON 은
    프레임워크가 의존성보다 먼저 422 로 거절하는 문서화된 예외라 여기서 다루지 않는다(유효한 JSON 만 보낸다).
    """
    lookups = _spy(monkeypatch, auth_service, "resolve_current_user")
    headers = {"Cookie": platform_session.cookie}
    if origin is not None:
        headers["Origin"] = origin
    bodies = _valid_bodies(str(uuid.uuid4()))

    wrong = []
    for method, path in sorted(_state_changing_protected(client_pg_anon.app)):
        response = _send(client_pg_anon, method, path, headers, bodies[(method, path)])
        if (response.status_code, _error_code(response)) != (403, "AUTH_ORIGIN_REJECTED"):
            wrong.append((method, path, response.status_code))
    assert wrong == []
    assert lookups == []


def test_origin_dependency_is_what_rejects_each_route(client_pg_anon, platform_session, monkeypatch):
    """대조군 — Origin 의존성을 빼면 같은 요청이 세션 판정까지 가고 Origin 거절이 사라진다.

    위 전수 테스트의 403 이 Origin 의존성에서 나온다는 관측이다(다른 거절이 가려 통과시키지 않는다).
    """
    from auth.http import require_origin

    client_pg_anon.app.dependency_overrides[require_origin] = lambda: None
    lookups: list = []

    def stopped_lookup(*args, **kwargs):
        # 세션 조회에서 멈춘다 — 업무 워크플로 · 제공자 호출까지 가지 않는다
        lookups.append(1)
        raise auth_service.AuthRequiredError()

    monkeypatch.setattr(auth_service, "resolve_current_user", stopped_lookup)
    headers = {"Cookie": platform_session.cookie, "Origin": FOREIGN_ORIGIN}
    bodies = _valid_bodies(str(uuid.uuid4()))

    wrong = []
    for method, path in sorted(_state_changing_protected(client_pg_anon.app)):
        before = len(lookups)
        response = _send(client_pg_anon, method, path, headers, bodies[(method, path)])
        if _error_code(response) == "AUTH_ORIGIN_REJECTED" or len(lookups) != before + 1:
            wrong.append((method, path, response.status_code))
    assert wrong == []


def test_safe_methods_need_no_origin(client_pg_anon, platform_session):
    response = client_pg_anon.get("/api/v1/incidents", headers={"Cookie": platform_session.cookie})
    assert response.status_code == 200


# --- 보안 헤더 -------------------------------------------------------------------------

SECURITY_HEADERS = {
    "cache-control": "no-store",
    "referrer-policy": "strict-origin",
    "x-content-type-options": "nosniff",
}


def test_protected_api_responses_carry_security_headers(
    client_pg, db, login_as, make_tenant, make_executable, resolvable, monkeypatch
):
    resolve = f"/api/v1/incidents/{resolvable.incident_id}/resolve"
    body = {"resolution": "NO_FURTHER_ACTION"}
    # 정지된 고객사의 인시던트 — 새 실행은 중앙 관리자도 409 다
    suspended, candidate = make_executable(db)
    suspended.tenant_id = make_tenant(TenantStatus.SUSPENDED)
    db.commit()
    responses = {
        "200": client_pg.get("/api/v1/incidents"),
        "401": client_pg.get("/api/v1/incidents", headers={"Cookie": "__Host-vg_session=not-a-session"}),
        "403 origin": client_pg.post(resolve, headers={"Origin": FOREIGN_ORIGIN}, json=body),
        "404": client_pg.get(f"/api/v1/incidents/{uuid.uuid4()}"),
        "409 suspended": client_pg.post(EXECUTE, json={
            "incident_id": suspended.incident_id,
            "runbook_id": candidate.runbook_id.value,
            "idempotency_key": str(uuid.uuid4()),
        }),
        "422": client_pg.post(EXECUTE, json={}),
    }

    def fail(*args, **kwargs):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(workflows, "resolve_incident", fail)
    responses["500"] = client_pg.post(resolve, json=body)
    login_as(UserRole.VIEWER, tenant_id=make_tenant(TenantStatus.SUSPENDED))
    responses["403 role"] = client_pg.post(resolve, json=body)
    login_as(UserRole.OPERATOR, tenant_id=make_tenant())
    responses["403 platform only"] = client_pg.get("/api/v1/metrics/timeseries")

    assert {label: response.status_code for label, response in responses.items()} == {
        "200": 200, "401": 401, "403 origin": 403, "404": 404, "422": 422, "500": 500, "403 role": 403,
        "409 suspended": 409, "403 platform only": 403,
    }
    missing = [
        label
        for label, response in responses.items()
        if {name: response.headers.get(name) for name in SECURITY_HEADERS} != SECURITY_HEADERS
    ]
    assert missing == []


# --- (e) 유휴 시간을 늘리지 않는다 -----------------------------------------------------


def test_protected_requests_do_not_extend_idle(client_pg, db, platform_session, resolvable):
    before = _last_activity(db, platform_session.session_id)
    for url in ("/api/v1/incidents", f"/api/v1/incidents/{resolvable.incident_id}", "/api/v1/assets"):
        assert client_pg.get(url).status_code == 200
    resolved = client_pg.post(
        f"/api/v1/incidents/{resolvable.incident_id}/resolve", json={"resolution": "NO_FURTHER_ACTION"}
    )
    assert resolved.status_code == 200
    db.expire_all()
    assert _last_activity(db, platform_session.session_id) == before


# --- (g) API 문서 ------------------------------------------------------------------------


def test_api_docs_are_off_by_default():
    assert Settings(_env_file=None, DATABASE_URL="postgresql://x").API_DOCS_ENABLED is False


@pytest.mark.parametrize(("enabled", "status"), [("false", 404), ("true", 200)])
def test_api_docs_toggle(monkeypatch, enabled, status):
    import main

    monkeypatch.setenv("API_DOCS_ENABLED", enabled)
    get_settings.cache_clear()
    try:
        with TestClient(main.create_app()) as client:
            for path in ("/docs", "/redoc", "/openapi.json"):
                assert client.get(path).status_code == status, path
    finally:
        get_settings.cache_clear()


# --- (h) 접근 로그 ------------------------------------------------------------------------


def _access_logs(captured: str) -> list[dict]:
    entries = []
    for line in captured.splitlines():
        try:
            parsed = json.loads(line)
        except ValueError:
            continue
        if parsed.get("event") == "http_request":
            entries.append(parsed)
    return entries


def test_access_log_carries_user_and_role_but_no_secrets(client_pg, platform_session, capsys):
    capsys.readouterr()
    client_pg.get("/api/v1/incidents")
    client_pg.get("/api/v1/incidents", headers={"Cookie": "__Host-vg_session=forged-value"})
    captured = capsys.readouterr().out

    allowed, rejected = _access_logs(captured)[-2:]
    assert (allowed["status_code"], allowed["user_id"], allowed["role"]) == (
        200, platform_session.user_id, "ADMIN"
    )
    assert rejected["status_code"] == 401
    assert "user_id" not in rejected and "role" not in rejected
    # 값을 assert 메시지에 싣지 않도록 포함 여부만 계산해 본다
    leaked = [
        name for name, value in (
            ("session token", platform_session.token),
            ("forged cookie", "forged-value"),
            ("cookie name", "__Host-vg_session"),
            ("email", "@example.com"),
        )
        if value in captured
    ]
    assert leaked == []
