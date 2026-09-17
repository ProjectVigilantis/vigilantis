"""기존 두 단가 프롬프트의 60호출 진단. 기본 실행은 오프라인 사전 확인이다."""

import argparse
import hashlib
import json
import logging
import sys
from collections import Counter
from contextlib import contextmanager
from dataclasses import asdict
from datetime import UTC, datetime
from decimal import Decimal
from importlib.metadata import version
from pathlib import Path
from time import perf_counter

ROUND = Path(__file__).resolve().parent
ROOT = ROUND.parents[6]
sys.path[:0] = [str(ROOT / p) for p in ("apps/core-api", "packages", "scripts")]

from ai.evaluation.savings.isolation import score_price_only_report
from ai.evaluation.savings.rate_only import (
    RATE_ONLY_PROMPT_VERSION,
    rate_only_prompt_fingerprint,
    rate_only_request,
)
from ai.model_client import AIModelError, build_outbound_payload
from ai.savings import (
    ProposedHourlyRates,
    accept_hourly_rates,
    savings_prompt_fingerprint,
    savings_request,
)
from finops_eval import _fixed_set, load_cases

CASE_IDS = ("A1", "A7", "A11")
COLUMNS = ("v0.9.0", "v0.9.1")
REPEATS = 10
MAX_CALLS = 60
SPEC_PATH = ROUND.parent.parent / "spec.json"
REQUESTS = {"v0.9.0": rate_only_request, "v0.9.1": savings_request}
TOKEN_KEYS = ("prompt_tokens", "completion_tokens", "cached_prompt_tokens")
MODEL = "gpt-5.6-luna"
SCHEDULE = [
    {"number": number, "case_id": case_id, "repeat": repeat, "column": column}
    for number, (case_id, repeat, column) in enumerate(
        (
            (case_id, repeat, column)
            for repeat in range(1, REPEATS + 1)
            for case_id in (CASE_IDS if repeat % 2 else CASE_IDS[::-1])
            for column in (COLUMNS if repeat % 2 else COLUMNS[::-1])
        ), 1,
    )
]
EXPECTED_SUFFIX = (
    "\n공개 근거의 필드명과 상태 코드는 한국어로 풀어 쓴다. 예를 들어 "
    "pricing_source=MODEL_KNOWLEDGE는 모델 지식 기반 추정이라고 표현한다."
)


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def digest(value):
    data = json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def read(path):
    return json.loads(path.read_text("utf-8"))


