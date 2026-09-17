"""승인된 v0.7.0 한 라운드만 실행한다. 기본 동작은 네트워크 없는 사전 확인이다."""

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
    check_readback,
    check_summary_facts,
    observation_cites_input,
)
from ai.evaluation.savings.isolation import price_only_prompt_fingerprint
from ai.evaluation.savings.scoring import score_report
from ai.evaluation.savings.three_call import (
    THREE_CALL_PROMPT_VERSION,
    run_three_call,
    three_call_prompt_fingerprint,
)
from ai.model_client import AIModelError
from ai.openai_client import OpenAIModelClient
from dotenv import dotenv_values
from finops_eval import _fixed_set, load_cases
from openai import OpenAI

SCHEDULE = [("A1", 1), ("A11", 1), ("A11", 2), ("A1", 2), ("A1", 3), ("A11", 3)]
STAGES = {"EvidenceSummaryOutput": "summary", "CandidateProposalOutput": "recommendation",
          "ProposedSavingsEstimate": "savings"}
TOKEN_KEYS = ("prompt_tokens", "completion_tokens", "cached_prompt_tokens")
CALL_BUDGET = 18


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
              three_call_prompt_fingerprint())
    expected = tuple(frozen[key] for key in (
        "runtime_prompt_sha256", "price_only_prompt_sha256", "three_call_prompt_sha256"))
    if actual != expected or THREE_CALL_PROMPT_VERSION != "v0.7.0":
        raise ValueError("PROMPT_DRIFT")
    if Counter(case_id for case_id, _ in SCHEDULE) != {"A1": 3, "A11": 3}:
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
        if len(calls) >= CALL_BUDGET or response_model.__name__ not in STAGES:
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
    return scored


