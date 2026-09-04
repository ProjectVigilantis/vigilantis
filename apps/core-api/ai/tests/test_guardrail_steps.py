"""가드레일 네 단계 함수와 그 종합 판정 테스트 — 통과 경로와 단계별 거절.

이슈 #114 설계 의도의 회귀 테스트를 겸한다: ①은 runbook_id를 문자열로만 보고 목록
대조는 ②가 한다. 미등록 ID가 ①에서 터지면 거절 기록에 실제로 막힌 단계가 남지 않는다.
"""

from datetime import timezone

import pytest
from pydantic import ValidationError
from ai.guardrails import (
    ARN_TARGET_NOT_MANAGED,
    SCHEMA_INVALID_PAYLOAD,
    WHITELIST_NOT_AI_RECOMMENDABLE,
    WHITELIST_NOT_ROLLBACK_RUNBOOK,
    WHITELIST_UNKNOWN_RUNBOOK,
    ManagedAssetLookup,
    RollbackExecutionCommand,
    SchemaCheckedCommand,
    run_action_whitelist,
    run_arn_match,
    run_aws_dry_run,
    run_guardrail_validation,
    run_schema_check,
)
from ai.whitelist import AI_RECOMMENDABLE_RUNBOOK_IDS, ROLLBACK_RUNBOOK_IDS, RunbookId
from schemas.agents import RunbookCandidateDraft
from schemas.guardrails import (
    GUARDRAIL_STEP_ORDER,
    ActionWhitelistReasonCode,
    ArnMatchReasonCode,
    GuardrailDecision,
    GuardrailStep,
    GuardrailStepStatus,
    GuardrailValidationContext,
    GuardrailValidationRequest,
    SchemaCheckReasonCode,
)
from schemas.precheck import (
    PrecheckOutcome,
    PrecheckReasonCode,
    VerificationMethod,
    build_verification_summary,
)
from schemas.runbook_parameters import Ec2RevertSizeParameters

TARGET_ARN = "arn:aws:ec2:ap-northeast-2:123456789012:instance/i-0123456789abcdef0"

VALID_PAYLOAD = {
    "runbook_id": RunbookId.RUNBOOK_EC2_RIGHTSIZING.value,
    "target_arn": TARGET_ARN,
    "parameters": {"target_instance_type": "t3.small"},
    "evidence_ids": ["ev-1", "ev-2"],
}

# Runbook별로 AI가 정하는 값(#154). ①이 이 표와 대조하므로, 여기 없는 키를 실으면
# 그 Runbook의 후보가 아니라 형식 위반이다.
PARAMS_BY_RUNBOOK = {
    "RUNBOOK_EC2_ISOLATE": {},
    "RUNBOOK_NACL_ADD_DENY": {
        "rule_number": 100, "cidr_block": "203.0.113.5/32", "protocol": "-1",
    },
    "RUNBOOK_NACL_RESTORE": {"rule_number": 100, "egress": False},
    "RUNBOOK_SG_DELETE_ISOLATED": {},
    "RUNBOOK_EC2_RIGHTSIZING": {"target_instance_type": "t3.small"},
    "RUNBOOK_EC2_ENABLE_AUTOSCALING": {"min_size": 1, "max_size": 2},
    "RUNBOOK_EBS_DELETE_UNATTACHED": {},
}

# 목록에 없는 ID는 typed 계약이 없어 봉투 검사로 끝난다 — 크기 상한이 마지막 방어다
UNTYPED_ID = "RUNBOOK_EBS_SNAPSHOT"
UNTYPED_PAYLOAD = {**VALID_PAYLOAD, "runbook_id": UNTYPED_ID}

# ①은 통과시키고 ②가 거절해야 하는 ID들 — 폐기 2종·미등록·표기 불일치
UNKNOWN_RUNBOOK_IDS = [
    "RUNBOOK_EBS_SNAPSHOT",
    "RUNBOOK_EC2_DOWNSIZE",
    "RUNBOOK_IP_BLOCK",
    "runbook_ec2_isolate",
    "RUNBOOK_EC2_ISOLATE ",
]


def _request(payload: dict) -> GuardrailValidationRequest:
    return GuardrailValidationRequest(
        validation_context=GuardrailValidationContext.AI_CANDIDATE,
        candidate_id="cand-1",
        command_payload=payload,
    )


def _checked(**overrides) -> SchemaCheckedCommand:
    """②를 보는 테스트의 입력 — ①을 실제로 통과시킨 값에서 일부만 바꾼다.

    runbook_id만 바꾸면 ①의 typed 대조에서 걸리므로 그 Runbook의 파라미터를 함께 싣는다.
    """
    runbook_id = overrides.get("runbook_id", VALID_PAYLOAD["runbook_id"])
    payload = {**VALID_PAYLOAD, "parameters": PARAMS_BY_RUNBOOK.get(runbook_id, {})}
    outcome = run_schema_check(_request({**payload, **overrides}))
    assert outcome.command is not None
    return outcome.command


