"""가드레일 ① Schema Check · ② Action Whitelist 테스트 — 통과 경로와 거절 사유 3종.

이슈 #114 설계 의도의 회귀 테스트를 겸한다: ①은 runbook_id를 문자열로만 보고 목록
대조는 ②가 한다. 미등록 ID가 ①에서 터지면 거절 기록에 실제로 막힌 단계가 남지 않는다.
"""

import sys
from pathlib import Path

import pytest

# apps/core-api 를 import 경로에 추가 (test_whitelist.py 와 동일 방식)
CORE_API = Path(__file__).resolve().parents[2]
if str(CORE_API) not in sys.path:
    sys.path.insert(0, str(CORE_API))

from ai.guardrails import (  # noqa: E402
    SCHEMA_INVALID_PAYLOAD,
    WHITELIST_NOT_AI_RECOMMENDABLE,
    WHITELIST_UNKNOWN_RUNBOOK,
    SchemaCheckedCommand,
    run_action_whitelist,
    run_schema_check,
)
from ai.whitelist import (  # noqa: E402
    AI_RECOMMENDABLE_RUNBOOK_IDS,
    ROLLBACK_RUNBOOK_IDS,
    RunbookId,
)
from schemas.agents import RunbookCandidateDraft  # noqa: E402
from schemas.guardrails import (  # noqa: E402
    GuardrailStep,
    GuardrailStepStatus,
    GuardrailValidationContext,
    GuardrailValidationRequest,
)

TARGET_ARN = "arn:aws:ec2:ap-northeast-2:123456789012:instance/i-0123456789abcdef0"

VALID_PAYLOAD = {
    "runbook_id": RunbookId.RUNBOOK_EC2_RIGHTSIZING.value,
    "target_arn": TARGET_ARN,
    "display_parameters": {"target_instance_type": "t3.small"},
    "evidence_ids": ["ev-1", "ev-2"],
}

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
    """②를 보는 테스트의 입력 — ①을 실제로 통과시킨 값에서 일부만 바꾼다."""
    outcome = run_schema_check(_request({**VALID_PAYLOAD, **overrides}))
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
        {"runbook_id": VALID_PAYLOAD["runbook_id"], "target_arn": TARGET_ARN},
        {
            "runbook_id": VALID_PAYLOAD["runbook_id"],
            "target_arn": TARGET_ARN,
            "display_parameters": {},
            "evidence_ids": [],
        },
    ],
    ids=["omitted", "explicit_empty"],
)
def test_schema_check_allows_empty_collections(payload):
    # Draft(packages/schemas/agents.py)가 기본값을 주는 두 필드는 ①도 필수로 보지
    # 않는다 — 생략도, 명시적 빈 컬렉션도 통과한다(빈 문자열 거절과 별개).
    outcome = run_schema_check(_request(payload))

    assert outcome.step_result.result is GuardrailStepStatus.PASS
    assert outcome.command is not None
    assert outcome.command.display_parameters == {}
    assert outcome.command.evidence_ids == []


@pytest.mark.parametrize(
    "payload",
    [
        {**VALID_PAYLOAD, "severity": "HIGH"},
        {k: v for k, v in VALID_PAYLOAD.items() if k != "runbook_id"},
        {k: v for k, v in VALID_PAYLOAD.items() if k != "target_arn"},
        {**VALID_PAYLOAD, "runbook_id": 123},
        {**VALID_PAYLOAD, "target_arn": None},
        {**VALID_PAYLOAD, "evidence_ids": "ev-1"},
        {**VALID_PAYLOAD, "display_parameters": {"cpu_threshold": 12}},
        {**VALID_PAYLOAD, "runbook_id": ""},
        {**VALID_PAYLOAD, "target_arn": ""},
        {**VALID_PAYLOAD, "evidence_ids": [""]},
        {**VALID_PAYLOAD, "display_parameters": {"": "t3.small"}},
        {**VALID_PAYLOAD, "display_parameters": {"target_instance_type": ""}},
    ],
    ids=[
        "extra_field",
        "missing_runbook_id",
        "missing_target_arn",
        "int_runbook_id",
        "null_target_arn",
        "str_instead_of_list",
        "int_parameter_value",
        "empty_runbook_id",
        "empty_target_arn",
        "empty_evidence_id",
        "empty_param_key",
        "empty_param_value",
    ],
)
def test_schema_check_rejects_malformed_payload(payload):
    outcome = run_schema_check(_request(payload))

    assert outcome.command is None
    assert outcome.step_result.step is GuardrailStep.SCHEMA_CHECK
    assert outcome.step_result.result is GuardrailStepStatus.FAIL
    assert outcome.step_result.reason_code == SCHEMA_INVALID_PAYLOAD
    assert outcome.step_result.verification_summary is None


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
    assert outcome.draft == RunbookCandidateDraft(
        runbook_id=RunbookId(runbook_id),
        target_arn=TARGET_ARN,
        display_parameters=VALID_PAYLOAD["display_parameters"],
        evidence_ids=VALID_PAYLOAD["evidence_ids"],
    )


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
    schema = run_schema_check(_request({**VALID_PAYLOAD, "runbook_id": "RUNBOOK_EBS_SNAPSHOT"}))
    assert schema.step_result.result is GuardrailStepStatus.PASS
    assert schema.command is not None

    whitelist = run_action_whitelist(schema.command)

    assert whitelist.step_result.step is GuardrailStep.ACTION_WHITELIST
    assert whitelist.step_result.result is GuardrailStepStatus.FAIL
    assert whitelist.step_result.reason_code == WHITELIST_UNKNOWN_RUNBOOK


def test_reason_codes_are_distinct_and_prefixed():
    # ④의 PRECHECK_* 코드와 reason_code 한 필드를 나눠 쓴다 — 접두로 단계를 구분한다
    codes = {SCHEMA_INVALID_PAYLOAD, WHITELIST_UNKNOWN_RUNBOOK, WHITELIST_NOT_AI_RECOMMENDABLE}

    assert len(codes) == 3
    assert SCHEMA_INVALID_PAYLOAD.startswith("SCHEMA_")
    assert WHITELIST_UNKNOWN_RUNBOOK.startswith("WHITELIST_")
    assert WHITELIST_NOT_AI_RECOMMENDABLE.startswith("WHITELIST_")
