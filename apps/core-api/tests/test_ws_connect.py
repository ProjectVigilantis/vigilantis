# ==============================================================================
# [파일 설명]
# WebSocket /api/v1/ws 연결 수립·Origin 검증. (Issue #73)
# ==============================================================================

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from starlette.websockets import WebSocketDisconnect

from schemas.api.ws import WsEventType

from realtime import incident_event

try:  # starlette 버전에 따라 사전 거절이 별도 예외로 온다
    from starlette.testclient import WebSocketDenialResponse
except ImportError:  # pragma: no cover
    WebSocketDenialResponse = WebSocketDisconnect


def test_connect_without_origin_is_accepted(client):
    with client.websocket_connect("/api/v1/ws"):
        pass  # 비브라우저 클라이언트 — Origin 없음 허용


def test_connect_with_allowed_origin_is_accepted(client):
    with client.websocket_connect(
        "/api/v1/ws", headers={"Origin": "http://localhost:3000"}
    ):
        pass


def test_connect_with_disallowed_origin_is_rejected(client):
    with pytest.raises((WebSocketDenialResponse, WebSocketDisconnect)):
        with client.websocket_connect(
            "/api/v1/ws", headers={"Origin": "http://evil.example"}
        ):
            pass


def test_any_client_frame_is_ignored_and_connection_stays(client):
    """프레임 전송 뒤 같은 연결로 이벤트를 수신해야 살아 있음이 증명된다 —
    연결 수만 보면 서버가 프레임을 처리하기 전에 참이 될 수 있다."""
    manager = client.app.state.realtime
    occurred_at = datetime(2026, 8, 19, 10, 30, 0, tzinfo=timezone.utc)
    with client.websocket_connect("/api/v1/ws") as ws:
        ws.send_text("ignored")
        ws.send_bytes(b"\x00binary-ignored")
        manager.publish(
            incident_event(
                WsEventType.INCIDENT_UPDATED,
                incident_id="inc-alive",
                occurred_at=occurred_at,
            )
        )
        received = json.loads(ws.receive_text())
        assert manager.connection_count == 1
    assert received["data"] == {"incident_id": "inc-alive"}


def test_disconnect_unregisters_connection(client):
    manager = client.app.state.realtime
    with client.websocket_connect("/api/v1/ws"):
        assert manager.connection_count == 1
    _wait_until(lambda: manager.connection_count == 0)


def _wait_until(condition, tries: int = 50):
    import time

    for _ in range(tries):
        if condition():
            return
        time.sleep(0.02)
    raise AssertionError("조건이 제한 시간 안에 성립하지 않았다")
