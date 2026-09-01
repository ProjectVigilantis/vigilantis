# ==============================================================================
# [파일 설명]
# 연결 관리자 격리·종료 견고화 검증 — 발행 중 실패·타임아웃 연결을 제거하면서
# 정상 연결은 계속 수신하는지, 종료 경합·전송 예외·느린 close·설정 오류가
# 발행과 종료를 막지 않는지. 앱 없이 RealtimeManager를 가짜 연결로 직접
# 돌린다. (Issue #73·#75)
# ==============================================================================

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from schemas.api.ws import WsEventType

from config import Settings
from realtime import RealtimeManager, incident_event

T0 = datetime(2026, 8, 19, 10, 0, 0, tzinfo=timezone.utc)


def _ev(incident_id: str, event_type: WsEventType = WsEventType.INCIDENT_CREATED):
    return incident_event(event_type, incident_id=incident_id, occurred_at=T0)


def _logs(caplog, message: str) -> list:
    return [r for r in caplog.records if r.message == message]


class FakeWebSocket:
    """send가 실패하거나(fail) 제한시간을 넘기고(slow_seconds), close가
    느린(close_seconds) 가짜 연결."""

    def __init__(
        self,
        *,
        fail: bool = False,
        slow_seconds: float = 0.0,
        close_seconds: float = 0.0,
    ) -> None:
        self.fail = fail
        self.slow_seconds = slow_seconds
        self.close_seconds = close_seconds
        self.sent: list[str] = []
        self.closed = False

    async def accept(self) -> None:
        pass

    async def send_text(self, text: str) -> None:
        if self.fail:
            raise RuntimeError("연결 깨짐")
        if self.slow_seconds:
            await asyncio.sleep(self.slow_seconds)
        self.sent.append(text)

    async def close(self) -> None:
        if self.close_seconds:
            await asyncio.sleep(self.close_seconds)
        self.closed = True


async def _publish_and_wait(manager: RealtimeManager, good: FakeWebSocket) -> None:
    """발행 후 정상 연결 수신 + 불량 연결 제거(수 1)까지 기다린다."""
    manager.publish(_ev("inc-x"))
    for _ in range(200):
        if good.sent and manager.connection_count == 1:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(
        f"수신={bool(good.sent)}, 연결 수={manager.connection_count} — 격리가 끝나지 않았다"
    )


def test_failing_connection_is_removed_and_others_still_receive():
    async def scenario():
        manager = RealtimeManager(send_timeout_seconds=1.0)
        await manager.start()
        good, bad = FakeWebSocket(), FakeWebSocket(fail=True)
        await manager.register(good)
        await manager.register(bad)
        await _publish_and_wait(manager, good)
        assert manager.connection_count == 1
        assert bad.closed
        await manager.stop()

    asyncio.run(scenario())


def test_slow_connection_hits_timeout_and_is_removed():
    async def scenario():
        manager = RealtimeManager(send_timeout_seconds=0.05)
        await manager.start()
        good, slow = FakeWebSocket(), FakeWebSocket(slow_seconds=0.5)
        await manager.register(good)
        await manager.register(slow)
        await _publish_and_wait(manager, good)
        assert manager.connection_count == 1
        assert slow.sent == []  # 제한시간 안에 전송을 못 끝냈다
        await manager.stop()

    asyncio.run(scenario())


# --- 이하 종료 경합·전송 격리 견고화 (Issue #75) ----------------------------------


def test_publish_after_loop_closed_without_stop_drops_with_warning(caplog):
    """stop() 없이 이벤트 루프가 먼저 닫힌 비정상 종료와 겹친 publish —
    발행 스레드로 예외가 새지 않고 경고만 남긴다. asyncio.run이 반환하며
    루프를 실제로 닫는 것을 이용해 스텁 없이 실제 경로로 검증한다."""

    async def start_only():
        manager = RealtimeManager(send_timeout_seconds=1.0)
        await manager.start()
        return manager

    manager = asyncio.run(start_only())  # run 종료가 루프를 닫는다
    with caplog.at_level(logging.WARNING, logger="vigilantis.realtime"):
        manager.publish(_ev("inc-race"))  # 예외 없이 반환해야 한다
    assert _logs(caplog, "publish_dropped_loop_closed")


