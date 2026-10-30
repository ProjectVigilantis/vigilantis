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


def _by_incident(owned: dict[str, str | None], scope: set[str] | None, calls: list[str] | None = None):
    """라우터 판정의 축소판 — scope None은 중앙(전체), 아니면 소유 incident의 tenant가 scope 안일 때만."""

    async def authorize(event, owner_lookup) -> bool:
        if scope is None:
            return True
        tenant = await owner_lookup.get(_resolver(owned, event.data.incident_id, calls))
        return tenant is not None and tenant in scope

    return authorize


def _resolver(owned, incident_id, calls):
    async def resolve():
        if calls is not None:
            calls.append(incident_id)
        return owned.get(incident_id)

    return resolve


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

            async def authorize(event, owner_lookup) -> bool:
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

        async def broken(event, owner_lookup) -> bool:
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

        async def deny(event, owner_lookup) -> bool:
            return False

        async def allow(event, owner_lookup) -> bool:
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


def test_managers_of_one_broadcast_share_one_owner_lookup_with_correct_filtering():
    owned = {"inc-a": "A"}
    calls: list[str] = []

    async def scenario():
        manager = RealtimeManager(send_timeout_seconds=1.0)
        central, mgr_a, mgr_b, empty = _Sock(), _Sock(), _Sock(), _Sock()
        await manager.register(central, _by_incident(owned, None, calls))
        await manager.register(mgr_a, _by_incident(owned, {"A"}, calls))
        await manager.register(mgr_b, _by_incident(owned, {"B"}, calls))
        await manager.register(empty, _by_incident(owned, set(), calls))
        await manager._broadcast(_incident("inc-a"))
        assert calls == ["inc-a"]
        assert len(central.sent) == 1 and len(mgr_a.sent) == 1
        assert mgr_b.sent == [] and empty.sent == []
        # 다음 발행은 같은 incident여도 새로 조회한다 — 이벤트 간 캐시가 없다
        await manager._broadcast(_incident("inc-a"))
        assert calls == ["inc-a", "inc-a"]
        assert len(mgr_a.sent) == 2

    _run(scenario())


def test_central_only_broadcast_does_no_owner_lookup():
    calls: list[str] = []

    async def scenario():
        manager = RealtimeManager(send_timeout_seconds=1.0)
        central = _Sock()
        await manager.register(central, _by_incident({}, None, calls))
        await manager._broadcast(_incident("inc-a"))
        assert len(central.sent) == 1
        assert calls == []

    _run(scenario())


@pytest.mark.parametrize("failure", ["raise", "none", "missing"])
def test_owner_lookup_failure_fails_closed_for_managers_but_central_receives(failure, caplog):
    owned = {} if failure == "missing" else {"inc-a": None}
    calls: list[str] = []

    async def scenario():
        manager = RealtimeManager(send_timeout_seconds=1.0)
        central, mgr_a, mgr_b = _Sock(), _Sock(), _Sock()

        async def boom():
            calls.append("inc-a")
            raise RuntimeError("SELECT secret_token_hash FROM incidents")

        def tenant_authorizer(scope):
            async def authorize(event, owner_lookup) -> bool:
                resolver = boom if failure == "raise" else _resolver(owned, event.data.incident_id, calls)
                tenant = await owner_lookup.get(resolver)
                return tenant is not None and tenant in scope

            return authorize

        await manager.register(central, _by_incident(owned, None, calls))
        await manager.register(mgr_a, tenant_authorizer({"A"}))
        await manager.register(mgr_b, tenant_authorizer({"B"}))
        with caplog.at_level(logging.WARNING, logger="vigilantis.realtime"):
            await manager._broadcast(_incident("inc-a"))
        assert calls == ["inc-a"]
        assert len(central.sent) == 1
        assert mgr_a.sent == [] and mgr_b.sent == []
        assert manager.connection_count == 3
        assert "secret_token_hash" not in caplog.text

    _run(scenario())


def test_cancelled_or_timed_out_waiter_does_not_cancel_shared_lookup():
    from realtime import IncidentOwnerLookup

    async def scenario():
        lookup = IncidentOwnerLookup(1.0)
        started, release = asyncio.Event(), asyncio.Event()
        calls: list[int] = []

        async def resolve():
            calls.append(1)
            started.set()
            await release.wait()
            return "A"

        cancelled = asyncio.create_task(lookup.get(resolve))
        await started.wait()
        timed_out = asyncio.create_task(asyncio.wait_for(lookup.get(resolve), timeout=0.01))
        remaining = asyncio.create_task(lookup.get(resolve))
        with pytest.raises(asyncio.TimeoutError):
            await timed_out
        cancelled.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cancelled
        assert not remaining.done()  # 공유 조회가 아직 진행 중이다
        release.set()
        assert await remaining == "A"
        assert calls == [1]
        await lookup.close()

    _run(scenario())


