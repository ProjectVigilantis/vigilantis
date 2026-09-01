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
#   - parameters는 Runbook별 typed 모델이다(#154, runbook_parameters.py) — Draft가
#     들고 온 것을 그대로 저장한다.
#   - display_parameters는 화면 표시 전용이며 서버가 parameters에서 생성한다.
#     관제자는 이 값을 보고 승인하고 실행은 parameters로 나가므로, 둘을 각각 LLM이
#     채우면 승인 근거와 실행 내용이 갈릴 수 있다 — 직접 채운 값은 거절한다.
# ==============================================================================

from __future__ import annotations

from enum import Enum, unique

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .runbook_parameters import (
    CANDIDATE_PARAMETER_MODELS,
    CandidateParameters,
    bind_candidate_parameters,
    build_display_parameters,
)
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
    parameters: CandidateParameters
    # 생략하면 서버가 채운다. DB에서 되읽을 때는 저장된 값이 그대로 들어온다
    display_parameters: dict[str, str] = Field(default_factory=dict)
    evidence_ids: list[str] = Field(min_length=1)
    status: CandidateStatus

    @model_validator(mode="before")
    @classmethod
    def _bind_parameters(cls, data):
        return bind_candidate_parameters(data)

    @model_validator(mode="after")
    def _enforce_contract(self):
        if self.runbook_id.value not in AI_RECOMMENDABLE_RUNBOOK_IDS:
            raise ValueError("Candidate에는 AI 추천 가능 Runbook(본편 7종)만 올 수 있습니다")
        expected = CANDIDATE_PARAMETER_MODELS[self.runbook_id]
        if not isinstance(self.parameters, expected):
            raise ValueError(
                f"{self.runbook_id.value}의 parameters는 {expected.__name__}이어야 합니다"
            )
        if any(not e for e in self.evidence_ids):
            raise ValueError("evidence_ids에는 빈 문자열이 올 수 없습니다")

        derived = build_display_parameters(self.parameters)
        if self.display_parameters and self.display_parameters != derived:
            raise ValueError(
                "display_parameters는 서버가 parameters에서 생성합니다 — 직접 채울 수 없습니다"
            )
        self.display_parameters = derived
        return self