# ------------------------------------------------------------------------------
# ① Schema Check
# ------------------------------------------------------------------------------


def test_schema_check_passes_valid_payload():
    outcome = run_schema_check(_request(VALID_PAYLOAD))

    assert outcome.step_result.step is GuardrailStep.SCHEMA_CHECK
    assert outcome.step_result.result is GuardrailStepStatus.PASS
    assert outcome.step_result.reason_code is None
    assert outcome.step_result.verification_summary is None
    assert outcome.command == SchemaCheckedCommand(**VALID_PAYLOAD)


@pytest.mark.parametrize(
    "payload",
    [
        {
            "runbook_id": RunbookId.RUNBOOK_SG_DELETE_ISOLATED.value,
            "target_arn": TARGET_ARN,
            "evidence_ids": ["ev-1"],
        },
        {
            "runbook_id": RunbookId.RUNBOOK_SG_DELETE_ISOLATED.value,
            "target_arn": TARGET_ARN,
            "parameters": {},
            "evidence_ids": ["ev-1"],
        },
    ],
    ids=["omitted", "explicit_empty"],
)
def test_schema_check_allows_empty_parameters_where_ai_decides_nothing(payload):
    # AI가 정할 값이 없는 Runbook 3종은 parameters가 빈 dict다(#154) — 생략도,
    # 명시적 빈 dict도 통과한다. 값이 필요한 Runbook에서 비면 아래에서 거절된다.
    outcome = run_schema_check(_request(payload))

    assert outcome.step_result.result is GuardrailStepStatus.PASS
    assert outcome.command is not None
    assert outcome.command.parameters == {}


@pytest.mark.parametrize("runbook_id", sorted(PARAMS_BY_RUNBOOK))
def test_schema_check_accepts_each_runbooks_own_parameters(runbook_id):
    outcome = run_schema_check(_request({
        **VALID_PAYLOAD,
        "runbook_id": runbook_id,
        "parameters": PARAMS_BY_RUNBOOK[runbook_id],
    }))

    assert outcome.step_result.result is GuardrailStepStatus.PASS


@pytest.mark.parametrize("runbook_id", sorted(set(PARAMS_BY_RUNBOOK) - {"RUNBOOK_NACL_ADD_DENY"}))
def test_schema_check_rejects_another_runbooks_parameters(runbook_id):
    """런북과 무관한 파라미터는 ①에서 끝난다 — ④ AWS Dry-Run까지 갈 값이 아니다(#154)."""
    outcome = run_schema_check(_request({
        **VALID_PAYLOAD,
        "runbook_id": runbook_id,
        "parameters": PARAMS_BY_RUNBOOK["RUNBOOK_NACL_ADD_DENY"],
    }))

    assert outcome.command is None
    assert outcome.step_result.reason_code == SCHEMA_INVALID_PAYLOAD


def test_schema_check_does_not_type_check_parameters_of_an_unlisted_runbook():
    """목록에 없는 ID는 typed 계약이 없다 — 여기서 거절하면 ②가 걸러낼 것이 없어진다."""
    outcome = run_schema_check(_request({
        **UNTYPED_PAYLOAD, "parameters": {"whatever": "value"},
    }))

    assert outcome.step_result.result is GuardrailStepStatus.PASS


@pytest.mark.parametrize(
    "payload",
    [
        {**VALID_PAYLOAD, "severity": "HIGH"},
        {k: v for k, v in VALID_PAYLOAD.items() if k != "runbook_id"},
        {k: v for k, v in VALID_PAYLOAD.items() if k != "target_arn"},
        {**VALID_PAYLOAD, "runbook_id": 123},
        {**VALID_PAYLOAD, "target_arn": None},
        {**VALID_PAYLOAD, "evidence_ids": "ev-1"},
        {**VALID_PAYLOAD, "parameters": {"cpu_threshold": 12}},
        {**VALID_PAYLOAD, "parameters": {"target_instance_type": {"nested": 1}}},
        {**VALID_PAYLOAD, "runbook_id": ""},
        {**VALID_PAYLOAD, "target_arn": ""},
        {**VALID_PAYLOAD, "evidence_ids": [""]},
        {**VALID_PAYLOAD, "evidence_ids": []},
        {**VALID_PAYLOAD, "parameters": {"": "t3.small"}},
        {**VALID_PAYLOAD, "parameters": {"target_instance_type": ""}},
        {**VALID_PAYLOAD, "target_arn": "a" * 513},
        {**VALID_PAYLOAD, "evidence_ids": ["e" * 37]},
        {**VALID_PAYLOAD, "evidence_ids": ["ev"] * 51},
        {**UNTYPED_PAYLOAD, "parameters": {"k" * 65: "t3.small"}},
        {**UNTYPED_PAYLOAD, "parameters": {"target_instance_type": "v" * 257}},
        {**UNTYPED_PAYLOAD, "parameters": {f"k{i}": "v" for i in range(21)}},
    ],
    ids=[
        "extra_field",
        "missing_runbook_id",
        "missing_target_arn",
        "int_runbook_id",
        "null_target_arn",
        "str_instead_of_list",
        "param_key_not_in_contract",
        "nested_param_value",
        "empty_runbook_id",
        "empty_target_arn",
        "empty_evidence_id",
        "no_evidence_ids",
        "empty_param_key",
        "empty_param_value",
        "target_arn_over_column_width",
        "evidence_id_over_uuid_length",
        "too_many_evidence_ids",
        "param_key_too_long",
        "param_value_too_long",
        "too_many_params",
    ],
)
def test_schema_check_rejects_malformed_payload(payload):
    outcome = run_schema_check(_request(payload))

    assert outcome.command is None
    assert outcome.step_result.step is GuardrailStep.SCHEMA_CHECK
    assert outcome.step_result.result is GuardrailStepStatus.FAIL
    assert outcome.step_result.reason_code == SCHEMA_INVALID_PAYLOAD
    assert outcome.step_result.verification_summary is None


