# ==============================================================================
# [파일 설명]  담당: 안성일 (AI / Guardrail)
# 4단계 Execution Guardrail입니다. LLM 출력을 순서대로 검증해 프롬프트 인젝션/RCE를
# 차단합니다: Schema → Action Whitelist → ARN Match → AWS Dry-Run.
# 이 파일은 그중 ① Schema Check와 ② Action Whitelist를 담습니다. (Issue #114)
#
# 계약 원칙
#   - 단계 결과는 GuardrailStepResult(packages/schemas/guardrails.py)로만 나간다.
#     거절일 때만 reason_code를 채우고, verification_summary는 ④ 전용이라 비운다.
#   - ①은 payload를 runbook_id가 문자열인 SchemaCheckedCommand로 변환한다. 여기서
#     RunbookCandidateDraft로 바로 가면 그 모델이 AI 추천 7종만 받으므로 미등록
#     ID·롤백 ID가 ①에서 터지고 ②가 걸러낼 것이 남지 않는다. 목록 대조는 ②다.
#   - ②를 통과한 명령만 RunbookCandidateDraft로 승격한다.
#   - 거절 사유는 문자열 상수다. Runbook별 실패 조건 확정 시 Enum으로 교체한다
#     (packages/schemas/guardrails.py 계약 원칙과 동일).
#   - 검증 문맥은 AI_CANDIDATE만 구현한다. 다른 문맥은 payload 모양이 달라 여기서
#     판정하면 거절 기록이 틀리므로, FAIL이 아니라 예외로 막는다.
#
# [남은 작업]
# 3. ARN Match: DB 수집 ARN과 대조(Scope Escalation 차단)
# 4. AWS Dry-Run: executor precheck 호출(#113 규약)
# 4단계 종합 판정 GuardrailValidationResult는 ③④를 붙일 때 만든다 — steps가 고정
# 4개인 계약이라 두 단계만으로는 조립되지 않는다.
# ==============================================================================

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Annotated, Final, Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from schemas.agents import RunbookCandidateDraft
from schemas.guardrails import (
    GuardrailStep,
    GuardrailStepResult,
    GuardrailStepStatus,
    GuardrailValidationContext,
    GuardrailValidationRequest,
)
from schemas.runbooks import RunbookId

from .whitelist import is_ai_recommendable, is_allowed_runbook

logger = logging.getLogger("vigilantis.ai")

# ------------------------------------------------------------------------------
# 거절 사유 코드 — GuardrailStepResult.reason_code에 담는 값
# ------------------------------------------------------------------------------

SCHEMA_INVALID_PAYLOAD: Final[str] = "SCHEMA_INVALID_PAYLOAD"
WHITELIST_UNKNOWN_RUNBOOK: Final[str] = "WHITELIST_UNKNOWN_RUNBOOK"
WHITELIST_NOT_AI_RECOMMENDABLE: Final[str] = "WHITELIST_NOT_AI_RECOMMENDABLE"

# 위반 항목 수·위치 문자열은 payload가 키우는 값이다 — 로그 한 줄이 무한정
# 길어지지 않게 자른다. loc에는 payload가 지은 키 이름(추가 필드명·dict 키)이
# 들어오므로 길이 제한이 곧 LLM 저작 텍스트의 로그 유입 상한이다.
_MAX_LOGGED_VIOLATIONS: Final[int] = 10
_MAX_LOGGED_LOC_CHARS: Final[int] = 80

# "빈 문자열은 거절"(#114)의 코드 표현 — 아래 모델의 모든 문자열 자리에 적용한다
_NonEmptyStr = Annotated[str, Field(min_length=1)]


class SchemaCheckedCommand(BaseModel):
    """①이 통과시킨 구조 — RunbookCandidateDraft와 필드 집합은 같고, runbook_id는
    문자열이다(확정 목록에 없는 ID도 이 단계는 통과해야 ②가 판정할 수 있다).
    빈 문자열 거절은 Draft가 제약하지 않는 display_parameters 내부에도 적용한다."""

    model_config = ConfigDict(extra="forbid")

    runbook_id: _NonEmptyStr
    target_arn: _NonEmptyStr
    display_parameters: dict[_NonEmptyStr, _NonEmptyStr] = Field(default_factory=dict)
    evidence_ids: list[_NonEmptyStr] = Field(default_factory=list)


