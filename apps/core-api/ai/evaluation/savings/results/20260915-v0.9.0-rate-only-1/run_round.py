"""v0.9.0 단가 전용 후보를 54호출 확인 후 최대 126호출 확대한다. 기본 동작은 네트워크 없는 사전 확인이다."""

import argparse
import hashlib
import json
import logging
import subprocess
import sys
from collections import Counter
from dataclasses import asdict
from datetime import UTC, datetime
from decimal import Decimal
from importlib.metadata import version
from pathlib import Path
from time import perf_counter

ROUND = Path(__file__).resolve().parent
ROOT = ROUND.parents[6]
sys.path[:0] = [str(ROOT / path) for path in ("apps/core-api", "packages", "scripts")]

from ai.agent import _incident_payload, finops_prompt_fingerprint
from ai.evaluation import (
    CaseRun,
    FactCheckResult,
    ReadbackResult,
    build_column_report,
    check_readback,
    check_summary_facts,
    observation_cites_input,
)
from ai.evaluation.savings.isolation import price_only_prompt_fingerprint
from ai.evaluation.savings.rate_only import (
    RATE_ONLY_PROMPT_VERSION,
    rate_only_prompt_fingerprint,
)
from ai.evaluation.savings.scoring import score_report
from ai.evaluation.savings.three_call import (
    run_three_call,
    three_call_prompt_fingerprint,
)
from ai.model_client import AIModelError
from ai.openai_client import OpenAIModelClient
from dotenv import dotenv_values
from finops_eval import _fixed_set, load_cases
from openai import OpenAI
from schemas.agents import AgentGraphOutput

CASE_IDS = ("A1", "A7", "A11", "A12", "A14", "A16")
SCHEDULE = [(case_id, repeat) for repeat in range(1, 11)
            for case_id in (CASE_IDS if repeat % 2 else tuple(reversed(CASE_IDS)))]
STAGES = {"EvidenceSummaryOutput": "summary", "CandidateProposalOutput": "recommendation",
          "ProposedHourlyRates": "savings"}
TOKEN_KEYS = ("prompt_tokens", "completion_tokens", "cached_prompt_tokens")
CALL_BUDGET = 180
PILOT_RUNS = 18


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
    actual = (finops_prompt_fingerprint(), price_only_prompt_fingerprint(),
              three_call_prompt_fingerprint(), rate_only_prompt_fingerprint(),
              three_call_prompt_fingerprint(price_mode="rates"))
    expected = tuple(frozen[key] for key in (
        "runtime_prompt_sha256", "price_only_prompt_sha256", "three_call_prompt_sha256",
        "rate_only_prompt_sha256", "rates_three_call_prompt_sha256"))
    if actual != expected or RATE_ONLY_PROMPT_VERSION != "v0.9.0":
        raise ValueError("PROMPT_DRIFT")
    if (Counter(case_id for case_id, _ in SCHEDULE) != dict.fromkeys(CASE_IDS, 10)
            or len(set(SCHEDULE)) != 60):
        raise ValueError("SCHEDULE_DRIFT")
    return frozen, spec, {case.case_id: case for case in cases}


def usage_total(calls):
    return {key: sum((call.get("usage") or {}).get(key, 0) for call in calls)
            for key in TOKEN_KEYS}


class PersistingClient:
    def __init__(self, inner, metadata):
        self.inner = inner
        self.metadata = metadata
        self.case_id = None
        self.repeat = None

    def complete(self, request, response_model):
        calls = self.metadata["calls"]
        if len(calls) >= min(CALL_BUDGET, self.metadata["stage_call_limit"]) or response_model.__name__ not in STAGES:
            raise ValueError("CALL_BUDGET_OR_STAGE")
        item = {"number": len(calls) + 1, "case_id": self.case_id, "repeat": self.repeat,
                "stage": STAGES[response_model.__name__], "status": "STARTED"}
        calls.append(item)
        write_json(ROUND / "metadata.json", self.metadata)
        started = perf_counter()
        usage = None
        try:
            response = self.inner.complete(request, response_model)
        except AIModelError as exc:
            usage = exc.usage
            item.update(status="ERROR", error=type(exc).__name__, error_phase=exc.phase)
            raise
        except Exception as exc:
            item.update(status="UNEXPECTED_ERROR", error=type(exc).__name__, error_phase="runner")
            raise
        else:
            usage = response.usage
            item.update(status="RETURNED", model=response.model)
            return response
        finally:
            item["elapsed_seconds"] = round(perf_counter() - started, 3)
            item["usage"] = None if usage is None else {
                key: getattr(usage, key) for key in TOKEN_KEYS}
            write_json(ROUND / "metadata.json", self.metadata)


