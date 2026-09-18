"""저장된 분석 결과의 조회 계약. 사용자 종료 판단·현재 제안 목록과 독립이다."""

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

from ..guardrails import GuardrailReasonCode, GuardrailStep
from ..runbooks import RunbookId


class AnalysisResultStatus(str, Enum):
    PENDING = "PENDING"
    IN_PROGRESS = "IN_PROGRESS"
    PROPOSALS_GENERATED = "PROPOSALS_GENERATED"
    NO_PROPOSAL = "NO_PROPOSAL"
    GUARDRAIL_REJECTED = "GUARDRAIL_REJECTED"
    FAILED = "FAILED"
    UNAVAILABLE = "UNAVAILABLE"  # 저장된 평가가 없어 성공·전체 거절을 확정할 수 없음


class GuardrailRejection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    runbook_id: RunbookId
    failed_step: GuardrailStep
    reason_code: GuardrailReasonCode | None = None


class AnalysisResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: AnalysisResultStatus
    # 모델 원문·내부 예외·AWS 응답은 포함하지 않는다.
    guardrail_rejections: list[GuardrailRejection] = Field(default_factory=list)
