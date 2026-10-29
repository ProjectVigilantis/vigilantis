# ==============================================================================
# [파일 설명]
# 연결별 이벤트 권한 판정 — RealtimeManager가 전송 직전에 호출하는 authorize 계약.
#   - 허용된 연결만 받고, 거부 · 판정 실패(fail closed)는 건너뛴다(연결 유지).
#   - 적재 이후 권한이 바뀌면 전송 시점의 판정이 이긴다.
#   - 건너뜀과 전송 실패(stale 제거)는 구분된다.
# 순서는 _broadcast를 직접 await해 결정적으로 만든다(sleep 없음).
# ==============================================================================

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

import pytest
from realtime import RealtimeManager
from routers.ws import _incident_tenant_id
from schemas.api.actions import ExecutionStatus
from realtime import execution_event, incident_event
from schemas.api.ws import WsEventType

T0 = datetime(2026, 8, 19, 10, 0, 0, tzinfo=UTC)


class _Sock:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.sent: list[str] = []
        self.closed = False

    async def accept(self) -> None:
        pass

    async def send_text(self, text: str) -> None:
        if self.fail:
            raise RuntimeError("broken")
        self.sent.append(text)

    async def close(self) -> None:
        self.closed = True


def _incident(incident_id: str):
    return incident_event(WsEventType.INCIDENT_UPDATED, incident_id=incident_id, occurred_at=T0)


def _execution(incident_id: str):
    return execution_event(
        incident_id=incident_id, execution_id="exe-1", status=ExecutionStatus.SUCCESS, updated_at=T0
    )


def _by_incident(owned: dict[str, str | None], scope: set[str] | None):
    """라우터 판정의 축소판 — scope None은 중앙(전체), 아니면 소유 incident의 tenant가 scope 안일 때만."""

    async def authorize(event) -> bool:
        if scope is None:
            return True
        tenant = owned.get(event.data.incident_id)
        return tenant is not None and tenant in scope

    return authorize


def _run(coro):
    return asyncio.run(coro)


def test_manager_receives_only_assigned_incident_events_and_central_receives_all():
    owned = {"inc-a": "A", "inc-b": "B", "inc-null": None}

    async def scenario():
        manager = RealtimeManager(send_timeout_seconds=1.0)
        central, mgr_a, empty = _Sock(), _Sock(), _Sock()
        await manager.register(central, _by_incident(owned, None))
        await manager.register(mgr_a, _by_incident(owned, {"A"}))
        await manager.register(empty, _by_incident(owned, set()))
        for incident_id in ("inc-a", "inc-b", "inc-null", "inc-missing"):
            await manager._broadcast(_incident(incident_id))
        await manager._broadcast(_execution("inc-b"))
        await manager._broadcast(_execution("inc-a"))
        assert len(central.sent) == 6
        assert len(mgr_a.sent) == 2  # inc-a 이벤트 + inc-a 실행 이벤트
        assert all('"inc-a"' in text for text in mgr_a.sent)
        assert empty.sent == []
        assert manager.connection_count == 3  # 건너뜀은 연결을 제거하지 않는다

    _run(scenario())


def test_authorization_revoked_after_enqueue_blocks_delivery():
    async def scenario():
        manager = RealtimeManager(send_timeout_seconds=1.0)
        await manager.start()
        try:
            state = {"allowed": True}

            async def authorize(event) -> bool:
                return state["allowed"]

            sock = _Sock()
            await manager.register(sock, authorize)
            # 소비 task를 돌리지 않고 직접 적재 → 권한 회수 → 소비하는 순서를 고정한다
            queue = manager._queue
            queue.put_nowait(_incident("inc-a"))
            state["allowed"] = False
            await manager._broadcast(queue.get_nowait())
            assert sock.sent == []
        finally:
            await manager.stop()

    _run(scenario())


def test_authorizer_error_fails_closed_without_leaking_detail(caplog):
    async def scenario():
        manager = RealtimeManager(send_timeout_seconds=1.0)

        async def broken(event) -> bool:
            raise RuntimeError("SELECT secret_token_hash FROM sessions")

        sock = _Sock()
        await manager.register(sock, broken)
        with caplog.at_level(logging.WARNING, logger="vigilantis.realtime"):
            await manager._broadcast(_incident("inc-a"))
        assert sock.sent == []
        assert not sock.closed
        assert manager.connection_count == 1
        records = [r for r in caplog.records if r.message == "ws_event_authorization_failed"]
        assert len(records) == 1 and records[0].error_type == "RuntimeError"
        assert "secret_token_hash" not in caplog.text

    _run(scenario())


def test_skipped_event_is_distinct_from_stale_socket():
    async def scenario():
        manager = RealtimeManager(send_timeout_seconds=1.0)
        denied, stale, ok = _Sock(), _Sock(fail=True), _Sock()

        async def deny(event) -> bool:
            return False

        async def allow(event) -> bool:
            return True

        await manager.register(denied, deny)
        await manager.register(stale, allow)
        await manager.register(ok, allow)
        await manager._broadcast(_incident("inc-a"))
        assert denied.sent == [] and not denied.closed
        assert stale.closed
        assert len(ok.sent) == 1
        assert manager.connection_count == 2

    _run(scenario())


