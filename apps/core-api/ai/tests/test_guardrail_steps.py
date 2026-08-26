"""가드레일 ① Schema Check · ② Action Whitelist 테스트 — 통과 경로와 거절 사유 3종.

이슈 #114 설계 의도의 회귀 테스트를 겸한다: ①은 runbook_id를 문자열로만 보고 목록
대조는 ②가 한다. 미등록 ID가 ①에서 터지면 거절 기록에 실제로 막힌 단계가 남지 않는다.
"""

import pytest
from ai.guardrails import (
    SCHEMA_INVALID_PAYLOAD,
    WHITELIST_NOT_AI_RECOMMENDABLE,
    WHITELIST_UNKNOWN_RUNBOOK,
    SchemaCheckedCommand,
    run_action_whitelist,
    run_schema_check,
)
from ai.whitelist import AI_RECOMMENDABLE_RUNBOOK_IDS, ROLLBACK_RUNBOOK_IDS, RunbookId
from schemas.agents import RunbookCandidateDraft
from schemas.guardrails import (
    ActionWhitelistReasonCode,
    GuardrailStep,
    GuardrailStepStatus,
    GuardrailValidationContext,
    GuardrailValidationRequest,
    SchemaCheckReasonCode,
)

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


@pytest.mark.parametrize(
    "validation_context",
    [GuardrailValidationContext.AUTO_ISOLATION, GuardrailValidationContext.ROLLBACK_EXECUTION],
)
def test_schema_check_implements_ai_candidate_only(validation_context):
    # 다른 문맥은 payload 모양이 달라 FAIL로 기록하면 거절 사유가 틀린다
    request = GuardrailValidationRequest(
        validation_context=validation_context,
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
    assert outcome.draft == RunbookCandidateDraft.model_validate({
        "runbook_id": runbook_id,
        "target_arn": TARGET_ARN,
        "parameters": PARAMS_BY_RUNBOOK[runbook_id],
        "evidence_ids": VALID_PAYLOAD["evidence_ids"],
    })


@pytest.mark.parametrize("runbook_id", UNKNOWN_RUNBOOK_IDS)
def test_action_whitelist_rejects_unknown_runbook(runbook_id):
    outcome = run_action_whitelist(_checked(runbook_id=runbook_id))

    assert outcome.draft is None
    assert outcome.step_result.step is GuardrailStep.ACTION_WHITELIST
    assert outcome.step_result.result is GuardrailStepStatus.FAIL
    assert outcome.step_result.reason_code == WHITELIST_UNKNOWN_RUNBOOK
    assert outcome.step_result.verification_summary is None


@pytest.mark.parametrize("runbook_id", sorted(ROLLBACK_RUNBOOK_IDS))
def test_action_whitelist_rejects_rollback_runbooks(runbook_id):
    # ADR-0004 정책 ②: 등록된 조치지만 AI는 제안할 수 없다
    outcome = run_action_whitelist(_checked(runbook_id=runbook_id))

    assert outcome.draft is None
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


def test_step_result_carries_reason_code_of_its_own_step():
    """②의 거절이 ② 코드로 기록된다 — 계약이 단계↔코드 정합을 강제하므로, 다른
    단계 코드를 넣었다면 GuardrailStepResult 생성 자체가 실패한다."""
    schema = run_schema_check(_request(UNTYPED_PAYLOAD))
    assert schema.command is not None
    result = run_action_whitelist(schema.command).step_result

    assert isinstance(result.reason_code, ActionWhitelistReasonCode)
    # DB에는 이 문자열이 남는다(apps/core-api/db/repositories/guardrails.py)
    assert result.model_dump(mode="json")["reason_code"] == "WHITELIST_UNKNOWN_RUNBOOK"
