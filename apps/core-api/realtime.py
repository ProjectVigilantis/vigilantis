# ==============================================================================
# [파일 설명]
# 실시간 전송 — WebSocket 연결 관리자와 발행 진입점. (Issue #73·#75)
#
#   - 연결 객체만 메모리에 보관한다. 업무 상태의 원천은 PostgreSQL이며,
#     발행 실패는 DB 상태를 되돌리지 않는다.
#   - publish()는 DB commit 이후에만 부른다. 어느 스레드에서든 호출할 수 있게
#     Queue에 넣기만 하고, 실제 전송은 이벤트 루프의 소비 task가 수행한다.
#     앱 종료와 겹치면 예외 대신 경고 로그를 남기고 버린다 — 발행 1건은
#     전송 결과 로그(성공·실패) 또는 드롭 경고를 반드시 남긴다.
#   - 연결별 전송 제한시간을 두고 느리거나 끊어진 연결은 제거한다 — 한 연결이
#     전체 발행을 막지 않게 하기 위함이다. 제거·종료 시의 close()도 같은
#     제한시간으로 병렬 정리한다.
#   - 전송 로그 식별자는 ContextVar가 아니라 WsEvent 자체에서 읽는다. 이벤트
#     타입에 없는 식별자는 null로 기록한다.
# ==============================================================================

from __future__ import annotations

import asyncio
import logging
import uuid
from contextlib import suppress
from datetime import datetime
from typing import Optional

from fastapi import WebSocket

from schemas.api.actions import ExecutionStatus
from schemas.api.ws import (
    ExecutionEventData,
    IncidentEventData,
    WsEvent,
    WsEventType,
)

logger = logging.getLogger("vigilantis.realtime")


def incident_event(
    event_type: WsEventType, *, incident_id: str, occurred_at: datetime
) -> WsEvent:
    """INCIDENT_CREATED·INCIDENT_UPDATED 봉투. occurred_at은 새 시각을 만들지 않고
    호출부 트랜잭션에서 저장된 Incident.updated_at을 받는다."""
    return WsEvent(
        event_id=uuid.uuid4().hex,
        event_type=event_type,
        occurred_at=occurred_at,
        data=IncidentEventData(incident_id=incident_id),
    )


def execution_event(
    *,
    incident_id: str,
    execution_id: str,
    status: ExecutionStatus,
    updated_at: datetime,
) -> WsEvent:
    """EXECUTION_UPDATED 봉투. occurred_at과 data.updated_at에 같은 Execution
    저장 시각을 넣는다."""
    return WsEvent(
        event_id=uuid.uuid4().hex,
        event_type=WsEventType.EXECUTION_UPDATED,
        occurred_at=updated_at,
        data=ExecutionEventData(
            incident_id=incident_id,
            execution_id=execution_id,
            status=status,
            updated_at=updated_at,
        ),
    )


