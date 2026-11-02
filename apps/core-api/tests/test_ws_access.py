# ==============================================================================
# [파일 설명]
# WebSocket /api/v1/ws 세션 보호 — 실제 PostgreSQL(commit된 세션). (#492 PR 2)
#
#   - 연결 수립: 세션 없음 · 무효 세션 · 허용 밖 Origin 은 accept 전에 1008. Origin 목록은 HTTP
#     상태 변경 검사와 같다(AUTH_ALLOWED_ORIGINS, 비면 CORS_ALLOW_ORIGINS).
#   - 계정 종류와 무관하게 유효한 세션이면 연결된다(#535). 고객사(TENANT) 계정은 자기 고객 Incident 의
#     이벤트만 받는다 — 다른 고객 · tenant_id NULL · 없는 Incident 는 건너뛴다.
#   - 연결 중: 감시가 짧은 주기로 다시 판정해 로그아웃 · 재로그인 회수 · 사용자 비활성 · 유휴 만료를
#     4401 "AUTH_REQUIRED" 로 닫는다. 주기가 길어도 유휴 만료 시각에는 그 시각에 닫는다.
#   - 연결 · 재확인은 last_activity_at 을 바꾸지 않는다.
#   - 세션 확인은 다른 스레드의 자기 DB 세션에서 돈다 — 그래서 시드를 실제로 commit한다(committed_auth).
# ==============================================================================

from __future__ import annotations

import json
import time
import uuid
from datetime import UTC, datetime, timedelta

import main
import pytest
import routers.ws as ws_router
from auth import sessions as auth_sessions
from auth.access import get_auth_session_factory
from auth.http import get_clock
from config import get_auth_settings, get_settings
from db import models
from db.repositories import auth as auth_repo
from fastapi.testclient import TestClient
from realtime import incident_event
from schemas.api.auth import AccountType, UserRole
from schemas.api.ws import WS_CLOSE_AUTH_REQUIRED, WsEventType
from sqlalchemy import delete, text
from starlette.websockets import WebSocketDisconnect

WS = "/api/v1/ws"
T0 = datetime(2026, 10, 20, 9, 0, tzinfo=UTC)


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


def _app(
    committed_auth,
    *,
    interval: float = 0.05,
    clock: Clock | None = None,
    cors_origins: str | None = None,
    auth_origins: str | None = None,
):
    get_settings.cache_clear()
    app = main.create_app()
    update = {"WS_SESSION_CHECK_SECONDS": interval}
    if cors_origins is not None:
        update["CORS_ALLOW_ORIGINS"] = cors_origins
    settings = get_settings().model_copy(update=update)
    app.dependency_overrides[get_settings] = lambda: settings
    if auth_origins is not None:
        auth_settings = get_auth_settings().model_copy(update={"AUTH_ALLOWED_ORIGINS": auth_origins})
        app.dependency_overrides[get_auth_settings] = lambda: auth_settings
    app.dependency_overrides[get_auth_session_factory] = lambda: committed_auth.sessions
    if clock is not None:
        app.dependency_overrides[get_clock] = lambda: clock
    return app


def _wait_until(condition, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.02)
    raise AssertionError("조건이 제한 시간 안에 성립하지 않았다")


def _assert_closed_auth_required(ws, manager) -> None:
    # 감시가 닫으면 연결 정리(unregister)까지 끝난다 — 그 뒤 받는 첫 메시지가 닫기다
    _wait_until(lambda: manager.connection_count == 0)
    with pytest.raises(WebSocketDisconnect) as closed:
        ws.receive_text()
    assert (closed.value.code, closed.value.reason) == (WS_CLOSE_AUTH_REQUIRED, "AUTH_REQUIRED")


def _last_activity(committed_auth, session_id: str) -> datetime:
    with committed_auth.sessions() as session:
        return session.execute(
            text("SELECT last_activity_at FROM auth_sessions WHERE session_id = :s"),
            {"s": session_id},
        ).scalar_one()


