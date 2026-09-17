"""B 장부의 계산·중단·전체 승인 한도를 독립 재계산한다. 외부 호출은 없다."""

import hashlib
import json
from collections import Counter
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

ROUND = Path(__file__).resolve().parent
ROOT = ROUND.parents[6]


def read(path):
    return json.loads(path.read_text("utf-8"))


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    frozen = read(ROUND / "preflight.json")
    metadata = read(ROUND / "metadata.json")
    analysis = read(ROUND / "analysis.json")
    approval = read(ROUND / "approval.json")
    preparation = read(ROUND / "preparation-verification.json")
    spec = read(ROUND.parent.parent / "spec.json")
    calls = metadata["calls"]
    assert metadata["status"] != "RUNNING"
    assert metadata["preflight_sha256"] == approval["preflight_sha256"] == sha(ROUND / "preflight.json")
    assert analysis["metadata_sha256"] == sha(ROUND / "metadata.json")
    assert sha(ROUND.parent.parent / "spec.json") == frozen["spec_sha256"]
    assert sha(ROUND.parent / "20260916-output-simplification-1/metadata.json") == frozen["prior_metadata_sha256"]
    assert calls and len(calls) == analysis["attempted_calls"] <= 60
    assert frozen["prior_attempted_calls"] == 2
    assert analysis["cumulative_attempted_calls"] == len(calls) + 2 <= 80
    assert analysis["unused_user_call_limit"] == 78 - len(calls)
    assert approval["candidate"] == "B" and approval["round_maximum_calls"] == 60
    for sources in (frozen["source_sha256"], preparation["source_sha256"]):
        assert all(sha(ROOT / path) == expected for path, expected in sources.items())

    checked = []
    for index, call in enumerate(calls):
        step = frozen["schedule"][index]
        assert all(call[key] == value for key, value in step.items())
        assert call["wire_verified"]
        assert call["wire_sha256"] == frozen["requests"][call["case_id"]]["wire_sha256"]
        if call["status"] != "RETURNED":
            assert index == len(calls) - 1 and call["status"] == "ERROR"
            checked.append({"case_id": call["case_id"], "grade": "MODEL_CALL_FAILED"})
            continue
        value = call["estimate"]
        if value["status"] != "ESTIMATED":
            assert value["basis"] is None and value["amount"] is None
            assert call["explanation_source"] is None
            assert call["grade"]["status"] == value["status"]
            checked.append({"case_id": call["case_id"], "grade": value["status"]})
            continue
        basis = value["basis"]
        expected = spec["cases"][call["case_id"]]
        assert call["diagnostics"] == [] and call["explanation_source"] == "SERVER_TEMPLATE"
        assert all(basis[key] == val for key, val in expected.items())
        assert basis["assumptions"]["hours"] == 730
        assert "서버가 두 단가의 차이에 월 730시간을 곱했습니다" in basis["explanation"]
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
        checked.append({"case_id": call["case_id"], "repeat": call["repeat"], "amount": str(actual),
                        "reference": str(reference), "lower": str(lower), "upper": str(upper),
                        "grade": grade})
    counts = Counter(row["grade"] for row in checked)
    status = metadata["status"]
    if status == "COMPLETED":
        assert len(calls) == 60 and counts["PASS"] >= 57
        assert counts["PASS"] + counts["UNAVAILABLE"] == 60 and analysis["passed"]
    else:
        assert not analysis["passed"]
        assert all(row["grade"] in ("PASS", "UNAVAILABLE") for row in checked[:-1])
        if status == "STOPPED_PRICE_DEVIATION":
            assert checked[-1]["grade"] == "PRICE_DEVIATION"
        elif status == "STOPPED_AVAILABILITY":
            assert counts["UNAVAILABLE"] == 4 and checked[-1]["grade"] == "UNAVAILABLE"
        else:
            assert checked[-1]["grade"] in ("INVALID", "MODEL_CALL_FAILED")
    tokens = {key: sum((call.get("usage") or {}).get(key, 0) for call in calls)
              for key in ("prompt_tokens", "completion_tokens", "cached_prompt_tokens")}
    cost = (tokens["prompt_tokens"] * Decimal("0.20")
            + tokens["completion_tokens"] * Decimal("1.20")
            - tokens["cached_prompt_tokens"] * Decimal("0.18")) / 1_000_000
    assert cost == Decimal(analysis["cost_with_reported_cache_usd"])
    assert cost + Decimal(frozen["prior_cost_usage_estimate_usd"]) == Decimal(
        analysis["cumulative_cost_usage_estimate_usd"],
    )
    history = read(ROUND / "history-before.json")
    prior = read(ROUND.parent / "20260916-post-guardrail-free-validation/verification.json")
    assert all(sha(ROOT / path) == value for path, value in history.items())
    assert all(sha(ROOT / path) == value for path, value in prior["code_sha256"].items())
    result = {
        "execution_integrity_verified": True, "candidate_quality_passed": analysis["passed"],
        "full_sweep_completed": status == "COMPLETED", "execution_status": status,
        "new_live_calls": len(calls), "cumulative_live_calls": len(calls) + 2,
        "extra_calls_in_this_verification": 0, "unused_user_limit": 78 - len(calls),
        "status_counts": dict(counts), "arithmetic_and_bounds": checked,
        "cost_usage_estimate_usd": str(cost), "explanation_source": "SERVER_TEMPLATE",
        "previous_code_files_unchanged": len(prior["code_sha256"]),
        "protected_files_unchanged": len(history),
        "verified_source_sha256": sha(Path(__file__)), "metadata_sha256": sha(ROUND / "metadata.json"),
    }
    (ROUND / "execution-verification.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", "utf-8",
    )
    print(json.dumps({key: value for key, value in result.items() if key != "arithmetic_and_bounds"}))


if __name__ == "__main__":
    main()
