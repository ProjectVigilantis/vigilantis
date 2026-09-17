"""기존 FinOps 계측 JSON을 독립적인 AWS 단가 스냅샷과 대조한다 (#347).

새 모델 호출·단가 조회·요약 채점은 하지 않는다. 반복 일치와 가격 정확도는 별도다.
"""

from collections import Counter, defaultdict
from decimal import ROUND_HALF_UP, Decimal

from pydantic import ValidationError
from schemas.savings import AISavingsEstimate, SavingsStatus

SCORER_VERSION = "v0.2.0"
RIGHTSIZING = "RUNBOOK_EC2_RIGHTSIZING"


def scorer_version(criteria: dict) -> str:
    """과거 대칭 기준의 결과 형식을 보존하며 현재 절감액 기준과 구분한다."""
    policy = criteria.get("amount_policy", "SYMMETRIC_ACCURACY")
    if policy == "SYMMETRIC_ACCURACY":
        return "v1"
    if policy == "ASYMMETRIC_SAVINGS":
        return SCORER_VERSION
    raise ValueError("알 수 없는 절감액 평가 기준입니다")


def _relative_error(actual: Decimal, expected: Decimal) -> Decimal:
    return abs(actual - expected) / expected


def _score_run(run: dict, expected: dict, reference: dict, criteria: dict) -> dict:
    result = {"case_id": run["case_id"]}
    if run["invocation_status"] != "SUCCEEDED":
        return {**result, "status": "GRAPH_FAILED_OR_EMPTY"}
    candidates = run["candidates"]
    if any(c.get("ai_savings_estimate") is not None for c in candidates
           if c["runbook_id"] != RIGHTSIZING):
        return {**result, "status": "UNEXPECTED_SAVINGS"}
    selected = [c for c in candidates if c["runbook_id"] == RIGHTSIZING]
    if not selected:
        return {**result, "status": "NO_RIGHTSIZING"}
    if len(selected) != 1:
        return {**result, "status": "DUPLICATE_RIGHTSIZING"}
    candidate = selected[0]
    grade = score_estimate(candidate.get("ai_savings_estimate"), expected, reference, criteria)
    if grade["status"] in {"PASS", "PRICE_DEVIATION"} and (
        candidate["target_arn"] != expected["target_arn"]
        or candidate["parameters"]["target_instance_type"] != expected["target_instance_type"]
    ):
        return {**result, "status": "CONTEXT_MISMATCH"}
    return {**result, **grade}


def score_estimate(value: dict | None, expected: dict, reference: dict, criteria: dict) -> dict:
    """절감 예상 자체를 채점한다. 후보 선택·전체 그래프 성공 여부는 호출자가 판정한다."""
    policy_version = scorer_version(criteria)
    if value is None:
        return {"status": "MISSING_ESTIMATE"}
    try:
        estimate = AISavingsEstimate.model_validate(value)
    except (ValidationError, ValueError):
        return {"status": "INVALID_CONTRACT"}
    if estimate.status is not SavingsStatus.ESTIMATED:
        return {"status": estimate.status.value, "reason": estimate.reason.value}
    basis = estimate.basis
    if any(getattr(basis, key) != expected[key] for key in (
        "target_arn", "region", "current_instance_type", "target_instance_type",
    )):
        return {"status": "CONTEXT_MISMATCH"}
    rates = reference["hourly_rates"]
    before = Decimal(rates[basis.current_instance_type]["usd"])
    after = Decimal(rates[basis.target_instance_type]["usd"])
    expected_amount = ((before - after) * 730).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    rate_errors = [
        _relative_error(basis.current_hourly_rate, before),
        _relative_error(basis.target_hourly_rate, after),
    ]
    amount_error = abs(estimate.amount - expected_amount)
    rates_accurate = max(rate_errors) <= Decimal(criteria["rate_relative_tolerance"])
    details = {}
    if policy_version == "v1":
        tolerance = max(
            Decimal(criteria["amount_absolute_tolerance_usd"]),
            expected_amount * Decimal(criteria["amount_relative_tolerance"]),
        )
        accurate = amount_error <= tolerance and rates_accurate
    else:
        absolute_floor = Decimal(criteria["amount_absolute_tolerance_usd"])
        lower = max(Decimal(0), expected_amount - max(
            absolute_floor,
            expected_amount * Decimal(criteria["amount_underestimate_relative_tolerance"]),
        )).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        upper = (expected_amount + max(
            absolute_floor,
            expected_amount * Decimal(criteria["amount_overestimate_relative_tolerance"]),
        )).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        # 표시 금액과 허용 경계를 같은 센트 단위로 비교한다. 산출값은 보정하지 않는다.
        accurate = lower <= estimate.amount <= upper
        signed_error = estimate.amount - expected_amount
        details = {
            "savings_direction": (
                "OVERSTATED" if signed_error > 0 else "UNDERSTATED" if signed_error < 0 else "MATCH"
            ),
            "signed_relative_error": str(signed_error / expected_amount),
            "amount_lower_bound_usd": str(lower),
            "amount_upper_bound_usd": str(upper),
            "rate_accuracy": {
                "within_symmetric_tolerance": rates_accurate,
                "affects_savings_pass": False,
                "current_signed_relative_error": str((basis.current_hourly_rate - before) / before),
                "target_signed_relative_error": str((basis.target_hourly_rate - after) / after),
            },
        }
    return {
        "status": "PASS" if accurate else "PRICE_DEVIATION",
        "amount": str(estimate.amount),
        "expected_amount": str(expected_amount),
        "absolute_error_usd": str(amount_error),
        "relative_error": str(_relative_error(estimate.amount, expected_amount)),
        "current_rate_relative_error": str(rate_errors[0]),
        "target_rate_relative_error": str(rate_errors[1]),
        "arithmetic_context_and_assumptions_valid": True,
        **details,
    }


