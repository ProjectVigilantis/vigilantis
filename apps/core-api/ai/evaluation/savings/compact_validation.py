"""후보 A의 고정 60회 충분성 평가. 기본 실행은 키를 읽지 않는 오프라인 검증이다."""

# 독립 CLI 실행을 위해 아래 저장소 경로 등록 뒤 내부 패키지를 import한다.
# ruff: noqa: E402

import argparse
import hashlib
import json
import logging
import sys
from contextlib import contextmanager
from dataclasses import asdict
from datetime import UTC, datetime
from decimal import Decimal
from importlib.metadata import version
from pathlib import Path
from time import perf_counter

ROOT = Path(__file__).resolve().parents[5]
sys.path[:0] = [str(ROOT / p) for p in ("apps/core-api", "packages", "scripts")]

from ai.evaluation.savings.compact import (
    PROMPT_VERSION,
    SYSTEM_PROMPT,
    ProposedHourlyRates,
    accept_compact_rates,
    compact_request,
)
from ai.evaluation.savings.isolation import score_price_only_report
from ai.evaluation.savings.scoring import score_estimate
from ai.evaluation.summary.cli import _fixed_set, load_cases
from ai.model_client import AIModelContractError, AIModelError, build_outbound_payload
from ai.openai_client import OpenAIModelClient
from ai.savings import (
    ProposedHourlyRates as ProductionRates,
)
from ai.savings import savings_request

ROUND = Path(__file__).parent / "results/20260916-output-simplification-1"
SPEC_PATH = Path(__file__).parent / "spec.json"
DESIGN_PATH = Path(__file__).parent / "plans/20260916-output-simplification.md"
MODEL = "gpt-5.6-luna"
CASE_IDS = ("A1", "A7", "A11", "A12", "A14", "A16")
REPEATS = 10
MAX_CALLS = 60
REMOVED = ("target_arn", "region", "current_instance_type", "target_instance_type")
SCHEDULE = [
    {"number": number, "case_id": case_id, "repeat": repeat}
    for number, (case_id, repeat) in enumerate((
        (case_id, repeat)
        for repeat in range(1, REPEATS + 1)
        for case_id in (CASE_IDS if repeat % 2 else CASE_IDS[::-1])
    ), 1)
]


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def digest(value):
    # 스키마 필드 순서를 포함한다. sort_keys를 사용하지 않는다.
    return hashlib.sha256(json.dumps(value, ensure_ascii=False).encode("utf-8")).hexdigest()


def read(path):
    return json.loads(path.read_text("utf-8"))


