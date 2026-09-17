"""동일한 오답·미산출·부분 계측이 가격 품질 통과로 둔갑하지 않는지 검증한다."""

import json
import runpy
import sys
from copy import deepcopy
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import pytest
from ai.evaluation.savings.isolation import (
    price_only_system_prompt,
    run_price_only,
    score_price_only_report,
)
from ai.evaluation.savings.scoring import score_estimate, score_report
from ai.model_client import FakeAIModelClient
from ai.savings import ProposedSavingsEstimate
from schemas.agents import AgentAssetContext
from schemas.savings import SavingsReason, SavingsStatus

ROOT = Path(__file__).resolve().parents[4]
SPEC_PATH = ROOT / "apps/core-api/ai/evaluation/savings/spec.json"


def sample():
    spec = json.loads(SPEC_PATH.read_text(encoding="utf-8"))
    runs = []
    for case_id, context in spec["cases"].items():
        large = context["current_instance_type"] == "t3.xlarge"
        candidate = {
            "runbook_id": "RUNBOOK_EC2_RIGHTSIZING",
            "target_arn": context["target_arn"],
            "parameters": {"target_instance_type": context["target_instance_type"]},
            "ai_savings_estimate": {
                "status": "ESTIMATED", "amount": "113.88" if large else "56.94",
                "basis": {**context,
                    "current_hourly_rate": "0.208000" if large else "0.104000",
                    "target_hourly_rate": "0.052000" if large else "0.026000",
                    "explanation": "Linux 공유 온디맨드 컴퓨팅 단가 차이 × 730시간의 참고 추정",
                },
            },
        }
        runs.extend(deepcopy({
            "case_id": case_id, "invocation_status": "SUCCEEDED", "candidates": [candidate],
        }) for _ in range(10))
    return {"fixed_set": spec["fixed_set"], "runs": runs}, spec


def test_correct_reference_values_pass_the_complete_set():
    raw, spec = sample()
    report = score_report(raw, spec)
    assert report["passed"] is True
    assert report["estimated"] == 60
    assert report["not_estimated_fraction"] == "0"
    assert report["results"][0]["expected_amount"] == "113.88"


def test_identical_wrong_prices_fail_even_when_arithmetic_and_stability_pass():
    raw, spec = sample()
    for run in raw["runs"][:10]:
        estimate = run["candidates"][0]["ai_savings_estimate"]
        estimate["amount"] = "56.94"
        estimate["basis"]["current_hourly_rate"] = "0.104000"
        estimate["basis"]["target_hourly_rate"] = "0.026000"
    report = score_report(raw, spec)
    assert report["passed"] is False
    assert report["status_counts"]["PRICE_DEVIATION"] == 10
    assert report["stability"]["A1"]["all_amounts_equal"] is True
    assert report["results"][0]["arithmetic_context_and_assumptions_valid"] is True
    assert report["results"][0]["absolute_error_usd"] == "56.94"


@pytest.mark.parametrize("kind", ["unavailable", "no_candidate", "failed"])
def test_silence_stays_in_the_denominator(kind):
    raw, spec = sample()
    for run in raw["runs"]:
        if kind == "unavailable":
            run["candidates"][0]["ai_savings_estimate"] = {
                "status": "UNAVAILABLE", "reason": "MODEL_UNAVAILABLE",
            }
        elif kind == "no_candidate":
            run["candidates"] = []
        else:
            run["invocation_status"] = "FAILED"
    report = score_report(raw, spec)
    assert report["passed"] is False
    assert report["estimated"] == 0
    assert report["not_estimated"] == 60
    assert report["not_estimated_fraction"] == "1"
    assert report["stability"]["A1"]["all_amounts_equal"] is None


@pytest.mark.parametrize("change, expected_status", [
    ({"currency": "KRW"}, "INVALID_CONTRACT"),
    ({"amount": "1.00"}, "INVALID_CONTRACT"),
    ({"basis": {"region": "us-east-1"}}, "CONTEXT_MISMATCH"),
    ({"basis": {"assumptions": {"hours": 720}}}, "INVALID_CONTRACT"),
])
def test_invalid_basis_or_assumptions_do_not_become_price_accuracy(change, expected_status):
    raw, spec = sample()
    estimate = raw["runs"][0]["candidates"][0]["ai_savings_estimate"]
    if "basis" in change:
        estimate["basis"].update(change["basis"])
    else:
        estimate.update(change)
    report = score_report(raw, spec)
    assert report["passed"] is False
    assert report["results"][0]["status"] == expected_status


def test_partial_probe_cannot_approve_a_baseline():
    raw, spec = sample()
    raw["runs"] = raw["runs"][:1]
    report = score_report(raw, spec)
    assert report["status_counts"] == {"PASS": 1}
    assert report["complete"] is False
    assert report["passed"] is False


