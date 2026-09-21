"""DB·AWS 실행 없이 현재 그래프와 Dispatcher 계약을 계측한다."""

from __future__ import annotations

import subprocess
import time
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any

from agent_dispatcher import verify_graph_output
from schemas.agents import AgentGraphOutput
from schemas.incidents import AgentInvocationStatus

from ai import agent
from ai.evaluation.common.fingerprints import input_fingerprint
from ai.evaluation.common.paths import REPO_ROOT
from ai.evaluation.common.reproducibility import field_agreement
from ai.evaluation.common.usage import UsageRecordingClient
from ai.model_client import AIModelClient
from ai.openai_client import OpenAIModelClient

from .dataset import ROOT, EvalCase, digest, load_cases, read_json
from .request_fingerprints import request_manifest
from .scoring import (
    Answer,
    ReviewFile,
    Score,
    load_answers,
    score_structured,
    score_summary,
)

SETTING_NAMES = (
    "OPENAI_MODEL", "OPENAI_TEMPERATURE", "OPENAI_REASONING_EFFORT", "OPENAI_TIMEOUT_SECONDS",
    "OPENAI_MAX_ATTEMPTS", "OPENAI_RETRY_BACKOFF_SECONDS", "OPENAI_MAX_RETRY_AFTER_SECONDS",
)
SERVICE_FILES = (
    "apps/core-api/ai/agent.py", "apps/core-api/ai/capabilities.py",
    "apps/core-api/ai/model_client.py", "apps/core-api/ai/openai_client.py",
    "apps/core-api/agent_dispatcher.py", "apps/core-api/config.py",
    "packages/schemas/agents.py", "packages/schemas/runbook_parameters.py",
    "packages/schemas/evidence.py", "packages/schemas/mock_logs.py",
    "apps/core-api/secops_context.py", "apps/core-api/incident_intake.py",
    "apps/core-api/mock_threat_source.py", "apps/core-api/threat_ingress.py",
    "apps/core-api/security/risk_evaluator.py", "apps/core-api/security/threat_normalizer.py",
    "scripts/secops_log_corpus.py",
)


def prompt_fingerprint() -> str:
    # 현재 서비스로 재조립·대조한 전체 동결 세트의 메뉴만 사용한다.
    # 공유 맵의 FinOps 전용 제약 변경이 SecOps 승인 지문을 흔들지 않게 한다.
    runbooks = {capability.runbook_id for case in load_cases()
                for capability in case.graph_input.capabilities}
    return digest({
        "summary": agent._SECOPS_SUMMARY_PROMPT,
        "risk": agent._SECOPS_RISK_PROMPT,
        "proposal": agent._SECOPS_PROPOSAL_PROMPT,
        "schemas": [model.model_json_schema() for model in (
            agent.EvidenceSummaryOutput, agent.RiskReassessmentOutput, agent.CandidateProposalOutput,
        )],
        "parameter_constraints": {
            runbook: agent._PARAMETER_CONSTRAINTS.get(runbook, ()) for runbook in runbooks
        },
    })


def fingerprints(cases: list[EvalCase]) -> dict[str, Any]:
    # 부분 측정도 전체 답지에서 선택한다. 품질 판정 분모와 대역 재구성을 혼동하지 않는다.
    answers = load_answers(load_cases())
    return {
        "inputs_sha256": input_fingerprint(cases),
        "answers_sha256": digest(read_json(ROOT / "answers.json")),
        "rubric_sha256": digest(read_json(ROOT / "rubric.json")),
        "prompt_sha256": prompt_fingerprint(),
        "request_sha256": digest(request_manifest(cases, answers)),
        # Windows autocrlf의 줄바꿈 차이를 코드 의미의 변경으로 세지 않는다.
        "service_sha256": digest({path: (REPO_ROOT / path).read_text(encoding="utf-8")
                                  for path in SERVICE_FILES}),
        "evaluator_sha256": digest({path.name: path.read_text(encoding="utf-8")
                                    for path in sorted(ROOT.glob("*.py"))}),
    }


