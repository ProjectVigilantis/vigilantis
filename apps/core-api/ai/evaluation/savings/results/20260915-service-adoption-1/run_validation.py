"""서비스 후보와 승인 v2를 같은 입력에서 검증한다. 승인 파일 없이는 모델을 호출하지 않는다."""

import argparse
import hashlib
import json
import logging
import subprocess
import sys
import types
from dataclasses import asdict
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from time import perf_counter

ROUND = Path(__file__).resolve().parent
ROOT = ROUND.parents[6]
sys.path[:0] = [str(ROOT / p) for p in ("apps/core-api", "packages", "scripts")]

from ai import agent
from ai.evaluation import (
    CaseRun,
    FactCheckResult,
    ReadbackResult,
    build_column_report,
    check_readback,
    check_summary_facts,
    observation_cites_input,
)
from ai.evaluation.judge import judge_fingerprint
from ai.evaluation.savings.scoring import score_report
from ai.model_client import AIModelError
from ai.openai_client import OpenAIModelClient
from dotenv import dotenv_values
from finops_eval import _fixed_set, load_cases
from finops_judge import judge_file, summarize
from openai import OpenAI
from schemas.agents import AgentGraphOutput

BASE_REF = "3e482a4aed9bdae3308d1eabc58676963e1265c2"
BASE_PROMPT = "1e2e5c45cd1b1d250ebf371ba65699827bf3c42f88f6d77080515a07c22e1cc7"
BASE_SOURCE = "822c8347be854594f99dcbf0dc091ee30c6b0e6561d9312a315aa0b44659be20"
LIMITS = {"baseline": 120, "candidate": 180, "judge": 240}
TOKEN_KEYS = ("prompt_tokens", "completion_tokens", "cached_prompt_tokens")
CASE_IDS = ("A1", "A7", "A11", "A12", "A14", "A16")
SCHEDULE = [(cid, repeat) for repeat in range(1, 11)
            for cid in (CASE_IDS if repeat % 2 else tuple(reversed(CASE_IDS)))]


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def read(path):
    return json.loads(path.read_text("utf-8"))


def baseline_module():
    source = subprocess.check_output(
        ["git", "show", f"{BASE_REF}:apps/core-api/ai/agent.py"], cwd=ROOT,
    )
    if hashlib.sha256(source).hexdigest() != BASE_SOURCE:
        raise ValueError("BASELINE_SOURCE_MISMATCH")
    module = types.ModuleType("finops_v2_reference")
    sys.modules[module.__name__] = module
    # 고정 Git 객체와 바이트 지문을 확인한 승인 기준선 소스만 로드한다.
    exec(compile(source, f"git:{BASE_REF}:ai/agent.py", "exec"), module.__dict__)  # noqa: S102
    if module.FINOPS_PROMPT_VERSION != "v2" or module.finops_prompt_fingerprint() != BASE_PROMPT:
        raise ValueError("BASELINE_PROMPT_MISMATCH")
    return module, hashlib.sha256(source).hexdigest()


def current_preflight():
    _, base_source = baseline_module()
    cases = load_cases()
    fixed = _fixed_set(cases)
    specification = read(ROUND.parent.parent / "spec.json")
    if fixed["input_sha256"] != specification["fixed_set"]["input_sha256"]:
        raise ValueError("GOLDEN_INPUT_MISMATCH")
    files = [
        "apps/core-api/ai/agent.py", "apps/core-api/ai/savings.py",
        "apps/core-api/ai/model_client.py", "apps/core-api/ai/openai_client.py",
        "apps/core-api/ai/evaluation/judge.py", "apps/core-api/ai/evaluation/factcheck.py",
        "apps/core-api/ai/evaluation/cases.py", "apps/core-api/ai/evaluation/readback.py",
        "apps/core-api/ai/evaluation/report.py", "apps/core-api/ai/evaluation/reproducibility.py",
        "apps/core-api/ai/evaluation/savings/scoring.py",
        "apps/core-api/ai/evaluation/summary_prompt_snapshot.json",
        "packages/schemas/agents.py", "packages/schemas/savings.py",
        "packages/schemas/rightsizing_policy.py", "scripts/finops_eval.py",
        "scripts/finops_judge.py",
        str(Path(__file__).relative_to(ROOT)),
    ]
    return {
        "candidate_version": agent.FINOPS_PROMPT_VERSION,
        "candidate_prompt_sha256": agent.finops_prompt_fingerprint(),
        "baseline_ref": BASE_REF, "baseline_source_sha256": base_source,
        "baseline_prompt_sha256": BASE_PROMPT,
        "judge_prompt_sha256": judge_fingerprint(),
        "fixed_set": fixed, "spec_sha256": sha(ROUND.parent.parent / "spec.json"),
        "source_sha256": {p.replace("\\", "/"): sha(ROOT / p) for p in files},
        "model": "gpt-5.6-luna", "reasoning_effort": "low", "temperature": None,
        "stage_call_limits": LIMITS, "maximum_calls": sum(LIMITS.values()),
        "sdk_max_retries": 0, "max_attempts": 1,
    }


