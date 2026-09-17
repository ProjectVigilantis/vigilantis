"""승인된 v0.8.0 가격표 비교 12호출. 기본 실행은 네트워크 없는 지문 확인이다."""

import argparse
import hashlib
import json
import logging
import subprocess
import sys
from collections import Counter
from datetime import UTC, datetime
from decimal import Decimal
from importlib.metadata import version
from pathlib import Path
from time import perf_counter

ROUND = Path(__file__).resolve().parent
ROOT = ROUND.parents[6]
sys.path[:0] = [str(ROOT / path) for path in ("apps/core-api", "packages", "scripts")]

from ai.agent import finops_prompt_fingerprint
from ai.evaluation.savings.isolation import (
    price_only_prompt_fingerprint,
    price_only_request,
    score_price_only_report,
)
from ai.evaluation.savings.price_table import (
    PRICE_TABLE_PROMPT_VERSION,
    ProposedTableSavings,
    accept_price_table,
    price_table_prompt_fingerprint,
    price_table_request,
)
from ai.evaluation.savings.three_call import three_call_prompt_fingerprint
from ai.model_client import AIModelError
from ai.openai_client import OpenAIModelClient
from ai.savings import ProposedSavingsEstimate, accept_savings_estimate
from dotenv import dotenv_values
from finops_eval import _fixed_set, load_cases
from openai import OpenAI

SCHEDULE = [
    ("A1", 1, "pair"), ("A1", 1, "table"),
    ("A11", 1, "table"), ("A11", 1, "pair"),
    ("A11", 2, "pair"), ("A11", 2, "table"),
    ("A1", 2, "table"), ("A1", 2, "pair"),
    ("A1", 3, "pair"), ("A1", 3, "table"),
    ("A11", 3, "table"), ("A11", 3, "pair"),
]
TOKEN_KEYS = ("prompt_tokens", "completion_tokens", "cached_prompt_tokens")
CALL_BUDGET = 12


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def preflight():
    frozen = json.loads((ROUND / "preflight.json").read_text("utf-8"))
    spec_path = ROUND.parent.parent / "spec.json"
    spec = json.loads(spec_path.read_text("utf-8"))
    cases = load_cases()
    if _fixed_set(cases) != frozen["fixed_set"] or spec["fixed_set"] != frozen["fixed_set"]:
        raise ValueError("INPUT_DRIFT")
    if sha(spec_path) != frozen["spec_sha256"]:
        raise ValueError("SPEC_DRIFT")
    for name, expected in frozen["implementation_sha256"].items():
        if sha(ROOT / name) != expected:
            raise ValueError("IMPLEMENTATION_DRIFT")
    actual = {
        "runtime_prompt_sha256": finops_prompt_fingerprint(),
        "price_only_prompt_sha256": price_only_prompt_fingerprint(),
        "price_table_prompt_sha256": price_table_prompt_fingerprint(),
        "three_call_pair_prompt_sha256": three_call_prompt_fingerprint(),
        "three_call_table_prompt_sha256": three_call_prompt_fingerprint(price_mode="table"),
    }
    if any(frozen[key] != value for key, value in actual.items()) or PRICE_TABLE_PROMPT_VERSION != "v0.8.0":
        raise ValueError("PROMPT_DRIFT")
    expected_schedule = Counter((c, r, mode) for c in ("A1", "A11") for r in range(1, 4)
                                for mode in ("pair", "table"))
    if Counter(SCHEDULE) != expected_schedule or len(SCHEDULE) != CALL_BUDGET:
        raise ValueError("SCHEDULE_DRIFT")
    if frozen["runner_sha256"] != sha(Path(__file__)):
        raise ValueError("RUNNER_DRIFT")
    return frozen, spec, {case.case_id: case for case in cases}


def usage_total(calls):
    return {key: sum((call.get("usage") or {}).get(key, 0) for call in calls) for key in TOKEN_KEYS}


def save_results(raws, spec):
    scored = {}
    for mode, raw in raws.items():
        path = ROUND / f"{mode}-raw.json"
        write_json(path, raw)
        scored[mode] = score_price_only_report(raw, spec)
        scored[mode].update(condition=mode, source_file=path.name, source_sha256=sha(path),
                            spec_sha256=sha(ROUND.parent.parent / "spec.json"))
        write_json(ROUND / f"{mode}-scored.json", scored[mode])
    return scored


