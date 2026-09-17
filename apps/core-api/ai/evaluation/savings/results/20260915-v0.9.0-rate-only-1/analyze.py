"""완료한 v0.9.0 원자료를 재채점하고 호출·기록·단가 진단을 확인한다. 모델 호출 없음."""

import hashlib
import importlib.util
import json
import sys
from collections import Counter
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path

ROUND = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("rate_only_round", ROUND / "run_round.py")
runner = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = runner
spec.loader.exec_module(runner)


def read(name):
    return json.loads((ROUND / name).read_text("utf-8"))


def analyze():
    frozen, evaluation_spec, _ = runner.preflight()
    raw, metadata, scored, flow = (read(name) for name in (
        "raw.json", "metadata.json", "scored.json", "flow-scored.json",
    ))
    if metadata["status"] != "COMPLETED_PLANNED_EXECUTIONS" or len(raw["runs"]) != 60:
        raise ValueError("FULL_ROUND_NOT_COMPLETE")
    runner.validate_saved_runs(raw, metadata)
    recalculated = runner.score_report(raw, evaluation_spec)
    assert all(scored[key] == value for key, value in recalculated.items())
    assert runner.usage_total(metadata["calls"]) == metadata["usage"]
    assert sum(len(r["calls"]) for r in raw["runs"]) == metadata["attempted_calls"]
    assert all(runner.usage_total(r["calls"])[key] == r[key]
               for r in raw["runs"] for key in runner.TOKEN_KEYS)
    pilot = read("pilot/raw.json")
    assert raw["runs"][:18] == pilot["runs"]
    pilot_meta = read("pilot/metadata.json")
    assert metadata["calls"][:54] == pilot_meta["calls"]
    assert metadata["stages"][0] == pilot_meta["stages"][0]
    for name, expected in read("pilot-snapshot.json").items():
        assert runner.sha(ROUND / "pilot" / name) == expected
    before = read("before.json")
    for group in ("historical_sha256", "protected_sha256"):
        assert all(runner.sha(runner.ROOT / name) == expected
                   for name, expected in before[group].items())
    observed = []
    for record in raw["runs"]:
        errors = [c for c in record["calls"] if "error_phase" in c]
        first_error = errors[0] if errors else {}
        observed.append(runner.CaseRun(
            case_id=record["case_id"],
            output=runner.AgentGraphOutput.model_validate({
                key: record[key] for key in runner.AgentGraphOutput.model_fields}),
            **{key: record[key] for key in runner.TOKEN_KEYS},
            fact=runner.FactCheckResult(**record["fact"]),
            readback=runner.ReadbackResult(**record["readback"]),
            observation_cites_input=record["observation_cites_input"],
            error=first_error.get("error"), error_phase=first_error.get("error_phase"),
        ))
    summary = runner.build_column_report(raw["label"], observed)
    # JSON 왕복 변환으로 tuple/list의 표현 차이만 정규화한다.
    normalized_summary = json.loads(json.dumps(asdict(summary)))
    assert all(flow[key] == value for key, value in normalized_summary.items())
    assert flow["field_stability"] == summary.field_stability
    joined = [{"case_id": r["case_id"], "repeat": r["repeat"], **grade}
              for r, grade in zip(raw["runs"], scored["results"], strict=True)]
    per_case = {}
    for case_id in runner.CASE_IDS:
        rows = [r for r in joined if r["case_id"] == case_id]
        amounts = [Decimal(r["amount"]) for r in rows if "amount" in r]
        per_case[case_id] = {
            "status_counts": dict(Counter(r["status"] for r in rows)),
            "amount_range_usd": [str(min(amounts)), str(max(amounts))] if amounts else None,
        }
    numeric = [r for r in joined if "amount" in r]
    outside_rates = [r for r in numeric if not r["rate_accuracy"]["within_symmetric_tolerance"]]
    boundary = [r for r in numeric if Decimal(r["amount"]) == Decimal(r["amount_lower_bound_usd"])]
    result = {
        "prompt_version": "v0.9.0", "price_gate_passed": scored["passed"],
        "runs": len(joined), "calls": metadata["attempted_calls"],
        "status_counts": scored["status_counts"], "per_case": per_case,
        "largest_overestimate": max(numeric, key=lambda r: Decimal(r["signed_relative_error"])),
        "largest_underestimate": min(numeric, key=lambda r: Decimal(r["signed_relative_error"])),
        "per_rate_diagnostic_failures": len(outside_rates),
        "lower_bound_hits": len(boundary),
        "per_rate_diagnostic_cases": [{"case_id": r["case_id"], "repeat": r["repeat"]} for r in outside_rates],
        "failures": [r for r in joined if r["status"] not in {"PASS", "UNAVAILABLE"}],
        "cost_with_reported_cache_usd": metadata["cost_estimate_with_reported_cache_usd"],
        "cost_without_cache_discount_usd": metadata["cost_estimate_without_cache_discount_usd"],
        "elapsed_seconds": metadata["elapsed_seconds"],
        "usage": metadata["usage"],
        "verification": {
            "score_reproduction": True, "flow_reproduction": True,
            "call_ledger_and_usage": True, "pilot_prefix_and_snapshot": True,
            "candidate_frozen_files": len(frozen["implementation_sha256"]),
            "historical_files_unchanged": len(before["historical_sha256"]),
            "runtime_adoption": False, "summary_paired_judge": False,
        },
        "source_sha256": {name: hashlib.sha256((ROUND / name).read_bytes()).hexdigest()
                          for name in ("raw.json", "metadata.json", "scored.json", "flow-scored.json")},
    }
    runner.write_json(ROUND / "analysis.json", result)
    print(json.dumps({k: result[k] for k in (
        "price_gate_passed", "runs", "calls", "status_counts", "per_rate_diagnostic_failures",
        "lower_bound_hits", "cost_with_reported_cache_usd", "cost_without_cache_discount_usd",
        "elapsed_seconds", "verification",
    )}, ensure_ascii=False))


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    analyze()