@pytest.mark.parametrize(
    "payload",
    [
        {**VALID_PAYLOAD, "target_arn": "a" * 512},
        {**VALID_PAYLOAD, "evidence_ids": ["e" * 36]},
        {**VALID_PAYLOAD, "evidence_ids": ["ev"] * 50},
        {**UNTYPED_PAYLOAD, "parameters": {"k" * 64: "t3.small"}},
        {**UNTYPED_PAYLOAD, "parameters": {"target_instance_type": "v" * 256}},
        {**UNTYPED_PAYLOAD, "parameters": {f"k{i}": "v" for i in range(20)}},
    ],
    ids=[
        "target_arn_at_column_width",
        "evidence_id_at_uuid_length",
        "evidence_ids_at_count_cap",
        "param_key_at_cap",
        "param_value_at_cap",
        "params_at_count_cap",
    ],
)
def test_schema_check_accepts_values_at_cap(payload):
    # 상한은 경계값까지 허용한다 — 정당한 값이 한 글자 차이로 막히면 안 된다
    outcome = run_schema_check(_request(payload))

    assert outcome.step_result.result is GuardrailStepStatus.PASS


def test_schema_check_does_not_cap_runbook_id():
    # runbook_id에만 길이 상한이 없다 — 길이로 미리 막으면 "목록에 없는 조치"라는
    # 거절 사유가 ②가 아니라 ①에 남는다(#114 설계)
    outcome = run_schema_check(_request({**VALID_PAYLOAD, "runbook_id": "R" * 5000}))

    assert outcome.step_result.result is GuardrailStepStatus.PASS
    assert run_action_whitelist(outcome.command).step_result.reason_code == (
        WHITELIST_UNKNOWN_RUNBOOK
    )


@pytest.mark.parametrize("runbook_id", UNKNOWN_RUNBOOK_IDS + sorted(ROLLBACK_RUNBOOK_IDS))
def test_schema_check_leaves_id_judgement_to_whitelist(runbook_id):
    # ①은 목록을 보지 않는다 — 미등록 ID·롤백 ID도 구조만 맞으면 통과시킨다
    outcome = run_schema_check(_request({**VALID_PAYLOAD, "runbook_id": runbook_id}))

    assert outcome.step_result.result is GuardrailStepStatus.PASS
    assert outcome.command is not None
    assert outcome.command.runbook_id == runbook_id


def test_schema_check_rejects_unimplemented_contexts():
    """아직 구현하지 않은 문맥은 payload 모양이 달라 FAIL로 기록하면 거절 사유가 틀린다.

    ROLLBACK_EXECUTION은 #241에서 구현했으므로 여기 없다 — 남은 것은 AUTO_ISOLATION
    하나이고, 그쪽은 호출 시점 자체가 아직 정해지지 않았다(ADR-0007 §Consequences).
    """
    request = GuardrailValidationRequest(
        validation_context=GuardrailValidationContext.AUTO_ISOLATION,
        execution_id="exec-1",
        command_payload=VALID_PAYLOAD,
    )

    with pytest.raises(NotImplementedError):
        run_schema_check(request)


# ------------------------------------------------------------------------------
# ② Action Whitelist
# ------------------------------------------------------------------------------


@pytest.mark.parametrize("runbook_id", sorted(AI_RECOMMENDABLE_RUNBOOK_IDS))
def test_action_whitelist_promotes_ai_recommendable(runbook_id):
    outcome = run_action_whitelist(_checked(runbook_id=runbook_id))

    assert outcome.step_result.step is GuardrailStep.ACTION_WHITELIST
    assert outcome.step_result.result is GuardrailStepStatus.PASS
    assert outcome.step_result.reason_code is None
    assert outcome.step_result.verification_summary is None
    assert outcome.command == RunbookCandidateDraft.model_validate({
        "runbook_id": runbook_id,
        "target_arn": TARGET_ARN,
        "parameters": PARAMS_BY_RUNBOOK[runbook_id],
        "evidence_ids": VALID_PAYLOAD["evidence_ids"],
    })


