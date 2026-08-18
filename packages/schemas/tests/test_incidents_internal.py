"""Incident 내부 수명주기 계약 테스트 (Issue #49).

AgentWaitSchedule의 응답 기한은 정확히 started_at + 60초 — 서버 타이머·
INCIDENT_UPDATED.occurred_at 카운트다운 기준과 맞아야 한다.
"""

import pytest
from pydantic import ValidationError

from schemas.incidents import (
    AGENT_TERMINAL_STATUSES,
    AgentInvocationStatus,
    AgentWaitSchedule,
)


def test_statuses_match_contract_exactly():
    assert {s.value for s in AgentInvocationStatus} == {
        "PENDING", "IN_PROGRESS", "SUCCEEDED", "NO_PROPOSAL", "FAILED",
    }
    assert {s.value for s in AGENT_TERMINAL_STATUSES} == {
        "SUCCEEDED", "NO_PROPOSAL", "FAILED",
    }


def make_schedule(**over):
    base = {
        "incident_id": "inc-20260814-001",
        "started_at": "2026-08-14T09:00:00Z",
        "response_deadline_at": "2026-08-14T09:01:00Z",
    }
    base.update(over)
    return base


def test_schedule_roundtrip():
    s = AgentWaitSchedule.model_validate(make_schedule())
    assert AgentWaitSchedule.model_validate_json(s.model_dump_json()) == s


@pytest.mark.parametrize("deadline", [
    "2026-08-14T09:00:59Z",  # 59초
    "2026-08-14T09:01:01Z",  # 61초
    "2026-08-14T08:59:00Z",  # 과거
])
def test_schedule_rejects_non_60s_deadline(deadline):
    with pytest.raises(ValidationError):
        AgentWaitSchedule.model_validate(make_schedule(response_deadline_at=deadline))


def test_schedule_rejects_extra_field():
    with pytest.raises(ValidationError):
        AgentWaitSchedule.model_validate(make_schedule(remaining_seconds=60))