def usage_total(calls):
    return {key: sum((call.get("usage") or {}).get(key) or 0 for call in calls)
            for key in TOKEN_KEYS}


def refresh_metadata(metadata):
    metadata["attempted_calls"] = len(metadata["calls"])
    metadata["usage"] = usage_total(metadata["calls"])
    u = metadata["usage"]
    gross = (Decimal(u["prompt_tokens"]) * Decimal("0.20")
             + Decimal(u["completion_tokens"]) * Decimal("1.20")) / 1_000_000
    cached = gross - Decimal(u["cached_prompt_tokens"]) * Decimal("0.18") / 1_000_000
    metadata["cost_without_cache_discount_usd"] = str(gross)
    metadata["cost_with_reported_cache_usd"] = str(cached)
    metadata["cost_note"] = "응답 usage와 공식 단가로 계산한 추정이며 실제 청구액은 아니다."
    metadata["calls_without_usage"] = sum(c.get("usage") is None for c in metadata["calls"])
    write(ROUND / "metadata.json", metadata)


class RecordingClient:
    def __init__(self, inner, metadata, stage):
        self.inner, self.metadata, self.stage = inner, metadata, stage
        self.context = {}
        self.failed = False
        self.stage_start = len(metadata["calls"])

    def complete(self, request, response_model):
        calls = self.metadata["calls"]
        if self.failed or len(calls) >= sum(LIMITS.values()):
            raise RuntimeError("CALL_BUDGET_OR_PREVIOUS_ERROR")
        if len(calls) - self.stage_start >= LIMITS[self.stage]:
            raise RuntimeError("STAGE_CALL_BUDGET")
        item = {"number": len(calls) + 1, "stage": self.stage, **self.context,
                "output_type": response_model.__name__, "status": "STARTED"}
        calls.append(item)
        refresh_metadata(self.metadata)
        started = perf_counter()
        try:
            response = self.inner.complete(request, response_model)
        except AIModelError as exc:
            self.failed = True
            item.update(status="ERROR", error=type(exc).__name__, error_phase=exc.phase)
            usage = exc.usage
            raise
        else:
            item.update(status="RETURNED", model=response.model)
            usage = response.usage
            return response
        finally:
            item["elapsed_seconds"] = round(perf_counter() - started, 3)
            if item["status"] != "STARTED":
                item["usage"] = None if usage is None else {
                    key: getattr(usage, key) for key in TOKEN_KEYS
                }
            refresh_metadata(self.metadata)


def flow_report(raw):
    observations = []
    for run in raw["runs"]:
        errors = [c for c in run["calls"] if c.get("status") == "ERROR"]
        error = errors[0] if errors else {}
        observations.append(CaseRun(
            case_id=run["case_id"],
            output=AgentGraphOutput.model_validate({
                key: run[key] for key in AgentGraphOutput.model_fields
            }),
            **{key: run[key] for key in TOKEN_KEYS},
            fact=FactCheckResult(**run["fact"]), readback=ReadbackResult(**run["readback"]),
            observation_cites_input=run["observation_cites_input"],
            error=error.get("error"), error_phase=error.get("error_phase"),
        ))
    return asdict(build_column_report(raw["label"], observations))


def generate(stage, client, frozen):
    module = baseline_module()[0] if stage == "baseline" else agent
    cases = {c.case_id: c for c in load_cases()}
    raw = {
        "label": f"gpt-5.6-luna/low {module.FINOPS_PROMPT_VERSION} {stage}",
        "execution_mode": "service_graph", "prompt_version": module.FINOPS_PROMPT_VERSION,
        "prompt_sha256": module.finops_prompt_fingerprint(), "repeats": 10,
        "case_ids": list(CASE_IDS), "fixed_set": frozen["fixed_set"],
        "model_snapshots": [], "runs": [],
    }
    target = ROUND / f"{stage}-raw.json"
    for cid, repeat in SCHEDULE:
        client.context = {"case_id": cid, "repeat": repeat}
        start = len(client.metadata["calls"])
        graph_input = cases[cid].graph_input
        output = module.run_finops_graph(graph_input, client=client)
        calls = client.metadata["calls"][start:]
        payload = module._incident_payload(graph_input)
        raw["runs"].append({
            "case_id": cid, "repeat": repeat, **output.model_dump(mode="json"),
            "calls": calls, **usage_total(calls),
            "fact": asdict(check_summary_facts(payload, output.summary_lines)),
            "readback": asdict(check_readback(payload, output)),
            "observation_cites_input": observation_cites_input(payload, output.summary_lines),
        })
        raw["model_snapshots"] = sorted({
            c["model"] for r in raw["runs"] for c in r["calls"] if c.get("model")
        })
        write(target, raw)
        print(f"{stage}: {len(raw['runs'])}/60, calls={len(client.metadata['calls'])}", flush=True)
        if client.failed:
            raise RuntimeError("GENERATION_CALL_ERROR")
    write(ROUND / f"{stage}-flow.json", flow_report(raw))
    if stage == "candidate":
        write(ROUND / "candidate-scored.json",
              score_report(raw, read(ROUND.parent.parent / "spec.json")))


