# ==============================================================================
# [파일 설명]
# /api/v1/* 접근 보호 — 실제 PostgreSQL 세션. (#492 PR 2)
#
#   - 경로 전수: 실제 앱의 경로를 열거해 공개 목록 밖은 전부 미인증 401임을 본다. 공개 목록은
#     실제 경로와 정확히 대조한다 — 새 경로가 조용히 공개되거나 목록이 낡지 않게.
#   - 역할: VIEWER 의 조치 실행 · 종료는 403 AUTH_FORBIDDEN 이고 Workflow 에 닿지 않는다(DB 무변화).
#   - 세션: 유휴 · 최대 수명 만료, 회수(LOGOUT · SUPERSEDED), 비활성 사용자는 401.
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
from auth import service as auth_service
from auth import sessions as auth_sessions
from config import AuthSettings, Settings, get_auth_settings, get_settings
from db import models
from db.repositories import auth as auth_repo
from fastapi.routing import iter_route_contexts
from fastapi.testclient import TestClient
from schemas.api.auth import UserRole
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


@pytest.mark.parametrize("role", [UserRole.OPERATOR, UserRole.ADMIN])
def test_operator_and_admin_may_execute(client_pg, login_as, db, make_executable, monkeypatch, role):
    login_as(role)
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


def test_viewer_cannot_execute_and_nothing_changes(client_pg, login_as, db, make_executable, monkeypatch):
    login_as(UserRole.VIEWER)
    incident, candidate = make_executable(db)
    db.commit()
    before = (_execution_count(db), incident.status)
    calls = _spy(monkeypatch, workflows, "reserve_execution")

    response = client_pg.post(EXECUTE, json={
        "incident_id": incident.incident_id,
        "runbook_id": candidate.runbook_id.value,
        "idempotency_key": str(uuid.uuid4()),
    })

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "AUTH_FORBIDDEN"
    assert calls == []
    db.refresh(incident)
    db.refresh(candidate)
    assert (_execution_count(db), incident.status) == before
    assert candidate.status is CandidateStatus.EXECUTABLE


@pytest.mark.parametrize("role", [UserRole.OPERATOR, UserRole.ADMIN])
def test_operator_and_admin_may_resolve(client_pg, login_as, db, resolvable, monkeypatch, role):
    login_as(role)
    calls = _spy(monkeypatch, workflows, "resolve_incident")

    response = client_pg.post(
        f"/api/v1/incidents/{resolvable.incident_id}/resolve", json={"resolution": "NO_FURTHER_ACTION"}
    )

    assert response.status_code == 200
    assert response.json()["status"] == "RESOLVED"
    assert calls == ["resolve_incident"]


def test_viewer_cannot_resolve_and_nothing_changes(client_pg, login_as, db, resolvable, monkeypatch):
    login_as(UserRole.VIEWER)
    status_before = resolvable.status
    calls = _spy(monkeypatch, workflows, "resolve_incident")

    response = client_pg.post(
        f"/api/v1/incidents/{resolvable.incident_id}/resolve", json={"resolution": "NO_FURTHER_ACTION"}
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "AUTH_FORBIDDEN"
    assert calls == []
    db.refresh(resolvable)
    assert resolvable.status == status_before
    assert resolvable.resolved_at is None


def test_viewer_can_read(client_pg, login_as, db, resolvable):
    login_as(UserRole.VIEWER)
    assert client_pg.get("/api/v1/incidents").status_code == 200
    assert client_pg.get(f"/api/v1/incidents/{resolvable.incident_id}").status_code == 200
    assert client_pg.get("/api/v1/assets").status_code == 200


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


@pytest.mark.parametrize("invalid_seed", [
    _idle_expired,
    _absolute_expired,
    _revoked(models.AuthSessionRevokeReason.LOGOUT),
    _revoked(models.AuthSessionRevokeReason.SUPERSEDED),
    _disabled_user,
], ids=["idle", "absolute", "logout", "superseded", "disabled"])
def test_invalid_session_is_401(client_pg_anon, db, make_auth_session, invalid_seed):
    seed = invalid_seed(make_auth_session, db, get_auth_settings())
    db.commit()

    for response in (
        client_pg_anon.get("/api/v1/incidents", headers=seed.headers),
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
    client_pg_anon, db, operator_session, resolvable, monkeypatch, origin
):
    lookups = _spy(monkeypatch, auth_service, "resolve_current_user")
    reserves = _spy(monkeypatch, workflows, "reserve_execution")
    resolves = _spy(monkeypatch, workflows, "resolve_incident")
    before = _execution_count(db)
    headers = {"Cookie": operator_session.cookie}
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


def test_safe_methods_need_no_origin(client_pg_anon, operator_session):
    response = client_pg_anon.get("/api/v1/incidents", headers={"Cookie": operator_session.cookie})
    assert response.status_code == 200


# --- 보안 헤더 -------------------------------------------------------------------------

SECURITY_HEADERS = {
    "cache-control": "no-store",
    "referrer-policy": "strict-origin",
    "x-content-type-options": "nosniff",
}


def test_protected_api_responses_carry_security_headers(client_pg, login_as, resolvable, monkeypatch):
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
    login_as(UserRole.VIEWER)
    responses["403 role"] = client_pg.post(resolve, json=body)

    assert {label: response.status_code for label, response in responses.items()} == {
        "200": 200, "401": 401, "403 origin": 403, "404": 404, "422": 422, "500": 500, "403 role": 403,
    }
    missing = [
        label
        for label, response in responses.items()
        if {name: response.headers.get(name) for name in SECURITY_HEADERS} != SECURITY_HEADERS
    ]
    assert missing == []


# --- (e) 유휴 시간을 늘리지 않는다 -----------------------------------------------------


def test_protected_requests_do_not_extend_idle(client_pg, db, operator_session, resolvable):
    before = _last_activity(db, operator_session.session_id)
    for url in ("/api/v1/incidents", f"/api/v1/incidents/{resolvable.incident_id}", "/api/v1/assets"):
        assert client_pg.get(url).status_code == 200
    resolved = client_pg.post(
        f"/api/v1/incidents/{resolvable.incident_id}/resolve", json={"resolution": "NO_FURTHER_ACTION"}
    )
    assert resolved.status_code == 200
    db.expire_all()
    assert _last_activity(db, operator_session.session_id) == before


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


def test_access_log_carries_user_and_role_but_no_secrets(client_pg, operator_session, capsys):
    capsys.readouterr()
    client_pg.get("/api/v1/incidents")
    client_pg.get("/api/v1/incidents", headers={"Cookie": "__Host-vg_session=forged-value"})
    captured = capsys.readouterr().out

    allowed, rejected = _access_logs(captured)[-2:]
    assert (allowed["status_code"], allowed["user_id"], allowed["role"]) == (
        200, operator_session.user_id, "OPERATOR"
    )
    assert rejected["status_code"] == 401
    assert "user_id" not in rejected and "role" not in rejected
    # 값을 assert 메시지에 싣지 않도록 포함 여부만 계산해 본다
    leaked = [
        name for name, value in (
            ("session token", operator_session.token),
            ("forged cookie", "forged-value"),
            ("cookie name", "__Host-vg_session"),
            ("email", "@example.com"),
        )
        if value in captured
    ]
    assert leaked == []