@pytest.mark.parametrize("runbook_id", UNKNOWN_RUNBOOK_IDS)
def test_action_whitelist_rejects_unknown_runbook(runbook_id):
    outcome = run_action_whitelist(_checked(runbook_id=runbook_id))

    assert outcome.command is None
    assert outcome.step_result.step is GuardrailStep.ACTION_WHITELIST
    assert outcome.step_result.result is GuardrailStepStatus.FAIL
    assert outcome.step_result.reason_code == WHITELIST_UNKNOWN_RUNBOOK
    assert outcome.step_result.verification_summary is None


@pytest.mark.parametrize("runbook_id", sorted(ROLLBACK_RUNBOOK_IDS))
def test_action_whitelist_rejects_rollback_runbooks(runbook_id):
    # ADR-0004 정책 ②: 등록된 조치지만 AI는 제안할 수 없다
    outcome = run_action_whitelist(_checked(runbook_id=runbook_id))

    assert outcome.command is None
    assert outcome.step_result.result is GuardrailStepStatus.FAIL
    assert outcome.step_result.reason_code == WHITELIST_NOT_AI_RECOMMENDABLE
    assert outcome.step_result.verification_summary is None


# ------------------------------------------------------------------------------
# 두 단계 연결
# ------------------------------------------------------------------------------


def test_unknown_runbook_is_recorded_as_whitelist_failure():
    # 이슈 #114 설계 의도 — 미등록 ID의 거절 기록은 ①이 아니라 ②에 남아야 한다
    schema = run_schema_check(_request(UNTYPED_PAYLOAD))
    assert schema.step_result.result is GuardrailStepStatus.PASS
    assert schema.command is not None

    whitelist = run_action_whitelist(schema.command)

    assert whitelist.step_result.step is GuardrailStep.ACTION_WHITELIST
    assert whitelist.step_result.result is GuardrailStepStatus.FAIL
    assert whitelist.step_result.reason_code == WHITELIST_UNKNOWN_RUNBOOK


def test_exposed_reason_codes_are_the_shared_contract_members():
    """이 파일이 노출하는 세 이름은 공용 계약(packages/schemas/guardrails.py)의
    멤버여야 한다 — 앱이 같은 값을 따로 정의하면 단계↔코드 정합 검증을 우회한다.

    접두·전역 고유성은 계약 쪽 테스트가 본다
    (packages/schemas/tests/test_guardrail_contracts.py). 여기서 보는 것은 앱이
    가리키는 대상이 그 계약인가다.
    """
    assert SCHEMA_INVALID_PAYLOAD is SchemaCheckReasonCode.SCHEMA_INVALID_PAYLOAD
    assert WHITELIST_UNKNOWN_RUNBOOK is (
        ActionWhitelistReasonCode.WHITELIST_UNKNOWN_RUNBOOK
    )
    assert WHITELIST_NOT_AI_RECOMMENDABLE is (
        ActionWhitelistReasonCode.WHITELIST_NOT_AI_RECOMMENDABLE
    )
    assert ARN_TARGET_NOT_MANAGED is ArnMatchReasonCode.ARN_TARGET_NOT_MANAGED


def test_step_result_carries_reason_code_of_its_own_step():
    """②의 거절이 ② 코드로 기록된다 — 계약이 단계↔코드 정합을 강제하므로, 다른
    단계 코드를 넣었다면 GuardrailStepResult 생성 자체가 실패한다."""
    schema = run_schema_check(_request(UNTYPED_PAYLOAD))
    assert schema.command is not None
    result = run_action_whitelist(schema.command).step_result

    assert isinstance(result.reason_code, ActionWhitelistReasonCode)
    # DB에는 이 문자열이 남는다(apps/core-api/db/repositories/guardrails.py)
    assert result.model_dump(mode="json")["reason_code"] == "WHITELIST_UNKNOWN_RUNBOOK"


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"target_arn": "\x00"}, id="arn_only_nul"),
        pytest.param(
            {"target_arn": "arn:aws:ec2:ap-northeast-2:123456789012:instance/i-0\x00"},
            id="arn_trailing_nul",
        ),
        pytest.param(
            {"target_arn": "arn:aws:ec2:\x00ap-northeast-2:123456789012:instance/i-0"},
            id="arn_embedded_nul",
        ),
        pytest.param({"evidence_ids": ["ev-1", "ev\x002"]}, id="evidence_id_nul"),
        pytest.param(
            {"parameters": {"target_instance_type": "t3\x00small"}}, id="param_value_nul"
        ),
        pytest.param(
            {"parameters": {"target_instance\x00_type": "t3.small"}}, id="param_key_nul"
        ),
    ],
)
def test_schema_check_rejects_nul(overrides):
    """NUL(0x00)이 든 문자열 필드는 ①에서 걸린다 — runbook_id만 예외(②의 몫).

    PostgreSQL text·jsonb가 담지 못하는 문자라, ③ ARN Match의 DB 조회와 후보
    저장(JSONB)에서 psycopg DataError가 난다 — 거절이 기록되는 대신 검증·저장이
    예외로 끝난다. ①이 이미 보는 크기 상한(DB 컬럼 폭)과 같은 부류의 제약이다.
    """
    outcome = run_schema_check(_request({**VALID_PAYLOAD, **overrides}))

    assert outcome.step_result.result is GuardrailStepStatus.FAIL
    assert outcome.step_result.reason_code is SCHEMA_INVALID_PAYLOAD
    assert outcome.command is None


