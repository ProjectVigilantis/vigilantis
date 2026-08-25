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
#   - 거절 사유는 공용 계약이 정의한 단계별 Enum이다(packages/schemas/guardrails.py).
#     이 파일은 값을 정의하지 않고 ①②가 쓰는 멤버만 짧은 이름으로 다시 노출한다.
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
from typing import Annotated, Final

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from schemas.agents import RunbookCandidateDraft
from schemas.guardrails import (
    ActionWhitelistReasonCode,
    GuardrailReasonCode,
    GuardrailStep,
    GuardrailStepResult,
    GuardrailStepStatus,
    GuardrailValidationContext,
    GuardrailValidationRequest,
    SchemaCheckReasonCode,
)
from schemas.runbooks import RunbookId

from .whitelist import is_ai_recommendable, is_allowed_runbook

logger = logging.getLogger("vigilantis.ai")

# ------------------------------------------------------------------------------
# 거절 사유 코드 — 값의 정의는 packages/schemas/guardrails.py에 있고 여기는 ①②가
# 쓰는 멤버의 짧은 이름이다. 호출부·테스트가 이 이름으로 참조한다.
# ------------------------------------------------------------------------------

SCHEMA_INVALID_PAYLOAD: Final = SchemaCheckReasonCode.SCHEMA_INVALID_PAYLOAD
WHITELIST_UNKNOWN_RUNBOOK: Final = ActionWhitelistReasonCode.WHITELIST_UNKNOWN_RUNBOOK
WHITELIST_NOT_AI_RECOMMENDABLE: Final = (
    ActionWhitelistReasonCode.WHITELIST_NOT_AI_RECOMMENDABLE
)

# 위반 항목 수·위치 문자열은 payload가 키우는 값이다 — 로그 한 줄이 무한정
# 길어지지 않게 자른다. loc에는 payload가 지은 키 이름(추가 필드명·dict 키)이
# 들어오므로 길이 제한이 곧 LLM 저작 텍스트의 로그 유입 상한이다.
_MAX_LOGGED_VIOLATIONS: Final[int] = 10
_MAX_LOGGED_LOC_CHARS: Final[int] = 80

# 필드별 값 제약 — "빈 문자열은 거절"(#114)에 크기 상한을 더한 것이다.
#
# 상한이 필요한 이유: payload는 LLM 출력이라 길이·개수를 스스로 정하는데, 뒤 단계가
# 이걸 막아주지 않는다. ③은 target_arn만 보고 ④는 executor parameters만 본다(ADR-0007
# §1). display_parameters·evidence_ids는 JSONB 컬럼이라 DB 폭 제한도 없어, 상한이
# 없으면 LLM이 지은 문자열이 그대로 저장되고 관제자 대시보드까지 간다. (PR #123 리뷰)
#
# runbook_id에만 상한이 없다 — 목록에 있는지는 ②가 판정하는 것이고, 여기서 길이로
# 미리 거절하면 미등록 ID의 거절 기록이 ②가 아니라 ①에 남는다(#114 설계).
_NonEmptyStr = Annotated[str, Field(min_length=1)]
_TargetArn = Annotated[str, Field(min_length=1, max_length=512)]  # DB 컬럼 폭과 동일
_EvidenceId = Annotated[str, Field(min_length=1, max_length=36)]  # DB의 UUID 길이
# 아래 넷은 런북별 typed 파라미터 계약(#154)이 조일 때까지의 잠정 상한이다
_ParamKey = Annotated[str, Field(min_length=1, max_length=64)]
_ParamValue = Annotated[str, Field(min_length=1, max_length=256)]
_MAX_EVIDENCE_IDS: Final[int] = 50
_MAX_PARAMS: Final[int] = 20