# --- 연결 수립 --------------------------------------------------------------------------


def _revoke_now(committed_auth, seed):
    with committed_auth.sessions() as session:
        auth_sessions.revoke_session(session, seed.token, now=datetime.now(UTC))
        session.commit()


def _disable_now(committed_auth, seed):
    with committed_auth.sessions() as session:
        auth_repo.set_status(
            session, user_id=seed.user_id, status=models.AppUserStatus.DISABLED, now=datetime.now(UTC)
        )
        session.commit()


@pytest.mark.parametrize("cookie", ["none", "unknown", "revoked", "disabled", "unassigned"])
def test_handshake_without_valid_session_is_rejected(committed_auth, cookie):
    # UNASSIGNED 계정은 세션이 있어도 세션 판정 자체가 거절한다(무효 세션과 같다) — 계정 종류 관문이 아니다
    if cookie == "unassigned":
        seed = committed_auth.make(account_type=AccountType.UNASSIGNED)
    else:
        seed = committed_auth.make()
    if cookie == "revoked":
        _revoke_now(committed_auth, seed)
    elif cookie == "disabled":
        _disable_now(committed_auth, seed)
    headers = {
        "none": {},
        "unknown": {"Cookie": "__Host-vg_session=not-a-session"},
        "revoked": {"Cookie": seed.cookie},
        "disabled": {"Cookie": seed.cookie},
        "unassigned": {"Cookie": seed.cookie},
    }[cookie]

    with TestClient(_app(committed_auth)) as client:
        with pytest.raises(WebSocketDisconnect) as closed, client.websocket_connect(WS, headers=headers):
            pass
        assert closed.value.code == 1008
        assert client.app.state.realtime.connection_count == 0


@pytest.fixture()
def tenant_incidents(committed_auth, make_incident):
    """고객 A · B 의 Incident 와 tenant_id NULL(MVP 기록) Incident 를 commit한다 → (tenant_a, tenant_b, ids)."""
    tenant_a, tenant_b = committed_auth.make_tenant(), committed_auth.make_tenant()
    ids: dict[str, str] = {}
    with committed_auth.sessions() as session:
        for name, tenant_id in (("a", tenant_a), ("b", tenant_b), ("null", None)):
            incident = make_incident(session)
            incident.tenant_id = tenant_id
            ids[name] = incident.incident_id
        session.commit()
    yield tenant_a, tenant_b, ids
    with committed_auth.sessions() as session:
        session.execute(delete(models.Incident).where(models.Incident.incident_id.in_(list(ids.values()))))
        session.commit()


def _next_incident_id(ws) -> str:
    return json.loads(ws.receive_text())["data"]["incident_id"]


@pytest.mark.parametrize("role", [UserRole.ADMIN, UserRole.VIEWER])
def test_tenant_account_connects_and_receives_only_its_own_tenant_events(
    committed_auth, allowed_origin, tenant_incidents, role
):
    """고객사 계정도 연결된다(#535). 다른 고객 · tenant_id NULL · 없는 Incident 이벤트는 건너뛰고 자기 고객 것만 받는다."""
    tenant_a, _tenant_b, ids = tenant_incidents
    seed = committed_auth.make(role, tenant_id=tenant_a)
    with TestClient(_app(committed_auth)) as client:
        manager = client.app.state.realtime
        with client.websocket_connect(WS, headers={"Cookie": seed.cookie, "Origin": allowed_origin}) as ws:
            assert manager.connection_count == 1
            # 거부될 이벤트를 먼저 발행한다 — 새면 첫 수신이 달라져 실패한다(큐 순서)
            for incident_id in (ids["b"], ids["null"], "inc-nonexistent", ids["a"]):
                manager.publish(incident_event(WsEventType.INCIDENT_UPDATED, incident_id=incident_id, occurred_at=T0))
            assert _next_incident_id(ws) == ids["a"]
        _wait_until(lambda: manager.connection_count == 0)