# ------------------------------------------------------------------------------
# ③ ARN Match
# ------------------------------------------------------------------------------


def _collected(*arns: str) -> ManagedAssetLookup:
    """수집 자산 조회의 Test Double — get_asset_by_arn과 같은 완전 일치 조회다."""
    managed = frozenset(arns)
    return lambda target_arn: target_arn in managed


def _draft(**overrides) -> RunbookCandidateDraft:
    """③을 보는 테스트의 입력 — ①②를 실제로 통과시켜 얻는다."""
    outcome = run_action_whitelist(_checked(**overrides))
    assert outcome.command is not None
    return outcome.command


def test_arn_match_passes_collected_asset():
    outcome = run_arn_match(_draft(), _collected(TARGET_ARN))

    assert outcome.step_result.step is GuardrailStep.ARN_MATCH
    assert outcome.step_result.result is GuardrailStepStatus.PASS
    assert outcome.step_result.reason_code is None
    assert outcome.step_result.verification_summary is None
    assert outcome.command is not None and outcome.command.target_arn == TARGET_ARN


def test_arn_match_rejects_uncollected_target():
    outcome = run_arn_match(_draft(), _collected())

    assert outcome.step_result.result is GuardrailStepStatus.FAIL
    assert outcome.step_result.reason_code is ARN_TARGET_NOT_MANAGED
    assert outcome.command is None


def test_arn_match_queries_the_exact_target_arn():
    """조회에 넘기는 값이 Draft의 target_arn 그대로여야 한다.

    정규화·절단을 끼우면 대조 대상이 실제 실행 대상과 갈린다 — 조회는 맞다고
    답했는데 executor는 다른 자원을 건드리는 상태가 만들어진다.
    """
    asked: list[str] = []

    def lookup(target_arn: str) -> bool:
        asked.append(target_arn)
        return True

    run_arn_match(_draft(), lookup)

    assert asked == [TARGET_ARN]


@pytest.mark.parametrize(
    "target_arn",
    [
        pytest.param(
            "arn:aws:ec2:ap-northeast-2:123456789012:instance/*", id="wildcard_resource"
        ),
        pytest.param(
            "arn:aws:ec2:ap-northeast-2:123456789012:instance/i-9999999999999999",
            id="same_prefix_other_instance",
        ),
        pytest.param("*", id="wildcard"),
        pytest.param("arn:aws:iam::999999999999:role/Admin", id="other_account_role"),
        pytest.param("'; DROP TABLE assets; --", id="not_an_arn"),
    ],
)
def test_arn_match_does_not_match_by_prefix(target_arn: str):
    """수집 목록에 없으면 거절한다 — 계정·리전 접두어가 같아도 마찬가지다.

    앞의 두 케이스는 TARGET_ARN과 계정·리전까지 같은 문자열로 시작하므로,
    접두어 검사를 판정으로 쓰면 그대로 통과한다. Scope Escalation 차단의
    정의가 접두어가 아니라 수집 자산과의 대조인 이유다(#177).
    """
    outcome = run_arn_match(_draft(target_arn=target_arn), _collected(TARGET_ARN))

    assert outcome.step_result.result is GuardrailStepStatus.FAIL
    assert outcome.step_result.reason_code is ARN_TARGET_NOT_MANAGED
    assert outcome.command is None


# ------------------------------------------------------------------------------
# ④ AWS Dry-Run
# ------------------------------------------------------------------------------

# 요약 문자열은 손으로 짓지 않고 계약의 조립 함수로 만든다 — 형식이 바뀌면 여기서도
# 깨져야 한다(ADR-0007 §3 형식, packages/schemas/precheck.py).
PASS_SUMMARY = build_verification_summary(
    VerificationMethod.DRY_RUN,
    verified=["호출 권한과 파라미터 형식(DryRun)"],
    unverified=["대상 자원 존재와 현재 상태(DryRun 비검증)"],
    operations=["ec2.modify_instance_attribute"],
)
FAIL_SUMMARY = build_verification_summary(
    VerificationMethod.DESCRIBE,
    verified=["없음(대상 인스턴스 부재)"],
    unverified=["AWS 대상 상태", "IAM 권한"],
)