def save_results(raw, spec):
    raw_path = ROUND / "raw.json"
    write_json(raw_path, raw)
    scored = score_report(raw, spec)
    scored.update(source_file="raw.json", source_sha256=sha(raw_path),
                  spec_sha256=sha(ROUND.parent.parent / "spec.json"))
    write_json(ROUND / "scored.json", scored)
    observed = []
    for record in raw["runs"]:
        errors = [call for call in record["calls"] if "error_phase" in call]
        first_error = errors[0] if errors else {}
        observed.append(CaseRun(
            case_id=record["case_id"],
            output=AgentGraphOutput.model_validate({
                key: record[key] for key in AgentGraphOutput.model_fields}),
            **{key: record[key] for key in TOKEN_KEYS},
            fact=FactCheckResult(**record["fact"]),
            readback=ReadbackResult(**record["readback"]),
            observation_cites_input=record["observation_cites_input"],
            error=first_error.get("error"), error_phase=first_error.get("error_phase"),
        ))
    summary = build_column_report(raw["label"], observed)
    write_json(ROUND / "flow-scored.json", {
        **asdict(summary), "runs": summary.runs,
        "failed_contract": summary.failed_contract,
        "field_stability": summary.field_stability,
        "unstable_field_names": summary.unstable_field_names,
        "observed_checks_passed": (
            summary.runs > 0 and summary.failed == 0 and summary.no_proposal == 0
            and summary.fact_checked == summary.runs and summary.fact_failed == 0
            and summary.field_slots > 0 and summary.stable_slots == summary.field_slots
            and summary.stability_cases == 6
        ),
        "automatic_checks_passed": (
            scored["complete"] and summary.runs == 60 and summary.failed == 0
            and summary.no_proposal == 0 and summary.fact_checked == 60
            and summary.fact_failed == 0 and summary.field_slots > 0
            and summary.stable_slots == summary.field_slots and summary.stability_cases == 6
        ),
        "source_sha256": sha(raw_path),
        "summary_restoration_evaluated": False,
        "public_explanation_semantics_reviewed": False,
        "service_baseline_evaluated": False,
        "known_prior_overestimate": "../20260915-v0.7.0-three-call-2/raw.json A1 repeat 1",
    })
    return scored


def validate_saved_runs(raw, metadata):
    identities = [(r["case_id"], r["repeat"]) for r in raw["runs"]]
    if identities != SCHEDULE[:len(identities)]:
        raise ValueError("SAVED_SCHEDULE_DRIFT")
    nested = [(r["case_id"], r["repeat"], c["stage"], c["status"], c.get("usage"))
              for r in raw["runs"] for c in r["calls"]]
    outer = [(c["case_id"], c["repeat"], c["stage"], c["status"], c.get("usage"))
             for c in metadata["calls"]]
    if nested != outer or len(outer) > CALL_BUDGET:
        raise ValueError("CALL_LEDGER_DRIFT")
    if [c["number"] for c in metadata["calls"]] != list(range(1, len(outer) + 1)):
        raise ValueError("CALL_NUMBER_DRIFT")


def pilot_eligible(raw, scored, spec):
    flow = json.loads((ROUND / "flow-scored.json").read_text("utf-8"))
    maximum_unavailable = int((1 - Decimal(spec["criteria"]["minimum_estimated_fraction"])) * len(SCHEDULE))
    return (
        len(raw["runs"]) == PILOT_RUNS
        and all(r["status"] in {"PASS", "UNAVAILABLE"} for r in scored["results"])
        and scored["status_counts"].get("UNAVAILABLE", 0) <= maximum_unavailable
        and flow["observed_checks_passed"]
        and all(c["status"] == "RETURNED" for r in raw["runs"] for c in r["calls"])
    )