@dataclass(frozen=True)
class SchemaCheckOutcome:
    """① 결과. command는 PASS일 때만 있다."""

    step_result: GuardrailStepResult
    command: Optional[SchemaCheckedCommand]


@dataclass(frozen=True)
class ActionWhitelistOutcome:
    """② 결과. draft는 PASS일 때만 있다."""

    step_result: GuardrailStepResult
    draft: Optional[RunbookCandidateDraft]


def _step_pass(step: GuardrailStep) -> GuardrailStepResult:
    return GuardrailStepResult(step=step, result=GuardrailStepStatus.PASS)


def _step_fail(step: GuardrailStep, reason_code: str) -> GuardrailStepResult:
    return GuardrailStepResult(
        step=step, result=GuardrailStepStatus.FAIL, reason_code=reason_code
    )


def run_schema_check(request: GuardrailValidationRequest) -> SchemaCheckOutcome:
    """① Schema Check — command_payload를 SchemaCheckedCommand로 변환한다.

    추가 필드·필수 누락·타입 불일치·빈 문자열은 SCHEMA_INVALID_PAYLOAD로 거절한다.
    Runbook별 파라미터 계약이 아직 없어(#49) 이 단계가 보는 것은 명령 봉투의 모양뿐이다.
    """
    if request.validation_context != GuardrailValidationContext.AI_CANDIDATE:
        raise NotImplementedError(
            f"{request.validation_context.value} 문맥의 Schema Check는 아직 없습니다"
        )

    try:
        command = SchemaCheckedCommand.model_validate(request.command_payload)
    except ValidationError as exc:
        # 어긋난 위치와 오류 종류만 남긴다 — payload 값은 로그로 나가지 않고,
        # 위치는 길이를 제한한다(ADR-0005 미보존 원칙과 같은 방향).
        logger.warning(
            "guardrail_schema_check_rejected",
            extra={
                "candidate_id": request.candidate_id,
                "violation_count": exc.error_count(),
                "violations": [
                    {
                        "loc": ".".join(str(part) for part in err["loc"])[
                            :_MAX_LOGGED_LOC_CHARS
                        ],
                        "type": err["type"],
                    }
                    for err in exc.errors()[:_MAX_LOGGED_VIOLATIONS]
                ],
            },
        )
        return SchemaCheckOutcome(
            step_result=_step_fail(GuardrailStep.SCHEMA_CHECK, SCHEMA_INVALID_PAYLOAD),
            command=None,
        )

    return SchemaCheckOutcome(
        step_result=_step_pass(GuardrailStep.SCHEMA_CHECK), command=command
    )


def _whitelist_fail(runbook_id: str, reason_code: str) -> ActionWhitelistOutcome:
    # runbook_id는 거절 원인 파악에 필요한 식별자라 남기되, payload가 지은 문자열이므로
    # 길이는 제한한다(정당한 ID는 전부 이 상한 안이다).
    logger.warning(
        "guardrail_action_whitelist_rejected",
        extra={
            "runbook_id": runbook_id[:_MAX_LOGGED_LOC_CHARS],
            "reason_code": reason_code,
        },
    )
    return ActionWhitelistOutcome(
        step_result=_step_fail(GuardrailStep.ACTION_WHITELIST, reason_code), draft=None
    )


def run_action_whitelist(command: SchemaCheckedCommand) -> ActionWhitelistOutcome:
    """② Action Whitelist — 확정 10종을 대조하고 AI 추천 가능 7종만 통과시킨다.

    통과한 명령만 RunbookCandidateDraft로 승격한다. 두 판정을 이미 거쳤으므로 Draft의
    AI 추천 검증(packages/schemas/agents.py)이 여기서 실패할 수는 없다.
    """
    if not is_allowed_runbook(command.runbook_id):
        return _whitelist_fail(command.runbook_id, WHITELIST_UNKNOWN_RUNBOOK)
    if not is_ai_recommendable(command.runbook_id):
        return _whitelist_fail(command.runbook_id, WHITELIST_NOT_AI_RECOMMENDABLE)

    draft = RunbookCandidateDraft(
        runbook_id=RunbookId(command.runbook_id),
        target_arn=command.target_arn,
        display_parameters=command.display_parameters,
        evidence_ids=command.evidence_ids,
    )
    return ActionWhitelistOutcome(
        step_result=_step_pass(GuardrailStep.ACTION_WHITELIST), draft=draft
    )