class _Precheck:
    """AWS 판정의 Test Double — 받은 Draft를 기록한다.

    실제 executor.precheck()를 부르지 않는 이유는 ④가 판정을 만들지 않기 때문이다.
    이 단계의 책임은 PrecheckOutcome을 GuardrailStepResult로 옮기는 것뿐이라,
    검증할 것은 AWS 응답 해석이 아니라 옮기기와 호출 여부다.
    """

    def __init__(self, outcome: PrecheckOutcome):
        self._outcome = outcome
        self.asked: list[RunbookCandidateDraft] = []

    def __call__(self, draft: RunbookCandidateDraft) -> PrecheckOutcome:
        self.asked.append(draft)
        return self._outcome


def _passing_precheck() -> _Precheck:
    return _Precheck(PrecheckOutcome(passed=True, verification_summary=PASS_SUMMARY))


def _failing_precheck(
    reason_code: PrecheckReasonCode = PrecheckReasonCode.PRECHECK_TARGET_NOT_FOUND,
) -> _Precheck:
    return _Precheck(
        PrecheckOutcome(
            passed=False, reason_code=reason_code, verification_summary=FAIL_SUMMARY
        )
    )


def test_aws_dry_run_passes_and_keeps_the_verification_summary():
    """통과해도 요약은 남는다 — 무엇을 확인하지 못했는지가 PASS에도 필요하다."""
    outcome = run_aws_dry_run(_draft(), _passing_precheck())

    assert outcome.step_result.step is GuardrailStep.AWS_DRY_RUN
    assert outcome.step_result.result is GuardrailStepStatus.PASS
    assert outcome.step_result.reason_code is None
    assert outcome.step_result.verification_summary == PASS_SUMMARY
    assert outcome.command is not None and outcome.command.target_arn == TARGET_ARN


@pytest.mark.parametrize("reason_code", list(PrecheckReasonCode))
def test_aws_dry_run_copies_the_rejection_without_reclassifying(reason_code):
    """사유 코드를 다시 분류하지 않는다 — executor가 고른 값이 그대로 기록된다.

    여기서 코드를 다시 매기면 거절 기록과 executor가 실제로 내린 판정이 갈린다
    (ADR-0007 §1 호출 규약의 1:1 매핑).
    """
    outcome = run_aws_dry_run(_draft(), _failing_precheck(reason_code))

    assert outcome.step_result.result is GuardrailStepStatus.FAIL
    assert outcome.step_result.reason_code is reason_code
    assert outcome.step_result.verification_summary == FAIL_SUMMARY
    assert outcome.command is None


def test_aws_dry_run_asks_about_the_candidates_own_draft():
    """판정 대상이 ③을 통과한 그 후보여야 한다 — 다른 값을 물으면 판정이 무의미하다."""
    draft = _draft()
    precheck = _passing_precheck()

    run_aws_dry_run(draft, precheck)

    assert precheck.asked == [draft]


# ------------------------------------------------------------------------------
# 4단계 종합 판정
# ------------------------------------------------------------------------------


def _validate(payload: dict | None = None, *, collected=(TARGET_ARN,), precheck=None):
    return run_guardrail_validation(
        _request(VALID_PAYLOAD if payload is None else payload),
        is_managed_arn=_collected(*collected),
        precheck=_passing_precheck() if precheck is None else precheck,
    )


# 단계별로 막히는 입력 — 하나의 단계만 실패하도록 나머지는 통과 조건으로 둔다
BLOCKED_AT = [
    pytest.param(
        {"payload": {**VALID_PAYLOAD, "unexpected_field": "x"}},
        GuardrailStep.SCHEMA_CHECK,
        SCHEMA_INVALID_PAYLOAD,
        id="schema_check",
    ),
    pytest.param(
        {"payload": UNTYPED_PAYLOAD},
        GuardrailStep.ACTION_WHITELIST,
        WHITELIST_UNKNOWN_RUNBOOK,
        id="action_whitelist",
    ),
    pytest.param(
        {"collected": ()},
        GuardrailStep.ARN_MATCH,
        ARN_TARGET_NOT_MANAGED,
        id="arn_match",
    ),
    pytest.param(
        {"precheck": _failing_precheck(PrecheckReasonCode.PRECHECK_UNAUTHORIZED)},
        GuardrailStep.AWS_DRY_RUN,
        PrecheckReasonCode.PRECHECK_UNAUTHORIZED,
        id="aws_dry_run",
    ),
]