def execute(frozen, spec, cases):
    # 기존 실행·중단 기록이 있으면 재실행하지 않는다. 미사용 호출도 재배정하지 않는다.
    if any((ROUND / name).exists() for name in ("metadata.json", "raw.json", "scored.json")):
        raise ValueError("ROUND_ALREADY_STARTED")
    credential_file = ROOT.parent.parent / ".env"
    api_key = dotenv_values(credential_file).get("OPENAI_API_KEY")
    if not api_key:
        raise ValueError("MISSING_API_KEY")
    metadata = {
        "scope": "User approved v0.7.0: Luna/low, A1 and A11 three times, at most 18 calls, no retries.",
        "trial_version": "v0.7.0", "execution_mode": "three_call_diagnostic",
        "started_at": datetime.now(UTC).isoformat(), "status": "RUNNING",
        "model": "gpt-5.6-luna", "reasoning_effort": "low", "temperature": None,
        "timeout_seconds": 30, "max_attempts": 1, "sdk_max_retries": 0,
        "call_budget": CALL_BUDGET,
        "credential_source": "main checkout .env; explicit user approval",
        "credential_preflight": "GET model metadata succeeded; no generation",
        "schedule": [{"case_id": case_id, "repeat": repeat} for case_id, repeat in SCHEDULE],
        "calls": [], "fixed_set": frozen["fixed_set"], "spec_sha256": frozen["spec_sha256"],
        "preflight_sha256": sha(ROUND / "preflight.json"), "runner_sha256": sha(Path(__file__)),
        "implementation_sha256": frozen["implementation_sha256"],
        "runtime_prompt_sha256": frozen["runtime_prompt_sha256"],
        "price_only_prompt_sha256": frozen["price_only_prompt_sha256"],
        "three_call_prompt_sha256": frozen["three_call_prompt_sha256"],
        "git_base_head": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "package_versions": {name: version(name) for name in ("openai", "pydantic", "langgraph")},
        "price_per_million_usd": {"input": "0.20", "cached_input": "0.02", "output": "1.20"},
        "pricing_source": "https://developers.openai.com/api/docs/models/gpt-5.6-luna",
        "stopping_rule": "Continue six planned pipelines through quality failures; abort on request, transport or unexpected runner error. Response parse failure skips dependent stages. No call reallocation.",
    }
    raw = {"label": "gpt-5.6-luna/low v0.7.0 three-call",
           "execution_mode": "three_call_diagnostic", "prompt_version": "v0.7.0",
           "prompt_sha256": frozen["three_call_prompt_sha256"], "model_snapshots": [],
           "repeats": 3, "case_ids": ["A1", "A11"], "fixed_set": frozen["fixed_set"], "runs": []}
    started = perf_counter()
    exit_code = 0
    write_json(ROUND / "metadata.json", metadata)
    save_results(raw, spec)
    try:
        with OpenAI(api_key=api_key, base_url="https://api.openai.com/v1",
                    timeout=30, max_retries=0) as sdk:
            client = PersistingClient(OpenAIModelClient(
                client=sdk, model="gpt-5.6-luna", timeout_seconds=30, max_attempts=1,
                retry_backoff_seconds=0, temperature=None, reasoning_effort="low"), metadata)
            for case_id, repeat in SCHEDULE:
                client.case_id, client.repeat = case_id, repeat
                case = cases[case_id]
                result = run_three_call(case.graph_input, client=client)
                record = result.record(case_id=case_id, repeat=repeat)
                record.update(usage_total(result.calls))
                payload = _incident_payload(case.graph_input)
                record["fact"] = asdict(check_summary_facts(payload, result.output.summary_lines))
                record["readback"] = asdict(check_readback(payload, result.output))
                record["observation_cites_input"] = observation_cites_input(
                    payload, result.output.summary_lines)
                raw["runs"].append(record)
                raw["model_snapshots"] = sorted({call["model"] for call in metadata["calls"]
                                                  if "model" in call})
                scored = save_results(raw, spec)
                grade = scored["results"][-1]
                print(json.dumps({"execution": len(raw["runs"]), "case_id": case_id,
                                  "repeat": repeat, "calls": len(metadata["calls"]),
                                  "grade": grade["status"], "amount": grade.get("amount"),
                                  "diagnostics": result.savings_diagnostics,
                                  "elapsed_seconds": result.elapsed_seconds},
                                 ensure_ascii=False), flush=True)
                if any(call.get("error_phase") in {"request", "transport"} for call in result.calls):
                    metadata["status"] = "ABORTED_MODEL_ERROR"
                    exit_code = 2
                    break
            else:
                metadata["status"] = "COMPLETED_PLANNED_EXECUTIONS"
    except Exception as exc:  # noqa: BLE001 — 마지막 실행 경계에서 메시지 없이 중단 상태만 보존한다.
        metadata.update(status="ABORTED_UNEXPECTED_ERROR", runner_error=type(exc).__name__)
        exit_code = 2
    finally:
        metadata["elapsed_seconds"] = round(perf_counter() - started, 3)
        metadata["ended_at"] = datetime.now(UTC).isoformat()
        metadata["attempted_calls"] = len(metadata["calls"])
        metadata["unused_call_budget"] = CALL_BUDGET - len(metadata["calls"])
        metadata["completed_executions"] = len(raw["runs"])
        metadata["usage"] = usage_total(metadata["calls"])
        metadata["calls_without_usage"] = sum(call.get("usage") is None for call in metadata["calls"])
        usage = metadata["usage"]
        metadata["cost_estimate_without_cache_discount_usd"] = str((
            usage["prompt_tokens"] * Decimal("0.20") + usage["completion_tokens"] * Decimal("1.20")
        ) / 1_000_000)
        metadata["cost_note"] = "Available response usage times public rates, without cache discount; not billed cost."
        metadata["service_baseline_evaluated"] = False
        metadata["exit_code"] = exit_code
        save_results(raw, spec)
        write_json(ROUND / "metadata.json", metadata)
    print(json.dumps({key: metadata[key] for key in (
        "status", "attempted_calls", "completed_executions", "elapsed_seconds", "usage",
        "cost_estimate_without_cache_discount_usd", "calls_without_usage", "exit_code")}), flush=True)
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
            print("PREFLIGHT_OK live_calls=0 pipelines=6 maximum_calls=18")
            code = 0
    except Exception as exc:  # noqa: BLE001 — 원본 예외 대신 클래스만 출력하고 실패 종료한다.
        print(json.dumps({"status": "STOPPED", "error_class": type(exc).__name__}), flush=True)
        code = 2
    raise SystemExit(code)
