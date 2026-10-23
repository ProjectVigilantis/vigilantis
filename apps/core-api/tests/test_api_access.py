# ==============================================================================
# [파일 설명]
# /api/v1/* 접근 보호 — 실제 PostgreSQL 세션. (#492 PR 2)
#
#   - 경로 전수: 실제 앱의 경로를 열거해 공개 목록 밖은 전부 미인증 401임을 본다. 공개 목록은
#     실제 경로와 정확히 대조한다 — 새 경로가 조용히 공개되거나 목록이 낡지 않게.
#   - 역할: VIEWER 의 조치 실행 · 종료는 403 AUTH_FORBIDDEN 이고 Workflow 에 닿지 않는다(DB 무변화).
#   - 계정 종류(#492): 업무 라우터는 임시로 PLATFORM 계정만 받는다 — 고객사(TENANT) 계정은 역할과
#     관계없이 403(경로 전수). 상태 변경은 역할(403) → 소속 테넌트 정지(409 TENANT_SUSPENDED) → 임시 관문(403)
#     순이라, 정지 테넌트의 OPERATOR 는 409 · VIEWER 는 403 으로 역할 판정이 관문에 가려지지 않는다.
#     고객사 계정의 /api/v1/auth · /api/v1/users 는 그대로 열린다.
#   - 세션: 유휴 · 최대 수명 만료, 회수(LOGOUT · SUPERSEDED), 비활성 · UNASSIGNED 사용자는 401.
#   - Origin: 상태 변경 요청은 세션 조회 전에 403 AUTH_ORIGIN_REJECTED.
#   - 보호 API 요청은 유휴 시간을 늘리지 않는다. 접근 로그에 user_id · role 만 남는다.
#   - 세션 토큰은 헤더로만 싣고 assert 메시지에 싣지 않는다.
# ==============================================================================

from __future__ import annotations

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
from schemas.api.auth import AccountType, SessionInfo, UserRole
from schemas.api.errors import ErrorCode
from schemas.api.tenants import TenantStatus
from schemas.candidates import CandidateStatus
from schemas.incidents import AgentInvocationStatus
from sqlalchemy import text
from starlette.websockets import WebSocketDisconnect

EXECUTE = "/api/v1/actions/execute"
FOREIGN_ORIGIN = "https://evil.example.com"