def new_run(cases: list[EvalCase], repeats: int, settings: Any, run_id: str) -> dict[str, Any]:
    if repeats < 1:
        raise ValueError("repeats must be positive")
    git = subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, capture_output=True,
                         text=True, check=True)
    calls = len(cases) * repeats * agent.SECOPS_MODEL_CALLS
    return {
        "run_id": run_id, "created_at": datetime.now(UTC).isoformat(),
        "scope": "MVP mock SSH scenarios; graph and dispatcher contract only",
        "git_head": git.stdout.strip(), **fingerprints(cases),
        "settings": {name: getattr(settings, name) for name in SETTING_NAMES},
        "case_ids": [case.case_id for case in cases], "repeats": repeats,
        "model_calls_upper_bound": calls,
        "http_attempts_upper_bound": calls * settings.OPENAI_MAX_ATTEMPTS,
        "state": "RUNNING", "records": [],
    }


def run_one(case: EvalCase, repeat: int, client: AIModelClient) -> dict[str, Any]:
    recording = UsageRecordingClient(client)
    started = time.monotonic()
    graph_output = agent.run_secops_graph(case.graph_input, client=recording)
    output = verify_graph_output(case.graph_input, graph_output, case.graph_input.incident_id)
    failure_kind = None
    if recording.last_error_phase == "transport":
        failure_kind = "TRANSPORT_ERROR"
    elif recording.last_error_phase == "request":
        failure_kind = "TOOL_ERROR"
    elif recording.last_error_phase == "response":
        failure_kind = "MODEL_RESPONSE_ERROR"
    elif output.invocation_status is AgentInvocationStatus.FAILED:
        failure_kind = "OUTPUT_CONTRACT_FAILURE"
    return {
        "case_id": case.case_id, "repeat": repeat,
        "data_source": "real_model" if isinstance(client, OpenAIModelClient) else "test_double",
        "graph_output": graph_output.model_dump(mode="json"),
        "output": output.model_dump(mode="json"),
        "dispatcher_rejected": output != graph_output,
        "failure_kind": failure_kind,
        "error_type": recording.last_error, "error_phase": recording.last_error_phase,
        "model_calls": recording.calls,
        "reported_prompt_tokens": recording.prompt_tokens,
        "reported_completion_tokens": recording.completion_tokens,
        "reported_cached_prompt_tokens": recording.cached_prompt_tokens,
        "model_snapshots": sorted(recording.model_snapshots),
        "elapsed_seconds": round(time.monotonic() - started, 3),
    }


def review_template(run: dict) -> dict:
    rubric = read_json(ROOT / "rubric.json")
    if run["rubric_sha256"] != digest(rubric):
        raise ValueError("The run used a different rubric")
    return {
        **{name: run[name] for name in ("run_id", "inputs_sha256", "answers_sha256", "rubric_sha256")},
        "reviewer": "UNASSIGNED", "reviewer_kind": "assistant",
        "reviews": [{
            "case_id": record["case_id"], "repeat": record["repeat"],
            "output_sha256": digest(record["output"]),
            "items": [{"check_id": check["check_id"], "verdict": "UNSURE", "quote": "",
                       "reason": "검토 대기"} for check in rubric["checks"]],
        } for record in run["records"] if record["output"]["invocation_status"] != "FAILED"],
    }


