# ==============================================================================
# [파일 설명]  담당: 안성일 (AI / Guardrail)
# 4단계 Execution Guardrail입니다. LLM 출력을 순서대로 검증해 프롬프트 인젝션/RCE를
# 차단합니다: Schema → Action Whitelist → ARN Match → AWS Dry-Run.
# 네 단계 함수와 그 종합 판정(GuardrailValidationResult 조립)을 담습니다.
# (Issue #114 · #177 · #208)
#
# 계약 원칙
#   - 단계 결과는 GuardrailStepResult(packages/schemas/guardrails.py)로만 나간다.
#     거절일 때만 reason_code를 채우고, verification_summary는 ④만 채운다.
#   - ①은 payload를 runbook_id가 문자열인 SchemaCheckedCommand로 변환한다. 여기서
#     RunbookCandidateDraft로 바로 가면 그 모델이 AI 추천 7종만 받으므로 미등록
#     ID·롤백 ID가 ①에서 터지고 ②가 걸러낼 것이 남지 않는다. 목록 대조는 ②다.
#   - ①은 parameters를 Runbook별 typed 계약(#154)에도 대조한다. 모델을 가진 ID만
#     대조하고 모르는 ID는 봉투 검사로 끝낸다 — 그래야 위 규칙과 어긋나지 않는다.
#   - ②를 통과한 명령만 RunbookCandidateDraft로 승격한다.
#   - ③은 수집된 자산인지만 대조하고 자산 종류↔Runbook 짝은 보지 않는다. 이 단계가
#     쓸 수 있는 사유 코드가 ARN_TARGET_NOT_MANAGED 하나뿐이라, 짝 불일치를 여기서
#     거절하면 거절 기록이 사실과 달라진다.
#   - ③은 DB를 직접 부르지 않고 조회를 인자로 받는다(ManagedAssetLookup). 외부 자원의
#     타입이 이 계층으로 넘어오지 않게 하는 것은 model_client.AIModelClient와 같다.
#   - ④도 같은 이유로 AWS 판정을 인자로 받는다(CandidatePrecheck). ai/는
#     services/aws/를 직접 부르지 않고, 오가는 타입은 packages/schemas의 것뿐이다.
#   - ④는 판정을 다시 분류하지 않는다. executor precheck의 PrecheckOutcome을
#     GuardrailStepResult로 1:1로 옮기기만 한다(ADR-0007 §1 호출 규약) — 사유 코드
#     분류가 두 곳으로 갈라지면 거절 기록과 실제 판정이 어긋난다.
#   - 거절 사유는 공용 계약이 정의한 단계별 Enum이다(packages/schemas/guardrails.py).
#     이 파일은 값을 정의하지 않고 ①②③이 쓰는 멤버만 짧은 이름으로 다시 노출한다.
#     ④의 코드는 executor가 골라 넘기므로 여기서 고를 일이 없다.
#   - 종합 판정은 첫 FAIL에서 멈추고 이후 단계를 NOT_RUN으로 남긴다. 이 단락이 곧
#     "거절된 대상으로 AWS를 부르지 않는다"는 보장이다 — ③이 막은 ARN이 ④까지 가면
#     범위를 벗어난 자원에 실제로 요청이 나간다.
#   - 검증 문맥은 AI_CANDIDATE만 구현한다. 다른 문맥은 payload 모양이 달라 여기서
#     판정하면 거절 기록이 틀리므로, FAIL이 아니라 예외로 막는다.
# ==============================================================================

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Annotated, Final, Protocol, Union

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    ValidationError,
)

from schemas.agents import RunbookCandidateDraft
from schemas.guardrails import (
    GUARDRAIL_STEP_ORDER,
    ActionWhitelistReasonCode,
    ArnMatchReasonCode,
    GuardrailDecision,
    GuardrailReasonCode,
    GuardrailStep,
    GuardrailStepResult,
    GuardrailStepStatus,
    GuardrailValidationContext,
    GuardrailValidationRequest,
    GuardrailValidationResult,
    SchemaCheckReasonCode,
)
from schemas.precheck import PrecheckOutcome
from schemas.runbook_parameters import CANDIDATE_PARAMETER_MODELS
from schemas.runbooks import RunbookId

