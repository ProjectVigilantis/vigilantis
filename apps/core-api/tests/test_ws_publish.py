# ==============================================================================
# [파일 설명]
# 발행 경로 검증 — WsEvent 봉투 수신, 끊긴 연결 제거, 발행 로그. (Issue #73)
# publish()는 실제 호출부(Workflow)처럼 이벤트 루프 밖 스레드에서 부른다.
# ==============================================================================

from __future__ import annotations

import json
from datetime import datetime, timezone

from schemas.api.actions import ExecutionStatus
from schemas.api.ws import WsEventType

from realtime import execution_event, incident_event

T0 = datetime(2026, 8, 19, 9, 30, 0, tzinfo=timezone.utc)


def test_incident_event_reaches_connected_client(client):
    manager = client.app.state.realtime
    with client.websocket_connect("/api/v1/ws") as ws:
        event = incident_event(
            WsEventType.INCIDENT_CREATED, incident_id="inc-1", occurred_at=T0
        )
        manager.publish(event)
        received = json.loads(ws.receive_text())
    assert received == {
        "event_id": event.event_id,
        "event_type": "INCIDENT_CREATED",
        "occurred_at": "2026-08-19T09:30:00Z",
        "data": {"incident_id": "inc-1"},
    }


def test_execution_event_carries_status_and_updated_at(client):
    manager = client.app.state.realtime
    with client.websocket_connect("/api/v1/ws") as ws:
        manager.publish(
            execution_event(
                incident_id="inc-1",
                execution_id="exe-1",
                status=ExecutionStatus.SUCCESS,
                updated_at=T0,
            )
        )
        received = json.loads(ws.receive_text())
    assert received["event_type"] == "EXECUTION_UPDATED"
    assert received["data"] == {
        "incident_id": "inc-1",
        "execution_id": "exe-1",
        "status": "SUCCESS",
        "updated_at": "2026-08-19T09:30:00Z",
    }
    # occurred_at은 새 시각이 아니라 저장된 updated_at과 같은 값이다
    assert received["occurred_at"] == received["data"]["updated_at"]


def test_all_connections_receive_same_event(client):
    manager = client.app.state.realtime
    with client.websocket_connect("/api/v1/ws") as ws1:
        with client.websocket_connect("/api/v1/ws") as ws2:
            event = incident_event(
                WsEventType.INCIDENT_UPDATED, incident_id="inc-2", occurred_at=T0
            )
            manager.publish(event)
            first = json.loads(ws1.receive_text())
            second = json.loads(ws2.receive_text())
    assert first == second
    assert first["event_id"] == event.event_id


def test_publish_with_no_connections_is_noop(client):
    manager = client.app.state.realtime
    manager.publish(
        incident_event(WsEventType.INCIDENT_CREATED, incident_id="inc-3", occurred_at=T0)
    )  # 수신자 0 — 예외 없이 지나가야 한다


def test_publish_log_reads_identifiers_from_event(client, capsys):
    manager = client.app.state.realtime
    with client.websocket_connect("/api/v1/ws") as ws:
        manager.publish(
            incident_event(
                WsEventType.INCIDENT_CREATED, incident_id="inc-log", occurred_at=T0
            )
        )
        ws.receive_text()
    logs = [
        json.loads(line)
        for line in capsys.readouterr().out.splitlines()
        if '"ws_event_published"' in line
    ]
    assert logs, "발행 로그가 남아야 한다"
    entry = logs[-1]
    assert entry["logger"] == "vigilantis.realtime"
    assert entry["incident_id"] == "inc-log"
    assert entry["execution_id"] is None  # 이벤트 타입에 없는 식별자는 null
    assert entry["receivers"] == 1