def execute(frozen, spec, cases):
    if any((ROUND / name).exists() for name in (
        "metadata.json", "pair-raw.json", "table-raw.json", "pair-scored.json", "table-scored.json",
    )):
        raise ValueError("ROUND_ALREADY_STARTED")
    api_key = dotenv_values(ROOT.parent.parent / ".env").get("OPENAI_API_KEY")
    if not api_key:
        raise ValueError("MISSING_API_KEY")
    metadata = {
        "scope": "User approved v0.8.0: price-only pair/table comparison, A1/A11 three times per condition, 12 calls maximum.",
        "trial_version": "v0.8.0", "execution_mode": "price_table_comparison",
        "started_at": datetime.now(UTC).isoformat(), "status": "RUNNING",
        "model": "gpt-5.6-luna", "reasoning_effort": "low", "temperature": None,
        "timeout_seconds": 30, "max_attempts": 1, "sdk_max_retries": 0, "call_budget": CALL_BUDGET,
        "credential_source": "main checkout .env; explicit user approval persists",
        "endpoint": "https://api.openai.com/v1",
        "schedule": [{"case_id": c, "repeat": r, "condition": m} for c, r, m in SCHEDULE],
        "calls": [], "fixed_set": frozen["fixed_set"], "spec_sha256": frozen["spec_sha256"],
        "preflight_sha256": sha(ROUND / "preflight.json"), "runner_sha256": sha(Path(__file__)),
        "implementation_sha256": frozen["implementation_sha256"],
        **{key: value for key, value in frozen.items() if key.endswith("prompt_sha256")},
        "git_base_head": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "package_versions": {name: version(name) for name in ("openai", "pydantic", "langgraph")},
        "price_per_million_usd": {"input": "0.20", "cached_input": "0.02", "output": "1.20"},
        "pricing_source": "https://developers.openai.com/api/docs/models/gpt-5.6-luna",
        "stopping_rule": "Collect planned quality failures. Abort request/transport/unexpected errors. No retry or reallocation.",
    }
    raws = {mode: {
        "label": f"gpt-5.6-luna/low v0.8.0 comparison {mode}",
        "execution_mode": "price_only_diagnostic", "condition": mode,
        "prompt_version": "v0.8.0" if mode == "table" else "v0.6.0",
        "prompt_sha256": frozen[f"price_{'table' if mode == 'table' else 'only'}_prompt_sha256"],
        "model_snapshots": [], "repeats": 3, "case_ids": ["A1", "A11"],
        "fixed_set": frozen["fixed_set"], "runs": [],
    } for mode in ("pair", "table")}
    started = perf_counter()
    exit_code = 0
    write_json(ROUND / "metadata.json", metadata)
    save_results(raws, spec)
    try:
        with OpenAI(api_key=api_key, base_url="https://api.openai.com/v1", timeout=30, max_retries=0) as sdk:
            client = OpenAIModelClient(client=sdk, model="gpt-5.6-luna", timeout_seconds=30,
                                       max_attempts=1, retry_backoff_seconds=0,
                                       temperature=None, reasoning_effort="low")
            for case_id, repeat, mode in SCHEDULE:
                if len(metadata["calls"]) >= CALL_BUDGET:
                    raise ValueError("CALL_BUDGET")
                asset = cases[case_id].graph_input.asset_context
                request = price_table_request(asset) if mode == "table" else price_only_request(asset)
                output_type = ProposedTableSavings if mode == "table" else ProposedSavingsEstimate
                item = {"number": len(metadata["calls"]) + 1, "case_id": case_id, "repeat": repeat,
                        "condition": mode, "stage": "savings", "status": "STARTED"}
                metadata["calls"].append(item)
                write_json(ROUND / "metadata.json", metadata)
                tick, usage = perf_counter(), None
                record = {"case_id": case_id, "repeat": repeat, "response_status": "ERROR",
                          "ai_savings_estimate": None, "savings_diagnostics": []}
                try:
                    response = client.complete(request, output_type)
                except AIModelError as exc:
                    usage = exc.usage
                    item.update(status="ERROR", error=type(exc).__name__, error_phase=exc.phase)
                except Exception as exc:
                    item.update(status="UNEXPECTED_ERROR", error=type(exc).__name__, error_phase="runner")
                    raise
                else:
                    usage = response.usage
                    item.update(status="RETURNED", model=response.model)
                    if mode == "table":
                        accepted = accept_price_table(response.output, asset)
                        estimate = accepted.estimate
                        record.update(price_table=accepted.hourly_rates,
                                      table_diagnostics=accepted.table_diagnostics,
                                      savings_diagnostics=accepted.savings_diagnostics)
                    else:
                        estimate = accept_savings_estimate(response.output, asset,
                                                          diagnostics=record["savings_diagnostics"])
                    record.update(response_status="RETURNED", ai_savings_estimate=estimate.model_dump(mode="json"))
                finally:
                    item["elapsed_seconds"] = round(perf_counter() - tick, 3)
                    item["usage"] = None if usage is None else {key: getattr(usage, key) for key in TOKEN_KEYS}
                    write_json(ROUND / "metadata.json", metadata)
                record.update(call_number=item["number"], elapsed_seconds=item["elapsed_seconds"], usage=item["usage"])
                raws[mode]["runs"].append(record)
                raws[mode]["model_snapshots"] = sorted({c["model"] for c in metadata["calls"]
                                                       if c["condition"] == mode and "model" in c})
                scored = save_results(raws, spec)
                grade = scored[mode]["results"][-1]
                print(json.dumps({"call": item["number"], "condition": mode, "case_id": case_id,
                                  "repeat": repeat, "grade": grade["status"], "amount": grade.get("amount"),
                                  "diagnostics": record["savings_diagnostics"]}, ensure_ascii=False), flush=True)
                if item.get("error_phase") in {"request", "transport"}:
                    metadata["status"], exit_code = "ABORTED_MODEL_ERROR", 2
                    break
            else:
                metadata["status"] = "COMPLETED_PLANNED_EXECUTIONS"
    except Exception as exc:  # noqa: BLE001 — 실행 경계에서 메시지 없이 중단 원인 클래스만 보존한다.
        metadata.update(status="ABORTED_UNEXPECTED_ERROR", runner_error=type(exc).__name__)
        exit_code = 2
    finally:
        metadata.update(ended_at=datetime.now(UTC).isoformat(), elapsed_seconds=round(perf_counter() - started, 3),
                        attempted_calls=len(metadata["calls"]), unused_call_budget=CALL_BUDGET - len(metadata["calls"]),
                        usage=usage_total(metadata["calls"]),
                        calls_without_usage=sum(c.get("usage") is None for c in metadata["calls"]), exit_code=exit_code)
        usage = metadata["usage"]
        metadata["cost_estimate_without_cache_discount_usd"] = str((
            usage["prompt_tokens"] * Decimal("0.20") + usage["completion_tokens"] * Decimal("1.20")
        ) / 1_000_000)
        metadata["cost_note"] = "Available response usage times public rates, without cache discount; not billed cost."
        metadata["service_baseline_evaluated"] = False
        scored = save_results(raws, spec)
        metadata["next_stage_eligible"] = scored["table"]["probe_passed"] and exit_code == 0
        metadata["status_counts"] = {mode: value["status_counts"] for mode, value in scored.items()}
        write_json(ROUND / "metadata.json", metadata)
    print(json.dumps({key: metadata[key] for key in (
        "status", "attempted_calls", "elapsed_seconds", "usage", "cost_estimate_without_cache_discount_usd",
        "calls_without_usage", "status_counts", "next_stage_eligible", "exit_code",
    )}), flush=True)
    return exit_code


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    logging.disable(logging.CRITICAL)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute-approved-round", action="store_true")
    args = parser.parse_args()
    try:
        prepared = preflight()
        if args.execute_approved_round:
            code = execute(*prepared)
        else:
            print("PREFLIGHT_OK live_calls=0 conditions=2 outputs_per_condition=6 maximum_calls=12")
            code = 0
    except Exception as exc:  # noqa: BLE001 — 원본 예외 대신 클래스만 출력하고 실패 종료한다.
        print(json.dumps({"status": "STOPPED", "error_class": type(exc).__name__}), flush=True)
        code = 2
    raise SystemExit(code)
