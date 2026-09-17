"""단가 전용 후보의 서버 계산·거절 경계와 실제 SDK 요청 형식을 확인한다."""

import json
from pathlib import Path

import httpx
import pytest
from ai.agent import CandidateProposalOutput, EvidenceSummaryOutput, ProposedCandidate
from ai.evaluation.savings.rate_only import rate_only_request
from ai.evaluation.savings.three_call import run_three_call
from ai.evaluation.summary import finops_cases
from ai.model_client import FakeAIModelClient
from ai.openai_client import OpenAIModelClient
from ai.savings import ProposedHourlyRates, accept_hourly_rates, savings_context
from openai import OpenAI
from pydantic import ValidationError
from schemas.assets import AssetInventory
from schemas.incidents import AgentInvocationStatus
from schemas.savings import SavingsReason, SavingsStatus

ROOT = Path(__file__).resolve().parents[4]


@pytest.fixture
def case():
    golden = ROOT / "datasets/golden/finops"
    inventory = AssetInventory.model_validate_json(
        (golden / "input/asset_inventory_001.json").read_text(encoding="utf-8"),
    )
    expected = json.loads((golden / "expected/asset_inventory_001.json").read_text(encoding="utf-8"))
    return next(c for c in finops_cases(inventory, expected) if c.case_id == "A1")


def rate_values(case, **changes):
    context = savings_context(case.graph_input.asset_context)
    return {
        "status": "ESTIMATED",
        **{key: context[key] for key in (
            "target_arn", "region", "current_instance_type", "target_instance_type",
        )},
        "current_hourly_rate": "0.104000", "target_hourly_rate": "0.026000",
        "explanation": "서울 Linux 공유 온디맨드 컴퓨팅 단가의 합성 예시다.",
        **changes,
    }


@pytest.mark.parametrize("failed_stage", [1, 2])
def test_service_call_failure_preserves_only_a_validated_candidate(case, failed_stage, caplog):
    from ai.agent import run_finops_graph
    from ai.model_client import AIModelUnavailableError

    prepared = [
        EvidenceSummaryOutput(
            observation="관측된 CPU 사용률이 낮다.",
            diagnosis="과대 사양일 가능성이 있다.",
            rationale="사양 변경 후보를 검토할 근거다.",
        ),
        CandidateProposalOutput(candidates=[ProposedCandidate(
            runbook_id="RUNBOOK_EC2_RIGHTSIZING",
            target_arn=case.graph_input.asset_context.arn,
            evidence_ids=[e.evidence_id for e in case.graph_input.evidences],
        )]),
        ProposedHourlyRates(**rate_values(case)),
    ]

    class FailingClient(FakeAIModelClient):
        attempted = 0

        def complete(self, request, response_model):
            self.attempted += 1
            if self.attempted == failed_stage:
                raise AIModelUnavailableError("private-provider-response", phase="transport")
            return super().complete(request, response_model)

    client = FailingClient(prepared)
    output = run_finops_graph(case.graph_input, client=client)
    assert client.attempted == failed_stage
    assert output.invocation_status is AgentInvocationStatus.FAILED
    assert output.candidates == []


@pytest.mark.parametrize("before, after, amount", [
    ("0.104000", "0.026000", "56.94"),
    ("0.100000", "0.100000", "0.00"),
    ("0.100500", "0.100000", "0.37"),  # 0.365, ROUND_HALF_UP
    ("0.100007", "0.100000", "0.01"),  # 중간 단가를 반올림하면 0이 된다.
    ("0.235200", "0.052800", "133.15"),  # 이전 과대 단가도 보정 없이 계산한다.
])
def test_server_calculates_only_after_accepting_rates(case, before, after, amount):
    proposal = ProposedHourlyRates(**rate_values(
        case, current_hourly_rate=before, target_hourly_rate=after,
    ))
    assert "amount" not in proposal.model_dump()
    result = accept_hourly_rates(proposal, case.graph_input.asset_context)
    assert result.status is SavingsStatus.ESTIMATED
    assert result.model_dump(mode="json")["amount"] == amount
    assert result.basis.assumptions.hours == 730


@pytest.mark.parametrize("invalid", [
    "0.12 USD", "NaN", "Infinity", "1e-3", "-0.1", "0.1234567", "", "0.1\x00",
])
def test_rate_output_contract_rejects_non_numeric_or_excess_precision(case, invalid):
    with pytest.raises(ValidationError):
        ProposedHourlyRates(**rate_values(case, current_hourly_rate=invalid))