def test_publish_during_stop_close_wait_warns_and_drops(caplog):
    """소비 task가 취소되고 stop()이 close 정리를 기다리는 동안의 publish —
    죽은 큐에 적재하지 않고 경고를 남기며, stop()도 제한시간 안에 끝난다."""

    class BlockingCloseWebSocket(FakeWebSocket):
        """close 진입을 알리고(close_entered) 신호(release)까지 잡아두는 연결."""

        def __init__(self) -> None:
            super().__init__()
            self.close_entered = asyncio.Event()
            self.release = asyncio.Event()

        async def close(self) -> None:
            self.close_entered.set()
            await self.release.wait()
            self.closed = True

    async def scenario():
        manager = RealtimeManager(send_timeout_seconds=5.0)
        await manager.start()
        blocking = BlockingCloseWebSocket()
        await manager.register(blocking)
        queue_before = manager._queue

        stop_task = asyncio.create_task(manager.stop())
        await asyncio.wait_for(blocking.close_entered.wait(), timeout=2.0)
        # 이 시점: 소비 task 취소 완료, stop()은 close 대기 중
        manager.publish(_ev("inc-stop"))  # 예외 없이 경고로 떨어져야 한다
        assert queue_before.qsize() == 0  # 소비자 없는 큐에 적재되지 않았다
        blocking.release.set()
        await asyncio.wait_for(stop_task, timeout=2.0)

    with caplog.at_level(logging.WARNING, logger="vigilantis.realtime"):
        asyncio.run(scenario())
    assert _logs(caplog, "publish_dropped_not_started")


def test_stop_drains_unconsumed_events_with_warning(caplog):
    """stop() 시점 큐에 남은 미소비 이벤트는 조용히 버리지 않고 건별 드롭
    경고를 남긴다."""

    async def scenario():
        manager = RealtimeManager(send_timeout_seconds=1.0)
        await manager.start()
        # start()와 stop() 사이에 await가 없어 소비 task가 한 번도 돌지 않는다
        manager._queue.put_nowait(_ev("inc-left"))
        await manager.stop()

    with caplog.at_level(logging.WARNING, logger="vigilantis.realtime"):
        asyncio.run(scenario())
    dropped = _logs(caplog, "publish_dropped_at_shutdown")
    assert len(dropped) == 1
    assert dropped[0].incident_id == "inc-left"


def test_enqueue_scheduled_before_stop_is_dropped_with_warning(caplog):
    """publish()가 적재 callback을 예약한 뒤 stop()이 먼저 참조를 무효화한
    경우 — callback이 루프 스레드 재확인에 걸려 드롭 경고를 남기고, 소비자
    없는 큐에는 적재되지 않는다."""

    async def scenario():
        manager = RealtimeManager(send_timeout_seconds=1.0)
        await manager.start()
        queue_before = manager._queue
        # callback은 예약만 되고, 다음 yield(stop 내부)까지 실행되지 않는다
        manager.publish(_ev("inc-sched"))
        await manager.stop()
        assert queue_before.qsize() == 0  # 버려진 큐에 적재되지 않았다

    with caplog.at_level(logging.WARNING, logger="vigilantis.realtime"):
        asyncio.run(scenario())
    dropped = _logs(caplog, "publish_dropped_at_shutdown")
    assert len(dropped) == 1
    assert dropped[0].incident_id == "inc-sched"


def test_inflight_event_cancelled_at_stop_leaves_drop_log(caplog):
    """소비 task가 이벤트를 꺼내 전송 중일 때 stop()이 취소한 경우 — 큐에도
    전송 로그에도 없는 조용한 유실 대신 드롭 경고를 남기고 종료는 정상."""

    class BlockingSendWebSocket(FakeWebSocket):
        """send 진입을 알리고(send_entered) 신호(release)까지 잡아두는 연결."""

        def __init__(self) -> None:
            super().__init__()
            self.send_entered = asyncio.Event()
            self.release = asyncio.Event()

        async def send_text(self, text: str) -> None:
            self.send_entered.set()
            await self.release.wait()
            self.sent.append(text)

    async def scenario():
        manager = RealtimeManager(send_timeout_seconds=5.0)
        await manager.start()
        blocking = BlockingSendWebSocket()
        await manager.register(blocking)
        manager.publish(_ev("inc-inflight"))
        await asyncio.wait_for(blocking.send_entered.wait(), timeout=2.0)
        # 이 시점: 이벤트는 큐에서 빠져 전송 중 — stop()의 취소가 여기를 끊는다
        await asyncio.wait_for(manager.stop(), timeout=2.0)
        assert blocking.sent == []  # 전송 완료 전에 끊겼다

    with caplog.at_level(logging.WARNING, logger="vigilantis.realtime"):
        asyncio.run(scenario())
    dropped = _logs(caplog, "publish_dropped_at_shutdown")
    assert len(dropped) == 1
    assert dropped[0].incident_id == "inc-inflight"


