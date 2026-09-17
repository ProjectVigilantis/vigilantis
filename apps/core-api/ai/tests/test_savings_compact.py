"""후보 A의 저장 수용·평가 중단 경계. 실제 가격 품질과 별도인 무료 검증이다."""

from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal

import httpx
import pytest
from ai.evaluation.savings import compact_validation as runner
from ai.evaluation.savings.compact import ProposedHourlyRates, accept_compact_rates
from ai.evaluation.savings.scoring import score_estimate
from ai.model_client import (
    AIModelContractError,
    AIModelResponse,
    AIModelTimeoutError,
    TokenUsage,
)
from schemas.savings import SavingsStatus


@pytest.fixture(scope="module")
def prepared():
    frozen, assets = runner.current_preflight()
    return frozen, assets, runner.read(runner.SPEC_PATH)


def proposal(**updates):
    return ProposedHourlyRates.model_construct(**{
        "status": "ESTIMATED", "explanation": "모델 지식 기반 추정값이다.",
        "current_hourly_rate": "0.208000", "target_hourly_rate": "0.052000", **updates,
    })


def test_sdk_input_unchanged_and_only_four_output_fields_removed(prepared):
    _, assets, _ = prepared
    wire = runner.capture_wire(assets["A1"])
    schema = wire["response_format"]["json_schema"]["schema"]
    assert list(schema["properties"]) == [
        "status", "explanation", "current_hourly_rate", "target_hourly_rate",
    ]
    assert schema["required"] == list(schema["properties"])
    assert wire["reasoning_effort"] == "low"
    assert "temperature" not in wire
    assert wire["model"] == "gpt-5.6-luna"


@pytest.mark.parametrize("updates", [
    {"current_hourly_rate": None}, {"target_hourly_rate": None},
    {"explanation": None}, {"explanation": " "}, {"explanation": "a\x00b"},
    {"explanation": "\ud800"}, {"explanation": "x" * 1001},
    {"current_hourly_rate": "private-rejected-value"},
    {"current_hourly_rate": "0.1234567"},
    {"current_hourly_rate": "-0.2"},
    {"target_hourly_rate": "0.300000"},
    {"status": "UNAVAILABLE"},
])
def test_unstorable_or_inconsistent_values_rejected_without_raw_values(prepared, updates):
    _, assets, _ = prepared
    diagnostics = []
    result = accept_compact_rates(proposal(**updates), assets["A1"], diagnostics=diagnostics)
    assert result.status is SavingsStatus.INVALID
    assert result.basis is None and result.amount is None
    assert diagnostics
    assert all(set(item) == {"location", "rule"} for item in diagnostics)
    assert "private-rejected-value" not in str(diagnostics)


def test_server_context_is_bound_without_claiming_model_echo_validation(prepared):
    _, assets, _ = prepared
    a1 = accept_compact_rates(proposal(), assets["A1"])
    a7 = accept_compact_rates(proposal(), assets["A7"])
    assert a1.basis.target_arn == assets["A1"].arn
    assert a7.basis.target_arn == assets["A7"].arn
    assert a1.basis.target_arn != a7.basis.target_arn
    assert a1.amount == a7.amount == Decimal("113.88")
    assert a1.basis.current_hourly_rate == Decimal("0.208000")
    assert "target_arn" not in ProposedHourlyRates.model_fields


@pytest.mark.parametrize("current,target,amount", [
    ("0.249600", "0.062400", "136.66"),
    ("0.166400", "0.052800", "82.93"),
])
def test_existing_price_failures_are_not_corrected_or_hidden(prepared, current, target, amount):
    _, assets, spec = prepared
    estimate = accept_compact_rates(proposal(
        current_hourly_rate=current, target_hourly_rate=target,
    ), assets["A1"])
    assert str(estimate.amount) == amount
    grade = score_estimate(estimate.model_dump(mode="json"), spec["cases"]["A1"],
                           spec["price_reference"], spec["criteria"])
    assert grade["status"] == "PRICE_DEVIATION"


@pytest.fixture
def round_files(prepared, tmp_path, monkeypatch):
    frozen, assets, spec = prepared
    monkeypatch.setattr(runner, "ROUND", tmp_path)
    runner.write(tmp_path / "preflight.json", frozen)
    runner.write(tmp_path / "approval.json", {
        "approved": True, "round_maximum_calls": 60, "user_total_call_limit": 80,
        "preflight_sha256": runner.sha(tmp_path / "preflight.json"),
        "note": "Offline test fixture only; not user approval.",
    })
    return frozen, assets, spec, tmp_path