@pytest.mark.parametrize("changes, rule", [
    ({"current_hourly_rate": "sensitive-value"}, "string_pattern_mismatch"),
    ({"current_hourly_rate": None}, "MISSING_RATE"),
    ({"region": "another-region"}, "CONTEXT_MISMATCH"),
    ({"target_instance_type": "t3.small"}, "CONTEXT_MISMATCH"),
    ({"explanation": "invalid\x00text"}, "value_error"),
    ({"current_hourly_rate": "1000001"}, "less_than_equal"),
    ({"current_hourly_rate": "0.010000"}, "greater_than_equal"),
    ({"current_hourly_rate": "1000000", "target_hourly_rate": "0"}, "less_than_equal"),
    ({"status": "UNAVAILABLE"}, "UNAVAILABLE_REQUIRES_NULL"),
])
def test_server_revalidates_typed_bypasses_and_preserves_only_diagnostic_rules(case, changes, rule):
    proposal = ProposedHourlyRates(**rate_values(case)).model_copy(update=changes)
    diagnostics = []
    result = accept_hourly_rates(proposal, case.graph_input.asset_context, diagnostics=diagnostics)
    assert result.status is SavingsStatus.INVALID
    assert result.amount is None and result.basis is None
    assert all(set(item) == {"location", "rule"} for item in diagnostics)
    assert any(item["rule"] == rule for item in diagnostics)
    assert "sensitive-value" not in json.dumps(diagnostics)


def test_unavailable_requires_null_rates_and_keeps_it_distinct_from_missing_output(case):
    unavailable = ProposedHourlyRates(**rate_values(
        case, status="UNAVAILABLE", current_hourly_rate=None, target_hourly_rate=None,
    ))
    result = accept_hourly_rates(unavailable, case.graph_input.asset_context)
    assert result.status is SavingsStatus.UNAVAILABLE and result.amount is None
    assert result.reason is SavingsReason.MODEL_UNAVAILABLE
    missing = accept_hourly_rates(None, case.graph_input.asset_context)
    assert missing.status is SavingsStatus.INVALID
    assert missing.reason is SavingsReason.MISSING_ESTIMATE


@pytest.mark.parametrize("empty", [False, True])
def test_three_call_candidate_uses_rate_contract_and_preserves_recommendation(case, empty):
    asset = case.graph_input.asset_context
    summary = EvidenceSummaryOutput(
        observation="CPU가 낮게 관측됐다.", diagnosis="사양 조정 후보로 볼 수 있다.",
        rationale="변경 전 업무 조건을 확인할 필요가 있다.",
    )
    proposal = CandidateProposalOutput(candidates=[] if empty else [ProposedCandidate(
        runbook_id="RUNBOOK_EC2_RIGHTSIZING", target_arn=asset.arn,
        evidence_ids=[e.evidence_id for e in case.graph_input.evidences],
    )])
    client = FakeAIModelClient([summary, proposal, ProposedHourlyRates(**rate_values(case))])
    result = run_three_call(case.graph_input, client=client, price_mode="rates")
    assert len(client.sent) == (2 if empty else 3)
    if empty:
        assert result.output.invocation_status is AgentInvocationStatus.NO_PROPOSAL
        assert result.price_stage == "NOT_RUN"
    else:
        assert result.output.invocation_status is AgentInvocationStatus.SUCCEEDED
        assert len(result.output.candidates) == 1
        assert result.output.candidates[0].ai_savings_estimate.model_dump(mode="json")["amount"] == "56.94"
        assert result.output.candidates[0].parameters.target_instance_type == "t3.medium"
        assert json.loads(client.sent[1]["user_json"])["summary_lines"] == list(summary.model_dump().values())
        assert "savings_context" not in json.loads(client.sent[1]["user_json"])
        assert json.loads(client.sent[2]["user_json"])["savings_context"] == savings_context(asset)


@pytest.mark.parametrize("request_kind", ["experimental", "service"])
def test_actual_openai_sdk_sends_required_nullable_pattern_without_amount(case, request_kind):
    from ai.savings import savings_request

    request_builder = savings_request if request_kind == "service" else rate_only_request
    captured = []
    values = rate_values(case)

    def respond(request):
        captured.append(json.loads(request.content))
        return httpx.Response(200, json={
            "id": "synthetic-response", "object": "chat.completion", "created": 0,
            "model": "gpt-5.6-luna",
            "choices": [{"index": 0, "finish_reason": "stop", "message": {
                "role": "assistant", "content": json.dumps(values),
            }}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        })

    with OpenAI(api_key="test-key", base_url="https://unit.test/v1", max_retries=0,
                http_client=httpx.Client(transport=httpx.MockTransport(respond))) as sdk:
        client = OpenAIModelClient(
            client=sdk, model="gpt-5.6-luna", max_attempts=1,
            timeout_seconds=30, retry_backoff_seconds=0,
            temperature=None, reasoning_effort="low",
        )
        result = client.complete(request_builder(case.graph_input.asset_context), ProposedHourlyRates)
    assert result.output == ProposedHourlyRates(**values)
    assert len(captured) == 1
    request = captured[0]
    assert request["reasoning_effort"] == "low"
    assert "temperature" not in request
    response_format = request["response_format"]["json_schema"]
    schema = response_format["schema"]
    assert response_format["strict"] is True
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(schema["properties"])
    assert "amount" not in schema["properties"]
    for name in ("current_hourly_rate", "target_hourly_rate"):
        alternatives = schema["properties"][name]["anyOf"]
        assert {"type": "null"} in alternatives
        assert any(item.get("type") == "string" and item.get("pattern") for item in alternatives)
