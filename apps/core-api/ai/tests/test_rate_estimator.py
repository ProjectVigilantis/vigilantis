"""서비스 단가 요청은 B 실측과 동일하며 출처·저장 수용은 서비스 경계에서 검증한다."""

import json
from pathlib import Path

import httpx
import pytest
from ai.evaluation.savings.numeric_validation import digest, load_cases
from ai.openai_client import OpenAIModelClient
from ai.rate_estimator import (
    ProposedHourlyRates,
    accept_hourly_rates,
    estimate_candidate_savings,
    server_explanation,
)
from openai import OpenAI
from schemas.candidates import RunbookCandidateData
from schemas.savings import SavingsStatus

ROOT = Path(__file__).resolve().parents[4]
RECORDED = ROOT / "apps/core-api/ai/evaluation/savings/results/20260916-numeric-output-1"


@pytest.fixture(scope="module")
def cases():
    return {case.case_id: case for case in load_cases()}


@pytest.mark.parametrize("case_id", ["A1", "A7", "A11", "A12", "A14", "A16"])
def test_service_sdk_request_matches_measured_b_exactly(cases, case_id):
    case = cases[case_id]
    asset = case.graph_input.asset_context
    sent = []

    def respond(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={
            "id": "synthetic", "object": "chat.completion", "created": 0,
            "model": "gpt-5.6-luna",
            "choices": [{"index": 0, "finish_reason": "stop", "message": {
                "role": "assistant", "content": json.dumps({
                    "status": "ESTIMATED", "current_hourly_rate": "0.208000",
                    "target_hourly_rate": "0.052000",
                }),
            }}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        })

    from ai.savings import savings_context

    context = savings_context(asset)
    candidate = RunbookCandidateData(
        candidate_id="synthetic-candidate", incident_id=case.graph_input.incident_id,
        runbook_id="RUNBOOK_EC2_RIGHTSIZING", target_arn=asset.arn,
        parameters={"target_instance_type": context["target_instance_type"]},
        evidence_ids=[case.graph_input.evidences[0].evidence_id], status="PENDING_VALIDATION",
    )
    with OpenAI(api_key="test-key", base_url="https://unit.test/v1", max_retries=0,
                http_client=httpx.Client(transport=httpx.MockTransport(respond))) as sdk:
        client = OpenAIModelClient(
            client=sdk, model="gpt-5.6-luna", reasoning_effort="low", temperature=None,
            max_attempts=1, timeout_seconds=30, retry_backoff_seconds=0,
        )
        result = estimate_candidate_savings(candidate, asset=asset, client=client)
    frozen = json.loads((RECORDED / "preflight.json").read_text("utf-8"))
    assert len(sent) == 1
    assert digest(sent[0]) == frozen["requests"][case_id]["wire_sha256"]
    assert result.status is SavingsStatus.ESTIMATED
    assert result.basis.explanation_source.value == "SERVER_TEMPLATE"
    assert result.basis.explanation == server_explanation(context)
    assert result.basis.target_arn == asset.arn
    assert "explanation_source" not in ProposedHourlyRates.model_fields


def test_all_recorded_b_rates_keep_amount_and_server_context(cases):
    records = json.loads((RECORDED / "metadata.json").read_text("utf-8"))["calls"]
    assert len(records) == 60
    for record in records:
        previous = record["estimate"]
        proposal = ProposedHourlyRates(
            status=previous["status"],
            current_hourly_rate=previous["basis"]["current_hourly_rate"],
            target_hourly_rate=previous["basis"]["target_hourly_rate"],
        )
        result = accept_hourly_rates(proposal, cases[record["case_id"]].graph_input.asset_context)
        expected = {**previous, "basis": {
            **previous["basis"], "explanation_source": "SERVER_TEMPLATE",
        }}
        assert result.model_dump(mode="json") == expected


@pytest.mark.parametrize("status,current,target,expected", [
    ("UNAVAILABLE", None, None, "UNAVAILABLE"),
    ("UNAVAILABLE", "0.208", None, "INVALID"),
    ("ESTIMATED", None, "0.052", "INVALID"),
    ("ESTIMATED", "0.052", "0.208", "INVALID"),
    ("ESTIMATED", "0.1040001", "0.052", "INVALID"),
])
def test_invalid_or_unavailable_rates_do_not_become_zero(cases, status, current, target, expected):
    proposal = ProposedHourlyRates.model_construct(
        status=status, current_hourly_rate=current, target_hourly_rate=target,
    )
    result = accept_hourly_rates(proposal, cases["A1"].graph_input.asset_context)
    assert result.status.value == expected
    assert result.amount is None and result.basis is None


def test_unstorable_server_context_is_rejected_before_persistence(cases):
    asset = cases["A1"].graph_input.asset_context.model_copy(update={"region": "nul\x00region"})
    diagnostics = []
    result = accept_hourly_rates(ProposedHourlyRates(
        status="ESTIMATED", current_hourly_rate="0.208", target_hourly_rate="0.052",
    ), asset, diagnostics=diagnostics)
    assert result.status is SavingsStatus.INVALID and result.basis is None
    assert diagnostics and "nul" not in str(diagnostics)
