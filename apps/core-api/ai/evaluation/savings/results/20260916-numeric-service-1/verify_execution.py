"""Recalculate the completed service round from saved evidence; no model or AWS calls."""

import hashlib
import json
import xml.etree.ElementTree as ET
import zipfile
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

ROUND = Path(__file__).resolve().parent
ROOT = ROUND.parents[6]


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def money(value):
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def verify():
    before = read(ROUND / "before.json")
    preflight = read(ROUND / "preflight.json")
    metadata = read(ROUND / "metadata.json")
    analysis = read(ROUND / "analysis.json")
    preparation = read(ROUND / "preparation-verification.json")
    approval = read(ROUND / "approval.json")
    free = read(ROUND / "free-service.json")
    spec = read(ROUND.parent.parent / "spec.json")
    prior = read(ROUND.parent / "20260916-numeric-output-1/analysis.json")
    assert metadata["mode"] == "ONLINE" and metadata["status"] == "COMPLETED"
    assert free["mode"] == "SDK_MOCK" and free["status"] == "COMPLETED"
    assert analysis["metadata_sha256"] == sha(ROUND / "metadata.json")
    assert analysis["preflight_sha256"] == approval["preflight_sha256"] == sha(ROUND / "preflight.json")
    assert preparation["free_service_sha256"] == sha(ROUND / "free-service.json")
    assert approval["approved"] and approval["maximum_new_calls"] == 18
    assert preflight["spec_sha256"] == sha(ROUND.parent.parent / "spec.json")
    assert preflight["prior_metadata_sha256"] == prior["metadata_sha256"]
    assert preflight["source_sha256"] == preparation["source_sha256"]
    for path, digest in preflight["source_sha256"].items():
        assert sha(ROOT / path) == digest, path
    for path, digest in before["history_sha256"].items():
        assert sha(ROOT / path) == digest, path
    assert sha(ROUND / "source-before.zip") == before["source_archive_sha256"]
    with zipfile.ZipFile(ROUND / "source-before.zip") as archive:
        for path, digest in before["source_sha256"].items():
            assert hashlib.sha256(archive.read(path)).hexdigest() == digest, path

    stages = ["EvidenceSummaryOutput", "CandidateProposalOutput", "ProposedHourlyRates"]
    order = [*stages[:2], "guardrail:PASS", stages[2], "lock"]
    case_ids = spec["fixed_set"]["case_ids"]
    calls = metadata["calls"]
    assert len(calls) == 18
    for index, call in enumerate(calls):
        assert call["number"] == index + 1 and call["status"] == "RETURNED"
        assert call["case_id"] == case_ids[index // 3]
        assert call["stage"] == stages[index % 3]
        assert call["model"] == "gpt-5.6-luna"
        assert call["request_shape_sha256"] == preflight["request_shapes"][call["stage"]]
        if index % 3 == 2:
            assert call["b_request_matches_after_fixture_identity_mapping"]
    assert [case["case_id"] for case in metadata["cases"]] == case_ids
    assert len(free["cases"]) == 6 and len(free["calls"]) == 18
    for record in (metadata, free):
        assert record["localstack_fixture"]["cleaned"]
        assert record["localstack_fixture"]["endpoint"] == "http://localhost:4566"
        for case in record["cases"]:
            assert case["order"] == order and case["grade"]["status"] == "PASS"
            assert (case["fresh_apps"], case["fresh_reads"], case["read_model_calls"]) == (2, 4, 0)
            assert [step["step"] for step in case["guardrail_steps"]] == [
                "SCHEMA_CHECK", "ACTION_WHITELIST", "ARN_MATCH", "AWS_DRY_RUN",
            ]
            assert all(step["result"] == "PASS" for step in case["guardrail_steps"])
            response = case["api_example"]
            assert response["incident_id"] == case["incident_id"]
            assert response["status"] == "AWAITING_APPROVAL"
            assert response["summary_lines"] == case["summary_lines"]
            assert len(response["recommendations"]) == 1
            assert response["recommendations"][0]["ai_savings_estimate"] == case["estimate"]

    rows = []
    criteria = spec["criteria"]
    for index, case in enumerate(metadata["cases"]):
        estimate = case["estimate"]
        basis = estimate["basis"]
        expected = spec["cases"][case["case_id"]]
        assert estimate["status"] == "ESTIMATED" and estimate["reason"] is None
        assert estimate["currency"] == "USD" and estimate["period"] == "MONTH"
        assert basis["explanation_source"] == "SERVER_TEMPLATE" and basis["explanation"]
        assert basis["assumptions"] == {
            "hours": 730, "operating_system": "LINUX", "tenancy": "SHARED",
            "purchase_option": "ON_DEMAND", "included_cost": "INSTANCE_COMPUTE_ONLY",
            "pricing_source": "MODEL_KNOWLEDGE",
        }
        for field in ("region", "current_instance_type", "target_instance_type"):
            assert basis[field] == expected[field]
        assert basis["target_arn"].endswith("/" + metadata["localstack_fixture"]["instance_ids"][index])
        current, target = (Decimal(basis[f"{name}_hourly_rate"]) for name in ("current", "target"))
        reference_current, reference_target = (
            Decimal(spec["price_reference"]["hourly_rates"][expected[f"{name}_instance_type"]]["usd"])
            for name in ("current", "target")
        )
        amount = Decimal(estimate["amount"])
        reference = money((reference_current - reference_target) * 730)
        assert amount == money((current - target) * 730)
        lower = money(reference - max(Decimal(criteria["amount_absolute_tolerance_usd"]),
                                      reference * Decimal(criteria["amount_underestimate_relative_tolerance"])))
        upper = money(reference + max(Decimal(criteria["amount_absolute_tolerance_usd"]),
                                      reference * Decimal(criteria["amount_overestimate_relative_tolerance"])))
        assert lower <= amount <= upper
        assert Decimal(case["grade"]["expected_amount"]) == reference
        assert Decimal(case["grade"]["amount_lower_bound_usd"]) == lower
        assert Decimal(case["grade"]["amount_upper_bound_usd"]) == upper
        rate_within = all(abs(value / expected_rate - 1) <= Decimal(criteria["rate_relative_tolerance"])
                          for value, expected_rate in ((current, reference_current), (target, reference_target)))
        assert case["grade"]["rate_accuracy"]["within_symmetric_tolerance"] == rate_within
        rows.append({"case_id": case["case_id"], "amount": str(amount), "reference": str(reference),
                     "lower": str(lower), "upper": str(upper), "at_lower_bound": amount == lower,
                     "signed_error_percent": str((amount / reference - 1) * 100),
                     "rates_within_10_percent": rate_within})

    usage = {key: sum(call["usage"][key] for call in calls) for key in analysis["usage"]}
    assert usage == analysis["usage"]
    cost = ((usage["prompt_tokens"] - usage["cached_prompt_tokens"]) * Decimal("0.20")
            + usage["cached_prompt_tokens"] * Decimal("0.02")
            + usage["completion_tokens"] * Decimal("1.20")) / 1_000_000
    assert cost == Decimal(analysis["cost_usage_estimate_usd"])
    cumulative_cost = cost + Decimal(prior["cumulative_cost_usage_estimate_usd"])
    assert cumulative_cost == Decimal(analysis["cumulative_cost_usage_estimate_usd"])
    assert prior["cumulative_attempted_calls"] == before["prior_paid_calls"] == 62
    assert 62 + len(calls) == analysis["cumulative_live_calls"] == approval["total_limit"] == 80
    suite = ET.parse(ROUND / "pytest-final.xml").getroot().find("testsuite").attrib
    assert (suite["tests"], suite["failures"], suite["errors"], suite["skipped"]) == ("2224", "0", "0", "2")
    result = {
        "verified_at": datetime.now(UTC).isoformat(), "passed": True,
        "method": "Independent saved-record checks and Decimal calculations; no external calls",
        "current_sources_unchanged": len(preflight["source_sha256"]),
        "historical_artifacts_unchanged": len(before["history_sha256"]),
        "before_archive_sources_verified": len(before["source_sha256"]),
        "online_service_paths": 6, "fresh_api_reads": 24, "read_model_calls": 0,
        "free_service_paths": 6, "pytest_passed": 2222, "pytest_skipped": 2,
        "new_paid_calls": 18, "cumulative_paid_calls": 80, "remaining_calls": 0,
        "cost_usage_estimate_usd": str(cost), "cumulative_cost_usage_estimate_usd": str(cumulative_cost),
        "cases": rows, "metadata_sha256": sha(ROUND / "metadata.json"),
        "analysis_sha256": sha(ROUND / "analysis.json"), "verifier_sha256": sha(Path(__file__)),
    }
    (ROUND / "execution-verification.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    verify()
