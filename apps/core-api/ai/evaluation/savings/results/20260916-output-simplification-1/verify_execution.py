"""보존된 두 응답의 산술·가격 경계·중단·지문을 오프라인으로 확인한다."""

import hashlib
import json
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

ROUND = Path(__file__).resolve().parent
ROOT = ROUND.parents[6]


def read(path):
    return json.loads(path.read_text("utf-8"))


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    preflight = read(ROUND / "preflight.json")
    metadata = read(ROUND / "metadata.json")
    analysis = read(ROUND / "analysis.json")
    approval = read(ROUND / "approval.json")
    preparation = read(ROUND / "preparation-verification.json")
    spec = read(ROUND.parent.parent / "spec.json")
    assert metadata["preflight_sha256"] == approval["preflight_sha256"] == sha(ROUND / "preflight.json")
    assert analysis["metadata_sha256"] == sha(ROUND / "metadata.json")
    assert metadata["status"] == "STOPPED_PRICE_DEVIATION"
    assert metadata["calls"] and len(metadata["calls"]) == analysis["attempted_calls"] == 2
    assert not analysis["passed"] and not analysis["score"]["probe_complete"]
    assert approval["user_total_call_limit"] == 80 and approval["round_maximum_calls"] == 60
    assert sha(ROUND.parent.parent / "spec.json") == preflight["spec_sha256"]
    for sources in (preflight["source_sha256"], preparation["source_sha256"]):
        assert all(sha(ROOT / path) == expected for path, expected in sources.items())
    checked = []
    for index, call in enumerate(metadata["calls"]):
        step = preflight["schedule"][index]
        assert all(call[key] == value for key, value in step.items())
        assert call["status"] == "RETURNED" and call["wire_verified"]
        assert call["wire_sha256"] == preflight["requests"][call["case_id"]]["wire_sha256"]
        assert call["diagnostics"] == []
        value = call["estimate"]
        basis = value["basis"]
        expected = spec["cases"][call["case_id"]]
        assert value["status"] == "ESTIMATED"
        assert all(basis[key] == val for key, val in expected.items())
        assert basis["assumptions"]["hours"] == 730
        actual = ((Decimal(basis["current_hourly_rate"]) - Decimal(basis["target_hourly_rate"]))
                  * 730).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        assert actual == Decimal(value["amount"])
        prices = spec["price_reference"]["hourly_rates"]
        reference = ((Decimal(prices[expected["current_instance_type"]]["usd"])
                      - Decimal(prices[expected["target_instance_type"]]["usd"]))
                     * 730).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        criteria = spec["criteria"]
        floor = Decimal(criteria["amount_absolute_tolerance_usd"])
        lower = max(Decimal(0), reference - max(
            floor, reference * Decimal(criteria["amount_underestimate_relative_tolerance"]),
        )).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        upper = (reference + max(
            floor, reference * Decimal(criteria["amount_overestimate_relative_tolerance"]),
        )).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        grade = "PASS" if lower <= actual <= upper else "PRICE_DEVIATION"
        assert call["grade"]["status"] == analysis["score"]["results"][index]["status"] == grade
        checked.append({"case_id": call["case_id"], "amount": str(actual),
                        "reference": str(reference), "lower": str(lower), "upper": str(upper),
                        "grade": grade})
    assert checked[-1]["grade"] == "PRICE_DEVIATION"
    assert all(row["grade"] == "PASS" for row in checked[:-1])
    assert all(call["usage"]["cached_prompt_tokens"] == 0 for call in metadata["calls"])
    input_tokens = sum(call["usage"]["prompt_tokens"] for call in metadata["calls"])
    output_tokens = sum(call["usage"]["completion_tokens"] for call in metadata["calls"])
    cost = (input_tokens * Decimal("0.20") + output_tokens * Decimal("1.20")) / 1_000_000
    assert cost == Decimal(analysis["cost_with_reported_cache_usd"])
    prior = read(ROUND.parent / "20260916-post-guardrail-free-validation/verification.json")
    history = read(ROUND.parent / "20260916-post-guardrail-free-validation/history-before.json")
    assert all(sha(ROOT / path) == value for path, value in prior["code_sha256"].items())
    assert all(sha(ROUND.parent / path) == value for path, value in history.items())
    result = {
        "execution_integrity_verified": True, "candidate_quality_passed": False,
        "full_sweep_completed": False, "first_price_failure_stopped_execution": True,
        "live_model_calls": len(metadata["calls"]), "extra_calls_in_this_verification": 0,
        "user_limit_unused": 78, "round_calls_unexecuted": 58,
        "arithmetic_and_bounds": checked, "cost_usage_estimate_usd": str(cost),
        "previous_code_files_unchanged": len(prior["code_sha256"]),
        "historical_files_unchanged": len(history),
        "verified_source_sha256": sha(Path(__file__)),
        "metadata_sha256": sha(ROUND / "metadata.json"),
        "preflight_sha256": sha(ROUND / "preflight.json"),
    }
    (ROUND / "execution-verification.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", "utf-8",
    )
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