from .whitelist import is_ai_recommendable, is_allowed_runbook

logger = logging.getLogger("vigilantis.ai")

# ------------------------------------------------------------------------------
# 거절 사유 코드 — 값의 정의는 packages/schemas/guardrails.py에 있고 여기는 ①②③이
# 쓰는 멤버의 짧은 이름이다. 호출부·테스트가 이 이름으로 참조한다.
# ------------------------------------------------------------------------------

SCHEMA_INVALID_PAYLOAD: Final = SchemaCheckReasonCode.SCHEMA_INVALID_PAYLOAD
WHITELIST_UNKNOWN_RUNBOOK: Final = ActionWhitelistReasonCode.WHITELIST_UNKNOWN_RUNBOOK
WHITELIST_NOT_AI_RECOMMENDABLE: Final = (
    ActionWhitelistReasonCode.WHITELIST_NOT_AI_RECOMMENDABLE
)
ARN_TARGET_NOT_MANAGED: Final = ArnMatchReasonCode.ARN_TARGET_NOT_MANAGED

# 위반 항목 수·위치 문자열은 payload가 키우는 값이다 — 로그 한 줄이 무한정
# 길어지지 않게 자른다. loc에는 payload가 지은 키 이름(추가 필드명·dict 키)이
# 들어오므로 길이 제한이 곧 LLM 저작 텍스트의 로그 유입 상한이다.
_MAX_LOGGED_VIOLATIONS: Final[int] = 10
_MAX_LOGGED_LOC_CHARS: Final[int] = 80


def _reject_nul(value: str) -> str:
    """PostgreSQL의 text·jsonb 는 NUL(0x00)을 담지 못한다 — 조회·저장에서 DataError다.

    ③이 target_arn 으로 DB를 조회하고 evidence_ids·parameters 는 후보 저장 시
    JSONB 컬럼에 담기므로, 여기서 막지 않으면 거절이 기록되는 대신 조회·저장이
    예외로 끝난다. 값이 아니라 담길 수 있는 문자인지의 문제라 크기 상한(DB 컬럼
    폭)과 같은 부류이며, 형식 판정이 아니다. runbook_id 는 여기서도 보지 않는다 —
    ID 판정은 ②의 몫이고(#114), ②의 거절은 저장 없이 기록된다.
    """
    if "\x00" in value:
        raise ValueError("NUL(0x00) 문자는 담을 수 없습니다")
    return value