def test_validation_passes_all_four_steps():
    outcome = _validate()
    result = outcome.result

    assert result.result is GuardrailDecision.PASS
    assert result.failed_step is None
    assert [step.step for step in result.steps] == list(GUARDRAIL_STEP_ORDER)
    assert all(step.result is GuardrailStepStatus.PASS for step in result.steps)
    assert outcome.command is not None and outcome.command.target_arn == TARGET_ARN


def test_validation_records_the_summary_only_on_the_aws_step():
    """확인 방식·한계 요약은 ④의 것이다 — 앞 세 단계는 AWS에 묻지 않는다."""
    steps = _validate().result.steps

    assert [step.verification_summary for step in steps] == [
        None,
        None,
        None,
        PASS_SUMMARY,
    ]


def test_validated_at_is_recorded_in_utc():
    """거절·통과 기록이 남는 시각은 저장·조회 계약과 같은 UTC여야 한다."""
    assert _validate().result.validated_at.tzinfo is timezone.utc


@pytest.mark.parametrize(("blocked", "step", "reason_code"), BLOCKED_AT)
def test_validation_records_the_step_that_blocked(blocked, step, reason_code):
    """막힌 단계가 기록되고, 그 뒤 단계는 돌지 않은 것으로 남는다."""
    outcome = _validate(**blocked)
    result = outcome.result
    failed_index = GUARDRAIL_STEP_ORDER.index(step)

    assert result.result is GuardrailDecision.FAIL
    assert result.failed_step is step
    assert outcome.command is None
    assert [s.step for s in result.steps] == list(GUARDRAIL_STEP_ORDER)
    assert [s.result for s in result.steps] == [
        *[GuardrailStepStatus.PASS] * failed_index,
        GuardrailStepStatus.FAIL,
        *[GuardrailStepStatus.NOT_RUN] * (len(GUARDRAIL_STEP_ORDER) - failed_index - 1),
    ]
    assert result.steps[failed_index].reason_code is reason_code


@pytest.mark.parametrize(
    ("blocked", "step", "reason_code"),
    [case for case in BLOCKED_AT if case.id != "aws_dry_run"],
)
def test_aws_is_not_asked_when_an_earlier_step_blocks(blocked, step, reason_code):
    """앞 단계가 막으면 AWS를 부르지 않는다.

    ③이 거절한 ARN을 ④가 물으면 범위를 벗어난 자원에 조회·DryRun 요청이 실제로
    나간다 — 단락이 곧 그 방어다.
    """
    precheck = _passing_precheck()

    _validate(**blocked, precheck=precheck)

    assert precheck.asked == []


# ------------------------------------------------------------------------------
# ROLLBACK_EXECUTION 문맥 (Issue #241, ADR-0004 정책 ①②)
#
# 롤백도 네 단계를 전부 지난다. 다른 것은 ①의 파라미터 계약과 ②의 허용 목록뿐이고,
# 그 둘이 문맥별로 갈리지 않으면 원복은 ①에서 계약이 없어 터지거나 ②에서 "AI 추천
# 불가"로 거절돼 자동 원복이 아예 서지 못한다.
# ------------------------------------------------------------------------------

REVERT_PAYLOAD = {
    "runbook_id": RunbookId.RUNBOOK_EC2_REVERT_SIZE.value,
    "target_arn": TARGET_ARN,
    # 되돌릴 값(instance_type)은 실리지 않는다 — 원천은 백업 레코드다(ADR-0008 §4)
    "parameters": {
        "instance_id": "i-0123456789abcdef0",
        "backup_record_id": "bk-1",
        "evidence_id": "ev-1",
    },
    "evidence_ids": ["ev-1"],
}


def _rollback_request(payload: dict) -> GuardrailValidationRequest:
    return GuardrailValidationRequest(
        validation_context=GuardrailValidationContext.ROLLBACK_EXECUTION,
        execution_id="exec-1",
        command_payload=payload,
    )


def _validate_rollback(payload: dict | None = None, *, collected=(TARGET_ARN,), precheck=None):
    return run_guardrail_validation(
        _rollback_request(REVERT_PAYLOAD if payload is None else payload),
        is_managed_arn=_collected(*collected),
        precheck=_passing_precheck() if precheck is None else precheck,
    )


def test_rollback_passes_all_four_steps():
    """자동 원복이 4단계를 전부 통과한다 — 우회 경로를 두지 않는다(ADR-0004 정책 ①)."""
    outcome = _validate_rollback()

    assert outcome.result.result is GuardrailDecision.PASS
    assert [step.step for step in outcome.result.steps] == list(GUARDRAIL_STEP_ORDER)
    assert all(s.result is GuardrailStepStatus.PASS for s in outcome.result.steps)


def test_rollback_promotes_a_rollback_command_not_a_draft():
    """롤백 3종은 Draft가 될 수 없다 — 그 모델이 AI 추천 7종만 받는다."""
    outcome = _validate_rollback()

    assert isinstance(outcome.command, RollbackExecutionCommand)
    assert outcome.command.runbook_id is RunbookId.RUNBOOK_EC2_REVERT_SIZE
    # ④가 그대로 executor.precheck에 넘길 수 있는 실행 파라미터 계약의 값이다
    assert isinstance(outcome.command.parameters, Ec2RevertSizeParameters)


