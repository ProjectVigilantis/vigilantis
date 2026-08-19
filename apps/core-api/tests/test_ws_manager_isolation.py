# ==============================================================================
# [파일 설명]
# 연결 관리자 격리 검증 — 발행 중 실패·타임아웃 연결을 제거하면서 정상 연결은
# 계속 수신하는지. 앱 없이 RealtimeManager를 가짜 연결로 직접 돌린다. (Issue #73)
# ==============================================================================

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from schemas.api.ws import WsEventType

from realtime import RealtimeManager, incident_event

T0 = datetime(2026, 8, 19, 10, 0, 0, tzinfo=timezone.utc)


class FakeWebSocket:
    """send가 실패하거나(fail) 제한시간을 넘기는(slow_seconds) 가짜 연결."""

    def __init__(self, *, fail: bool = False, slow_seconds: float = 0.0) -> None:
        self.fail = fail
        self.slow_seconds = slow_seconds
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
        self.closed = True


async def _publish_and_wait(manager: RealtimeManager, good: FakeWebSocket) -> None:
    """발행 후 정상 연결 수신 + 불량 연결 제거(수 1)까지 기다린다."""
    manager.publish(
        incident_event(WsEventType.INCIDENT_CREATED, incident_id="inc-x", occurred_at=T0)
    )
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
