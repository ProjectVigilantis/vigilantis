"""WebSocket 공통 이벤트 봉투 계약 테스트 (확정 설계 4.5).

이벤트 3종, event_type ↔ data 형태 정합, Incident data는 incident_id만(생성 이벤트만
category를 더 싣는다), "Z" 직렬화.
"""

import pytest
from pydantic import ValidationError

from schemas.api.incidents import IncidentCategory
from schemas.api.ws import (
    ExecutionEventData,
    IncidentCreatedData,
    IncidentEventData,
    WsEvent,
    WsEventType,
)


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
    data = {"incident_id": "inc-20260812-001"}
    if event_type == "INCIDENT_CREATED":
        data["category"] = "FINOPS"
    base = {
        "event_id": "evt-20260812-002",
        "event_type": event_type,
        "occurred_at": "2026-08-12T09:01:00Z",
        "data": data,
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


def test_incident_created_carries_category():
    # 받자마자 트랙별 알림 제목을 고르므로 생성 이벤트만 category를 싣는다
    evt = WsEvent.model_validate(make_incident_event("INCIDENT_CREATED"))
    assert isinstance(evt.data, IncidentCreatedData)
    assert evt.data.incident_id == "inc-20260812-001"
    assert evt.data.category is IncidentCategory.FINOPS


def test_incident_updated_carries_incident_id_only():
    evt = WsEvent.model_validate(make_incident_event("INCIDENT_UPDATED"))
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
    make_incident_event(data={"incident_id": "inc-1", "category": "FINOPS", "detail": "x"}),
    # 생성 이벤트인데 category가 없거나 계약에 없는 값
    make_incident_event(data={"incident_id": "inc-1"}),
    make_incident_event(data={"incident_id": "inc-1", "category": "ASSET"}),
    # 수정 이벤트에 category — 재조회 신호라 incident_id만 담는다
    make_incident_event("INCIDENT_UPDATED", data={"incident_id": "inc-1", "category": "FINOPS"}),
])
def test_event_contract_violations(data):
    with pytest.raises(ValidationError):
        WsEvent.model_validate(data)
