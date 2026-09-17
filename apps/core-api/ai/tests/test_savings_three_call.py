"""3단계 요청의 연결·단락·거부 진단 보존을 확인한다. 실제 가격 품질은 측정하지 않는다."""

import json
from pathlib import Path

import pytest
from ai.agent import CandidateProposalOutput, EvidenceSummaryOutput, ProposedCandidate
from ai.evaluation.savings.isolation import price_only_prompt_fingerprint
from ai.evaluation.savings.three_call import run_three_call
from ai.evaluation.summary import finops_cases
from ai.model_client import (
    AIModelUnavailableError,
    FakeAIModelClient,
    TokenUsage,
)
from ai.savings import ProposedSavingsEstimate, accept_savings_estimate, savings_context
from schemas.assets import AssetInventory
from schemas.incidents import AgentInvocationStatus
from schemas.savings import SavingsReason, SavingsStatus

ROOT = Path(__file__).resolve().parents[4]


@pytest.fixture
def case():
    golden = ROOT / "datasets/golden/finops"
    inventory = AssetInventory.model_validate_json(
        (golden / "input/asset_inventory_001.json").read_text(encoding="utf-8")
    )
    expected = json.loads((golden / "expected/asset_inventory_001.json").read_text(encoding="utf-8"))
    return next(c for c in finops_cases(inventory, expected) if c.case_id == "A1")


def outputs(case, *, values=None, candidate_target=None, empty=False):
    asset = case.graph_input.asset_context
    summary = EvidenceSummaryOutput(
        observation="CPU 관측 근거다.", diagnosis="과대 사양일 가능성이 있다.",
        rationale="사양 변경 후보를 검토할 근거다.",
    )
    candidate = ProposedCandidate(
        runbook_id="RUNBOOK_EC2_RIGHTSIZING", target_arn=candidate_target or asset.arn,
        evidence_ids=[e.evidence_id for e in case.graph_input.evidences],
    )
    context = savings_context(asset)
    estimate = ProposedSavingsEstimate(**{
        "status": "ESTIMATED",
        **{key: context[key] for key in (
            "target_arn", "region", "current_instance_type", "target_instance_type"
        )},
        # 호출 배선 검증용 합성 단가. AWS 참조 가격이 아니다.
        "current_hourly_rate": "0.200000", "target_hourly_rate": "0.100000",
        "amount": "73.00", "explanation": "합성 단가 차이 × 730시간의 참고 추정",
        **(values or {}),
    })
    return [summary, CandidateProposalOutput(candidates=[] if empty else [candidate]), estimate]


def test_three_calls_pass_new_summary_to_recommendation_and_price_only_to_last_stage(case):
    prepared = outputs(case)
    client = FakeAIModelClient(prepared)
    result = run_three_call(case.graph_input, client=client)
    assert len(client.sent) == 3
    assert [c["stage"] for c in result.calls] == ["summary", "recommendation", "savings"]
    second = json.loads(client.sent[1]["user_json"])
    third = json.loads(client.sent[2]["user_json"])
    assert second["summary_lines"] == list(prepared[0].model_dump().values())
    assert "savings_context" not in second
    assert "절감" not in client.sent[1]["system_prompt"]
    assert set(third) == {"savings_context"}
    assert third["savings_context"] == savings_context(case.graph_input.asset_context)
    candidate = result.output.candidates[0]
    assert candidate.parameters.target_instance_type == "t3.medium"
    assert str(candidate.ai_savings_estimate.amount) == "73.00"
    assert result.price_stage == "RETURNED"
    assert result.savings_diagnostics == []
    assert result.record(case_id="A1", repeat=1)["calls"] == result.calls
    assert price_only_prompt_fingerprint() == (
        "6bbd22aeeaf3326acbc7c84102507c90db290c8d2b3da1e785266de7488fb1bb"
    )


@pytest.mark.parametrize("values, location, rule", [
    ({"amount": "74.00"}, [], "ARITHMETIC_MISMATCH"),
    ({"amount": None}, [], "MISSING_AMOUNT"),
    ({"amount": "sensitive-value-must-not-be-recorded"}, ["amount"], "decimal_parsing"),
    ({"region": "another-region"}, ["region"], "CONTEXT_MISMATCH"),
    ({"status": "sensitive-unknown-status"}, ["status"], "UNKNOWN_STATUS"),
    ({"explanation": " "}, ["basis", "explanation"], "value_error"),
])
def test_rejected_estimate_retains_candidate_and_only_field_rule_diagnostics(
    case, values, location, rule,
):
    client = FakeAIModelClient(outputs(case, values=values))
    result = run_three_call(case.graph_input, client=client)
    assert result.output.invocation_status is AgentInvocationStatus.SUCCEEDED
    assert len(result.output.candidates) == 1
    estimate = result.output.candidates[0].ai_savings_estimate
    assert estimate.status is SavingsStatus.INVALID and estimate.amount is None
    assert result.savings_diagnostics == [{"location": location, "rule": rule}]
    record = result.record(case_id="A1", repeat=1)
    assert record["savings_diagnostics"] == result.savings_diagnostics
    assert "sensitive-" not in json.dumps(record)


@pytest.mark.parametrize("kind, count, status", [
    ("empty", 2, AgentInvocationStatus.NO_PROPOSAL),
    ("invalid_target", 2, AgentInvocationStatus.FAILED),
])
def test_last_call_is_skipped_without_a_valid_rightsizing_candidate(case, kind, count, status):
    client = FakeAIModelClient(outputs(
        case, empty=kind == "empty", candidate_target="outside-input" if kind == "invalid_target" else None,
    ))
    result = run_three_call(case.graph_input, client=client)
    assert len(client.sent) == count
    assert result.output.invocation_status is status
    assert result.price_stage == "NOT_RUN"
    assert result.savings_diagnostics == []


@pytest.mark.parametrize("failed_stage", [1, 2, 3])
def test_call_errors_keep_stage_and_usage_without_retry(case, failed_stage):
    class FailingClient(FakeAIModelClient):
        attempted = 0

        def complete(self, request, response_model):
            self.attempted += 1
            if self.attempted == failed_stage:
                raise AIModelUnavailableError(
                    "sensitive-provider-error", phase="transport",
                    usage=TokenUsage(prompt_tokens=10, completion_tokens=2, total_tokens=12),
                )
            return super().complete(request, response_model)

    client = FailingClient(outputs(case))
    result = run_three_call(case.graph_input, client=client)
    assert client.attempted == failed_stage
    assert len(result.calls) == failed_stage
    error = result.calls[-1]
    assert error["error"] == "AIModelUnavailableError" and error["error_phase"] == "transport"
    assert error["usage"]["prompt_tokens"] == 10
    assert "sensitive-provider-error" not in json.dumps(result.record(case_id="A1", repeat=1))
    assert result.price_stage == ("ERROR" if failed_stage == 3 else "NOT_RUN")


def test_optional_diagnostics_do_not_change_existing_acceptance(case):
    asset = case.graph_input.asset_context
    valid = outputs(case)[2]
    for proposal in [None, valid, ProposedSavingsEstimate(status="UNAVAILABLE"),
                     valid.model_copy(update={"status": "UNAVAILABLE"})]:
        diagnostic = []
        assert accept_savings_estimate(proposal, asset) == accept_savings_estimate(
            proposal, asset, diagnostics=diagnostic,
        )
    assert all(issue["rule"] == "UNAVAILABLE_REQUIRES_NULL" for issue in diagnostic)
    missing = accept_savings_estimate(None, asset, diagnostics=[])
    assert missing.reason is SavingsReason.MISSING_ESTIMATE
