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
#   - 거절 사유 코드는 단계별 Enum이며 이 파일이 네 단계 전부의 단일 원천이다.
#     단계와 맞지 않는 코드는 GuardrailStepResult가 거절한다. 접두(SCHEMA_·
#     WHITELIST_·ARN_·PRECHECK_)가 단계를 표시하므로 거절 기록만으로 어느 단계가
#     막았는지 역산할 수 있다. (#125)
# ==============================================================================

from __future__ import annotations

from enum import Enum, unique
from typing import Any, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .api.assets import UtcDateTime


@unique
class GuardrailValidationContext(str, Enum):
    AI_CANDIDATE = "AI_CANDIDATE"
    # 서버가 사람 승인 없이 시작한 격리 전부 — High 즉시 선차단과 1분 미응답 만료
    # 자동 격리를 모두 포함한다. 둘의 구분은 Execution의 trigger_source가 담는다.
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


# ------------------------------------------------------------------------------
# 단계별 거절 사유 코드 — GuardrailStepResult.reason_code에 담는 값
#
# 값은 DB에 문자열로 남는다(GuardrailEvaluation.steps는 JSON 직렬화 저장 —
# apps/core-api/db/repositories/guardrails.py). 값 문자열을 바꾸면 과거 거절 기록의
# 사유를 읽을 수 없게 되므로, 이름은 늘리되 기존 값은 바꾸지 않는다.
# ------------------------------------------------------------------------------


@unique
class SchemaCheckReasonCode(str, Enum):
    """① Schema Check 거절 사유 — 명령 봉투의 모양이 계약과 다르다."""

    SCHEMA_INVALID_PAYLOAD = "SCHEMA_INVALID_PAYLOAD"


@unique
class ActionWhitelistReasonCode(str, Enum):
    """② Action Whitelist 거절 사유.

    "목록에 없음"과 "목록에는 있으나 AI가 제안하면 안 됨"을 가른다 — 후자는 롤백
    3종을 AI에게 추천시키려는 인젝션 시도의 신호라 기록에서 섞이면 안 된다
    (ADR-0004 정책 ②).
    """

    WHITELIST_UNKNOWN_RUNBOOK = "WHITELIST_UNKNOWN_RUNBOOK"
    WHITELIST_NOT_AI_RECOMMENDABLE = "WHITELIST_NOT_AI_RECOMMENDABLE"


@unique
class ArnMatchReasonCode(str, Enum):
    """③ ARN Match 거절 사유 — 대상이 DB에 수집된 자산이 아니다(Scope Escalation)."""

    ARN_TARGET_NOT_MANAGED = "ARN_TARGET_NOT_MANAGED"


@unique
class PrecheckReasonCode(str, Enum):
    """④ AWS Dry-Run 거절 사유. AWS 응답 → 코드 매핑은 ADR-0007 §2 표가 원천이다.

    executor 호출 계약(packages/schemas/precheck.py)이 이 Enum을 재노출하므로
    schemas.precheck에서 가져오는 기존 import 경로는 그대로다.
    """

    PRECHECK_UNAUTHORIZED = "PRECHECK_UNAUTHORIZED"
    PRECHECK_TARGET_NOT_FOUND = "PRECHECK_TARGET_NOT_FOUND"
    PRECHECK_INVALID_STATE = "PRECHECK_INVALID_STATE"
    PRECHECK_NOT_IMPLEMENTED = "PRECHECK_NOT_IMPLEMENTED"
    # #154(런북별 typed 파라미터 계약) 이전의 과도기 코드 — 파라미터 키 누락·형식
    # 위반이 ① Schema Check에서 걸리지 않고 ④에서 처음 드러나는 동안만 쓰인다.
    PRECHECK_PARAM_INVALID = "PRECHECK_PARAM_INVALID"
    PRECHECK_AWS_ERROR = "PRECHECK_AWS_ERROR"


GuardrailReasonCode = Union[
    SchemaCheckReasonCode,
    ActionWhitelistReasonCode,
    ArnMatchReasonCode,
    PrecheckReasonCode,
]

# 단계 ↔ 그 단계가 쓸 수 있는 코드 목록. GuardrailStepResult가 이 표로 정합을
# 강제하므로, 단계나 코드를 늘릴 때 여기 등록하지 않으면 계약이 거절한다.
STEP_REASON_CODES: dict[GuardrailStep, type[Enum]] = {
    GuardrailStep.SCHEMA_CHECK: SchemaCheckReasonCode,
    GuardrailStep.ACTION_WHITELIST: ActionWhitelistReasonCode,
    GuardrailStep.ARN_MATCH: ArnMatchReasonCode,
    GuardrailStep.AWS_DRY_RUN: PrecheckReasonCode,
}


class GuardrailStepResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    step: GuardrailStep
    result: GuardrailStepStatus
    reason_code: Optional[GuardrailReasonCode] = None
    # AWS_DRY_RUN 단계에서 실제 사용한 검증 방식·한계 요약(Dry-Run 미지원 대체 검증 포함)
    verification_summary: Optional[str] = Field(None, min_length=1)

    @model_validator(mode="after")
    def _reason_only_on_fail(self):
        if self.result != GuardrailStepStatus.FAIL and self.reason_code is not None:
            raise ValueError("reason_code는 FAIL 단계에만 기록합니다")
        return self

    @model_validator(mode="after")
    def _reason_belongs_to_step(self):
        # 접두가 단계를 표시하는 성질을 이 검증이 지킨다 — ②에 PRECHECK_*가 들어가면
        # 거절 기록에서 어느 단계가 막았는지 역산할 수 없게 된다.
        if self.reason_code is None:
            return self
        expected = STEP_REASON_CODES[self.step]
        if not isinstance(self.reason_code, expected):
            raise ValueError(
                f"{self.step.value} 단계의 reason_code는 {expected.__name__}여야 합니다"
                f" (받은 값: {self.reason_code.value})"
            )
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
