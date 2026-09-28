"""독립 단가 호출의 후보 결속·오류 분류. 가격 품질은 검증하지 않는다."""

import pytest
from ai.model_client import (
    AIModelContractError,
    AIModelRejectedError,
    AIModelTimeoutError,
    AIModelUnavailableError,
    FakeAIModelClient,
)
from ai.rate_estimator import ProposedHourlyRates, estimate_candidate_savings
from schemas.agents import AgentAssetContext
from schemas.candidates import CandidateStatus, RunbookCandidateData
from schemas.runbook_parameters import Ec2RightsizingCandidateParameters
from schemas.savings import SavingsReason, SavingsStatus

ARN = "arn:aws:ec2:ap-northeast-2:123456789012:instance/i-0abc"


@pytest.fixture
def context():
    asset = AgentAssetContext.model_validate({
        "arn": ARN, "resource_id": "i-0abc", "asset_type": "EC2", "resource_role": "PRIMARY",
        "account_id": "123456789012", "region": "ap-northeast-2", "state": "running",
        "spec": {"instance_type": "t3.xlarge"}, "relationships": [],
        "evaluation_status": "COMPLETED", "health_score": 3, "verdict": "COST_CANDIDATE",
        "collected_at": "2026-09-16T00:00:00Z",
    })
    candidate = RunbookCandidateData(
        candidate_id="candidate-1", incident_id="incident-1",
        runbook_id="RUNBOOK_EC2_RIGHTSIZING", target_arn=ARN, evidence_ids=["ev-1"],
        parameters=Ec2RightsizingCandidateParameters(target_instance_type="t3.medium"),
        status=CandidateStatus.PENDING_VALIDATION,
    )
    return asset, candidate


@pytest.mark.parametrize("error", [
    AIModelTimeoutError, AIModelUnavailableError, AIModelRejectedError, AIModelContractError,
])
def test_typed_call_failure_does_not_change_candidate(context, error, caplog):
    asset, candidate = context
    original = candidate.model_dump()

    class BrokenClient:
        calls = 0

        def complete(self, request, response_model):
            self.calls += 1
            raise error("private-provider-response")

    client = BrokenClient()
    result = estimate_candidate_savings(candidate, asset=asset, client=client)
    assert client.calls == 1
    assert candidate.model_dump() == original
    assert result.status is SavingsStatus.INVALID
    assert result.reason is SavingsReason.MISSING_ESTIMATE
    assert result.amount is None and result.basis is None
    assert error.__name__ in caplog.text
    assert "private-provider-response" not in caplog.text


@pytest.mark.parametrize("mismatch", ["target", "size", "missing-context", "runbook"])
def test_mismatched_candidate_never_calls_model(context, mismatch):
    asset, candidate = context
    if mismatch == "target":
        candidate = candidate.model_copy(update={"target_arn": ARN + "1"})
    elif mismatch == "size":
        candidate = candidate.model_copy(update={
            "parameters": Ec2RightsizingCandidateParameters(target_instance_type="t3.small"),
        })
    elif mismatch == "missing-context":
        asset = asset.model_copy(update={"spec": asset.spec.model_copy(update={
            "instance_type": "unsupported.type",
        })})
    else:
        from schemas.runbooks import RunbookId

        candidate = candidate.model_copy(update={"runbook_id": RunbookId.RUNBOOK_SG_DELETE_ISOLATED})
    client = FakeAIModelClient([])
    result = estimate_candidate_savings(candidate, asset=asset, client=client)
    assert client.sent == []
    assert result.reason is SavingsReason.CONTEXT_MISMATCH


@pytest.mark.parametrize("rate", ["nul\x00text", "surrogate\ud800", "9" * 1001])
def test_unstorable_output_is_only_an_invalid_estimate(context, rate):
    asset, candidate = context
    proposal = ProposedHourlyRates.model_construct(
        status="ESTIMATED", current_hourly_rate=rate, target_hourly_rate="0.026000",
    )
    result = estimate_candidate_savings(candidate, asset=asset, client=FakeAIModelClient([proposal]))
    assert result.status is SavingsStatus.INVALID
    assert result.reason is SavingsReason.INVALID_ESTIMATE
    assert candidate.ai_savings_estimate is None


def test_unexpected_implementation_error_is_not_normalized(context):
    asset, candidate = context

    class BrokenClient:
        def complete(self, request, response_model):
            raise RuntimeError("implementation defect")

    with pytest.raises(RuntimeError, match="implementation defect"):
        estimate_candidate_savings(candidate, asset=asset, client=BrokenClient())
