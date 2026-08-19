# ==============================================================================
# [파일 설명]  담당: 안성일 (AI/Guardrail · Architect)
# AI 제안 후보(RunbookCandidate)의 상태·저장 계약입니다. (Issue #49)
# Workflow가 LangGraph 출력 Draft에 서버 ID와 PENDING_VALIDATION 상태를 부여해
# 이 계약으로 저장한 뒤 Guardrail에 전달한다.
#
# 계약 원칙
#   - runbook_id는 AI 추천 가능 본편 7종만 — 롤백 3종은 후보가 될 수 없다(ADR-0004).
#   - 상태 전이는 PENDING_VALIDATION→EXECUTABLE|REJECTED,
#     EXECUTABLE→CLAIMED|INVALIDATED만 허용 — 전이 검증은 DB·Workflow가 담당한다.
#   - Runbook별 typed parameters는 세부 계약 확정 후 추가한다(#49 확정 —
#     자유 형식 dict를 계약으로 두지 않기 위해 이번 범위에서 제외).
# ==============================================================================

from __future__ import annotations

from enum import Enum, unique

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .runbooks import AI_RECOMMENDABLE_RUNBOOK_IDS, RunbookId


@unique
class CandidateStatus(str, Enum):
    PENDING_VALIDATION = "PENDING_VALIDATION"
    EXECUTABLE = "EXECUTABLE"
    REJECTED = "REJECTED"
    CLAIMED = "CLAIMED"
    INVALIDATED = "INVALIDATED"


class RunbookCandidateData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidate_id: str = Field(min_length=1)  # 서버 발급 식별자
    incident_id: str = Field(min_length=1)
    runbook_id: RunbookId
    target_arn: str = Field(min_length=1)
    display_parameters: dict[str, str] = Field(default_factory=dict)
    evidence_ids: list[str] = Field(default_factory=list)
    status: CandidateStatus

    @model_validator(mode="after")
    def _enforce_contract(self):
        if self.runbook_id.value not in AI_RECOMMENDABLE_RUNBOOK_IDS:
            raise ValueError("Candidate에는 AI 추천 가능 Runbook(본편 7종)만 올 수 있습니다")
        if any(not e for e in self.evidence_ids):
            raise ValueError("evidence_ids에는 빈 문자열이 올 수 없습니다")
        return self