def test_incident_tenant_lookup_propagates_db_error():
    """DB 오류는 삼키지 않는다 — 매니저가 fail closed로 받는다."""

    class _Boom:
        def __enter__(self):
            raise RuntimeError("db down")

        def __exit__(self, *exc):
            return False

    try:
        _incident_tenant_id(lambda: _Boom(), "inc-a")
    except RuntimeError:
        return
    raise AssertionError("DB 오류가 전파되어야 한다")

# --- 실제 DB · 라우터에 묶인 authorize (TENANT_MANAGER 담당 범위) ----------------------------
# 순서는 큐 순서로 고정한다: 거부될 이벤트를 먼저, 허용될 이벤트를 나중에 발행하고 첫 수신이
# 허용 이벤트인지 본다. 거부 이벤트가 새면 첫 수신이 달라져 실패한다(sleep 없음).


def _seed_scope(committed_auth, make_incident):
    from schemas.api.auth import PlatformRole

    tenant_a, tenant_b = committed_auth.make_tenant(), committed_auth.make_tenant()
    manager = committed_auth.make(platform_role=PlatformRole.TENANT_MANAGER)
    ids: dict[str, str] = {}
    with committed_auth.sessions() as db:
        for name, tenant_id in (("a", tenant_a), ("b", tenant_b), ("null", None)):
            incident = make_incident(db)
            incident.tenant_id = tenant_id
            ids[name] = incident.incident_id
        db.commit()
    return manager, tenant_a, tenant_b, ids


def _assign(committed_auth, user_id, tenant_ids):
    from datetime import datetime

    from db.repositories import auth as auth_repo

    with committed_auth.sessions() as db:
        auth_repo.replace_assigned_tenants(db, user_id=user_id, tenant_ids=tuple(tenant_ids), now=datetime.now(UTC))
        db.commit()


@pytest.fixture()
def scope_world(committed_auth, make_incident):
    from db import models
    from sqlalchemy import delete

    world = _seed_scope(committed_auth, make_incident)
    manager, _, _, ids = world
    yield world
    with committed_auth.sessions() as db:
        _assign_clear = models.AppUserTenantAssignment
        db.execute(delete(_assign_clear).where(_assign_clear.user_id == manager.user_id))
        db.execute(delete(models.Incident).where(models.Incident.incident_id.in_(list(ids.values()))))
        db.commit()


def _publish_ids(manager, *incident_ids):
    for incident_id in incident_ids:
        manager.publish(_incident(incident_id))


def _next_incident_id(ws) -> str:
    import json

    return json.loads(ws.receive_text())["data"]["incident_id"]


def _drain_until(ws, incident_id: str) -> list[str]:
    seen: list[str] = []
    while True:
        current = _next_incident_id(ws)
        seen.append(current)
        if current == incident_id:
            return seen


def test_bound_authorizer_filters_by_persisted_incident_tenant_and_follows_assignment_changes(
    committed_auth, scope_world
):
    import main
    from auth.access import get_auth_session_factory
    from fastapi.testclient import TestClient

    from schemas.api.auth import PlatformRole

    seed, tenant_a, tenant_b, ids = scope_world
    central = committed_auth.make(platform_role=PlatformRole.CENTRAL_ADMIN)
    _assign(committed_auth, seed.user_id, [tenant_a])
    app = main.create_app()
    app.dependency_overrides[get_auth_session_factory] = lambda: committed_auth.sessions
    with TestClient(app) as client:
        manager = client.app.state.realtime
        with (
            client.websocket_connect("/api/v1/ws", headers={"Cookie": central.cookie}) as central_ws,
            client.websocket_connect("/api/v1/ws", headers={"Cookie": seed.cookie}) as ws,
        ):
            # B · NULL 소유 이벤트는 건너뛰고 A만 받는다 (실행 이벤트도 같은 범위)
            _publish_ids(manager, ids["b"], ids["null"], "inc-nonexistent", ids["a"])
            assert _next_incident_id(ws) == ids["a"]

            # 같은 세션에서 담당을 B로 교체 — 이미 연결된 소켓에 즉시 반영된다
            _assign(committed_auth, seed.user_id, [tenant_b])
            _publish_ids(manager, ids["a"], ids["b"])
            assert _next_incident_id(ws) == ids["b"]

            # 담당을 전부 회수하면 아무것도 받지 못한다. 중앙 연결이 장벽이다: 이벤트 하나의 모든 연결
            # 판정이 끝나야 다음 이벤트로 넘어가므로, 중앙이 장벽 이벤트를 받았다면 그 앞 이벤트의
            # 담당자 판정은 끝났다. 그 뒤 재배정해도 앞 이벤트는 새지 않는다.
            _assign(committed_auth, seed.user_id, [])
            _publish_ids(manager, ids["a"], "inc-barrier")
            central_seen = set(_drain_until(central_ws, "inc-barrier"))
            _assign(committed_auth, seed.user_id, [tenant_b])
            _publish_ids(manager, ids["b"])
            assert _next_incident_id(ws) == ids["b"]

            # 중앙은 B · NULL · 미존재 incident까지 전부 받았다 (레거시 미배정 포함)
            assert {ids["a"], ids["b"], ids["null"], "inc-nonexistent"} <= central_seen