def write(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", "utf-8")
    temporary.replace(path)


def make_client(sdk):
    return OpenAIModelClient(
        client=sdk, model=MODEL, reasoning_effort="low", temperature=None,
        timeout_seconds=30, max_attempts=1, retry_backoff_seconds=0,
    )


def capture_wire(asset, *, production=False):
    """설치된 SDK의 실제 요청 직렬화. MockTransport만 사용한다."""
    import httpx
    from openai import OpenAI

    captured = []
    output = {
        "status": "UNAVAILABLE", "explanation": "오프라인 검증용 합성 응답",
        "current_hourly_rate": None, "target_hourly_rate": None,
    }
    if production:
        context = savings_request(asset).user_payload["savings_context"]
        output.update({key: context[key] for key in REMOVED})

    def respond(request):
        captured.append(json.loads(request.content))
        return httpx.Response(200, json={
            "id": "synthetic", "object": "chat.completion", "created": 0, "model": MODEL,
            "choices": [{"index": 0, "finish_reason": "stop", "message": {
                "role": "assistant", "content": json.dumps(output),
            }}],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        })

    with OpenAI(api_key="offline-only", base_url="https://unit.test/v1", max_retries=0,
                http_client=httpx.Client(transport=httpx.MockTransport(respond))) as sdk:
        make_client(sdk).complete(
            savings_request(asset) if production else compact_request(asset),
            ProductionRates if production else ProposedHourlyRates,
        )
    if len(captured) != 1:
        raise ValueError("SDK_REQUEST_COUNT")
    return captured[0]


def current_preflight():
    cases = load_cases()
    fixed = _fixed_set(cases)
    spec = read(SPEC_PATH)
    if fixed != spec["fixed_set"] or list(CASE_IDS) != fixed["case_ids"]:
        raise ValueError("FIXED_INPUT_DRIFT")
    assets = {case.case_id: case.graph_input.asset_context for case in cases}
    schema = ProductionRates.model_json_schema()
    for key in REMOVED:
        del schema["properties"][key]
    schema["required"] = [key for key in schema["required"] if key not in REMOVED]
    if digest(schema) != digest(ProposedHourlyRates.model_json_schema()):
        raise ValueError("UNPLANNED_SCHEMA_DIFFERENCE")
    requests = {}
    for case_id in CASE_IDS:
        asset = assets[case_id]
        before = capture_wire(asset, production=True)
        after = capture_wire(asset)
        expected = json.loads(json.dumps(before))
        expected["messages"][0]["content"] = expected["messages"][0]["content"].replace(
            "목표 타입은 서버가 결정한 값을 복사한다.",
            "목표 타입은 서버가 결정한 값을 사용한다.",
        )
        wire_schema = expected["response_format"]["json_schema"]["schema"]
        for key in REMOVED:
            del wire_schema["properties"][key]
        wire_schema["required"] = [key for key in wire_schema["required"] if key not in REMOVED]
        if digest(after) != digest(expected):
            raise ValueError("UNPLANNED_SDK_REQUEST_DIFFERENCE")
        requests[case_id] = {
            "outbound_sha256": digest(build_outbound_payload(compact_request(asset))),
            "wire_sha256": digest(after),
            "wire_bytes": len(json.dumps(after, ensure_ascii=False).encode("utf-8")),
        }
    sources = [
        "apps/core-api/ai/evaluation/savings/compact.py",
        "apps/core-api/ai/evaluation/savings/compact_validation.py",
        "apps/core-api/ai/savings.py", "apps/core-api/ai/model_client.py",
        "apps/core-api/ai/openai_client.py", "apps/core-api/ai/evaluation/cases.py",
        "apps/core-api/ai/evaluation/savings/isolation.py",
        "apps/core-api/ai/evaluation/savings/scoring.py",
        "packages/schemas/savings.py", "packages/schemas/agents.py",
        "packages/schemas/rightsizing_policy.py", "scripts/finops_eval.py", "uv.lock",
    ]
    return {
        "round": ROUND.name, "candidate": PROMPT_VERSION, "columns": 1,
        "maximum_calls": MAX_CALLS, "schedule": SCHEDULE, "fixed_set": fixed,
        "model": MODEL, "reasoning_effort": "low", "temperature": None,
        "sdk_max_retries": 0, "max_attempts": 1, "timeout_seconds": 30,
        "requests": requests, "prompt_sha256": digest(SYSTEM_PROMPT),
        "output_schema_sha256": digest(ProposedHourlyRates.model_json_schema()),
        "spec_sha256": sha(SPEC_PATH), "design_sha256": sha(DESIGN_PATH),
        "source_sha256": {p: sha(ROOT / p) for p in sources},
        "package_versions": {p: version(p) for p in ("openai", "pydantic", "langgraph")},
        "price_per_million_usd": {"input": "0.20", "cached_input": "0.02", "output": "1.20"},
        "pricing_source": "https://developers.openai.com/api/docs/models/gpt-5.6-luna",
        "pricing_checked_at": "2026-09-16", "response_context_echo_checked": False,
        "service_baseline_evaluated": False,
        "stop_on": ["FIRST_PRICE_DEVIATION", "FIRST_OUTPUT_CONTRACT_ERROR",
                    "FOURTH_UNAVAILABLE", "FIRST_INFRASTRUCTURE_OR_RUNNER_ERROR"],
    }, assets


def validate_approval(frozen):
    if read(ROUND / "preflight.json") != frozen:
        raise ValueError("PREFLIGHT_DRIFT")
    approval = read(ROUND / "approval.json")
    expected = {
        "approved": True, "round_maximum_calls": MAX_CALLS,
        "user_total_call_limit": 80, "preflight_sha256": sha(ROUND / "preflight.json"),
    }
    if any(approval.get(key) != value for key, value in expected.items()):
        raise ValueError("ROUND_NOT_APPROVED")


def analyze(metadata, spec):
    calls = metadata["calls"]
    if len(calls) > MAX_CALLS or any(
        {key: call[key] for key in expected} != expected
        for call, expected in zip(calls, SCHEDULE, strict=False)
    ):
        raise ValueError("SAVED_SCHEDULE_DRIFT")
    raw = {
        "fixed_set": metadata["fixed_set"], "case_ids": list(CASE_IDS), "repeats": REPEATS,
        "prompt_version": PROMPT_VERSION, "prompt_sha256": metadata["prompt_sha256"],
        "model_snapshots": sorted({c["model"] for c in calls if c.get("model")}),
        "runs": [{"case_id": c["case_id"], "repeat": c["repeat"],
                  "response_status": c["status"], "ai_savings_estimate": c.get("estimate")}
                 for c in calls],
    }
    scored = score_price_only_report(raw, spec)
    usage = {key: sum((c.get("usage") or {}).get(key, 0) for c in calls)
             for key in ("prompt_tokens", "completion_tokens", "cached_prompt_tokens")}
    cost = (Decimal(usage["prompt_tokens"]) * Decimal("0.20")
            + Decimal(usage["completion_tokens"]) * Decimal("1.20")
            - Decimal(usage["cached_prompt_tokens"]) * Decimal("0.18")) / 1_000_000
    return {
        "execution_status": metadata["status"], "attempted_calls": len(calls),
        "returned_calls": sum(c["status"] == "RETURNED" for c in calls),
        "planned_calls": MAX_CALLS, "unexecuted_calls": MAX_CALLS - len(calls),
        "unused_user_call_limit": 80 - len(calls),
        "estimated_fraction_of_planned": str(Decimal(scored["estimated"]) / MAX_CALLS),
        "usage": usage, "calls_without_usage": sum(c.get("usage") is None for c in calls),
        "cost_with_reported_cache_usd": str(cost),
        "cost_note": "usage 기반 추정. 미보고 usage·캐시 쓰기 비용·실제 청구액은 미확인.",
        "passed": metadata["status"] == "COMPLETED" and scored["probe_passed"],
        "response_context_echo_checked": False, "service_baseline_evaluated": False,
        "score": scored,
    }


def persist(metadata, spec):
    write(ROUND / "metadata.json", metadata)
    result = analyze(metadata, spec)
    result.update(metadata_sha256=sha(ROUND / "metadata.json"),
                  preflight_sha256=metadata["preflight_sha256"])
    write(ROUND / "analysis.json", result)
    return result


@contextmanager
def live_client(verify_wire):
    # 승인·재실행 거부 확인 뒤에만 기존에 승인된 기본 체크아웃의 키를 읽는다.
    import httpx
    from dotenv import dotenv_values
    from openai import OpenAI

    key = dotenv_values(ROOT.parent.parent / ".env").get("OPENAI_API_KEY")
    if not key:
        raise ValueError("MISSING_KEY")
    with OpenAI(api_key=key, base_url="https://api.openai.com/v1", timeout=30, max_retries=0,
                http_client=httpx.Client(event_hooks={"request": [verify_wire]})) as sdk:
        yield make_client(sdk)


def execute(frozen, assets, *, client_factory=live_client):
    validate_approval(frozen)
    if any((ROUND / name).exists() for name in ("started.json", "metadata.json", "analysis.json")):
        raise ValueError("ROUND_ALREADY_STARTED")
    with (ROUND / "started.json").open("x", encoding="utf-8") as file:
        json.dump({"started_at": datetime.now(UTC).isoformat()}, file)
    metadata = {**frozen, "preflight_sha256": sha(ROUND / "preflight.json"),
                "started_at": datetime.now(UTC).isoformat(), "status": "RUNNING", "calls": []}
    spec = read(SPEC_PATH)
    started = perf_counter()
    persist(metadata, spec)

    def verify_wire(request):
        item = metadata["calls"][-1]
        wire = digest(json.loads(request.content))
        if item.get("wire_verified") or wire != frozen["requests"][item["case_id"]]["wire_sha256"]:
            raise ValueError("WIRE_REQUEST_DRIFT_OR_DUPLICATE")
        item.update(wire_verified=True, wire_sha256=wire)
        persist(metadata, spec)

    try:
        with client_factory(verify_wire) as client:
            for expected in SCHEDULE:
                asset = assets[expected["case_id"]]
                request = compact_request(asset)
                if digest(build_outbound_payload(request)) != frozen["requests"][expected["case_id"]]["outbound_sha256"]:
                    raise ValueError("OUTBOUND_DRIFT")
                item = {**expected, "status": "STARTED", "started_at": datetime.now(UTC).isoformat()}
                metadata["calls"].append(item)
                persist(metadata, spec)
                call_started = perf_counter()
                try:
                    response = client.complete(request, ProposedHourlyRates)
                    item.update(status="RETURNED", model=response.model, usage=asdict(response.usage))
                    diagnostics = []
                    estimate = accept_compact_rates(response.output, asset, diagnostics=diagnostics)
                    item.update(estimate=estimate.model_dump(mode="json"), diagnostics=diagnostics)
                    grade = score_estimate(item["estimate"], spec["cases"][item["case_id"]],
                                           spec["price_reference"], spec["criteria"])
                    item["grade"] = grade
                    if grade["status"] == "PRICE_DEVIATION":
                        metadata["status"] = "STOPPED_PRICE_DEVIATION"
                    elif grade["status"] not in ("PASS", "UNAVAILABLE"):
                        metadata["status"] = "STOPPED_OUTPUT_CONTRACT_ERROR"
                    elif sum(c.get("grade", {}).get("status") == "UNAVAILABLE"
                             for c in metadata["calls"]) > 3:
                        metadata["status"] = "STOPPED_AVAILABILITY"
                except AIModelContractError as exc:
                    item.update(status="ERROR", error_class=type(exc).__name__, error_phase=exc.phase,
                                usage=asdict(exc.usage) if exc.usage is not None else None)
                    metadata["status"] = "STOPPED_OUTPUT_CONTRACT_ERROR"
                except AIModelError as exc:
                    item.update(status="ERROR", error_class=type(exc).__name__, error_phase=exc.phase,
                                usage=asdict(exc.usage) if exc.usage is not None else None)
                    metadata["status"] = "ABORTED_MODEL_ERROR"
                except Exception as exc:
                    item.update(status="ERROR", error_class=type(exc).__name__, error_phase="runner")
                    raise
                finally:
                    item["elapsed_seconds"] = round(perf_counter() - call_started, 3)
                    persist(metadata, spec)
                print(f"{item['number']}/{MAX_CALLS} {item['case_id']} repeat={item['repeat']} "
                      f"grade={item.get('grade', {}).get('status', item['status'])}", flush=True)
                if metadata["status"] != "RUNNING":
                    break
            else:
                metadata["status"] = "COMPLETED"
    except Exception as exc:  # 공급자 원문·자격증명 대신 오류 종류만 기록한다.
        metadata.update(status="ABORTED_RUNNER_ERROR", error_class=type(exc).__name__)
    finally:
        metadata.update(ended_at=datetime.now(UTC).isoformat(),
                        elapsed_seconds=round(perf_counter() - started, 3))
        result = persist(metadata, spec)
    print(json.dumps({key: result[key] for key in (
        "execution_status", "attempted_calls", "passed", "cost_with_reported_cache_usd",
    )}), flush=True)
    return 0 if result["passed"] else 2


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    logging.disable(logging.CRITICAL)
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--freeze", action="store_true")
    modes.add_argument("--execute-approved", action="store_true")
    modes.add_argument("--analyze", action="store_true")
    args = parser.parse_args()
    frozen, assets = current_preflight()
    if args.freeze:
        if any((ROUND / name).exists() for name in ("approval.json", "started.json", "metadata.json")):
            raise ValueError("CANNOT_REFREEZE_APPROVED_ROUND")
        ROUND.mkdir(parents=True, exist_ok=True)
        write(ROUND / "preflight.json", frozen)
    elif args.execute_approved:
        return execute(frozen, assets)
    else:
        if read(ROUND / "preflight.json") != frozen:
            raise ValueError("PREFLIGHT_DRIFT")
        if args.analyze:
            metadata = read(ROUND / "metadata.json")
            if metadata["preflight_sha256"] != sha(ROUND / "preflight.json"):
                raise ValueError("SAVED_PREFLIGHT_DRIFT")
            persist(metadata, read(SPEC_PATH))
    print("PREFLIGHT_OK live_calls=0 columns=1 maximum_calls=60")
    return 0


if __name__ == "__main__":
    try:
        code = main()
    except Exception as exc:  # 공급자 원문 대신 오류 종류만 출력한다.
        print(json.dumps({"status": "STOPPED", "error_class": type(exc).__name__}), flush=True)
        code = 2
    raise SystemExit(code)
