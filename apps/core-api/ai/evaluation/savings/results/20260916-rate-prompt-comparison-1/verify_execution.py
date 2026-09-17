"""완료된 60호출 기록과 서버 산술·기존 판정을 오프라인에서 대조한다."""

import json
from collections import Counter
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import run_comparison as runner
from schemas.savings import AISavingsEstimate


def verify():
    frozen, _ = runner.current_preflight()
    runner.validate_approval(frozen)
    metadata = runner.read(runner.ROUND / "metadata.json")
    saved = runner.read(runner.ROUND / "analysis.json")
    spec = runner.read(runner.SPEC_PATH)
    recalculated = runner.analyze(metadata, spec)
    recalculated.update(
        metadata_sha256=runner.sha(runner.ROUND / "metadata.json"),
        preflight_sha256=metadata["preflight_sha256"],
    )
    assert recalculated == saved, "SAVED_ANALYSIS_DRIFT"
    assert metadata["preflight_sha256"] == runner.sha(runner.ROUND / "preflight.json")
    assert all(metadata[k] == v for k, v in frozen.items()), "SAVED_SETTINGS_DRIFT"
    assert metadata["status"] == "COMPLETED" and len(metadata["calls"]) == 60
    assert saved["comparison_complete"] and not saved["service_baseline_evaluated"]
    assert all(c["status"] == "RETURNED" for c in metadata["calls"])
    assert all(c["usage"] is not None for c in metadata["calls"])
    assert [c["number"] for c in metadata["calls"]] == list(range(1, 61))
    actual = Counter((c["column"], c["case_id"], c["repeat"]) for c in metadata["calls"])
    expected = Counter((col, cid, rep) for col in runner.COLUMNS
                       for cid in runner.CASE_IDS for rep in range(1, 11))
    assert actual == expected, "MISSING_OR_DUPLICATE_CALL"
    grades = {
        (col, g["case_id"], g["repeat"]): g
        for col, values in saved["columns"].items() for g in values["results"]
    }
    arithmetic_checked = 0
    rows = []
    for call in metadata["calls"]:
        estimate = AISavingsEstimate.model_validate(call["estimate"])
        grade = grades[call["column"], call["case_id"], call["repeat"]]
        row = {k: call[k] for k in ("number", "column", "case_id", "repeat", "model")}
        row.update(status=grade["status"], estimate_status=estimate.status.value)
        if estimate.status.value != "ESTIMATED":
            row["reason"] = estimate.reason.value
            rows.append(row)
            continue
        basis = estimate.basis
        context = spec["cases"][call["case_id"]]
        assert all(getattr(basis, k) == v for k, v in context.items())
        cents = Decimal("0.01")
        amount = ((basis.current_hourly_rate - basis.target_hourly_rate) * 730).quantize(
            cents, rounding=ROUND_HALF_UP,
        )
        assert estimate.amount == amount, "SERVER_ARITHMETIC_MISMATCH"
        ref = spec["price_reference"]["hourly_rates"]
        current = Decimal(ref[basis.current_instance_type]["usd"])
        target = Decimal(ref[basis.target_instance_type]["usd"])
        reference = ((current - target) * 730).quantize(cents, rounding=ROUND_HALF_UP)
        criteria = spec["criteria"]
        floor = Decimal(criteria["amount_absolute_tolerance_usd"])
        lower = max(Decimal(0), reference - max(
            floor, reference * Decimal(criteria["amount_underestimate_relative_tolerance"]),
        )).quantize(cents, rounding=ROUND_HALF_UP)
        upper = (reference + max(
            floor, reference * Decimal(criteria["amount_overestimate_relative_tolerance"]),
        )).quantize(cents, rounding=ROUND_HALF_UP)
        assert (lower <= amount <= upper) == (grade["status"] == "PASS")
        current_error = (basis.current_hourly_rate - current) / current
        target_error = (basis.target_hourly_rate - target) / target
        rate_ok = max(abs(current_error), abs(target_error)) <= Decimal(criteria["rate_relative_tolerance"])
        assert rate_ok == grade["rate_accuracy"]["within_symmetric_tolerance"]
        arithmetic_checked += 1
        row.update(
            current_hourly_rate=str(basis.current_hourly_rate),
            target_hourly_rate=str(basis.target_hourly_rate),
            amount=str(amount), reference_amount=str(reference),
            amount_lower_bound=str(lower), amount_upper_bound=str(upper),
            signed_error_percent=str((amount - reference) / reference * 100),
            current_rate_error_percent=str(current_error * 100),
            target_rate_error_percent=str(target_error * 100),
            at_lower_bound=amount == lower, rates_within_tolerance=rate_ok,
            explanation=basis.explanation,
        )
        rows.append(row)
    summary = {}
    for column in runner.COLUMNS:
        selected = [r for r in rows if r["column"] == column]
        priced = [r for r in selected if "amount" in r]
        rate_deviations = [r for r in priced if not r["rates_within_tolerance"]]
        rate_directions = Counter(
            "HAS_RATE_ABOVE_10_PERCENT"
            if max(Decimal(r["current_rate_error_percent"]), Decimal(r["target_rate_error_percent"])) > 10
            else "ONLY_RATE_BELOW_MINUS_10_PERCENT"
            for r in rate_deviations
        )
        summary[column] = {
            "status_counts": dict(Counter(r["status"] for r in selected)),
            "at_lower_bound": sum(r["at_lower_bound"] for r in priced),
            "rate_diagnostic_outside": len(rate_deviations),
            "rate_diagnostic_directions": dict(rate_directions),
            "minimum_savings_error_percent": str(min(
                Decimal(r["signed_error_percent"]) for r in priced
            )) if priced else None,
            "maximum_savings_error_percent": str(max(
                Decimal(r["signed_error_percent"]) for r in priced
            )) if priced else None,
            "savings_directions": dict(Counter(
                "OVERSTATED" if Decimal(r["signed_error_percent"]) > 0 else
                "UNDERSTATED" if Decimal(r["signed_error_percent"]) < 0 else "MATCH"
                for r in priced
            )),
            "internal_code_explanations": sum(
                "pricing_source" in r["explanation"] or "MODEL_KNOWLEDGE" in r["explanation"]
                for r in priced
            ),
        }
    return {
        "verified_at": datetime.now(UTC).isoformat(),
        "scope": "Completed 60-call diagnostic; offline record and arithmetic verification",
        "additional_model_calls": 0, "passed": True, "calls_verified": 60,
        "arithmetic_checked": arithmetic_checked, "summary": summary,
        "deviations": [r for r in rows if r["status"] != "PASS"], "rows": rows,
        "service_baseline_evaluated": False,
        "source_sha256": {name: runner.sha(runner.ROUND / name) for name in (
            "metadata.json", "analysis.json", "preflight.json", "approval.json",
            "run_comparison.py", "verify_execution.py",
        )},
    }


if __name__ == "__main__":
    result = verify()
    runner.write(Path(__file__).parent / "execution-verification.json", result)
    print(json.dumps({k: result[k] for k in (
        "passed", "calls_verified", "arithmetic_checked", "summary", "deviations",
    )}, ensure_ascii=False, indent=2))