class SchemaCheckedCommand(BaseModel):
    """①이 통과시킨 구조 — RunbookCandidateDraft와 필드 집합은 같고, runbook_id는
    문자열이다(확정 목록에 없는 ID도 이 단계는 통과해야 ②가 판정할 수 있다).
    Draft보다 엄격한 지점은 빈 문자열 거절(display_parameters 내부 포함)과
    크기 상한이다 — 위 제약 별칭·상수가 정의한다."""

    model_config = ConfigDict(extra="forbid")

    runbook_id: _NonEmptyStr
    target_arn: _TargetArn
    display_parameters: Annotated[
        dict[_ParamKey, _ParamValue], Field(max_length=_MAX_PARAMS)
    ] = Field(default_factory=dict)
    evidence_ids: Annotated[
        list[_EvidenceId], Field(max_length=_MAX_EVIDENCE_IDS)
    ] = Field(default_factory=list)


@dataclass(frozen=True)
class SchemaCheckOutcome:
    """① 결과. command는 PASS일 때만 있다."""

    step_result: GuardrailStepResult
    command: SchemaCheckedCommand | None


@dataclass(frozen=True)
class ActionWhitelistOutcome:
    """② 결과. draft는 PASS일 때만 있다."""

    step_result: GuardrailStepResult
    draft: RunbookCandidateDraft | None


def _step_pass(step: GuardrailStep) -> GuardrailStepResult:
    return GuardrailStepResult(step=step, result=GuardrailStepStatus.PASS)


def _step_fail(
    step: GuardrailStep, reason_code: GuardrailReasonCode
) -> GuardrailStepResult:
    return GuardrailStepResult(
        step=step, result=GuardrailStepStatus.FAIL, reason_code=reason_code
    )


def run_schema_check(request: GuardrailValidationRequest) -> SchemaCheckOutcome:
    """① Schema Check — command_payload를 SchemaCheckedCommand로 변환한다.

    추가 필드·필수 누락·타입 불일치·빈 문자열은 SCHEMA_INVALID_PAYLOAD로 거절한다.
    Runbook별 typed 파라미터 계약이 아직 없어(#154) 이 단계가 보는 것은 명령 봉투의
    모양뿐이다.
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


def _whitelist_fail(
    runbook_id: str, reason_code: ActionWhitelistReasonCode
) -> ActionWhitelistOutcome:
    # runbook_id는 거절 원인 파악에 필요한 식별자라 남기되, payload가 지은 문자열이므로
    # 길이는 제한한다(정당한 ID는 전부 이 상한 안이다).
    logger.warning(
        "guardrail_action_whitelist_rejected",
        extra={
            "runbook_id": runbook_id[:_MAX_LOGGED_LOC_CHARS],
            # .value로 남긴다 — str,Enum 멤버를 그대로 넘기면 포매터가 str()을 써서
            # "ActionWhitelistReasonCode.WHITELIST_UNKNOWN_RUNBOOK"이 찍히고 DB에
            # 저장되는 문자열과 로그가 어긋난다.
            "reason_code": reason_code.value,
        },
    )
    return ActionWhitelistOutcome(
        step_result=_step_fail(GuardrailStep.ACTION_WHITELIST, reason_code), draft=None
    )


def run_action_whitelist(command: SchemaCheckedCommand) -> ActionWhitelistOutcome:
    """② Action Whitelist — 확정 10종을 대조하고 AI 추천 가능 7종만 통과시킨다.

    **AI_CANDIDATE 문맥 전용이다.** 승격 대상 RunbookCandidateDraft가 "Graph가 출력하는
    후보 초안"이고, AI 추천 불가 판정(WHITELIST_NOT_AI_RECOMMENDABLE)도 AI가 제안한
    경우에만 옳다 — 롤백 3종은 ROLLBACK_EXECUTION에서는 정당한 실행 대상이다(ADR-0004
    정책 ②의 "트리거는 시스템·관제자"). 지금은 ①이 다른 문맥을 앞에서 막지만 이 함수
    자체는 문맥을 받지 않으므로, 나머지 문맥은 ③④와 함께 붙일 때 문맥 인자를 받는
    형태로 바꾼다. (PR #123 리뷰)

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
