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
#   - ①은 parameters를 Runbook별 typed 계약(#154)에도 대조한다. 모델을 가진 ID만
#     대조하고 모르는 ID는 봉투 검사로 끝낸다 — 그래야 위 규칙과 어긋나지 않는다.
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
from typing import Annotated, Final, Union

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    ValidationError,
)

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
from schemas.runbook_parameters import CANDIDATE_PARAMETER_MODELS
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
# 이걸 막아주지 않는다. ③은 target_arn만 본다(ADR-0007 §1). parameters·evidence_ids는
# JSONB 컬럼이라 DB 폭 제한도 없어, 상한이 없으면 LLM이 지은 문자열이 그대로 저장되고
# 관제자 대시보드까지 간다. (PR #123 리뷰)
#
# runbook_id에만 상한이 없다 — 목록에 있는지는 ②가 판정하는 것이고, 여기서 길이로
# 미리 거절하면 미등록 ID의 거절 기록이 ②가 아니라 ①에 남는다(#114 설계).
_NonEmptyStr = Annotated[str, Field(min_length=1)]
_TargetArn = Annotated[str, Field(min_length=1, max_length=512)]  # DB 컬럼 폭과 동일
_EvidenceId = Annotated[str, Field(min_length=1, max_length=36)]  # DB의 UUID 길이
_ParamKey = Annotated[str, Field(min_length=1, max_length=64)]
# 봉투 단계의 값 제약이다. Runbook별 typed 계약(#154)이 알려진 ID의 값을 판정하지만,
# 미등록 ID는 그 모델이 없어 이 상한이 마지막 방어다. 중첩 구조는 받지 않는다 —
# 스칼라만 허용하면 LLM이 파라미터 안에 다른 모양을 밀어 넣을 수 없다.
# Strict 계열을 쓰는 이유는 "100"이 100으로, 1이 True로 바뀐 채 typed 계약에 닿지
# 않게 하기 위해서다(bool은 int의 하위 타입이다).
_ParamScalar = Union[
    Annotated[str, Field(min_length=1, max_length=256)], StrictInt, StrictBool
]
_MAX_EVIDENCE_IDS: Final[int] = 50
_MAX_PARAMS: Final[int] = 20


class SchemaCheckedCommand(BaseModel):
    """①이 통과시킨 구조 — RunbookCandidateDraft와 필드 집합은 같고, runbook_id는
    문자열이다(확정 목록에 없는 ID도 이 단계는 통과해야 ②가 판정할 수 있다).
    parameters도 아직 dict다 — Runbook별 모델로 좁히는 것은 run_schema_check가
    ID를 알고 나서 하고, Draft로 승격될 때 typed 값이 된다.
    Draft보다 엄격한 지점은 빈 문자열 거절(parameters 내부 포함)과 크기 상한이다 —
    위 제약 별칭·상수가 정의한다."""

    model_config = ConfigDict(extra="forbid")

    runbook_id: _NonEmptyStr
    target_arn: _TargetArn
    parameters: Annotated[
        dict[_ParamKey, _ParamScalar], Field(max_length=_MAX_PARAMS)
    ] = Field(default_factory=dict)
    # 비어 있을 수 없다 — precheck의 evidence_id(단수)를 첫 항목에서 뽑는다(#154)
    evidence_ids: Annotated[
        list[_EvidenceId], Field(min_length=1, max_length=_MAX_EVIDENCE_IDS)
    ]


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


def _schema_check_fail(
    candidate_id: str | None, exc: ValidationError
) -> SchemaCheckOutcome:
    # 어긋난 위치와 오류 종류만 남긴다 — payload 값은 로그로 나가지 않고,
    # 위치는 길이를 제한한다(ADR-0005 미보존 원칙과 같은 방향).
    logger.warning(
        "guardrail_schema_check_rejected",
        extra={
            "candidate_id": candidate_id,
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


def _candidate_parameters_model(runbook_id: str) -> type[BaseModel] | None:
    """runbook_id가 가진 후보 파라미터 모델. 모르는 ID면 None.

    확정 목록에 없는 ID와 롤백 3종이 모두 None이다 — 전자는 ②의 대조 대상이고
    후자는 애초에 AI 후보가 될 수 없다(ADR-0004 정책 ②). 둘 다 ①이 거절하면
    거절 기록에 실제로 막은 단계가 남지 않으므로, 여기서는 통과시킨다.
    """
    try:
        return CANDIDATE_PARAMETER_MODELS.get(RunbookId(runbook_id))
    except ValueError:
        return None


def run_schema_check(request: GuardrailValidationRequest) -> SchemaCheckOutcome:
    """① Schema Check — command_payload를 SchemaCheckedCommand로 변환한다.

    추가 필드·필수 누락·타입 불일치·빈 문자열은 SCHEMA_INVALID_PAYLOAD로 거절한다.
    봉투를 통과하면 parameters를 Runbook별 typed 계약(#154)에 한 번 더 대조한다 —
    형식 위반이 ④ AWS Dry-Run까지 가지 않고 여기서 끝난다.
    """
    if request.validation_context != GuardrailValidationContext.AI_CANDIDATE:
        raise NotImplementedError(
            f"{request.validation_context.value} 문맥의 Schema Check는 아직 없습니다"
        )

    try:
        command = SchemaCheckedCommand.model_validate(request.command_payload)
    except ValidationError as exc:
        return _schema_check_fail(request.candidate_id, exc)

    model = _candidate_parameters_model(command.runbook_id)
    if model is not None:
        try:
            model.model_validate(command.parameters)
        except ValidationError as exc:
            return _schema_check_fail(request.candidate_id, exc)

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
            # 값 문자열로 명시 고정 — services/aws/executor.py의 _fail과 같은 표기다.
            # 포매터(JsonLineFormatter)가 str,Enum을 값으로 직렬화하므로 멤버를 그대로
            # 넘겨도 결과는 같지만, DB에 저장되는 값과 동일함을 호출부에서 드러낸다.
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
    AI 추천 검증(packages/schemas/agents.py)이 여기서 실패할 수는 없다. parameters도
    같다 — 승격되는 ID는 ①이 typed 계약으로 이미 대조한 ID다.
    """
    if not is_allowed_runbook(command.runbook_id):
        return _whitelist_fail(command.runbook_id, WHITELIST_UNKNOWN_RUNBOOK)
    if not is_ai_recommendable(command.runbook_id):
        return _whitelist_fail(command.runbook_id, WHITELIST_NOT_AI_RECOMMENDABLE)

    draft = RunbookCandidateDraft(
        runbook_id=RunbookId(command.runbook_id),
        target_arn=command.target_arn,
        parameters=command.parameters,
        evidence_ids=command.evidence_ids,
    )
    return ActionWhitelistOutcome(
        step_result=_step_pass(GuardrailStep.ACTION_WHITELIST), draft=draft
    )