def test_input_drift_rejects_comparison():
    raw, spec = sample()
    raw["fixed_set"] = {**raw["fixed_set"], "input_sha256": "changed"}
    with pytest.raises(ValueError, match="입력 지문"):
        score_report(raw, spec)


def test_cli_writes_results_with_source_identity(tmp_path, monkeypatch):
    raw, _ = sample()
    source = tmp_path / "raw.json"
    source.write_text(json.dumps(raw), encoding="utf-8")
    output = tmp_path / "reports/savings.json"
    monkeypatch.setattr(sys, "argv", ["savings_eval.py", str(source), "--out", str(output)])
    cli = runpy.run_path(str(ROOT / "scripts/savings_eval.py"))
    assert cli["main"]() == 0
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["passed"] is True
    assert report["scorer_version"] == "v0.2.0"
    assert len(report["source_sha256"]) == len(report["spec_sha256"]) == 64


def test_estimate_accuracy_does_not_approve_a_different_candidate_target():
    raw, spec = sample()
    candidate = raw["runs"][0]["candidates"][0]
    expected = spec["cases"]["A1"]
    assert score_estimate(
        candidate["ai_savings_estimate"], expected, spec["price_reference"], spec["criteria"],
    )["status"] == "PASS"
    candidate["target_arn"] = spec["cases"]["A11"]["target_arn"]
    assert score_report(raw, spec)["results"][0]["status"] == "CONTEXT_MISMATCH"


@pytest.mark.parametrize("wrong_region", [False, True])
def test_price_only_call_sends_only_context_and_uses_existing_acceptance(wrong_region):
    raw, spec = sample()
    expected = spec["cases"]["A1"]
    estimate = raw["runs"][0]["candidates"][0]["ai_savings_estimate"]
    asset = AgentAssetContext.model_validate({
        "arn": expected["target_arn"], "resource_id": "i-0a1b2c3d4e5f00001",
        "asset_type": "EC2", "resource_role": "PRIMARY", "account_id": "123456789012",
        "region": expected["region"], "state": "running",
        "spec": {"instance_type": expected["current_instance_type"]},
        "relationships": [], "evaluation_status": "COMPLETED", "health_score": 3,
        "verdict": "COST_CANDIDATE", "collected_at": "2026-09-15T00:00:00Z",
    })
    values = {"status": "ESTIMATED", "amount": estimate["amount"], **estimate["basis"]}
    if wrong_region:
        values["region"] = "us-east-1"
    client = FakeAIModelClient([ProposedSavingsEstimate(**values)])
    result = run_price_only(asset, client=client)
    assert len(client.sent) == 1
    payload = json.loads(client.sent[0]["user_json"])
    assert set(payload) == {"savings_context"}
    context = payload["savings_context"]
    assert {k:context[k] for k in expected} == expected
    assert set(context) == {*expected, "currency", "period", "assumptions"}
    assert context["assumptions"]["hours"] == 730
    assert "단가 식별:" in price_only_system_prompt()
    assert "candidates" not in price_only_system_prompt()
    if wrong_region:
        assert result.status is SavingsStatus.INVALID
        assert result.reason is SavingsReason.CONTEXT_MISMATCH
    else:
        assert result.status is SavingsStatus.ESTIMATED
        assert str(result.amount) == estimate["amount"]


@pytest.mark.parametrize("kind", ["complete", "partial", "duplicate", "unavailable", "failed"])
def test_price_only_probe_keeps_coverage_and_service_guarantees_separate(kind):
    full, spec = sample()
    value = full["runs"][0]["candidates"][0]["ai_savings_estimate"]
    raw = {
        "fixed_set": spec["fixed_set"], "case_ids": ["A1"], "repeats": 3,
        "prompt_version": "test", "prompt_sha256": "test", "model_snapshots": ["fake"],
        "runs": [{
            "case_id": "A1", "repeat": repeat, "response_status": "RETURNED",
            "ai_savings_estimate": deepcopy(value),
        } for repeat in range(1, 4)],
    }
    if kind == "partial":
        raw["runs"].pop()
    elif kind == "duplicate":
        raw["runs"][2]["repeat"] = 2
    elif kind == "unavailable":
        raw["runs"][0]["ai_savings_estimate"] = {
            "status": "UNAVAILABLE", "reason": "MODEL_UNAVAILABLE",
        }
    elif kind == "failed":
        raw["runs"][0]["response_status"] = "ERROR"
        raw["runs"][0]["ai_savings_estimate"] = None
    report = score_price_only_report(raw, spec)
    assert report["probe_passed"] is (kind == "complete")
    assert report["service_baseline_evaluated"] is False
    if kind in {"partial", "duplicate"}:
        assert report["probe_complete"] is False
    if kind in {"unavailable", "failed"}:
        assert report["estimated"] == 2
        assert report["not_estimated"] == 1


