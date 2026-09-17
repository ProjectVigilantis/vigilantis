"""가격표 변환이 서버 목표를 보존하는지 확인한다. 실제 단가 정확도는 유료 평가 몫이다."""

import json
from dataclasses import asdict
from pathlib import Path

import httpx
import pytest
from ai.agent import CandidateProposalOutput, EvidenceSummaryOutput, ProposedCandidate
from ai.evaluation.savings.isolation import (
    price_only_prompt_fingerprint,
    price_only_request,
)
from ai.evaluation.savings.price_table import (
    TABLE_INSTANCE_TYPES,
    ProposedT3HourlyRates,
    ProposedTableSavings,
    accept_price_table,
    price_table_request,
)
from ai.evaluation.savings.three_call import (
    run_three_call,
    three_call_prompt_fingerprint,
)
from ai.evaluation.summary import finops_cases
from ai.model_client import FakeAIModelClient
from ai.openai_client import OpenAIModelClient
from ai.savings import savings_context
from openai import OpenAI
from schemas.assets import AssetInventory
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


def proposal(asset, **changes):
    context = savings_context(asset)
    return ProposedTableSavings(**{
        "status": "ESTIMATED",
        **{key: context[key] for key in (
            "target_arn", "region", "current_instance_type", "target_instance_type",
        )},
        # 비례하지 않는 합성 단가로 크기 비율이 아닌 행 대응을 검증한다.
        "hourly_rates": ProposedT3HourlyRates.model_validate({
            "t3.small": "0.030000", "t3.medium": "0.100000",
            "t3.large": "0.150000", "t3.xlarge": "0.200000",
        }),
        "explanation": "합성 단가 차이 × 730시간의 참고 추정", "amount": "73.00",
        **changes,
    })


def pipeline_outputs(case, table):
    return [
        EvidenceSummaryOutput(observation="CPU 관측 근거", diagnosis="과대 사양일 가능성",
                              rationale="다운사이징 검토 근거"),
        CandidateProposalOutput(candidates=[ProposedCandidate(
            runbook_id="RUNBOOK_EC2_RIGHTSIZING", target_arn=case.graph_input.asset_context.arn,
            evidence_ids=[e.evidence_id for e in case.graph_input.evidences],
        )]),
        table,
    ]


def test_server_fixed_pair_selects_the_corresponding_ai_rate_rows(case):
    asset = case.graph_input.asset_context
    accepted = accept_price_table(proposal(asset), asset)
    assert accepted.estimate.status is SavingsStatus.ESTIMATED
    basis = accepted.estimate.basis
    assert basis.current_instance_type == "t3.xlarge"
    assert basis.target_instance_type == "t3.medium"
    assert str(basis.current_hourly_rate) == "0.200000"
    assert str(basis.target_hourly_rate) == "0.100000"
    assert str(accepted.estimate.amount) == "73.00"
    assert accepted.savings_diagnostics == []


def test_model_target_change_is_rejected_and_does_not_change_the_execution_candidate(case):
    table = proposal(case.graph_input.asset_context, target_instance_type="t3.small", amount="124.10")
    client = FakeAIModelClient(pipeline_outputs(case, table))
    result = run_three_call(case.graph_input, client=client, price_mode="table")
    candidate = result.output.candidates[0]
    assert candidate.parameters.target_instance_type == "t3.medium"
    assert candidate.ai_savings_estimate.status is SavingsStatus.INVALID
    assert candidate.ai_savings_estimate.reason is SavingsReason.CONTEXT_MISMATCH
    assert result.savings_diagnostics == [
        {"location": ["target_instance_type"], "rule": "CONTEXT_MISMATCH"},
    ]
    assert len(client.sent) == 3


@pytest.mark.parametrize("missing", [None, "private-invalid-unselected-rate"])
def test_unselected_row_does_not_remove_a_valid_estimate_and_unsafe_value_is_not_retained(case, missing):
    asset = case.graph_input.asset_context
    table = proposal(asset)
    table.hourly_rates.small = missing
    accepted = accept_price_table(table, asset)
    assert accepted.estimate.status is SavingsStatus.ESTIMATED
    assert accepted.hourly_rates["t3.small"] is None
    assert "private-invalid" not in json.dumps(asdict(accepted), default=str)
    assert bool(accepted.table_diagnostics) is (missing is not None)