def judge(client):
    payloads = {case.case_id: agent._incident_payload(case.graph_input) for case in load_cases()}
    result = {"judge_prompt_sha256": judge_fingerprint(), "sources": []}
    for stage in ("baseline", "candidate"):
        source_path = ROUND / f"{stage}-raw.json"
        raw = read(source_path)
        column = {"column": stage, "raw_sha256": sha(source_path), "runs": []}
        result["sources"].append(column)
        for index, run in enumerate(raw["runs"]):
            client.context = {"column": stage, "case_id": run["case_id"], "repeat": run["repeat"]}
            judged = judge_file({**raw, "runs": [run]}, payloads, client, 1)
            for item in judged:
                item["run_index"] = index
            column["runs"].extend(judged)
            write(ROUND / "judged.json", result)
            if client.failed:
                raise RuntimeError("JUDGE_CALL_ERROR")
        column["summary"] = summarize(raw["label"], column["runs"], 1)
        write(ROUND / "judged.json", result)


def check_stage(stage, frozen, metadata):
    approval_path = ROUND / "approval.json"
    if not approval_path.exists():
        raise ValueError("APPROVAL_REQUIRED")
    approval = read(approval_path)
    if (approval.get("maximum_calls") != sum(LIMITS.values())
            or approval.get("preflight_sha256") != sha(ROUND / "preflight.json")
            or not approval.get("user_quote")):
        raise ValueError("APPROVAL_DOES_NOT_MATCH_FROZEN_PLAN")
    if stage in metadata["stages"]:
        raise ValueError("STAGE_ALREADY_STARTED")
    if stage != "baseline" and metadata["stages"].get("baseline", {}).get("status") != "COMPLETED":
        raise ValueError("BASELINE_REQUIRED")
    if stage == "judge":
        if metadata["stages"].get("candidate", {}).get("status") != "COMPLETED":
            raise ValueError("CANDIDATE_REQUIRED")
        if not read(ROUND / "candidate-scored.json")["passed"]:
            raise ValueError("CANDIDATE_PRICE_GATE_NOT_PASSED")
        for column in ("baseline", "candidate"):
            raw = read(ROUND / f"{column}-raw.json")
            if len(raw["runs"]) != 60 or raw["fixed_set"] != frozen["fixed_set"]:
                raise ValueError("COMPARISON_INPUT_MISMATCH")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--freeze", action="store_true")
    parser.add_argument("--stage", choices=tuple(LIMITS))
    args = parser.parse_args()
    current = current_preflight()
    metadata_path = ROUND / "metadata.json"
    metadata = read(metadata_path) if metadata_path.exists() else {"calls": [], "stages": {}}
    if args.freeze:
        if metadata["calls"]:
            raise ValueError("CANNOT_REFREEZE_AFTER_CALLS")
        write(ROUND / "preflight.json", current)
    if not args.stage:
        print(json.dumps(current, ensure_ascii=False, indent=2))
        return
    frozen = read(ROUND / "preflight.json")
    if frozen != current:
        raise ValueError("FROZEN_SOURCE_CHANGED")
    check_stage(args.stage, frozen, metadata)
    # 모든 승인·동일 후보·중복 실행 검사를 통과한 뒤에만 키를 읽는다.
    key = dotenv_values(ROOT.parent.parent / ".env").get("OPENAI_API_KEY")
    if not key:
        raise ValueError("APPROVED_MAIN_CHECKOUT_KEY_MISSING")
    stage = {"status": "STARTED", "started_at": datetime.now(UTC).isoformat(),
             "call_start": len(metadata["calls"])}
    metadata["stages"][args.stage] = stage
    metadata["preflight_sha256"] = sha(ROUND / "preflight.json")
    refresh_metadata(metadata)
    started = perf_counter()
    try:
        with OpenAI(api_key=key, base_url="https://api.openai.com/v1",
                    timeout=30, max_retries=0) as sdk:
            inner = OpenAIModelClient(
                client=sdk, model="gpt-5.6-luna", timeout_seconds=30, max_attempts=1,
                retry_backoff_seconds=0, temperature=None, reasoning_effort="low",
            )
            client = RecordingClient(inner, metadata, args.stage)
            if args.stage == "judge":
                judge(client)
            else:
                generate(args.stage, client, frozen)
        stage["status"] = "COMPLETED"
    except Exception as exc:  # noqa: BLE001 -- 최종 경계에 예외 종류만 보존한다.
        stage.update(status="ERROR", error=type(exc).__name__)
        print(f"stage stopped: {type(exc).__name__}", flush=True)
        return 2
    finally:
        stage.update(ended_at=datetime.now(UTC).isoformat(),
                     elapsed_seconds=round(perf_counter() - started, 3),
                     call_end=len(metadata["calls"]))
        refresh_metadata(metadata)
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    logging.disable(logging.CRITICAL)
    raise SystemExit(main())
