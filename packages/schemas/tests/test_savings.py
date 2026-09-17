"""절감 예상의 상태·계산·저장 가능성 및 후보 연결을 소유 계약에서 검증한다."""

from copy import deepcopy

import pytest
from pydantic import ValidationError
from schemas.agents import RunbookCandidateDraft
from schemas.candidates import RunbookCandidateData
from schemas.savings import AISavingsEstimate

ARN = "arn:aws:ec2:ap-northeast-2:123456789012:instance/i-0123456789abcdef0"


@pytest.fixture()
def estimate():
    # 검증용 합성 단가. 실제 AWS 단가의 정확성 근거로 쓰지 않는다.
    return {
        "status": "ESTIMATED", "amount": "56.94",
        "basis": {
            "target_arn": ARN, "region": "ap-northeast-2",
            "current_instance_type": "t3.xlarge", "target_instance_type": "t3.medium",
            "current_hourly_rate": "0.104", "target_hourly_rate": "0.026",
            "explanation": "모델 추정 단가 차이 0.078 USD × 730시간.",
            "explanation_source": "SERVER_TEMPLATE",
        },
    }


def test_decimal_contract_round_trip_and_real_zero(estimate):
    value = AISavingsEstimate.model_validate(estimate)
    dumped = value.model_dump(mode="json")
    assert dumped["amount"] == "56.94"
    assert dumped["basis"]["current_hourly_rate"] == "0.104000"
    assert AISavingsEstimate.model_validate_json(value.model_dump_json()) == value
    estimate["amount"] = "0.00"
    estimate["basis"]["target_hourly_rate"] = "0.104"
    assert AISavingsEstimate.model_validate(estimate).amount == 0


def test_explanation_origin_is_independent_of_model_rate_origin(estimate):
    value = AISavingsEstimate.model_validate(estimate).model_dump(mode="json")
    assert value["basis"]["explanation_source"] == "SERVER_TEMPLATE"
    assert value["basis"]["assumptions"]["pricing_source"] == "MODEL_KNOWLEDGE"
    assert value["amount"] == "56.94"


def test_explanation_origin_is_required(estimate):
    estimate["basis"].pop("explanation_source")
    with pytest.raises(ValidationError) as exc:
        AISavingsEstimate.model_validate(estimate)
    assert [(error["loc"], error["type"]) for error in exc.value.errors()] == [
        (("basis", "explanation_source"), "missing"),
    ]


@pytest.mark.parametrize("source", [None, "MODEL_GENERATED", "AWS_VERIFIED"])
def test_non_server_explanation_origin_is_rejected(estimate, source):
    estimate["basis"]["explanation_source"] = source
    with pytest.raises(ValidationError) as exc:
        AISavingsEstimate.model_validate(estimate)
    assert [error["loc"] for error in exc.value.errors()] == [
        ("basis", "explanation_source"),
    ]


@pytest.mark.parametrize("amount", ["56.95", "56.941", "-1", "NaN", "Infinity", "1e1000"])
def test_invalid_amount_or_arithmetic_is_rejected(estimate, amount):
    estimate["amount"] = amount
    with pytest.raises(ValidationError):
        AISavingsEstimate.model_validate(estimate)


@pytest.mark.parametrize("field,value", [
    ("current_hourly_rate", "NaN"), ("current_hourly_rate", "0.1040001"),
    ("target_hourly_rate", "-0.1"), ("explanation", ""),
    ("explanation", "설명\x00"), ("explanation", "\ud800"),
    ("region", "ap-northeast-2\x00"), ("target_arn", ARN + "\x00"),
    ("explanation", "설명" * 501),
])
def test_invalid_basis_is_rejected(estimate, field, value):
    estimate["basis"][field] = value
    with pytest.raises(ValidationError):
        AISavingsEstimate.model_validate(estimate)


@pytest.mark.parametrize("field,value", [
    ("hours", 720), ("operating_system", "WINDOWS"),
    ("purchase_option", "SPOT"), ("pricing_source", "AWS_API"),
])
def test_different_assumptions_need_a_new_contract(estimate, field, value):
    estimate["basis"]["assumptions"] = {field: value}
    with pytest.raises(ValidationError):
        AISavingsEstimate.model_validate(estimate)


@pytest.mark.parametrize("status,reason", [
    ("UNAVAILABLE", "MODEL_UNAVAILABLE"), ("INVALID", "INVALID_ESTIMATE"),
])
def test_unestimated_amount_is_null(status, reason):
    value = AISavingsEstimate(status=status, reason=reason)
    assert value.model_dump(mode="json")["amount"] is None
    with pytest.raises(ValidationError):
        AISavingsEstimate(status=status, reason=reason, amount=0)


def test_candidate_binds_estimate_to_execution_target(estimate):
    candidate = {
        "candidate_id": "cand-1", "incident_id": "inc-1",
        "runbook_id": "RUNBOOK_EC2_RIGHTSIZING", "target_arn": ARN,
        "parameters": {"target_instance_type": "t3.medium"}, "evidence_ids": ["ev-1"],
        "status": "PENDING_VALIDATION",
        "ai_savings_estimate": estimate,
    }
    RunbookCandidateData.model_validate(candidate)
    changed = deepcopy(candidate)
    changed["parameters"]["target_instance_type"] = "t3.small"
    with pytest.raises(ValidationError):
        RunbookCandidateData.model_validate(changed)
    changed = deepcopy(candidate)
    changed["target_arn"] = ARN + "-other"
    with pytest.raises(ValidationError):
        RunbookCandidateData.model_validate(changed)
    changed = deepcopy(candidate)
    changed["runbook_id"] = "RUNBOOK_EBS_DELETE_UNATTACHED"
    changed["parameters"] = {}
    with pytest.raises(ValidationError):
        RunbookCandidateData.model_validate(changed)
    candidate.pop("ai_savings_estimate")
    assert RunbookCandidateData.model_validate(candidate).ai_savings_estimate is None


@pytest.mark.parametrize("has_estimate", [False, True])
def test_graph_draft_rejects_savings_even_when_null(estimate, has_estimate):
    draft = {
        "runbook_id": "RUNBOOK_EC2_RIGHTSIZING", "target_arn": ARN,
        "parameters": {"target_instance_type": "t3.medium"}, "evidence_ids": ["ev-1"],
        "ai_savings_estimate": estimate if has_estimate else None,
    }
    with pytest.raises(ValidationError) as exc:
        RunbookCandidateDraft.model_validate(draft)
    assert [(error["loc"], error["type"]) for error in exc.value.errors()] == [
        (("ai_savings_estimate",), "extra_forbidden"),
    ]