def write(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", "utf-8")
    temporary.replace(path)


def current_preflight():
    cases = load_cases()
    fixed = _fixed_set(cases)
    spec = read(SPEC_PATH)
    if fixed != spec["fixed_set"] or RATE_ONLY_PROMPT_VERSION != COLUMNS[0]:
        raise ValueError("INPUT_OR_VERSION_DRIFT")
    assets = {c.case_id: c.graph_input.asset_context for c in cases if c.case_id in CASE_IDS}
    if set(assets) != set(CASE_IDS):
        raise ValueError("MISSING_CASE")
    requests = {}
    for case_id, asset in assets.items():
        pair = [build_outbound_payload(REQUESTS[column](asset)) for column in COLUMNS]
        if pair[0]["user_payload"] != pair[1]["user_payload"]:
            raise ValueError("PAYLOAD_DIFFERENCE")
        if pair[1]["system_prompt"] != pair[0]["system_prompt"] + EXPECTED_SUFFIX:
            raise ValueError("UNPLANNED_PROMPT_DIFFERENCE")
        requests[case_id] = {
            "user_payload_sha256": digest(pair[0]["user_payload"]),
            "outbound_sha256": {column: digest(p) for column, p in zip(COLUMNS, pair, strict=True)},
        }
    sources = [
        "apps/core-api/ai/savings.py", "apps/core-api/ai/model_client.py",
        "apps/core-api/ai/openai_client.py", "apps/core-api/ai/evaluation/cases.py",
        "apps/core-api/ai/evaluation/savings/rate_only.py",
        "apps/core-api/ai/evaluation/savings/isolation.py",
        "apps/core-api/ai/evaluation/savings/scoring.py",
        "packages/schemas/savings.py", "packages/schemas/agents.py",
        "packages/schemas/rightsizing_policy.py", "scripts/finops_eval.py",
        "uv.lock", str(Path(__file__).relative_to(ROOT)).replace("\\", "/"),
    ]
    return {
        "round": ROUND.name, "execution_mode": "rate_prompt_comparison_diagnostic",
        "columns": list(COLUMNS), "case_ids": list(CASE_IDS), "repeats": REPEATS,
        "maximum_calls": MAX_CALLS, "schedule": SCHEDULE,
        "model": MODEL, "reasoning_effort": "low", "temperature": None,
        "sdk_max_retries": 0, "max_attempts": 1, "timeout_seconds": 30,
        "fixed_set": fixed, "requests": requests,
        "prompt_sha256": {
            "v0.9.0": rate_only_prompt_fingerprint(),
            "v0.9.1": savings_prompt_fingerprint(),
        },
        "output_schema_sha256": digest(ProposedHourlyRates.model_json_schema()),
        "spec_sha256": sha(SPEC_PATH),
        "source_sha256": {p: sha(ROOT / p) for p in sources},
        "plan_sha256": sha(ROUND / "plan.md"),
        "package_versions": {p: version(p) for p in ("openai", "pydantic", "langgraph")},
        "price_per_million_usd": {"input": "0.20", "cached_input": "0.02", "output": "1.20"},
        "pricing_source": "https://developers.openai.com/api/docs/models/gpt-5.6-luna",
        "pricing_checked_at": "2026-09-16",
        "service_baseline_evaluated": False,
    }, assets


def validate_preflight(frozen):
    if read(ROUND / "preflight.json") != frozen:
        raise ValueError("PREFLIGHT_DRIFT")


def validate_approval(frozen):
    validate_preflight(frozen)
    approval = read(ROUND / "approval.json")
    expected = {
        "approved": True, "maximum_calls": MAX_CALLS,
        "preflight_sha256": sha(ROUND / "preflight.json"),
    }
    if any(approval.get(k) != v for k, v in expected.items()):
        raise ValueError("ROUND_NOT_APPROVED")


def analyze(metadata, spec):
    calls = metadata["calls"]
    if len(calls) > MAX_CALLS or any(
        {key: call[key] for key in expected} != expected
        for call, expected in zip(calls, SCHEDULE, strict=False)
    ):
        raise ValueError("SAVED_SCHEDULE_DRIFT")
    columns = {}
    for column in COLUMNS:
        selected = [c for c in calls if c["column"] == column]
        raw = {
            "fixed_set": metadata["fixed_set"], "case_ids": list(CASE_IDS), "repeats": REPEATS,
            "prompt_version": column, "prompt_sha256": metadata["prompt_sha256"][column],
            "model_snapshots": sorted({c["model"] for c in selected if c.get("model")}),
            "runs": [
                {"case_id": c["case_id"], "repeat": c["repeat"],
                 "response_status": c["status"], "ai_savings_estimate": c.get("estimate")}
                for c in selected
            ],
        }
        scored = score_price_only_report(raw, spec)
        grades = scored["results"]
        priced = [g for g in grades if "signed_relative_error" in g]
        scored.update(
            planned_runs=30, pending_runs=30 - len(selected),
            estimated_fraction_of_planned=str(Decimal(scored["estimated"]) / 30),
            over_limit_count=sum(g["status"] == "PRICE_DEVIATION"
                                 and g.get("savings_direction") == "OVERSTATED" for g in grades),
            under_limit_count=sum(g["status"] == "PRICE_DEVIATION"
                                  and g.get("savings_direction") == "UNDERSTATED" for g in grades),
            maximum_overstatement_fraction=str(max(
                [Decimal(0)] + [Decimal(g["signed_relative_error"]) for g in priced],
            )) if priced else None,
            rate_diagnostic_outside_count=sum(
                not g["rate_accuracy"]["within_symmetric_tolerance"] for g in priced
            ),
            per_case_status_counts={
                cid: dict(Counter(g["status"] for g in grades if g["case_id"] == cid))
                for cid in CASE_IDS
            },
        )
        columns[column] = scored
    usage = {k: sum((c.get("usage") or {}).get(k, 0) for c in calls) for k in TOKEN_KEYS}
    gross = (Decimal(usage["prompt_tokens"]) * Decimal("0.20")
             + Decimal(usage["completion_tokens"]) * Decimal("1.20")) / 1_000_000
    return {
        "execution_status": metadata["status"], "attempted_calls": len(calls),
        "unused_calls": MAX_CALLS - len(calls), "usage": usage,
        "calls_without_usage": sum(c.get("usage") is None for c in calls),
        "cost_without_cache_discount_usd": str(gross),
        "cost_with_reported_cache_usd": str(
            gross - Decimal(usage["cached_prompt_tokens"]) * Decimal("0.18") / 1_000_000
        ),
        "cost_note": "usage와 공개 단가의 추정. 미보고 usage·캐시 쓰기 비용·실제 청구는 미확인.",
        "comparison_complete": (
            metadata["status"] == "COMPLETED" and len(calls) == MAX_CALLS
            and all(c["status"] == "RETURNED" and "estimate" in c for c in calls)
        ),
        "columns": columns, "service_baseline_evaluated": False,
    }


def persist(metadata, spec):
    write(ROUND / "metadata.json", metadata)
    result = analyze(metadata, spec)
    result["metadata_sha256"] = sha(ROUND / "metadata.json")
    result["preflight_sha256"] = metadata["preflight_sha256"]
    write(ROUND / "analysis.json", result)
    return result


@contextmanager
def live_client():
    # 승인·중복 실행 확인 뒤에만 키를 읽고 SDK를 만든다.
    from ai.openai_client import OpenAIModelClient
    from dotenv import dotenv_values
    from openai import OpenAI

    key = dotenv_values(ROOT.parent.parent / ".env").get("OPENAI_API_KEY")
    if not key:
        raise ValueError("MISSING_KEY")
    with OpenAI(api_key=key, base_url="https://api.openai.com/v1", timeout=30, max_retries=0) as sdk:
        yield OpenAIModelClient(
            client=sdk, model=MODEL, timeout_seconds=30, max_attempts=1,
            retry_backoff_seconds=0, temperature=None, reasoning_effort="low",
        )


def execute(frozen, assets):
    validate_approval(frozen)
    if any((ROUND / name).exists() for name in ("metadata.json", "analysis.json", "started.json")):
        raise ValueError("ROUND_ALREADY_STARTED")
    with (ROUND / "started.json").open("x", encoding="utf-8") as file:
        json.dump({"started_at": datetime.now(UTC).isoformat(),
                   "preflight_sha256": sha(ROUND / "preflight.json")}, file)
    metadata = {**frozen, "preflight_sha256": sha(ROUND / "preflight.json"),
                "started_at": datetime.now(UTC).isoformat(), "status": "RUNNING", "calls": []}
    spec = read(SPEC_PATH)
    started = perf_counter()
    persist(metadata, spec)
    try:
        with live_client() as client:
            for expected in SCHEDULE:
                if len(metadata["calls"]) >= MAX_CALLS:
                    raise ValueError("CALL_CAP_EXCEEDED")
                asset = assets[expected["case_id"]]
                request = REQUESTS[expected["column"]](asset)
                wanted = frozen["requests"][expected["case_id"]]["outbound_sha256"][expected["column"]]
                if digest(build_outbound_payload(request)) != wanted:
                    raise ValueError("OUTBOUND_DRIFT")
                item = {**expected, "status": "STARTED", "started_at": datetime.now(UTC).isoformat()}
                metadata["calls"].append(item)
                persist(metadata, spec)
                call_start = perf_counter()
                try:
                    response = client.complete(request, ProposedHourlyRates)
                except AIModelError as exc:
                    item.update(status="ERROR", error_class=type(exc).__name__, error_phase=exc.phase,
                                usage=asdict(exc.usage) if exc.usage is not None else None)
                    metadata["status"] = "ABORTED_MODEL_ERROR"
                    break
                except Exception as exc:
                    item.update(status="ERROR", error_class=type(exc).__name__, error_phase="runner")
                    raise
                else:
                    item.update(status="RETURNED", model=response.model, usage=asdict(response.usage))
                    diagnostics = []
                    estimate = accept_hourly_rates(response.output, asset, diagnostics=diagnostics)
                    item.update(estimate=estimate.model_dump(mode="json"), diagnostics=diagnostics)
                finally:
                    item["elapsed_seconds"] = round(perf_counter() - call_start, 3)
                    persist(metadata, spec)
                print(f"{item['number']}/{MAX_CALLS} {item['column']} {item['case_id']} "
                      f"repeat={item['repeat']} status={item['estimate']['status']}", flush=True)
            else:
                metadata["status"] = "COMPLETED"
    except Exception as exc:  # noqa: BLE001 -- terminal boundary, class only.
        metadata.update(status="ABORTED_RUNNER_ERROR", error_class=type(exc).__name__)
    finally:
        metadata.update(ended_at=datetime.now(UTC).isoformat(),
                        elapsed_seconds=round(perf_counter() - started, 3))
        result = persist(metadata, spec)
    print(json.dumps({k: result[k] for k in (
        "execution_status", "attempted_calls", "comparison_complete", "cost_without_cache_discount_usd",
    )}), flush=True)
    return 0 if result["comparison_complete"] else 2


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    logging.disable(logging.CRITICAL)
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--freeze", action="store_true")
    mode.add_argument("--execute-approved", action="store_true")
    mode.add_argument("--analyze", action="store_true")
    args = parser.parse_args()
    frozen, assets = current_preflight()
    if args.freeze:
        if any((ROUND / n).exists() for n in ("approval.json", "started.json", "metadata.json")):
            raise ValueError("CANNOT_REFREEZE_APPROVED_ROUND")
        write(ROUND / "preflight.json", frozen)
    elif args.execute_approved:
        return execute(frozen, assets)
    else:
        validate_preflight(frozen)
        if args.analyze:
            metadata = read(ROUND / "metadata.json")
            if metadata["preflight_sha256"] != sha(ROUND / "preflight.json"):
                raise ValueError("SAVED_PREFLIGHT_DRIFT")
            persist(metadata, read(SPEC_PATH))
    print("PREFLIGHT_OK live_calls=0 columns=2 maximum_calls=60")
    return 0


if __name__ == "__main__":
    try:
        code = main()
    except Exception as exc:  # noqa: BLE001 -- no raw credentials or provider payload.
        print(json.dumps({"status": "STOPPED", "error_class": type(exc).__name__}), flush=True)
        code = 2
    raise SystemExit(code)
