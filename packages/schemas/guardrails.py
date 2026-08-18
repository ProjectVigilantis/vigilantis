# ==============================================================================
# [파일 설명]  담당: 안성일 (AI/Guardrail · Architect)
# 4단계 Guardrail 검증의 문맥·단계·판정·결과 계약입니다. (Issue #55)
# 순서는 SSOT 확정: SCHEMA_CHECK → ACTION_WHITELIST → ARN_MATCH → AWS_DRY_RUN.
#
# 계약 원칙
#   - 검증 요청은 candidate_id(AI 후보) 또는 execution_id(자동 격리·Rollback) 중
#     정확히 하나를 참조한다. command_payload는 Schema Check "전" 경계에서만
#     받는 JSON 값이며 실행 계약이 아니다.
#   - 단계 결과는 항상 4개·고정 순서. 실패 단계 이후는 NOT_RUN, 이전은 PASS.
#   - PASS 시 저장할 validated_command(불변 실행 명령)는 ADR-0004 어휘
#     (trigger_source·approval_mode) 승인 후 RunbookCommand 계약과 함께 추가한다.
#   - reason_code 값 Enum은 Runbook별 실패 조건 확정 시 교체한다(#49와 동일 원칙).
# ==============================================================================

from __future__ import annotations

from enum import Enum, unique
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .api.assets import UtcDateTime


@unique
class GuardrailValidationContext(str, Enum):
    AI_CANDIDATE = "AI_CANDIDATE"
    AUTO_ISOLATION = "AUTO_ISOLATION"
    ROLLBACK_EXECUTION = "ROLLBACK_EXECUTION"


@unique
class GuardrailStep(str, Enum):
    SCHEMA_CHECK = "SCHEMA_CHECK"
    ACTION_WHITELIST = "ACTION_WHITELIST"
    ARN_MATCH = "ARN_MATCH"
    AWS_DRY_RUN = "AWS_DRY_RUN"


# 단계 실행 순서의 단일 원천 — 결과 리스트는 항상 이 순서·이 개수다
GUARDRAIL_STEP_ORDER: tuple[GuardrailStep, ...] = (
    GuardrailStep.SCHEMA_CHECK,
    GuardrailStep.ACTION_WHITELIST,
    GuardrailStep.ARN_MATCH,
    GuardrailStep.AWS_DRY_RUN,
)


@unique
class GuardrailDecision(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"


@unique
class GuardrailStepStatus(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    NOT_RUN = "NOT_RUN"


class GuardrailStepResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    step: GuardrailStep
    result: GuardrailStepStatus
    reason_code: Optional[str] = Field(None, min_length=1)
    # AWS_DRY_RUN 단계에서 실제 사용한 검증 방식·한계 요약(Dry-Run 미지원 대체 검증 포함)
    verification_summary: Optional[str] = Field(None, min_length=1)

    @model_validator(mode="after")
    def _reason_only_on_fail(self):
        if self.result != GuardrailStepStatus.FAIL and self.reason_code is not None:
            raise ValueError("reason_code는 FAIL 단계에만 기록합니다")
        return self

    @model_validator(mode="after")
    def _verification_summary_only_on_dry_run(self):
        if self.step != GuardrailStep.AWS_DRY_RUN and self.verification_summary is not None:
            raise ValueError("verification_summary는 AWS_DRY_RUN 단계에만 기록합니다")
        return self


class GuardrailValidationRequest(BaseModel):
    """Guardrail 진입 요청 — 서버가 저장된 문맥으로 구성한다(외부 요청 DTO 아님)."""

    model_config = ConfigDict(extra="forbid")

    validation_context: GuardrailValidationContext
    candidate_id: Optional[str] = Field(None, min_length=1)
    execution_id: Optional[str] = Field(None, min_length=1)
    # Schema Check 전 경계의 JSON 값 — 1단계가 typed 실행 명령으로 변환한다
    command_payload: dict[str, Any]

    @model_validator(mode="after")
    def _exactly_one_reference(self):
        if (self.candidate_id is None) == (self.execution_id is None):
            raise ValueError("candidate_id 또는 execution_id 중 정확히 하나만 참조해야 합니다")
        if self.validation_context == GuardrailValidationContext.AI_CANDIDATE:
            if self.candidate_id is None:
                raise ValueError("AI_CANDIDATE 검증은 candidate_id를 참조해야 합니다")
        elif self.execution_id is None:
            raise ValueError(
                f"{self.validation_context.value} 검증은 execution_id를 참조해야 합니다"
            )
        return self


class GuardrailValidationResult(BaseModel):
    """4단계 검증의 전체 결과 — GuardrailEvaluation 저장·Candidate 상태 갱신의 근거."""

    model_config = ConfigDict(extra="forbid")

    result: GuardrailDecision
    failed_step: Optional[GuardrailStep] = None
    steps: list[GuardrailStepResult]
    validated_at: UtcDateTime

    @model_validator(mode="after")
    def _enforce_contract(self):
        if [s.step for s in self.steps] != list(GUARDRAIL_STEP_ORDER):
            raise ValueError("steps는 고정 순서 4단계(SCHEMA→WHITELIST→ARN→DRY_RUN)여야 합니다")

        if self.result == GuardrailDecision.PASS:
            if self.failed_step is not None:
                raise ValueError("PASS이면 failed_step은 null이어야 합니다")
            if any(s.result != GuardrailStepStatus.PASS for s in self.steps):
                raise ValueError("PASS이면 네 단계가 모두 PASS여야 합니다")
            return self

        # FAIL: 실패 단계 이전은 PASS, 해당 단계는 FAIL, 이후는 NOT_RUN
        if self.failed_step is None:
            raise ValueError("FAIL이면 failed_step이 필요합니다")
        failed_index = GUARDRAIL_STEP_ORDER.index(self.failed_step)
        for i, s in enumerate(self.steps):
            expected = (
                GuardrailStepStatus.PASS if i < failed_index
                else GuardrailStepStatus.FAIL if i == failed_index
                else GuardrailStepStatus.NOT_RUN
            )
            if s.result != expected:
                raise ValueError(
                    f"FAIL({self.failed_step.value})이면 {s.step.value}는 {expected.value}여야 합니다"
                )
        return self