def score_report(raw: dict, spec: dict) -> dict:
    """부분 실행은 진단값만 반환하고 게이트를 통과시키지 않는다."""
    if raw["fixed_set"] != spec["fixed_set"]:
        raise ValueError("평가 입력 지문이 다릅니다. 입력과 독립 가격 기준을 함께 재확정하세요")
    expected = spec["cases"]
    if any(run["case_id"] not in expected for run in raw["runs"]):
        raise ValueError("고정 세트 밖의 케이스입니다")
    results = [
        _score_run(run, expected[run["case_id"]], spec["price_reference"], spec["criteria"])
        for run in raw["runs"]
    ]
    counts = Counter(item["status"] for item in results)
    per_case = Counter(item["case_id"] for item in results)
    repeats = spec["criteria"]["repeats"]
    complete = per_case == Counter(dict.fromkeys(expected, repeats))
    estimated = counts["PASS"] + counts["PRICE_DEVIATION"]
    missing = len(results) - estimated
    values = defaultdict(list)
    for item in results:
        if "amount" in item:
            values[item["case_id"]].append(item["amount"])
    stability = {
        case_id: {
            "estimated_runs": len(values[case_id]),
            "distinct_amounts": sorted(set(values[case_id])),
            "all_amounts_equal": len(set(values[case_id])) == 1 if len(values[case_id]) >= 2 else None,
        }
        for case_id in expected
    }
    unavailable = counts["UNAVAILABLE"]
    # 분모를 성공한 추정으로 줄이지 않는다. 후보 누락·실패도 미산출이다.
    coverage = Decimal(estimated) / len(results) if results else Decimal(0)
    passed = (
        complete
        and coverage >= Decimal(spec["criteria"]["minimum_estimated_fraction"])
        and counts["PASS"] + unavailable == len(results)
    )
    return {
        "scorer_version": scorer_version(spec["criteria"]),
        "spec_version": spec["version"],
        "prompt_version": raw.get("prompt_version"),
        "prompt_sha256": raw.get("prompt_sha256"),
        "model_snapshots": raw.get("model_snapshots", []),
        "label": raw.get("label"),
        "price_reference_url": spec["price_reference"]["url"],
        "fixed_set": spec["fixed_set"],
        "criteria": spec["criteria"],
        "complete": complete,
        "passed": passed,
        "runs": len(results),
        "estimated": estimated,
        "not_estimated": missing,
        "not_estimated_fraction": str(1 - coverage),
        "status_counts": dict(sorted(counts.items())),
        "stability": stability,
        "results": results,
    }