def test_close_cancels_pending_lookup_and_abandoned_failure_is_retrieved():
    import gc
    import weakref

    from realtime import IncidentOwnerLookup

    async def scenario():
        loop = asyncio.get_running_loop()
        reported: list[dict] = []
        loop.set_exception_handler(lambda _loop, context: reported.append(context))

        # 1) 끝나지 않은 조회는 close()가 취소하고 끝날 때까지 기다린다
        pending = IncidentOwnerLookup(1.0)
        started = asyncio.Event()
        cancelled: list[bool] = []

        async def hang():
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.append(True)
                raise

        waiter = asyncio.create_task(pending.get(hang))
        await started.wait()
        await pending.close()
        assert cancelled == [True]
        with pytest.raises(asyncio.CancelledError):
            await waiter

        # 2) 대기자가 떠난 뒤 실패한 조회의 예외도 회수된다
        failing = IncidentOwnerLookup(1.0)
        gate = asyncio.Event()
        started2 = asyncio.Event()
        finished = asyncio.Event()

        async def fail_later():
            started2.set()
            await gate.wait()
            raise RuntimeError("db down")

        waiter2 = asyncio.create_task(failing.get(fail_later))
        await started2.wait()
        # _retrieve 콜백 뒤에 등록되므로, 이 신호가 오면 회수 콜백은 이미 실행됐다
        failing._task.add_done_callback(lambda _t: finished.set())
        task_ref = weakref.ref(failing._task)
        waiter2.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter2
        gate.set()
        await finished.wait()
        assert task_ref() is not None and task_ref().done() and not task_ref().cancelled()
        await failing.close()
        # 모든 참조를 놓아 task가 회수되게 한다 — 예외가 미회수였다면 이때 핸들러에 보고된다
        del waiter2, failing
        gc.collect()
        assert task_ref() is None
        assert reported == []

    _run(scenario())


def test_close_is_bounded_when_resolver_delays_cancellation_and_failure_is_still_retrieved():
    import gc

    from realtime import IncidentOwnerLookup

    async def scenario():
        loop = asyncio.get_running_loop()
        reported: list[dict] = []
        loop.set_exception_handler(lambda _loop, context: reported.append(context))
        lookup = IncidentOwnerLookup(0.01)
        started, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def stubborn():
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await release.wait()  # 취소 후 정리를 늦추는 resolver
                raise RuntimeError("late cleanup failure")

        waiter = asyncio.create_task(lookup.get(stubborn))
        await started.wait()
        task = lookup._task
        task.add_done_callback(lambda _t: finished.set())
        await asyncio.wait_for(lookup.close(), timeout=5.0)  # release 전에 close가 돌아와야 한다
        assert not task.done()
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        release.set()
        await finished.wait()
        assert task.done()
        del waiter, lookup, task
        gc.collect()
        assert reported == []

    _run(scenario())


def test_broadcast_is_not_held_by_cancellation_resistant_lookup():
    started = asyncio.Event
    release_holder: list[asyncio.Event] = []

    async def scenario():
        manager = RealtimeManager(send_timeout_seconds=0.05)
        began, release = started(), started()
        release_holder.append(release)

        async def stubborn():
            began.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await release.wait()
                raise

        async def authorize(event, owner_lookup) -> bool:
            return await owner_lookup.get(stubborn) is not None

        sock = _Sock()
        await manager.register(sock, authorize)
        # 권한 판정은 제한시간으로 건너뛰고, 정리 대기도 제한시간 안에서 끝나 발행이 돌아온다
        await asyncio.wait_for(manager._broadcast(_incident("inc-a")), timeout=5.0)
        assert began.is_set() and sock.sent == []
        release.set()
        await asyncio.sleep(0)

    _run(scenario())


def test_broadcast_cancellation_closes_owner_lookup():
    started = asyncio.Event
    state = {"cancelled": False}

    async def scenario():
        manager = RealtimeManager(send_timeout_seconds=5.0)
        began = started()

        async def hang():
            began.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                state["cancelled"] = True
                raise

        async def authorize(event, owner_lookup) -> bool:
            return await owner_lookup.get(hang) is not None

        await manager.register(_Sock(), authorize)
        task = asyncio.create_task(manager._broadcast(_incident("inc-a")))
        await began.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert state["cancelled"]

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


def test_bound_authorizers_share_one_owner_lookup_per_broadcast_and_keep_live_assignment_check(
    committed_auth, scope_world, monkeypatch
):
    import main
    import routers.ws as ws_router
    from auth.access import get_auth_session_factory
    from fastapi.testclient import TestClient

    from schemas.api.auth import PlatformRole

    seed, tenant_a, tenant_b, ids = scope_world
    central = committed_auth.make(platform_role=PlatformRole.CENTRAL_ADMIN)
    _assign(committed_auth, seed.user_id, [tenant_a])
    lookups: list[str] = []
    original = ws_router._incident_tenant_id

    def counting(session_factory, incident_id):
        lookups.append(incident_id)
        return original(session_factory, incident_id)

    monkeypatch.setattr(ws_router, "_incident_tenant_id", counting)
    app = main.create_app()
    app.dependency_overrides[get_auth_session_factory] = lambda: committed_auth.sessions
    with TestClient(app) as client:
        manager = client.app.state.realtime
        with (
            client.websocket_connect("/api/v1/ws", headers={"Cookie": central.cookie}) as central_ws,
            client.websocket_connect("/api/v1/ws", headers={"Cookie": seed.cookie}) as ws1,
            client.websocket_connect("/api/v1/ws", headers={"Cookie": seed.cookie}) as ws2,
        ):
            # 담당 매니저 연결 둘이 있어도 발행 1회의 소유 조회는 1번이다
            _publish_ids(manager, ids["a"])
            assert _next_incident_id(ws1) == ids["a"]
            assert _next_incident_id(ws2) == ids["a"]
            assert lookups == [ids["a"]]

            # 담당 회수는 공유 조회와 무관하게 연결별 실시간 판정이 이긴다 — 같은 incident라도 새 조회다
            _assign(committed_auth, seed.user_id, [])
            _publish_ids(manager, ids["a"], "inc-barrier")
            _drain_until(central_ws, "inc-barrier")
            _assign(committed_auth, seed.user_id, [tenant_b])
            _publish_ids(manager, ids["b"])
            assert _next_incident_id(ws1) == ids["b"]
            assert _next_incident_id(ws2) == ids["b"]
            assert lookups.count(ids["a"]) == 2 and lookups.count(ids["b"]) == 1