class RealtimeManager:
    """연결 등록·해제와 발행 Queue 소비. 앱 수명주기(start/stop)에 묶인다."""

    def __init__(self, *, send_timeout_seconds: float) -> None:
        self._send_timeout = send_timeout_seconds
        self._connections: set[WebSocket] = set()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._queue: Optional[asyncio.Queue[WsEvent]] = None
        self._consumer: Optional[asyncio.Task] = None

    @property
    def connection_count(self) -> int:
        return len(self._connections)

    # --- 수명주기 ---------------------------------------------------------------

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._queue = asyncio.Queue()
        self._consumer = asyncio.create_task(self._consume())

    async def stop(self) -> None:
        # 참조를 먼저 무효화한다 — 이후의 publish()와 이미 예약된 적재
        # callback(_enqueue_on_loop)은 재확인에 걸려 경고를 남기고 드롭된다
        queue = self._queue
        self._loop = None
        self._queue = None
        if self._consumer is not None:
            self._consumer.cancel()
            try:
                await self._consumer
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001 — 이미 죽은 task의 원예외는 로그만 남기고 종료를 계속한다
                logger.exception("ws_consumer_task_failed")
            self._consumer = None
        # 소비되지 못하고 남은 이벤트도 조용히 버리지 않는다
        if queue is not None:
            while True:
                try:
                    event = queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                logger.warning(
                    "publish_dropped_at_shutdown", extra=_event_fields(event)
                )
        connections = list(self._connections)
        self._connections.clear()
        if connections:
            await asyncio.gather(
                *(self._close_quietly(websocket) for websocket in connections)
            )

    # --- 연결 관리 ---------------------------------------------------------------

    async def register(self, websocket: WebSocket) -> None:
        await websocket.accept()
        self._connections.add(websocket)

    def unregister(self, websocket: WebSocket) -> None:
        self._connections.discard(websocket)

    # --- 발행 --------------------------------------------------------------------

    def publish(self, event: WsEvent) -> None:
        """DB commit 이후 호출. 스레드 무관 — Queue 적재만 하고 즉시 반환한다."""
        loop = self._loop
        if loop is None:
            logger.warning(
                "publish_dropped_not_started", extra=_event_fields(event)
            )
            return
        try:
            # 큐에 직접 넣지 않는다 — 예약과 실행 사이에 stop()이 끝났을 수
            # 있으므로, 루프 스레드에서 활성 큐를 재확인한 뒤 적재한다
            loop.call_soon_threadsafe(self._enqueue_on_loop, event)
        except RuntimeError:
            # 루프가 이미 닫힘(프로세스 종료와 경합) — 발행부로 예외를 새게
            # 하지 않고 버린다. 상태 원본은 DB이므로 이벤트 1건 유실은 허용
            logger.warning(
                "publish_dropped_loop_closed", extra=_event_fields(event)
            )

    def _enqueue_on_loop(self, event: WsEvent) -> None:
        """루프 스레드에서 실행되는 적재 단계. stop()도 루프 스레드에서 참조를
        무효화하므로 여기서의 재확인에는 경합이 없다."""
        queue = self._queue
        if queue is None:
            logger.warning(
                "publish_dropped_at_shutdown", extra=_event_fields(event)
            )
            return
        queue.put_nowait(event)

    async def _consume(self) -> None:
        queue = self._queue  # stop()이 참조를 먼저 무효화해도 소비 중인 큐는 유지
        assert queue is not None
        while True:
            event = await queue.get()
            try:
                await self._broadcast(event)
            except asyncio.CancelledError:
                # 종료 취소가 전송 중 이벤트를 끊은 경우 — 큐에도 없고 전송
                # 로그도 없는 유실이 되지 않게 드롭 경고를 남기고 취소는 전파
                logger.warning(
                    "publish_dropped_at_shutdown", extra=_event_fields(event)
                )
                raise
            except Exception:  # noqa: BLE001 — 이벤트 1건 실패로 소비를 멈추지 않는다
                logger.exception(
                    "ws_event_broadcast_failed", extra=_event_fields(event)
                )

    async def _broadcast(self, event: WsEvent) -> None:
        text = event.model_dump_json()
        connections = list(self._connections)
        results = await asyncio.gather(
            *(self._send_one(websocket, text) for websocket in connections),
            return_exceptions=False,
        )
        stale = [
            websocket
            for websocket, ok in zip(connections, results, strict=True)
            if not ok
        ]
        for websocket in stale:
            self.unregister(websocket)
        if stale:
            await asyncio.gather(
                *(self._close_quietly(websocket) for websocket in stale)
            )
        logger.info(
            "ws_event_published",
            extra={
                **_event_fields(event),
                "receivers": len(connections) - len(stale),
                "dropped": len(stale),
            },
        )

    async def _send_one(self, websocket: WebSocket, text: str) -> bool:
        try:
            await asyncio.wait_for(websocket.send_text(text), timeout=self._send_timeout)
            return True
        except Exception:  # noqa: BLE001 — 원인 무관하게 해당 연결만 제거한다
            return False

    async def _close_quietly(self, websocket: WebSocket) -> None:
        """제거·종료용 close — 병렬 호출 전제. 실패는 무시하고 지연은 전송
        제한시간 1회분으로 상한을 둔다(느린 연결이 정리를 잡아두지 않게)."""
        with suppress(Exception):
            await asyncio.wait_for(websocket.close(), timeout=self._send_timeout)


def _event_fields(event: WsEvent) -> dict:
    return {
        "event_id": event.event_id,
        "event_type": event.event_type.value,
        "incident_id": event.data.incident_id,
        "execution_id": getattr(event.data, "execution_id", None),
    }