# 세션 없이 열리는 HTTP 경로 전부. 늘리려면 여기와 auth/README.md 정책을 함께 고친다.
PUBLIC_ROUTES = {
    ("GET", "/health"),
    ("POST", "/api/v1/auth/login"),
    ("POST", "/api/v1/auth/login/new-password"),
    ("POST", "/api/v1/auth/login/totp-setup"),
    ("POST", "/api/v1/auth/login/totp-setup/verify"),
    ("POST", "/api/v1/auth/login/totp"),
    ("POST", "/api/v1/auth/login/email-verification"),
    ("POST", "/api/v1/auth/password-reset"),
    ("POST", "/api/v1/auth/password-reset/confirm"),
    # (#492) CLI 발급 1회용 링크로 PLATFORM ADMIN 생성 — 권한 근거는 링크뿐, 기본 꺼짐(404)
    ("POST", "/api/v1/auth/platform-enrollment"),
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


# 상태 변경의 판정 순서 — 역할(VIEWER 403) → 소속 테넌트 정지(409) → 임시 플랫폼 관문(403).
# 정지 테넌트의 OPERATOR · ADMIN 이 409 를 받는 것이 역할 검사를 통과했다는 관측이고, 같은 정지 테넌트의
# VIEWER 가 403 인 것이 역할 검사가 관문에 가려지지 않았다는 관측이다.
@pytest.mark.parametrize(("tenant_status", "role", "expected"), [
    (TenantStatus.ACTIVE, UserRole.ADMIN, (403, "AUTH_FORBIDDEN")),
    (TenantStatus.ACTIVE, UserRole.OPERATOR, (403, "AUTH_FORBIDDEN")),
    (TenantStatus.ACTIVE, UserRole.VIEWER, (403, "AUTH_FORBIDDEN")),
    (TenantStatus.SUSPENDED, UserRole.ADMIN, (409, "TENANT_SUSPENDED")),
    (TenantStatus.SUSPENDED, UserRole.OPERATOR, (409, "TENANT_SUSPENDED")),
    (TenantStatus.SUSPENDED, UserRole.VIEWER, (403, "AUTH_FORBIDDEN")),
])
def test_tenant_accounts_cannot_change_business_state_and_nothing_changes(
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

    assert (reserves, resolves) == ([], [])
    assert _business_state(db, incident, candidate) == before


@pytest.mark.parametrize("tenant_status", [TenantStatus.ACTIVE, TenantStatus.SUSPENDED])
@pytest.mark.parametrize("role", list(UserRole))
def test_tenant_accounts_cannot_read_unisolated_business_data_yet(
    client_pg, login_as, make_tenant, resolvable, tenant_status, role
):
    """정지 테넌트의 「기존 기록 조회」도 아직 열지 않는다 — 업무 조회가 테넌트 범위로 걸러지지 않는다."""
    login_as(role, tenant_id=make_tenant(tenant_status))
    for url in (
        "/api/v1/incidents",
        f"/api/v1/incidents/{resolvable.incident_id}",
        "/api/v1/assets",
        "/api/v1/metrics/timeseries",
    ):
        response = client_pg.get(url)
        assert (response.status_code, response.json()["error"]["code"]) == (403, "AUTH_FORBIDDEN"), url


def test_every_business_route_is_platform_only_while_customer_scoped_routes_stay_open(
    client_pg, login_as, make_tenant
):
    """임시 관문의 경로 전수 — 인증 · 사용자 관리 밖의 모든 세션 경로는 고객사 ADMIN 에게 403 이다.

    새 업무 경로가 관문 없이 붙으면 여기서 걸린다(포함 시점 의존성이 빠진 경우 포함).
    """
    seed = login_as(UserRole.ADMIN, tenant_id=make_tenant())
    http, _ = _routes(client_pg.app)
    business = sorted(
        (method, path)
        for method, path in http - PUBLIC_ROUTES
        if not path.startswith(("/api/v1/auth/", "/api/v1/users"))
    )
    assert {("POST", EXECUTE), ("GET", "/api/v1/assets"), ("GET", "/api/v1/metrics/timeseries")} <= set(business)

    leaks = []
    for method, path in business:
        url = path.replace("{incident_id}", str(uuid.uuid4()))
        response = client_pg.request(method, url, json={} if method == "POST" else None)
        code = response.json().get("error", {}).get("code") if response.content else None
        if (response.status_code, code) != (403, "AUTH_FORBIDDEN"):
            leaks.append((method, path, response.status_code))
    assert leaks == []

    me = client_pg.get("/api/v1/auth/me")
    assert me.status_code == 200
    assert (me.json()["account_type"], me.json()["tenant_id"]) == ("TENANT", seed.tenant_id)
    assert client_pg.get("/api/v1/users").status_code == 200


# --- 판정 함수(임시 관문과 무관한 역할 · 정지 규칙) -----------------------------------------


def _user(account_type, role, tenant_status=None) -> auth_service.CurrentUser:
    now = datetime.now(UTC)
    return auth_service.CurrentUser(
        user_id=str(uuid.uuid4()),
        email="u@example.com",
        role=role,
        account_type=account_type,
        tenant_id=None if account_type is AccountType.PLATFORM else str(uuid.uuid4()),
        tenant_status=tenant_status,
        session=SessionInfo(absolute_expires_at=now, idle_expires_at=now),
    )


@pytest.mark.parametrize(("user", "expected"), [
    (_user(AccountType.PLATFORM, UserRole.ADMIN), None),
    (_user(AccountType.TENANT, UserRole.ADMIN, TenantStatus.ACTIVE), None),
    (_user(AccountType.TENANT, UserRole.OPERATOR, TenantStatus.ACTIVE), None),
    (_user(AccountType.TENANT, UserRole.VIEWER, TenantStatus.ACTIVE), ErrorCode.AUTH_FORBIDDEN),
    (_user(AccountType.TENANT, UserRole.OPERATOR, TenantStatus.SUSPENDED), ErrorCode.TENANT_SUSPENDED),
    (_user(AccountType.TENANT, UserRole.VIEWER, TenantStatus.SUSPENDED), ErrorCode.AUTH_FORBIDDEN),
], ids=["platform", "tenant-admin", "tenant-operator", "tenant-viewer", "suspended-operator", "suspended-viewer"])
def test_action_role_rule_is_role_then_suspension(user, expected):
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
        ("POST", "/api/v1/tenants"): {
            "name": "학원", "aws_account_id": "123456789012", "regions": ["ap-northeast-2"],
        },
        ("PATCH", "/api/v1/tenants/{tenant_id}"): {"name": "x"},
        ("DELETE", "/api/v1/tenants/{tenant_id}"): None,
        ("POST", "/api/v1/tenants/{tenant_id}/suspend"): {"reason": "해지"},
    }


def _send(client, method, path, headers, body):
    url = (
        path.replace("{incident_id}", str(uuid.uuid4()))
        .replace("{user_id}", str(uuid.uuid4()))
        .replace("{tenant_id}", str(uuid.uuid4()))
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
    client_pg, login_as, make_tenant, resolvable, monkeypatch
):
    resolve = f"/api/v1/incidents/{resolvable.incident_id}/resolve"
    body = {"resolution": "NO_FURTHER_ACTION"}
    responses = {
        "200": client_pg.get("/api/v1/incidents"),
        "401": client_pg.get("/api/v1/incidents", headers={"Cookie": "__Host-vg_session=not-a-session"}),
        "403 origin": client_pg.post(resolve, headers={"Origin": FOREIGN_ORIGIN}, json=body),
        "404": client_pg.get(f"/api/v1/incidents/{uuid.uuid4()}"),
        "422": client_pg.post(EXECUTE, json={}),
    }

    def fail(*args, **kwargs):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(workflows, "resolve_incident", fail)
    responses["500"] = client_pg.post(resolve, json=body)
    login_as(UserRole.VIEWER, tenant_id=make_tenant(TenantStatus.SUSPENDED))
    responses["403 role"] = client_pg.post(resolve, json=body)
    login_as(UserRole.OPERATOR, tenant_id=make_tenant(TenantStatus.SUSPENDED))
    responses["409 suspended"] = client_pg.post(resolve, json=body)
    login_as(UserRole.OPERATOR, tenant_id=make_tenant())
    responses["403 platform only"] = client_pg.get("/api/v1/incidents")

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
