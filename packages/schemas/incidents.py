# ==============================================================================
# [파일 설명]  담당: 안성일 (AI/Guardrail · Architect)
# Incident 내부 수명주기 계약입니다. (Issue #49)
#   - AgentInvocationStatus: Incident의 AI 분석 호출 상태. PENDING→IN_PROGRESS는
#     Workflow의 원자 Claim으로만 전이하고, Terminal 3종은 다시 Claim하지 않는다.
#   - AgentWaitSchedule: Medium·Low의 안전한 PASS 제안 저장 후 시작되는 응답 대기.
#     response_deadline_at은 항상 started_at + 60초이며, 같은 started_at을
#     INCIDENT_UPDATED.occurred_at으로 사용해 Dashboard 카운트다운 기준과 맞춘다.
#     타임아웃 판정 기준은 브라우저가 아니라 서버(PostgreSQL)의 이 값이다.
#   - INCIDENT_RESOLVABLE_STATUSES: 관제자 종료 처리가 출발할 수 있는 상태.
#     허용 여부의 근거는 공개 응답 계약이 RESOLVED에 요구하는 모양이다.
# 공개 Incident DTO(api/incidents.py)와는 별개 계약이다.
# ==============================================================================

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from enum import Enum, unique

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .api.assets import UtcDateTime
from .api.incidents import IncidentStatus


@unique
class AgentInvocationStatus(str, Enum):
    PENDING = "PENDING"
    IN_PROGRESS = "IN_PROGRESS"
    SUCCEEDED = "SUCCEEDED"
    NO_PROPOSAL = "NO_PROPOSAL"
    FAILED = "FAILED"


# Graph 출력이 반환할 수 있는 Terminal 상태 — PENDING·IN_PROGRESS는 Workflow가
# Incident에 저장하는 호출 상태이지 Graph 출력값이 아니다.
AGENT_TERMINAL_STATUSES: frozenset[AgentInvocationStatus] = frozenset(
    {
        AgentInvocationStatus.SUCCEEDED,
        AgentInvocationStatus.NO_PROPOSAL,
        AgentInvocationStatus.FAILED,
    }
)


# 관제자가 종료 처리할 수 있는 출발 상태 (Issue #199).
#   - ACTION_IN_PROGRESS는 제외한다 — 공개 응답 계약(api/incidents.py)이 RESOLVED에
#     진행 중인 실행이 없을 것을 요구하므로, 허용하면 종료 직후 조회가 깨진다.
#   - ANALYZING도 제외한다 — AI 분석이 끝나며 제안이 붙고 AWAITING_APPROVAL로
#     되살아나므로 종료가 뒤집힌다.
#   - RESOLVED 재요청은 거절이 아니라 멱등 응답이라 이 집합에 넣지 않는다.
#   - AWAITING_CLOSURE는 이 집합의 주 출발점이다 — 조치가 끝나고 종료 판단만 남은
#     자리라, 관제자 [종료 판단]이 여기서 열리지 않으면 그 상태가 무의미해진다
#     (Issue #240).
INCIDENT_RESOLVABLE_STATUSES: frozenset[IncidentStatus] = frozenset(
    {
        IncidentStatus.AWAITING_APPROVAL,
        IncidentStatus.AWAITING_CLOSURE,
        IncidentStatus.FAILED,
    }
)


# 아직 끝나지 않은 Incident — 같은 대상에 새 Incident를 만들지 않는 기준 (Issue #265).
#   - RESOLVED만 뺀다. 관제자가 닫은 건이라, 이후 같은 자산이 다시 판정되면 새 카드가
#     맞다 — 그때의 낭비는 그때의 사건이다.
#   - FAILED는 넣는다. 흐름은 멈췄지만 관제자가 종료 처리로 닫을 수 있는 상태라
#     (INCIDENT_RESOLVABLE_STATUSES) 실패는 사람이 보고 닫아야 풀린다는 것이 그 설계다.
#     빼면 실패 원인이 그대로인 자산에 수집 주기마다 새 카드가 쌓인다 — 기본 주기
#     300초면 시간당 12장이다. (2026-09-03 안성일 결정)
#   - 나머지 넷은 아직 사람이 볼 것이 남아 있어, 같은 대상에 카드를 더 만들면 관제자가
#     같은 자산을 두 번 판단하게 된다.
INCIDENT_OPEN_STATUSES: frozenset[IncidentStatus] = frozenset(
    {
        IncidentStatus.ANALYZING,
        IncidentStatus.AWAITING_APPROVAL,
        IncidentStatus.ACTION_IN_PROGRESS,
        IncidentStatus.AWAITING_CLOSURE,
        IncidentStatus.FAILED,
    }
)


def _as_utc(v: datetime) -> datetime:
    return v.replace(tzinfo=timezone.utc) if v.tzinfo is None else v


class AgentWaitSchedule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    incident_id: str = Field(min_length=1)
    started_at: UtcDateTime
    response_deadline_at: UtcDateTime

    @model_validator(mode="after")
    def _deadline_is_started_plus_60s(self):
        delta = _as_utc(self.response_deadline_at) - _as_utc(self.started_at)
        if delta != timedelta(seconds=60):
            raise ValueError("response_deadline_at은 정확히 started_at + 60초여야 합니다")
        return self