def test_consumer_survives_broadcast_failure(monkeypatch, caplog):
    """전송 준비 중 예외 1건 — 소비 task가 죽지 않고 다음 이벤트를 처리하며
    stop()도 예외 없이 끝난다."""

    async def scenario():
        manager = RealtimeManager(send_timeout_seconds=1.0)
        await manager.start()
        good = FakeWebSocket()
        await manager.register(good)

        original_broadcast = manager._broadcast
        failed_once = False

        async def broadcast_fails_once(event):
            nonlocal failed_once
            if not failed_once:
                failed_once = True
                raise RuntimeError("전송 준비 중 예외")
            await original_broadcast(event)

        monkeypatch.setattr(manager, "_broadcast", broadcast_fails_once)
        manager.publish(_ev("inc-dead"))
        manager.publish(_ev("inc-alive", WsEventType.INCIDENT_UPDATED))
        for _ in range(200):
            if good.sent:
                break
            await asyncio.sleep(0.01)
        # 두 번째 이벤트가 도달했다 = 소비 task가 첫 실패에서 죽지 않았다
        assert len(good.sent) == 1
        assert "inc-alive" in good.sent[0]
        await manager.stop()

    with caplog.at_level(logging.ERROR, logger="vigilantis.realtime"):
        asyncio.run(scenario())
    assert _logs(caplog, "ws_event_broadcast_failed")


def test_slow_close_does_not_block_broadcast_removal():
    """불량 연결 제거의 close가 느려도 발행 흐름은 전송 제한시간 1회분 안에
    다음 이벤트로 넘어간다."""

    async def scenario():
        manager = RealtimeManager(send_timeout_seconds=0.25)
        await manager.start()
        good = FakeWebSocket()
        bad = FakeWebSocket(fail=True, close_seconds=30.0)
        await manager.register(good)
        await manager.register(bad)
        await _publish_and_wait(manager, good)
        # close 30초를 기다린다면 아래 두 번째 이벤트가 대기(최대 2초) 안에 못 온다
        manager.publish(_ev("inc-next", WsEventType.INCIDENT_UPDATED))
        for _ in range(200):
            if len(good.sent) == 2:
                break
            await asyncio.sleep(0.01)
        assert len(good.sent) == 2
        assert not bad.closed  # 제한시간에 잘려 close 완료 전에 정리가 끝났다
        await manager.stop()

    asyncio.run(scenario())


def test_slow_close_does_not_block_stop():
    """종료 시 남은 연결들의 close가 느려도 stop()이 제한시간 1회분 수준으로
    끝난다(병렬 정리). 무제한 순차 대기라면 60초가 걸려 아래 2초를 넘긴다."""

    async def scenario():
        manager = RealtimeManager(send_timeout_seconds=0.25)
        await manager.start()
        await manager.register(FakeWebSocket(close_seconds=30.0))
        await manager.register(FakeWebSocket(close_seconds=30.0))
        await asyncio.wait_for(manager.stop(), timeout=2.0)
        assert manager.connection_count == 0

    asyncio.run(scenario())


def test_ws_send_timeout_must_be_positive(monkeypatch):
    """0 이하로 오설정되면 모든 연결이 즉시 제거되므로 기동 단계에서 거부한다."""
    # conftest의 setdefault에 기대지 않고 필수 설정을 명시해 자립시킨다
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@localhost:5432/x")
    for bad_value in ("0", "-1"):
        monkeypatch.setenv("WS_SEND_TIMEOUT_SECONDS", bad_value)
        with pytest.raises(ValidationError) as exc_info:
            Settings()
        # 실패한 필드가 이 설정임을 고정한다 — 다른 필드 누락으로 통과하면 안 된다
        assert exc_info.value.errors()[0]["loc"] == ("WS_SEND_TIMEOUT_SECONDS",)
    monkeypatch.setenv("WS_SEND_TIMEOUT_SECONDS", "2.5")
    assert Settings().WS_SEND_TIMEOUT_SECONDS == 2.5