# 필드별 값 제약 — "빈 문자열은 거절"(#114)에 크기 상한을 더한 것이다.
#
# 상한이 필요한 이유: payload는 LLM 출력이라 길이·개수를 스스로 정하는데, 뒤 단계가
# 이걸 막아주지 않는다. ③은 target_arn만 본다(ADR-0007 §1). parameters·evidence_ids는
# JSONB 컬럼이라 DB 폭 제한도 없어, 상한이 없으면 LLM이 지은 문자열이 그대로 저장되고
# 관제자 대시보드까지 간다. (PR #123 리뷰)
#
# runbook_id에만 상한이 없다 — 목록에 있는지는 ②가 판정하는 것이고, 여기서 길이로
# 미리 거절하면 미등록 ID의 거절 기록이 ②가 아니라 ①에 남는다(#114 설계).
_MAX_ARN_CHARS: Final[int] = 512  # DB 컬럼 폭과 동일
_NonEmptyStr = Annotated[str, Field(min_length=1)]
_TargetArn = Annotated[
    str, Field(min_length=1, max_length=_MAX_ARN_CHARS), AfterValidator(_reject_nul)
]
_EvidenceId = Annotated[
    str, Field(min_length=1, max_length=36), AfterValidator(_reject_nul)
]  # DB의 UUID 길이
_ParamKey = Annotated[
    str, Field(min_length=1, max_length=64), AfterValidator(_reject_nul)
]
# 봉투 단계의 값 제약이다. Runbook별 typed 계약(#154)이 알려진 ID의 값을 판정하지만,
# 미등록 ID는 그 모델이 없어 이 상한이 마지막 방어다. 중첩 구조는 받지 않는다 —
# 스칼라만 허용하면 LLM이 파라미터 안에 다른 모양을 밀어 넣을 수 없다.
# Strict 계열을 쓰는 이유는 "100"이 100으로, 1이 True로 바뀐 채 typed 계약에 닿지
# 않게 하기 위해서다(bool은 int의 하위 타입이다).
_ParamScalar = Union[
    Annotated[str, Field(min_length=1, max_length=256), AfterValidator(_reject_nul)],
    StrictInt,
    StrictBool,
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


@dataclass(frozen=True)
class ArnMatchOutcome:
    """③ 결과. draft는 PASS일 때만 있고, ②가 넘긴 것을 그대로 통과시킨다."""

    step_result: GuardrailStepResult
    draft: RunbookCandidateDraft | None


@dataclass(frozen=True)
class AwsDryRunOutcome:
    """④ 결과. draft는 PASS일 때만 있고, ③이 넘긴 것을 그대로 통과시킨다."""

    step_result: GuardrailStepResult
    draft: RunbookCandidateDraft | None


@dataclass(frozen=True)
class GuardrailOutcome:
    """네 단계 종합 결과. draft는 네 단계를 모두 통과했을 때만 있다 — 후보를
    EXECUTABLE로 저장하는 근거이며, 거절이면 남는 것은 result의 거절 기록뿐이다."""

    result: GuardrailValidationResult
    draft: RunbookCandidateDraft | None


def _step_pass(
    step: GuardrailStep, verification_summary: str | None = None
) -> GuardrailStepResult:
    return GuardrailStepResult(
        step=step,
        result=GuardrailStepStatus.PASS,
        verification_summary=verification_summary,
    )


def _step_fail(
    step: GuardrailStep,
    reason_code: GuardrailReasonCode,
    verification_summary: str | None = None,
) -> GuardrailStepResult:
    return GuardrailStepResult(
        step=step,
        result=GuardrailStepStatus.FAIL,
        reason_code=reason_code,
        verification_summary=verification_summary,
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
    자체는 문맥을 받지 않으므로, 나머지 문맥을 구현할 때 문맥 인자를 받는 형태로
    바꾼다 — 그 문맥들은 호출 시점 자체가 아직 결정되지 않았다(ADR-0007 §Consequences).
    (PR #123 리뷰)

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


class ManagedAssetLookup(Protocol):
    """수집된 자산인지 답하는 조회 경계 — 구현은 이 계층 밖에 둔다.

    호출부가 DB 조회(apps/core-api/db/repositories/assets.py::get_asset_by_arn)를
    감아 넘긴다. ai/가 db/를 직접 부르지 않는 이유는 model_client.AIModelClient와
    같다 — 외부 자원의 타입(여기서는 ORM 객체)이 AI 계층으로 넘어오지 않는다.
    """

    def __call__(self, target_arn: str, /) -> bool: ...


def run_arn_match(
    draft: RunbookCandidateDraft, is_managed_arn: ManagedAssetLookup
) -> ArnMatchOutcome:
    """③ ARN Match — 대상이 우리가 수집한 자산인지 대조한다(Scope Escalation 차단).

    판정 기준은 수집 여부 하나이며, 계정·리전 접두어로 거르지 않는다.
    arn:aws:ec2:<리전>:<계정>:instance/* 는 정당한 대상과 같은 문자열로 시작해
    접두어 검사를 그대로 통과하기 때문이다 — 이 단계가 막아야 하는 것이 바로
    그 부류다.

    자산 종류가 Runbook과 맞는지(SG ARN에 EC2 Rightsizing 등)는 보지 않는다.
    이 단계가 쓸 수 있는 사유 코드가 ARN_TARGET_NOT_MANAGED 하나뿐이라, 짝
    불일치를 여기서 거절하면 수집된 자산이 "미수집"으로 기록돼 거절 사유가
    사실과 달라진다.
    """
    if is_managed_arn(draft.target_arn):
        return ArnMatchOutcome(
            step_result=_step_pass(GuardrailStep.ARN_MATCH), draft=draft
        )

    # 범위를 벗어난 대상을 지목했다는 것 자체가 조사 대상이라 ARN을 남긴다.
    # 길이는 자른다 — Draft의 target_arn에는 상한이 없어(packages/schemas/agents.py)
    # ①을 거치지 않고 만들어진 Draft가 오면 LLM이 지은 문자열이 그대로 들어온다.
    logger.warning(
        "guardrail_arn_match_rejected",
        extra={
            "runbook_id": draft.runbook_id.value,
            "target_arn": draft.target_arn[:_MAX_ARN_CHARS],
            "reason_code": ARN_TARGET_NOT_MANAGED.value,
        },
    )
    return ArnMatchOutcome(
        step_result=_step_fail(GuardrailStep.ARN_MATCH, ARN_TARGET_NOT_MANAGED),
        draft=None,
    )


class CandidatePrecheck(Protocol):
    """④가 부르는 AWS 판정 경계 — 구현은 이 계층 밖에 둔다.

    호출부가 services/aws/executor.py::precheck()를 감아 넘긴다. 후보를 실행
    파라미터로 바꾸는 변환(schemas.runbook_parameters.build_precheck_parameters)이
    그 감싸는 자리의 몫인 이유는, 변환에 필요한 값이 이 계층에 없기 때문이다 —
    target_arn이 가리키는 자원 ID는 executor.parse_arn이 해석하고, 나머지는 DB·AWS
    조회로 채운다. ai/가 services/aws/를 직접 부르지 않는 이유는 ManagedAssetLookup과
    같다.

    감싸는 쪽이 지켜야 하는 것 둘.
      - **RUNBOOK_NACL_RESTORE 후보에는 backup_loader를 배선한다.** 백업 레코드가
        필요한 4종 중 이 하나만 롤백 3종이 아니라 AI 추천 7종이라(schemas/runbooks.py)
        ②를 통과해 여기까지 온다. 미배선이면 precheck는 FAIL이 아니라 RuntimeError다
        (ADR-0007 §1 — 배선 오류를 거절로 기록하면 멀쩡한 명령에 거절 사유가 붙는다).
      - **동기 함수다**(ADR-0007 §1). async 문맥에서 threadpool로 감싸는 것도 호출부다.
    """

    def __call__(self, draft: RunbookCandidateDraft, /) -> PrecheckOutcome: ...


def run_aws_dry_run(
    draft: RunbookCandidateDraft, precheck: CandidatePrecheck
) -> AwsDryRunOutcome:
    """④ AWS Dry-Run — 실제 AWS에 물어 판정한다(ADR-0007).

    판정을 여기서 다시 분류하지 않는다. PrecheckOutcome을 GuardrailStepResult로
    1:1로 옮기기만 한다(ADR-0007 §1 호출 규약) — 사유 코드 분류가 두 곳에 생기면
    거절 기록과 executor가 실제로 내린 판정이 어긋난다. 단계↔코드 정합은 공용 계약이
    강제하므로(GuardrailStepResult), 다른 단계의 코드가 섞이면 여기서 ValidationError다.

    verification_summary는 PASS·FAIL 모두 옮긴다. 무엇을 확인하지 못했는지는 통과한
    경우에도 관제자에게 나가야 하는 정보다(ADR-0007 §3).

    미구현 런북을 호출 전에 거르지 않는다 — 그 판정의 소유권은 디스패치 테이블을
    가진 executor다(PRECHECK_NOT_IMPLEMENTED).
    """
    outcome = precheck(draft)

    if outcome.passed:
        return AwsDryRunOutcome(
            step_result=_step_pass(
                GuardrailStep.AWS_DRY_RUN, outcome.verification_summary
            ),
            draft=draft,
        )

    # executor도 자체 로그를 남기지만(vigilantis.aws) AWS에 닿기 전에 끝난 거절은
    # 남기지 않는다. 거절 기록을 단계별로 훑을 수 있어야 하므로 ②③과 같은 자리에
    # 한 줄을 남긴다. 요약 문자열은 단계 결과에 이미 담겨 있어 여기서 다시 쓰지 않는다.
    logger.warning(
        "guardrail_aws_dry_run_rejected",
        extra={
            "runbook_id": draft.runbook_id.value,
            "target_arn": draft.target_arn[:_MAX_ARN_CHARS],
            "reason_code": outcome.reason_code.value if outcome.reason_code else None,
        },
    )
    return AwsDryRunOutcome(
        step_result=_step_fail(
            GuardrailStep.AWS_DRY_RUN,
            outcome.reason_code,
            outcome.verification_summary,
        ),
        draft=None,
    )


def _validation_result(steps: list[GuardrailStepResult]) -> GuardrailValidationResult:
    """실행된 단계 결과에 NOT_RUN을 채워 고정 4단계 계약으로 만든다.

    남은 단계는 GUARDRAIL_STEP_ORDER에서 잘라 온다 — 단계 개수·순서를 이 파일이
    다시 세면 계약과 갈릴 수 있고, 그러면 조립이 통째로 거절된다.
    """
    failed = next((s for s in steps if s.result == GuardrailStepStatus.FAIL), None)
    return GuardrailValidationResult(
        result=GuardrailDecision.FAIL if failed else GuardrailDecision.PASS,
        failed_step=failed.step if failed else None,
        steps=[
            *steps,
            *(
                GuardrailStepResult(step=step, result=GuardrailStepStatus.NOT_RUN)
                for step in GUARDRAIL_STEP_ORDER[len(steps):]
            ),
        ],
        validated_at=datetime.now(timezone.utc),
    )


def run_guardrail_validation(
    request: GuardrailValidationRequest,
    *,
    is_managed_arn: ManagedAssetLookup,
    precheck: CandidatePrecheck,
) -> GuardrailOutcome:
    """네 단계를 순서대로 돌려 GuardrailValidationResult를 조립한다.

    첫 FAIL에서 멈춘다 — 뒤 단계는 실행하지 않고 NOT_RUN으로 남긴다. 이것이 곧
    "거절된 명령으로 AWS를 부르지 않는다"는 보장이다: ③이 막은 ARN이 ④까지 가면
    범위를 벗어난 자원에 조회·DryRun 요청이 실제로 나간다.

    조회 경계 둘(is_managed_arn·precheck)은 키워드로만 받는다 — 호출부가 순서를
    바꿔 넘기면 두 경계가 조용히 뒤바뀐다.

    AI_CANDIDATE 문맥 전용이다(①이 다른 문맥을 NotImplementedError로 막는다).
    """
    schema_check = run_schema_check(request)
    steps = [schema_check.step_result]
    if schema_check.command is None:
        return GuardrailOutcome(result=_validation_result(steps), draft=None)

    whitelist = run_action_whitelist(schema_check.command)
    steps.append(whitelist.step_result)
    if whitelist.draft is None:
        return GuardrailOutcome(result=_validation_result(steps), draft=None)

    arn_match = run_arn_match(whitelist.draft, is_managed_arn)
    steps.append(arn_match.step_result)
    if arn_match.draft is None:
        return GuardrailOutcome(result=_validation_result(steps), draft=None)

    dry_run = run_aws_dry_run(arn_match.draft, precheck)
    steps.append(dry_run.step_result)
    return GuardrailOutcome(result=_validation_result(steps), draft=dry_run.draft)