def test_tenant_ws_client_fixture_connects_and_receives_own_tenant_event(
    committed_auth, make_tenant_ws_client, tenant_incidents
):
    tenant_a, _tenant_b, ids = tenant_incidents
    client, _seed = make_tenant_ws_client(tenant_a)
    manager = client.app.state.realtime
    with client.websocket_connect(WS) as ws:
        manager.publish(incident_event(WsEventType.INCIDENT_UPDATED, incident_id=ids["b"], occurred_at=T0))
        manager.publish(incident_event(WsEventType.INCIDENT_UPDATED, incident_id=ids["a"], occurred_at=T0))
        assert _next_incident_id(ws) == ids["a"]


def test_handshake_with_foreign_origin_is_rejected_even_with_a_session(committed_auth):
    seed = committed_auth.make()
    headers = {"Cookie": seed.cookie, "Origin": "https://evil.example.com"}
    with TestClient(_app(committed_auth)) as client:
        with pytest.raises(WebSocketDisconnect) as closed, client.websocket_connect(WS, headers=headers):
            pass
        assert closed.value.code == 1008


def test_handshake_origin_uses_the_state_change_origin_list(committed_auth):
    """AUTH_ALLOWED_ORIGINS 가 CORS_ALLOW_ORIGINS 보다 좁으면 CORS 에만 있는 Origin 은 WebSocket 도 거절한다."""
    app_origin, cors_only = "https://app.example.com", "https://cors-only.example.com"
    seed = committed_auth.make()
    app = _app(committed_auth, cors_origins=f"{app_origin},{cors_only}", auth_origins=app_origin)
    with TestClient(app) as client:
        manager = client.app.state.realtime
        with (
            pytest.raises(WebSocketDisconnect) as closed,
            client.websocket_connect(WS, headers={"Cookie": seed.cookie, "Origin": cors_only}),
        ):
            pass
        assert closed.value.code == 1008
        # HTTP 상태 변경 검사도 같은 목록으로 같은 Origin 을 거절한다(세션 조회 전)
        rejected = client.post(
            f"/api/v1/incidents/{uuid.uuid4()}/resolve",
            headers={"Cookie": seed.cookie, "Origin": cors_only},
            json={"resolution": "NO_FURTHER_ACTION"},
        )
        assert (rejected.status_code, rejected.json()["error"]["code"]) == (403, "AUTH_ORIGIN_REJECTED")

        with client.websocket_connect(WS, headers={"Cookie": seed.cookie, "Origin": app_origin}):
            assert manager.connection_count == 1
        _wait_until(lambda: manager.connection_count == 0)


def test_valid_session_connects_and_receives(committed_auth, allowed_origin):
    seed = committed_auth.make()
    with TestClient(_app(committed_auth)) as client:
        manager = client.app.state.realtime
        with client.websocket_connect(WS, headers={"Cookie": seed.cookie, "Origin": allowed_origin}) as ws:
            manager.publish(incident_event(WsEventType.INCIDENT_UPDATED, incident_id="inc-ok", occurred_at=T0))
            assert json.loads(ws.receive_text())["data"] == {"incident_id": "inc-ok"}
            assert manager.connection_count == 1
        _wait_until(lambda: manager.connection_count == 0)


# --- 연결 중 재확인 ----------------------------------------------------------------------