def test_rollback_schema_check_uses_the_execution_parameter_contract():
    """①이 후보 계약을 쓰면 원복은 계약이 없어 봉투 검사로 끝난다 — 형식 위반이 ④까지 간다."""
    broken = {
        **REVERT_PAYLOAD,
        "parameters": {**REVERT_PAYLOAD["parameters"], "instance_id": "not-an-instance-id"},
    }

    outcome = run_schema_check(_rollback_request(broken))

    assert outcome.step_result.result is GuardrailStepStatus.FAIL
    assert outcome.step_result.reason_code == SCHEMA_INVALID_PAYLOAD


# 롤백 3종의 실행 파라미터. SG_RECREATE만 자원 ID를 받지 않는다 — 복원 대상을 백업
# 레코드가 가리키기 때문이다(packages/schemas/runbook_parameters.py).
ROLLBACK_PARAMS = {
    "RUNBOOK_EC2_REVERT_SIZE": REVERT_PAYLOAD["parameters"],
    "RUNBOOK_EC2_UNISOLATE": REVERT_PAYLOAD["parameters"],
    "RUNBOOK_SG_RECREATE": {"backup_record_id": "bk-1", "evidence_id": "ev-1"},
}


@pytest.mark.parametrize("runbook_id", sorted(ROLLBACK_RUNBOOK_IDS))
def test_rollback_whitelist_admits_all_three_rollback_runbooks(runbook_id):
    """AI_CANDIDATE에서 거절되는 3종이 여기서는 정당한 실행 대상이다(ADR-0004 정책 ②).

    ①을 실제로 통과시켜 얻는다 — ②가 승격할 때 파라미터가 이미 실행 계약의 값이라는
    전제가 그 순서에서만 성립한다.
    """
    payload = {
        **REVERT_PAYLOAD,
        "runbook_id": runbook_id,
        "parameters": ROLLBACK_PARAMS[runbook_id],
    }
    checked = run_schema_check(_rollback_request(payload)).command
    assert checked is not None

    outcome = run_action_whitelist(
        checked, GuardrailValidationContext.ROLLBACK_EXECUTION
    )

    assert outcome.step_result.result is GuardrailStepStatus.PASS
    assert outcome.command.runbook_id.value == runbook_id


def test_rollback_whitelist_rejects_a_main_runbook():
    """원복 경로에 본편 런북이 실리면 거절한다 — AI 추천 불가와는 반대 방향의 신호다."""
    outcome = run_action_whitelist(
        _checked(), GuardrailValidationContext.ROLLBACK_EXECUTION
    )

    assert outcome.step_result.result is GuardrailStepStatus.FAIL
    assert outcome.step_result.reason_code == WHITELIST_NOT_ROLLBACK_RUNBOOK
    assert outcome.command is None


def test_rollback_whitelist_still_rejects_unknown_runbooks():
    outcome = run_action_whitelist(
        SchemaCheckedCommand.model_validate(UNTYPED_PAYLOAD),
        GuardrailValidationContext.ROLLBACK_EXECUTION,
    )

    assert outcome.step_result.reason_code == WHITELIST_UNKNOWN_RUNBOOK


def test_rollback_is_blocked_when_the_target_is_no_longer_collected():
    """③은 문맥을 보지 않는다 — 조치 뒤 자산이 수집 목록에서 사라지면 원복도 막힌다."""
    outcome = _validate_rollback(collected=())

    assert outcome.result.failed_step is GuardrailStep.ARN_MATCH
    assert outcome.command is None


def test_rollback_dry_run_rejection_stops_the_command():
    """④ 거절이면 명령이 나오지 않는다 — 호출부가 CRITICAL로 넘길 신호다(정책 ④)."""
    outcome = _validate_rollback(
        precheck=_failing_precheck(PrecheckReasonCode.PRECHECK_TARGET_NOT_FOUND)
    )

    assert outcome.result.result is GuardrailDecision.FAIL
    assert outcome.result.failed_step is GuardrailStep.AWS_DRY_RUN
    assert outcome.command is None


def test_rollback_command_rejects_a_non_rollback_runbook():
    """운반 타입 자체가 롤백 3종만 받는다 — ②를 우회해 만들어진 명령도 막힌다."""
    with pytest.raises(ValidationError):
        RollbackExecutionCommand(
            runbook_id=RunbookId.RUNBOOK_EC2_RIGHTSIZING,
            target_arn=TARGET_ARN,
            parameters={
                "instance_id": "i-0123456789abcdef0",
                "current_instance_type": "t3.xlarge",
                "target_instance_type": "t3.medium",
                "evidence_id": "ev-1",
            },
            evidence_ids=["ev-1"],
        )