def synthetic_price_estimate(current, target="0.100000", reference_current="0.200000"):
    raw, spec = sample()
    value = raw["runs"][0]["candidates"][0]["ai_savings_estimate"]
    value["basis"].update(current_hourly_rate=current, target_hourly_rate=target)
    value["amount"] = str(((Decimal(current) - Decimal(target)) * 730).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP,
    ))
    # AWS 가격 대신 비례 관계에 의존하지 않는 합성 단가로 판정 경계를 검증한다.
    spec["price_reference"]["hourly_rates"]["t3.xlarge"]["usd"] = reference_current
    spec["price_reference"]["hourly_rates"]["t3.medium"]["usd"] = "0.100000"
    return value, spec


@pytest.mark.parametrize("rate, amount, status, direction", [
    ("0.179986", "58.39", "PRICE_DEVIATION", "UNDERSTATED"),
    ("0.180000", "58.40", "PASS", "UNDERSTATED"),
    ("0.200000", "73.00", "PASS", "MATCH"),
    ("0.210000", "80.30", "PASS", "OVERSTATED"),
    ("0.210014", "80.31", "PRICE_DEVIATION", "OVERSTATED"),
])
def test_savings_allow_twenty_percent_below_and_ten_percent_above(rate, amount, status, direction):
    value, spec = synthetic_price_estimate(rate)
    result = score_estimate(value, spec["cases"]["A1"], spec["price_reference"], spec["criteria"])
    assert result["amount"] == amount
    assert result["status"] == status
    assert result["savings_direction"] == direction
    assert result["amount_lower_bound_usd"] == "58.40"
    assert result["amount_upper_bound_usd"] == "80.30"


def test_cent_rounding_at_twenty_percent_is_allowed_without_repairing_the_output():
    raw, spec = sample()
    value = next(run for run in raw["runs"] if run["case_id"] == "A11")["candidates"][0]["ai_savings_estimate"]
    value["basis"].update(current_hourly_rate="0.083200", target_hourly_rate="0.020800")
    value["amount"] = "45.55"
    original = deepcopy(value)
    result = score_estimate(value, spec["cases"]["A11"], spec["price_reference"], spec["criteria"])
    assert result["status"] == "PASS"
    assert result["amount_lower_bound_usd"] == "45.55"
    assert result["amount_upper_bound_usd"] == "62.63"
    assert result["rate_accuracy"]["within_symmetric_tolerance"] is False
    assert value == original


@pytest.mark.parametrize("current, target, current_error, target_error", [
    ("0.300000", "0.200000", "0.5", "1"),
    ("0.150000", "0.050000", "-0.25", "-0.5"),
])
def test_correct_savings_do_not_claim_that_inaccurate_rates_are_correct(current, target, current_error, target_error):
    value, spec = synthetic_price_estimate(current, target)
    result = score_estimate(value, spec["cases"]["A1"], spec["price_reference"], spec["criteria"])
    assert result["status"] == "PASS"
    assert result["savings_direction"] == "MATCH"
    assert result["rate_accuracy"] == {
        "within_symmetric_tolerance": False, "affects_savings_pass": False,
        "current_signed_relative_error": current_error, "target_signed_relative_error": target_error,
    }


@pytest.mark.parametrize("rate, amount, status", [
    ("0.100630", "0.46", "PASS"),
    ("0.103370", "2.46", "PASS"),
    ("0.103384", "2.47", "PRICE_DEVIATION"),
])
def test_existing_one_dollar_floor_is_preserved_for_small_savings(rate, amount, status):
    value, spec = synthetic_price_estimate(rate, reference_current="0.102000")
    result = score_estimate(value, spec["cases"]["A1"], spec["price_reference"], spec["criteria"])
    assert result["amount"] == amount
    assert result["status"] == status
    assert result["amount_lower_bound_usd"] == "0.46"
    assert result["amount_upper_bound_usd"] == "2.46"


def test_legacy_criteria_keep_symmetric_savings_and_rate_checks():
    value, spec = synthetic_price_estimate("0.180000")
    legacy = json.loads(SPEC_PATH.with_name("spec-v1.json").read_text("utf-8"))
    assert score_estimate(value, spec["cases"]["A1"], spec["price_reference"], legacy["criteria"])["status"] == "PRICE_DEVIATION"
    value, spec = synthetic_price_estimate("0.150000", "0.050000")
    result = score_estimate(value, spec["cases"]["A1"], spec["price_reference"], legacy["criteria"])
    assert result["status"] == "PRICE_DEVIATION"
    assert "rate_accuracy" not in result
