"""WebSocket 공통 이벤트 봉투 계약 테스트 (확정 설계 4.5).

이벤트 3종, event_type ↔ data 형태 정합, Incident data는 incident_id만, "Z" 직렬화.
"""

import pytest
from pydantic import ValidationError

from schemas.api.ws import ExecutionEventData, IncidentEventData, WsEvent, WsEventType


def make_execution_event(**over):
    base = {
        "event_id": "evt-20260812-001",
        "event_type": "EXECUTION_UPDATED",
        "occurred_at": "2026-08-12T09:02:04Z",
        "data": {
            "incident_id": "inc-20260812-001",
            "execution_id": "exec-20260812-001",
            "status": "IN_PROGRESS",
            "updated_at": "2026-08-12T09:02:04Z",
        },
    }
    base.update(over)
    return base


def make_incident_event(event_type="INCIDENT_CREATED", **over):
    base = {
        "event_id": "evt-20260812-002",
        "event_type": event_type,
        "occurred_at": "2026-08-12T09:01:00Z",
        "data": {"incident_id": "inc-20260812-001"},
    }
    base.update(over)
    return base


def test_event_types_match_contract_exactly():
    assert {t.value for t in WsEventType} == {
        "INCIDENT_CREATED", "INCIDENT_UPDATED", "EXECUTION_UPDATED",
    }


def test_execution_event_roundtrip_and_z():
    evt = WsEvent.model_validate(make_execution_event())
    assert isinstance(evt.data, ExecutionEventData)
    dumped = evt.model_dump_json()
    assert '"2026-08-12T09:02:04Z"' in dumped
    assert WsEvent.model_validate_json(dumped) == evt


@pytest.mark.parametrize("event_type", ["INCIDENT_CREATED", "INCIDENT_UPDATED"])
def test_incident_events_carry_incident_id_only(event_type):
    evt = WsEvent.model_validate(make_incident_event(event_type))
    assert isinstance(evt.data, IncidentEventData)
    assert evt.data.incident_id == "inc-20260812-001"


@pytest.mark.parametrize("data", [
    # Incident 이벤트인데 Execution 형태 data — incident_id만 담아야 한다
    make_incident_event(data={
        "incident_id": "inc-1", "execution_id": "exec-1",
        "status": "SUCCESS", "updated_at": "2026-08-12T09:02:04Z",
    }),
    # Execution 이벤트인데 incident_id만 있는 data
    make_execution_event(data={"incident_id": "inc-1"}),
    # 미등록 이벤트 종류 (자산 목록 변경 이벤트는 MVP 밖)
    make_incident_event(event_type="ASSET_UPDATED"),
    # 미등록 실행 상태
    make_execution_event(data={
        "incident_id": "inc-1", "execution_id": "exec-1",
        "status": "DONE", "updated_at": "2026-08-12T09:02:04Z",
    }),
    # 빈 event_id / 봉투·data extra 거부
    make_execution_event(event_id=""),
    {**make_execution_event(), "channel": "ws"},
    make_incident_event(data={"incident_id": "inc-1", "detail": "x"}),
])
def test_event_contract_violations(data):
    with pytest.raises(ValidationError):
        WsEvent.model_validate(data)