@pytest.mark.parametrize("change", ["LOGOUT", "SUPERSEDED", "DISABLED"])
def test_revocation_while_connected_closes_4401(committed_auth, change):
    seed = committed_auth.make()
    with TestClient(_app(committed_auth, interval=0.05)) as client:
        manager = client.app.state.realtime
        with client.websocket_connect(WS, headers={"Cookie": seed.cookie}) as ws:
            assert manager.connection_count == 1
            with committed_auth.sessions() as session:
                if change == "DISABLED":
                    auth_repo.set_status(
                        session,
                        user_id=seed.user_id,
                        status=models.AppUserStatus.DISABLED,
                        now=datetime.now(UTC),
                    )
                else:
                    auth_sessions.revoke_session(
                        session,
                        seed.token,
                        now=datetime.now(UTC),
                        reason=models.AuthSessionRevokeReason(change),
                    )
                session.commit()
            _assert_closed_auth_required(ws, manager)


def test_idle_expiry_while_connected_closes_4401(committed_auth):
    seed = committed_auth.make(now=T0)
    clock = Clock(T0)
    with TestClient(_app(committed_auth, interval=0.05, clock=clock)) as client:
        manager = client.app.state.realtime
        with client.websocket_connect(WS, headers={"Cookie": seed.cookie}) as ws:
            assert manager.connection_count == 1
            clock.now = T0 + timedelta(seconds=get_auth_settings().AUTH_SESSION_IDLE_SECONDS)
            _assert_closed_auth_required(ws, manager)


def test_expiry_is_checked_at_the_expiry_time_not_the_next_interval(committed_auth):
    """주기(1시간)보다 유휴 만료(약 1초 뒤)가 먼저면 그 시각에 다시 판정해 닫는다."""
    idle = get_auth_settings().AUTH_SESSION_IDLE_SECONDS
    seed = committed_auth.make(now=datetime.now(UTC) - timedelta(seconds=idle - 1))
    with TestClient(_app(committed_auth, interval=3600)) as client:
        manager = client.app.state.realtime
        with client.websocket_connect(WS, headers={"Cookie": seed.cookie}) as ws:
            _assert_closed_auth_required(ws, manager)


def test_connection_and_rechecks_never_extend_idle(committed_auth, monkeypatch):
    checks = []
    real = ws_router.resolve_session_user

    def counting(*args, **kwargs):
        checks.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(ws_router, "resolve_session_user", counting)
    seed = committed_auth.make()
    before = _last_activity(committed_auth, seed.session_id)
    with TestClient(_app(committed_auth, interval=0.05)) as client:
        manager = client.app.state.realtime
        with client.websocket_connect(WS, headers={"Cookie": seed.cookie}) as ws:
            _wait_until(lambda: len(checks) >= 4)  # 연결 1회 + 재확인 3회 이상
            manager.publish(incident_event(WsEventType.INCIDENT_UPDATED, incident_id="inc-alive", occurred_at=T0))
            assert json.loads(ws.receive_text())["data"] == {"incident_id": "inc-alive"}
            assert _last_activity(committed_auth, seed.session_id) == before


def test_failed_recheck_closes_1011_without_session_data(committed_auth, monkeypatch, capsys):
    real = ws_router.resolve_session_user
    calls = []

    def failing_after_handshake(*args, **kwargs):
        calls.append(1)
        if len(calls) > 1:
            raise RuntimeError("db down")
        return real(*args, **kwargs)

    monkeypatch.setattr(ws_router, "resolve_session_user", failing_after_handshake)
    seed = committed_auth.make()
    with TestClient(_app(committed_auth, interval=0.05)) as client:
        manager = client.app.state.realtime
        with client.websocket_connect(WS, headers={"Cookie": seed.cookie}) as ws:
            _wait_until(lambda: manager.connection_count == 0)
            with pytest.raises(WebSocketDisconnect) as closed:
                ws.receive_text()
    assert closed.value.code == 1011
    # create_app 이 루트 로그 핸들러를 갈아끼우므로 stdout JSON 줄로 본다
    captured = capsys.readouterr().out
    failures = [
        json.loads(line) for line in captured.splitlines() if '"ws_session_check_failed"' in line
    ]
    assert [entry["error_type"] for entry in failures] == ["RuntimeError"]
    leaked = seed.token in captured or "db down" in captured
    assert not leaked