@pytest.mark.parametrize("bad", ["private-invalid-selected-rate", "NaN", "Infinity"])
def test_invalid_selected_rate_rejects_estimate_and_retains_only_safe_table_values(case, bad):
    asset = case.graph_input.asset_context
    table = proposal(asset)
    table.hourly_rates.xlarge = bad
    accepted = accept_price_table(table, asset)
    assert accepted.estimate.status is SavingsStatus.INVALID
    assert accepted.estimate.basis is None
    assert accepted.hourly_rates["t3.xlarge"] is None
    assert accepted.table_diagnostics
    assert all(set(item) == {"location", "rule"} for item in accepted.table_diagnostics)
    assert bad not in json.dumps(asdict(accepted), default=str)


def test_unknown_other_rows_are_allowed_but_selected_rows_are_needed(case):
    asset = case.graph_input.asset_context
    table = proposal(asset)
    table.hourly_rates.small = table.hourly_rates.large = None
    assert accept_price_table(table, asset).estimate.status is SavingsStatus.ESTIMATED
    table.hourly_rates.medium = None
    assert accept_price_table(table, asset).estimate.status is SavingsStatus.INVALID
    table.status, table.amount, table.hourly_rates.xlarge = "UNAVAILABLE", None, None
    assert accept_price_table(table, asset).estimate.status is SavingsStatus.UNAVAILABLE


def test_sdk_sends_three_requests_with_server_target_and_price_table_schema(case):
    asset = case.graph_input.asset_context
    outputs = pipeline_outputs(case, proposal(asset))
    sent = []

    def respond(request):
        sent.append(json.loads(request.content))
        content = outputs[len(sent) - 1].model_dump_json(by_alias=True)
        return httpx.Response(200, json={
            "id": "chatcmpl-fixture", "object": "chat.completion", "created": 1,
            "model": "fixture-model", "choices": [{"index": 0, "finish_reason": "stop",
                                                       "message": {"role": "assistant", "content": content}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        })

    with OpenAI(api_key="fixture-key", max_retries=0,
                http_client=httpx.Client(transport=httpx.MockTransport(respond))) as sdk:
        client = OpenAIModelClient(client=sdk, model="gpt-5.6-luna", timeout_seconds=30,
                                   max_attempts=1, retry_backoff_seconds=0, reasoning_effort="low")
        result = run_three_call(case.graph_input, client=client, price_mode="table")
    assert len(sent) == 3
    assert [item["response_format"]["json_schema"]["name"] for item in sent] == [
        "EvidenceSummaryOutput", "CandidateProposalOutput", "ProposedTableSavings",
    ]
    assert all(item["reasoning_effort"] == "low" and "temperature" not in item for item in sent)
    second = json.loads(sent[1]["messages"][1]["content"])
    third = json.loads(sent[2]["messages"][1]["content"])
    assert second["summary_lines"] == result.output.summary_lines
    assert "savings_context" not in second
    assert third["savings_context"] == savings_context(asset)
    assert third["savings_context"]["target_instance_type"] == "t3.medium"
    assert third["table_instance_types"] == list(TABLE_INSTANCE_TYPES)
    schema = sent[2]["response_format"]["json_schema"]["schema"]
    assert list(schema["properties"]).index("hourly_rates") < list(schema["properties"]).index("explanation")
    assert list(schema["$defs"]["ProposedT3HourlyRates"]["properties"]) == list(TABLE_INSTANCE_TYPES)
    assert result.output.candidates[0].ai_savings_estimate.status is SavingsStatus.ESTIMATED
    record = result.record(case_id="A1", repeat=1)
    assert record["price_table"]["t3.medium"] == "0.100000"
    assert [c["usage"]["prompt_tokens"] for c in result.calls] == [10, 10, 10]


def test_price_table_input_keeps_server_context_and_has_no_reference_rates(case):
    asset = case.graph_input.asset_context
    request = price_table_request(asset)
    assert request.user_payload["savings_context"] == price_only_request(asset).user_payload["savings_context"]
    assert set(request.user_payload) == {"savings_context", "table_instance_types"}


def test_existing_pair_prompt_and_three_call_fingerprints_are_preserved():
    assert price_only_prompt_fingerprint() == "6bbd22aeeaf3326acbc7c84102507c90db290c8d2b3da1e785266de7488fb1bb"
    assert three_call_prompt_fingerprint() == "a10d1654b458c35b2afc856a1d979bdc1dde2a97f16c0bcd03e66b9884b7952f"


def test_unknown_experiment_mode_stops_before_any_model_call(case):
    client = FakeAIModelClient([])
    with pytest.raises(ValueError):
        run_three_call(case.graph_input, client=client, price_mode="unknown")
    assert client.sent == []