def execute(frozen, spec, cases, stage):
    if stage == "pilot":
        if any((ROUND / name).exists() for name in ("metadata.json", "raw.json", "scored.json")):
            raise ValueError("ROUND_ALREADY_STARTED")
        metadata = {
            "scope": "User approved steps 1-3 and <=200 calls. This frozen candidate uses at most 180 calls, 54 then 126, no retries.",
            "trial_version": "v0.9.0", "execution_mode": "three_call_rate_only_candidate",
            "started_at": datetime.now(UTC).isoformat(), "status": "RUNNING",
            "model": "gpt-5.6-luna", "reasoning_effort": "low", "temperature": None,
            "timeout_seconds": 30, "max_attempts": 1, "sdk_max_retries": 0,
            "user_call_ceiling": 200, "call_budget": CALL_BUDGET,
            "credential_source": "main checkout .env; explicit prior user approval",
            "schedule": [{"case_id": c, "repeat": r} for c, r in SCHEDULE],
            "calls": [], "stages": [], "fixed_set": frozen["fixed_set"],
            "spec_sha256": frozen["spec_sha256"], "preflight_sha256": sha(ROUND / "preflight.json"),
            "runner_sha256": sha(Path(__file__)), "implementation_sha256": frozen["implementation_sha256"],
            "prompt_sha256": frozen["rates_three_call_prompt_sha256"],
            "git_base_head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
            "package_versions": {name: version(name) for name in ("openai", "pydantic", "langgraph")},
            "price_per_million_usd": {"input": "0.20", "cached_input": "0.02", "output": "1.20"},
            "pricing_source": "https://developers.openai.com/api/docs/models/gpt-5.6-luna",
            "stopping_rule": "Complete pilot diagnostics; expand only the identical eligible candidate. Abort request/transport/runner errors. No retries, replacement runs, or unused-call reallocation.",
        }
        raw = {"label": "gpt-5.6-luna/low v0.9.0 three-call rate-only",
               "execution_mode": "three_call_rate_only_candidate", "prompt_version": "v0.9.0",
               "prompt_sha256": frozen["rates_three_call_prompt_sha256"], "model_snapshots": [],
               "repeats": 10, "case_ids": list(CASE_IDS), "fixed_set": frozen["fixed_set"], "runs": []}
        schedule = SCHEDULE[:PILOT_RUNS]
        call_limit = 54
    else:
        snapshot = json.loads((ROUND / "pilot-snapshot.json").read_text("utf-8"))
        if any(sha(ROUND / name) != expected for name, expected in snapshot.items()):
            raise ValueError("PILOT_RECORD_DRIFT_OR_EXPANSION_STARTED")
        metadata = json.loads((ROUND / "metadata.json").read_text("utf-8"))
        raw = json.loads((ROUND / "raw.json").read_text("utf-8"))
        validate_saved_runs(raw, metadata)
        if metadata["status"] != "PILOT_ELIGIBLE" or not pilot_eligible(raw, score_report(raw, spec), spec):
            raise ValueError("PILOT_NOT_ELIGIBLE")
        if metadata["preflight_sha256"] != sha(ROUND / "preflight.json"):
            raise ValueError("PREFLIGHT_RECORD_DRIFT")
        schedule = SCHEDULE[PILOT_RUNS:]
        call_limit = CALL_BUDGET
    credential_file = ROOT.parent.parent / ".env"
    api_key = dotenv_values(credential_file).get("OPENAI_API_KEY")
    if not api_key:
        raise ValueError("MISSING_API_KEY")
    metadata.update(status="RUNNING", stage_call_limit=call_limit)
    stage_record = {"stage": stage, "started_at": datetime.now(UTC).isoformat(),
                    "call_start": len(metadata["calls"]), "call_limit": call_limit}
    metadata["stages"].append(stage_record)
    started = perf_counter()
    exit_code = 0
    write_json(ROUND / "metadata.json", metadata)
    save_results(raw, spec)
    try:
        with OpenAI(api_key=api_key, base_url="https://api.openai.com/v1", timeout=30, max_retries=0) as sdk:
            client = PersistingClient(OpenAIModelClient(
                client=sdk, model="gpt-5.6-luna", timeout_seconds=30, max_attempts=1,
                retry_backoff_seconds=0, temperature=None, reasoning_effort="low"), metadata)
            for case_id, repeat in schedule:
                client.case_id, client.repeat = case_id, repeat
                case = cases[case_id]
                result = run_three_call(case.graph_input, client=client, price_mode="rates")
                record = result.record(case_id=case_id, repeat=repeat)
                record.update(usage_total(result.calls))
                record["execution_mode"] = "three_call_rate_only_candidate"
                payload = _incident_payload(case.graph_input)
                record["fact"] = asdict(check_summary_facts(payload, result.output.summary_lines))
                record["readback"] = asdict(check_readback(payload, result.output))
                record["observation_cites_input"] = observation_cites_input(payload, result.output.summary_lines)
                raw["runs"].append(record)
                raw["model_snapshots"] = sorted({c["model"] for c in metadata["calls"] if "model" in c})
                scored = save_results(raw, spec)
                grade = scored["results"][-1]
                print(json.dumps({"stage": stage, "execution": len(raw["runs"]), "case_id": case_id,
                                  "repeat": repeat, "calls": len(metadata["calls"]),
                                  "grade": grade["status"], "amount": grade.get("amount"),
                                  "diagnostics": result.savings_diagnostics,
                                  "elapsed_seconds": result.elapsed_seconds}, ensure_ascii=False), flush=True)
                if any(c.get("error_phase") in {"request", "transport"} for c in result.calls):
                    metadata["status"] = "ABORTED_MODEL_ERROR"
                    exit_code = 2
                    break
            else:
                validate_saved_runs(raw, metadata)
                if stage == "pilot":
                    metadata["status"] = "PILOT_ELIGIBLE" if pilot_eligible(raw, scored, spec) else "PILOT_REJECTED"
                else:
                    metadata["status"] = "COMPLETED_PLANNED_EXECUTIONS"
    except Exception as exc:  # noqa: BLE001 -- terminal boundary stores no raw messages.
        metadata.update(status="ABORTED_UNEXPECTED_ERROR", runner_error=type(exc).__name__)
        exit_code = 2
    finally:
        stage_record.update(ended_at=datetime.now(UTC).isoformat(),
                            elapsed_seconds=round(perf_counter() - started, 3),
                            call_end=len(metadata["calls"]), status=metadata["status"])
        metadata["elapsed_seconds"] = round(sum(s["elapsed_seconds"] for s in metadata["stages"]), 3)
        metadata["ended_at"] = stage_record["ended_at"]
        metadata["attempted_calls"] = len(metadata["calls"])
        metadata["unused_call_budget"] = CALL_BUDGET - len(metadata["calls"])
        metadata["completed_executions"] = len(raw["runs"])
        metadata["usage"] = usage_total(metadata["calls"])
        metadata["calls_without_usage"] = sum(c.get("usage") is None for c in metadata["calls"])
        usage = metadata["usage"]
        gross = usage["prompt_tokens"] * Decimal("0.20") + usage["completion_tokens"] * Decimal("1.20")
        metadata["cost_estimate_without_cache_discount_usd"] = str(gross / 1_000_000)
        metadata["cost_estimate_with_reported_cache_usd"] = str((gross - usage["cached_prompt_tokens"] * Decimal("0.18")) / 1_000_000)
        metadata["cost_note"] = "Available response usage times public rates; not billed cost. Cache writes or usage not reported cannot be reconciled here."
        metadata["service_baseline_evaluated"] = False
        metadata["exit_code"] = exit_code
        save_results(raw, spec)
        write_json(ROUND / "metadata.json", metadata)
        if stage == "pilot":
            pilot_dir = ROUND / "pilot"
            pilot_dir.mkdir(exist_ok=False)
            names = ("raw.json", "metadata.json", "scored.json", "flow-scored.json")
            for name in names:
                (pilot_dir / name).write_bytes((ROUND / name).read_bytes())
            write_json(ROUND / "pilot-snapshot.json", {name: sha(ROUND / name) for name in names})
    print(json.dumps({key: metadata[key] for key in (
        "status", "attempted_calls", "completed_executions", "elapsed_seconds", "usage",
        "cost_estimate_without_cache_discount_usd", "calls_without_usage", "exit_code")}), flush=True)
    return exit_code


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    logging.disable(logging.CRITICAL)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute-approved-stage", choices=("pilot", "expansion"))
    args = parser.parse_args()
    try:
        prepared = preflight()
        if args.execute_approved_stage:
            code = execute(*prepared, args.execute_approved_stage)
        else:
            print("PREFLIGHT_OK live_calls=0 columns=1 pilot_maximum_calls=54 total_maximum_calls=180")
            code = 0
    except Exception as exc:  # noqa: BLE001 -- class only, never provider payload or credentials.
        print(json.dumps({"status": "STOPPED", "error_class": type(exc).__name__}), flush=True)
        code = 2
    raise SystemExit(code)
