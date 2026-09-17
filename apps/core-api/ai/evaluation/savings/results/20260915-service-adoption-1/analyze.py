"""저장된 생성·판정과 호출 기록을 다시 대조한다. 네트워크 호출은 없다."""

import contextlib
import hashlib
import io
import json
import sys
from collections import Counter
from decimal import Decimal
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[7]
sys.path[:0] = [str(_ROOT / p) for p in ("apps/core-api", "packages", "scripts")]

from ai.evaluation.judge import RestorationOutput, score_restoration
from finops_eval import _fixed_set
from run_validation import (
    LIMITS,
    ROOT,
    ROUND,
    SCHEDULE,
    agent,
    baseline_module,
    flow_report,
    judge_fingerprint,
    load_cases,
    read,
    score_report,
    sha,
    summarize,
    usage_total,
    write,
)


def main():
    frozen = read(ROUND / "preflight.json")
    approval = read(ROUND / "approval.json")
    metadata = read(ROUND / "metadata.json")
    assert approval["preflight_sha256"] == sha(ROUND / "preflight.json")
    assert metadata["preflight_sha256"] == approval["preflight_sha256"]
    assert approval["maximum_calls"] == sum(LIMITS.values()) == 540
    judge_ran = metadata["stages"]["judge"]["status"] == "COMPLETED"
    expected_calls = 540 if judge_ran else 300
    assert len(metadata["calls"]) == metadata["attempted_calls"] == expected_calls
    assert [c["number"] for c in metadata["calls"]] == list(range(1, expected_calls + 1))
    assert all(c["status"] == "RETURNED" and c.get("usage") is not None
               for c in metadata["calls"])
    assert usage_total(metadata["calls"]) == metadata["usage"]
    assert metadata["calls_without_usage"] == 0
    for stage, limit in LIMITS.items():
        state = metadata["stages"][stage]
        if stage == "judge" and not judge_ran:
            assert state["status"] == "SKIPPED_PRICE_GATE"
            assert state["call_end"] == state["call_start"] == 300
            assert not any(c["stage"] == "judge" for c in metadata["calls"])
            continue
        assert state["status"] == "COMPLETED"
        assert state["call_end"] - state["call_start"] == limit
        assert sum(c["stage"] == stage for c in metadata["calls"]) == limit

    cases = load_cases()
    assert frozen["fixed_set"] == _fixed_set(cases)
    assert frozen["spec_sha256"] == sha(ROUND.parent.parent / "spec.json")
    assert frozen["judge_prompt_sha256"] == judge_fingerprint()
    assert frozen["candidate_prompt_sha256"] == agent.finops_prompt_fingerprint()
    assert frozen["baseline_source_sha256"] == baseline_module()[1]
    # PR 버전 표기·승인 스냅샷 갱신 뒤에도 계측 당시 소스 지문은 덮어쓰지 않는다.
    source_changes = {path: {"measured": value, "current": sha(ROOT / path)}
                      for path, value in frozen["source_sha256"].items()
                      if sha(ROOT / path) != value}
    allowed_after_acceptance = {
        "apps/core-api/ai/agent.py",
        "apps/core-api/ai/evaluation/summary_prompt_snapshot.json",
        "scripts/finops_eval.py",
    }
    assert not (set(source_changes) - allowed_after_acceptance)
    if "apps/core-api/ai/agent.py" in source_changes:
        assert agent.FINOPS_PROMPT_VERSION == "v1.0.0"
        measured_source = (ROOT / "apps/core-api/ai/agent.py").read_bytes().replace(
            b'FINOPS_PROMPT_VERSION = "v1.0.0"', b'FINOPS_PROMPT_VERSION = "v0.9.1"',
        )
        assert hashlib.sha256(measured_source).hexdigest() == frozen["source_sha256"]["apps/core-api/ai/agent.py"]
    if "scripts/finops_eval.py" in source_changes:
        edits = read(ROUND / "post-measurement-edits.json")
        assert edits["path"] == "scripts/finops_eval.py"
        assert edits["current_sha256"] == sha(ROOT / edits["path"])
        measured_source = (ROOT / edits["path"]).read_bytes()
        for edit in reversed(edits["edits"]):
            assert measured_source.count(edit["new"].encode("utf-8")) == 1
            measured_source = measured_source.replace(edit["new"].encode("utf-8"), edit["old"].encode("utf-8"))
        assert hashlib.sha256(measured_source).hexdigest() == frozen["source_sha256"][edits["path"]]

    raw = {stage: read(ROUND / f"{stage}-raw.json") for stage in ("baseline", "candidate")}
    flows = {}
    for stage, data in raw.items():
        assert data["fixed_set"] == frozen["fixed_set"]
        assert data["prompt_sha256"] == frozen[f"{stage}_prompt_sha256"]
        assert [(r["case_id"], r["repeat"]) for r in data["runs"]] == SCHEDULE
        assert len(data["runs"]) == 60
        for row in data["runs"]:
            calls = [c for c in metadata["calls"] if c["stage"] == stage
                     and c["case_id"] == row["case_id"] and c["repeat"] == row["repeat"]]
            assert calls == row["calls"]
            assert len(calls) == (2 if stage == "baseline" else 3)
        flows[stage] = flow_report(data)
        assert flows[stage] == read(ROUND / f"{stage}-flow.json")
    scored = score_report(raw["candidate"], read(ROUND.parent.parent / "spec.json"))
    assert scored == read(ROUND / "candidate-scored.json")

    judged = read(ROUND / "judged.json") if judge_ran else {"sources": []}
    if judge_ran:
        assert judged["judge_prompt_sha256"] == frozen["judge_prompt_sha256"]
        assert [s["column"] for s in judged["sources"]] == ["baseline", "candidate"]
    else:
        assert not scored["passed"] and not (ROUND / "judged.json").exists()
    payloads = {c.case_id: agent._incident_payload(c.graph_input) for c in cases}
    summaries = {}
    for column in judged["sources"]:
        stage = column["column"]
        assert column["raw_sha256"] == sha(ROUND / f"{stage}-raw.json")
        assert len(column["runs"]) == 60
        for index, row in enumerate(column["runs"]):
            original = raw[stage]["runs"][index]
            assert row["run_index"] == index and row["case_id"] == original["case_id"]
            assert len(row["restoration"]) == len(row["defects"]) == 1
            calls = [c for c in metadata["calls"] if c["stage"] == "judge"
                     and c["column"] == stage and c["case_id"] == row["case_id"]
                     and c["repeat"] == original["repeat"]]
            assert [c["output_type"] for c in calls] == ["RestorationOutput", "DefectJudgement"]
            restoration = row["restoration"][0]
            checked = score_restoration(
                RestorationOutput.model_validate(restoration["output"]), payloads[row["case_id"]],
            )
            for name in ("verdict_ok", "skip_reason_ok", "restored", "missing", "unexpected"):
                expected = getattr(checked, name)
                assert restoration[name] == (list(expected) if isinstance(expected, tuple) else expected)
        with contextlib.redirect_stdout(io.StringIO()):
            summary = summarize(raw[stage]["label"], column["runs"], 1)
        assert summary == column["summary"]
        summaries[stage] = summary

    old, new = summaries.get("baseline"), summaries.get("candidate")
    candidate_flow = flows["candidate"]
    gates = {
        "price": scored["passed"],
        "contract_and_generation": candidate_flow["succeeded"] == 60,
        "no_proposal": candidate_flow["no_proposal"] == 0,
        "input_facts": candidate_flow["fact_checked"] == 60 and candidate_flow["fact_failed"] == 0,
        "execution_fields": candidate_flow["stable_slots"] == candidate_flow["field_slots"] == 36,
        "judge_completed": judge_ran and old["judged"] == new["judged"] == 60 and old["errors"] == new["errors"] == 0,
        "restoration": (all(new["restored"].get(k, 0) >= v for k, v in old["restored"].items())
                        if judge_ran else None),
    }
    values = scored["results"]
    rate_diagnostics = [v for v in values if not v.get("rate_accuracy", {}).get("within_symmetric_tolerance", True)]
    u = metadata["usage"]
    gross = (Decimal(u["prompt_tokens"]) * Decimal("0.20")
             + Decimal(u["completion_tokens"]) * Decimal("1.20")) / 1_000_000
    cached = gross - Decimal(u["cached_prompt_tokens"]) * Decimal("0.18") / 1_000_000
    assert str(gross) == metadata["cost_without_cache_discount_usd"]
    assert str(cached) == metadata["cost_with_reported_cache_usd"]
    result = {
        "passed": all(gates.values()), "gates": gates,
        "calls": expected_calls, "approved_maximum_calls": 540,
        "judge_status": metadata["stages"]["judge"]["status"],
        "usage": u, "cost_with_reported_cache_usd": str(cached),
        "cost_without_cache_discount_usd": str(gross),
        "elapsed_stage_seconds": sum(s["elapsed_seconds"] for s in metadata["stages"].values()),
        "price_counts": scored["status_counts"],
        "savings_direction_counts": dict(Counter(v.get("savings_direction") for v in values)),
        "minimum_savings_signed_relative_error": str(min(Decimal(v["signed_relative_error"]) for v in values)),
        "maximum_savings_signed_relative_error": str(max(Decimal(v["signed_relative_error"]) for v in values)),
        "rate_diagnostic_outside_10_percent": len(rate_diagnostics),
        "rate_diagnostic_affects_savings_pass": False,
        "rate_diagnostic_over_10_percent": sum(any(Decimal(v["rate_accuracy"][k]) > Decimal("0.10")
                                                    for k in ("current_signed_relative_error", "target_signed_relative_error"))
                                               for v in rate_diagnostics),
        "amount_on_lower_bound": sum(v["amount"] == v["amount_lower_bound_usd"] for v in values),
        "flows": flows, "summary_comparison": summaries,
        "sources_changed_after_measurement": source_changes,
        "artifacts_sha256": {p: sha(ROUND / p) for p in (
            "preflight.json", "approval.json", "metadata.json", "baseline-raw.json",
            "candidate-raw.json", "baseline-flow.json", "candidate-flow.json",
            "candidate-scored.json", "judged.json",
        ) if (ROUND / p).exists()},
        "analysis_source_sha256": sha(Path(__file__)),
    }
    write(ROUND / "analysis.json", result)
    print(json.dumps({k: v for k, v in result.items()
                      if k not in {"flows", "artifacts_sha256", "sources_changed_after_measurement"}},
                     ensure_ascii=False, indent=2))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