def report(run: dict, cases: list[EvalCase], answers: dict[str, Answer],
           review: ReviewFile | None = None) -> dict:
    """계획한 전체 실행을 분모로 삼으며, 일부 실행만으로 기준선을 통과시키지 않는다."""
    current = fingerprints(cases)
    for field in ("inputs_sha256", "answers_sha256", "rubric_sha256"):
        if run[field] != current[field]:
            raise ValueError(f"Cannot score with changed {field}; retain the original inputs/answers/rubric")
    case_ids = run["case_ids"]
    if len(case_ids) != len(set(case_ids)) or set(case_ids) != {case.case_id for case in cases}:
        raise ValueError("Run case IDs differ from the selected fixed inputs")
    repeats = run["repeats"]
    if type(repeats) is not int or repeats < 1:
        raise ValueError("Invalid repeat count")
    planned = {(case_id, repeat) for case_id in case_ids for repeat in range(1, repeats + 1)}
    observed: dict[tuple[str, int], dict] = {}
    for record in run["records"]:
        key = (record["case_id"], record["repeat"])
        if key not in planned or key in observed:
            raise ValueError("Duplicate or unplanned run record")
        observed[key] = record
    reviews = {}
    if review is not None:
        if not review.reviewer.strip() or review.reviewer == "UNASSIGNED":
            raise ValueError("Name the actual summary reviewer")
        for field in ("run_id", "inputs_sha256", "answers_sha256", "rubric_sha256"):
            if getattr(review, field) != run[field]:
                raise ValueError(f"Review does not belong to this run: {field}")
        for item in review.reviews:
            key = (item.case_id, item.repeat)
            if key not in observed or key in reviews:
                raise ValueError("Duplicate or unplanned summary review")
            reviews[key] = item
    rubric = read_json(ROOT / "rubric.json")
    counts = {name: 0 for name in ("PASS", "FAIL", "PENDING", "TOOL_ERROR", "TRANSPORT_ERROR")}
    scores = []
    outputs: dict[str, list[AgentGraphOutput]] = {case_id: [] for case_id in case_ids}
    for key in sorted(planned):
        record = observed.get(key)
        if record is None:
            result = Score("PENDING", ("run_missing",))
            semantic = Score("PENDING", ("not_generated",))
        else:
            output = AgentGraphOutput.model_validate(record["output"])
            outputs[key[0]].append(output)
            failure = record["failure_kind"]
            if failure in ("TRANSPORT_ERROR", "TOOL_ERROR"):
                counts[failure] += 1
                scores.append({"case_id": key[0], "repeat": key[1], "verdict": failure})
                continue
            result = score_structured(output, answers[key[0]])
            semantic = (score_summary(output, reviews.get(key), rubric)
                        if output.invocation_status is not AgentInvocationStatus.FAILED
                        else Score("PENDING", ("no_summary_to_review",)))
        # 두 판정을 함께 보고해 잘못된 검토 기록이 확인된 모델 결함을 가리지 않게 한다.
        verdict = ("FAIL" if result.verdict == "FAIL" or semantic.verdict == "FAIL" else
                   "TOOL_ERROR" if "TOOL_ERROR" in (result.verdict, semantic.verdict) else
                   "PENDING" if "PENDING" in (result.verdict, semantic.verdict) else "PASS")
        counts[verdict] += 1
        scores.append({"case_id": key[0], "repeat": key[1], "verdict": verdict,
                       "structured": asdict(result), "summary": asdict(semantic)})
    from config import Settings

    default_settings = {name: Settings.model_fields[name].default for name in SETTING_NAMES}
    current_service = (
        run.get("request_sha256") == current["request_sha256"]
        and run["settings"] == default_settings
    )
    complete_set = set(case_ids) == {case.case_id for case in load_cases()}
    all_pass = counts["PASS"] == len(planned) and run["state"] == "COMPLETE"
    real_measured = bool(observed) and all(record["data_source"] == "real_model" for record in observed.values())
    snapshots = sorted({name for record in observed.values() for name in record["model_snapshots"]})
    return {
        "run_id": run["run_id"], "planned": len(planned), "recorded": len(observed),
        "counts": counts, "scores": scores,
        "current_service": current_service,
        "request_fingerprint_recorded": "request_sha256" in run,
        "request_sha256": current["request_sha256"],
        "source_files_match": run["service_sha256"] == current["service_sha256"],
        "quality_pass": all_pass,
        "repeat_requirement_met": repeats >= 3,
        "full_primary_set": complete_set,
        "real_model_measurement": real_measured,
        "model_snapshots": snapshots,
        "baseline_candidate": (all_pass and repeats >= 3 and current_service and complete_set
                               and real_measured and len(snapshots) == 1),
        "reviewer": review.reviewer if review is not None else None,
        "reviewer_kind": review.reviewer_kind if review is not None else None,
        "risk_agreement": {
            case_id: [output.reviewed_risk_level.value if output.reviewed_risk_level else None
                      for output in values] for case_id, values in outputs.items()
        },
        # 완전 일치율은 관측값이다. 허용 범위 안의 rule_number 차이는 결함이 아니다.
        "field_agreement": {
            case_id: [asdict(item) for item in field_agreement(values)]
            for case_id, values in outputs.items() if len(values) >= 2
        },
        "reported_tokens": {
            field: sum(record[field] for record in observed.values())
            for field in ("reported_prompt_tokens", "reported_completion_tokens", "reported_cached_prompt_tokens")
        },
        "limits": ["생성·요약 검토 결과이며 실제 탐지·AWS 실행·저장 화면의 성공률이 아니다.",
                   "이름을 남긴 검토자의 의미 판정이다. 자동 판정자 정확도를 검증한 결과가 아니다.",
                   "토큰 수는 제공자가 반환한 사용량이다. 응답 없는 전송 실패의 과금은 확정할 수 없다."],
    }