@dataclass
class SyntheticClient:
    assets: dict
    spec: dict
    verify_wire: object
    unavailable: int = 0
    defect: str | None = None
    count: int = 0

    def complete(self, request, response_model):
        self.count += 1
        step = runner.SCHEDULE[self.count - 1]
        saved = runner.read(runner.ROUND / "metadata.json")["calls"]
        assert len(saved) == self.count and saved[-1]["status"] == "STARTED"
        wire = runner.capture_wire(self.assets[step["case_id"]])
        if self.defect == "wire":
            wire["model"] = "unexpected-model"
        self.verify_wire(httpx.Request("POST", "https://unit.test/v1", json=wire))
        if self.defect == "timeout":
            raise AIModelTimeoutError("private-provider-error")
        if self.defect == "parse":
            raise AIModelContractError("private-provider-error", phase="response")
        context = request.user_payload["savings_context"]
        prices = self.spec["price_reference"]["hourly_rates"]
        output = proposal(current_hourly_rate=prices[context["current_instance_type"]]["usd"],
                          target_hourly_rate=prices[context["target_instance_type"]]["usd"])
        if self.count <= self.unavailable:
            output = proposal(status="UNAVAILABLE", current_hourly_rate=None, target_hourly_rate=None)
        if self.defect == "price":
            output = proposal(current_hourly_rate="0.249600", target_hourly_rate="0.062400")
        if self.defect == "contract":
            output = proposal(explanation="private-rejected-value\x00")
        assert response_model is ProposedHourlyRates
        return AIModelResponse(output=output, usage=TokenUsage(10, 5, 15), model="synthetic-only")


def run_synthetic(round_files, **options):
    frozen, assets, spec, directory = round_files
    clients = []

    @contextmanager
    def factory(verify_wire):
        client = SyntheticClient(assets, spec, verify_wire, **options)
        clients.append(client)
        yield client

    code = runner.execute(frozen, assets, client_factory=factory)
    return code, clients[0], runner.read(directory / "analysis.json")


@pytest.mark.parametrize("unavailable,expected_calls,passed", [(0, 60, True), (3, 60, True), (4, 4, False)])
def test_call_cap_and_availability_denominator(round_files, unavailable, expected_calls, passed):
    code, client, result = run_synthetic(round_files, unavailable=unavailable)
    assert client.count == result["attempted_calls"] == expected_calls
    assert result["passed"] is passed
    assert result["planned_calls"] == 60
    assert result["unexecuted_calls"] == 60 - expected_calls
    assert code == (0 if passed else 2)
    if unavailable == 3:
        assert result["estimated_fraction_of_planned"] == "0.95"


@pytest.mark.parametrize("defect,status", [
    ("price", "STOPPED_PRICE_DEVIATION"),
    ("contract", "STOPPED_OUTPUT_CONTRACT_ERROR"),
    ("parse", "STOPPED_OUTPUT_CONTRACT_ERROR"),
    ("timeout", "ABORTED_MODEL_ERROR"),
    ("wire", "ABORTED_RUNNER_ERROR"),
])
def test_first_defect_stops_without_retry_and_blocks_restart(round_files, defect, status):
    code, client, result = run_synthetic(round_files, defect=defect)
    assert code == 2 and client.count == result["attempted_calls"] == 1
    assert not result["passed"] and result["execution_status"] == status
    frozen, assets, _, directory = round_files
    saved = (directory / "metadata.json").read_text("utf-8")
    assert "private-provider-error" not in saved and "private-rejected-value" not in saved
    with pytest.raises(ValueError, match="ROUND_ALREADY_STARTED"):
        runner.execute(frozen, assets, client_factory=lambda hook: pytest.fail("restarted"))


@pytest.mark.parametrize("file", ["approval.json", "preflight.json"])
def test_drift_rejected_before_credentials_or_attempt(round_files, file):
    frozen, assets, _, directory = round_files
    runner.write(directory / file, {})
    with pytest.raises(ValueError):
        runner.execute(frozen, assets, client_factory=lambda hook: pytest.fail("read credentials"))
    assert not (directory / "started.json").exists()


def test_partial_or_duplicate_schedule_never_passes(prepared):
    frozen, _, spec = prepared
    assert not runner.analyze({**frozen, "status": "COMPLETED", "calls": []}, spec)["passed"]
    bad = {**runner.SCHEDULE[0], "number": 2, "status": "STARTED"}
    with pytest.raises(ValueError, match="SAVED_SCHEDULE_DRIFT"):
        runner.analyze({**frozen, "status": "COMPLETED", "calls": [bad]}, spec)
