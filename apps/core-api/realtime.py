# ==============================================================================
# [파일 설명]
# 실시간 전송 — WebSocket 연결 관리자와 발행 진입점. (Issue #73)
#
#   - 연결 객체만 메모리에 보관한다. 업무 상태의 원천은 PostgreSQL이며,
#     발행 실패는 DB 상태를 되돌리지 않는다.
#   - publish()는 DB commit 이후에만 부른다. 어느 스레드에서든 호출할 수 있게
#     Queue에 넣기만 하고, 실제 전송은 이벤트 루프의 소비 task가 수행한다.
#   - 연결별 전송 제한시간을 두고 느리거나 끊어진 연결은 제거한다 — 한 연결이
#     전체 발행을 막지 않게 하기 위함이다.
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
        if self._consumer is not None:
            self._consumer.cancel()
            with suppress(asyncio.CancelledError):
                await self._consumer
            self._consumer = None
        for websocket in list(self._connections):
            with suppress(Exception):
                await websocket.close()
        self._connections.clear()
        self._loop = None
        self._queue = None

    # --- 연결 관리 ---------------------------------------------------------------

    async def register(self, websocket: WebSocket) -> None:
        await websocket.accept()
        self._connections.add(websocket)

    def unregister(self, websocket: WebSocket) -> None:
        self._connections.discard(websocket)

    # --- 발행 --------------------------------------------------------------------

    def publish(self, event: WsEvent) -> None:
        """DB commit 이후 호출. 스레드 무관 — Queue 적재만 하고 즉시 반환한다."""
        if self._loop is None or self._queue is None:
            logger.warning(
                "publish_dropped_not_started", extra=_event_fields(event)
            )
            return
        self._loop.call_soon_threadsafe(self._queue.put_nowait, event)

    async def _consume(self) -> None:
        assert self._queue is not None
        while True:
            event = await self._queue.get()
            await self._broadcast(event)

    async def _broadcast(self, event: WsEvent) -> None:
        text = event.model_dump_json()
        connections = list(self._connections)
        results = await asyncio.gather(
            *(self._send_one(websocket, text) for websocket in connections),
            return_exceptions=False,
        )
        dropped = 0
        for websocket, ok in zip(connections, results, strict=True):
            if not ok:
                dropped += 1
                self.unregister(websocket)
                with suppress(Exception):
                    await websocket.close()
        logger.info(
            "ws_event_published",
            extra={
                **_event_fields(event),
                "receivers": len(connections) - dropped,
                "dropped": dropped,
            },
        )

    async def _send_one(self, websocket: WebSocket, text: str) -> bool:
        try:
            await asyncio.wait_for(websocket.send_text(text), timeout=self._send_timeout)
            return True
        except Exception:  # noqa: BLE001 — 원인 무관하게 해당 연결만 제거한다
            return False


def _event_fields(event: WsEvent) -> dict:
    return {
        "event_id": event.event_id,
        "event_type": event.event_type.value,
        "incident_id": event.data.incident_id,
        "execution_id": getattr(event.data, "execution_id", None),
    }
