# ==============================================================================
# [파일 설명]
# FinOps 3단계(#583) 오프라인 지표와 의미 검토 보조 — 모델을 부르지 않는다.
#
# finops_eval.py --json 원자료(와 승인 v2 원자료)를 읽어 다음을 낸다. 모든 지표는 실패·누락을
# 분모에서 빼지 않고 계획 대비 적용 범위(coverage)를 함께 적는다.
#   ① decision 뒤집힘 — 같은 입력의 회차들이 낸 후보 runbook 집합(정규화: 중복 제거·정렬)이
#      갈리는 케이스 비율. 진짜 NO_PROPOSAL은 빈 집합이고 FAILED·상태/후보 불일치 회차는 제외한다.
#   ② DECISION_MISMATCH — failure_reason 개수와 비율. 분모는 계획한 실행 수다.
#   ③ NO_PROPOSAL — 후보 판과 승인 v2의 비교. 입력·설정·반복 수가 같을 때만 비교 가능으로 본다.
#   ④ rationale_supports_decision 의미 검토 — 요약 3줄과 후보 집합을 한 묶음(packet)으로 만들어
#      서로 다른 검토자가 독립 파일로 판정하고, 인용·출력 지문이 맞는지 확인해 집계한다.
#      승인 v2도 같은 묶음에서 자신의 후보 집합으로 해석한다.
# 합격 문턱은 정하지 않는다. 목업 원자료는 data_source가 real_model이 아니라 실모델 품질로
# 표시되지 않는다.
#
# 실행 (repo 루트에서, 호출 없음):
#   (진입점 스크립트는 없다. repo 루트에서 PYTHONPATH=apps/core-api:packages 를 두고)
#   uv run python -m ai.evaluation.summary.finops_stage3 metrics cand.json --v2 v2.json [--judged judged.json]
#   uv run python -m ai.evaluation.summary.finops_stage3 packet cand.json v2.json --out packet.json
#   uv run python -m ai.evaluation.summary.finops_stage3 review-template packet.json --reviewer R --model M --effort high --out r.json
#   uv run python -m ai.evaluation.summary.finops_stage3 review-score packet.json r_claude.json r_gpt.json
# ==============================================================================

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

VALID_STATUSES = ("SUCCEEDED", "NO_PROPOSAL")
DECISION_MISMATCH = "DECISION_MISMATCH"
CRITERION_ID = "rationale_supports_decision"
# SecOps 항목과 별개의 FinOps 문구다 — 복원·결함 판정자 프롬프트(judge.py)는 바꾸지 않는다
CRITERION = (
    "요약 3줄의 근거·설명이 실제로 낸 decision(후보 runbook 집합)을 뒷받침한다. 후보를 제시했다면 "
    "요약이 관측된 낭비 근거를 그 조치의 목적과 연결한다. 후보가 비었다면(무제안) 요약이 관측과 "
    "연결된 보류 이유를 밝힌다. 요약이 decision과 반대 방향을 가리키거나 제시하지 않은 조치를 "
    "권하면 FAIL이다. 판정은 요약과 decision만으로 하며 입력에 없는 사실을 보태지 않는다."
)


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _same_file_key(path: str) -> str:
    return os.path.normcase(os.path.abspath(path))


def load_raw(path: Path) -> dict[str, Any]:
    raw = json.loads(path.read_text("utf-8"))
    raw["_path"] = str(path)
    return raw


# --- 결정과 계획 범위 ------------------------------------------------------------


def decision_set(run: dict[str, Any]) -> Optional[tuple[str, ...]]:
    """유효 회차의 정규화된 후보 runbook 집합. 진짜 NO_PROPOSAL은 (). FAILED와 상태·후보가
    어긋난 회차(SUCCEEDED인데 후보 없음 등)는 결정으로 읽을 수 없어 None이다."""
    ids = tuple(sorted({item["runbook_id"] for item in run.get("candidates", ())}))
    status = run["invocation_status"]
    if status == "SUCCEEDED" and ids:
        return ids
    if status == "NO_PROPOSAL" and not ids:
        return ()
    return None


def decision_label(decision: tuple[str, ...]) -> str:
    return "+".join(decision) or "NO_PROPOSAL"


def _plan(raw: dict[str, Any]) -> tuple[list[str], int, str]:
    """(계획 케이스, 케이스당 반복, 출처). 선언이 없는 구 원자료는 기록된 실행에서 추정하며,
    그때는 통째로 빠진 케이스를 알 수 없다."""
    declared = raw.get("planned")
    if declared:
        return list(declared["case_ids"]), int(declared["repeats"]), "declared"
    case_ids = list(dict.fromkeys(run["case_id"] for run in raw["runs"]))
    counts = Counter(run["case_id"] for run in raw["runs"])
    repeats = int(raw.get("repeats") or max(counts.values(), default=0))
    return case_ids, repeats, "inferred_from_runs"


def coverage(raw: dict[str, Any]) -> dict[str, Any]:
    case_ids, repeats, source = _plan(raw)
    counts = Counter(run["case_id"] for run in raw["runs"])
    planned = len(case_ids) * repeats
    recorded_in_plan = sum(min(counts[case_id], repeats) for case_id in case_ids)
    decisions = [decision_set(run) for run in raw["runs"]]
    return {
        "planned_source": source,
        "planned_cases": len(case_ids),
        "repeats": repeats,
        "planned_runs": planned,
        "recorded_runs": len(raw["runs"]),
        "missing_runs": planned - recorded_in_plan,
        "unplanned_runs": len(raw["runs"]) - recorded_in_plan,
        "valid_runs": sum(1 for item in decisions if item is not None),
        "failed_runs": sum(1 for run in raw["runs"] if run["invocation_status"] == "FAILED"),
        "inconsistent_runs": sum(
            1
            for run, item in zip(raw["runs"], decisions)
            if item is None and run["invocation_status"] != "FAILED"
        ),
        "data_source": raw.get("data_source", "unlabeled"),
        "real_model_measurement": raw.get("data_source") == "real_model",
    }


# --- ① decision 뒤집힘 -------------------------------------------------------------


def decision_flip_metrics(raw: dict[str, Any]) -> dict[str, Any]:
    case_ids, _, _ = _plan(raw)
    by_case: dict[str, list[tuple[str, ...]]] = {case_id: [] for case_id in case_ids}
    for run in raw["runs"]:
        decision = decision_set(run)
        if decision is not None and run["case_id"] in by_case:
            by_case[run["case_id"]].append(decision)
    cases: dict[str, Any] = {}
    unmeasured: dict[str, int] = {}
    for case_id, decisions in by_case.items():
        if len(decisions) < 2:
            unmeasured[case_id] = len(decisions)
            continue
        counted = Counter(decision_label(item) for item in decisions)
        cases[case_id] = {
            "valid_runs": len(decisions),
            "decisions": dict(counted),
            "flip_ratio": round(1 - max(counted.values()) / len(decisions), 4),
        }
    flipped = sum(1 for item in cases.values() if item["flip_ratio"] > 0)
    return {
        "cases_planned": len(case_ids),
        "cases_measured": len(cases),
        "cases_with_flip": flipped,
        "flip_rate": flipped / len(cases) if cases else None,
        "cases_unmeasured_valid_runs": unmeasured,
        "cases": cases,
        "coverage": coverage(raw),
    }


# --- ② DECISION_MISMATCH -----------------------------------------------------------


def decision_mismatch_metrics(raw: dict[str, Any]) -> dict[str, Any]:
    cov = coverage(raw)
    count = sum(1 for run in raw["runs"] if run.get("failure_reason") == DECISION_MISMATCH)
    planned = cov["planned_runs"]
    return {
        "count": count,
        "denominator": planned,
        "denominator_kind": "planned_runs",
        "rate": count / planned if planned else None,
        # 누락 실행이 모두 불일치였다면의 상한 — 누락을 성공으로 세지 않는다
        "rate_upper_bound": (count + cov["missing_runs"]) / planned if planned else None,
        "failed_runs": cov["failed_runs"],
        "failed_without_reason": sum(
            1
            for run in raw["runs"]
            if run["invocation_status"] == "FAILED" and not run.get("failure_reason")
        ),
        "coverage": cov,
    }


# --- ③ NO_PROPOSAL 비교 ------------------------------------------------------------


def no_proposal_metrics(raw: dict[str, Any]) -> dict[str, Any]:
    cov = coverage(raw)
    count = sum(1 for run in raw["runs"] if run["invocation_status"] == "NO_PROPOSAL")
    planned = cov["planned_runs"]
    return {
        "count": count,
        "rate_of_planned": count / planned if planned else None,
        "rate_of_valid": count / cov["valid_runs"] if cov["valid_runs"] else None,
        "coverage": cov,
    }


def compare_no_proposal(
    candidate: dict[str, Any], v2: dict[str, Any], judged: Optional[dict[str, Any]] = None
) -> dict[str, Any]:
    """같은 입력·설정·반복에서 후보 판과 승인 v2의 NO_PROPOSAL을 견준다.

    입력·설정이 다르거나 짝 판정(paired judging)이 확인되지 않으면 비교 판정(verdict_eligible)을
    내리지 않는다. 개수·비율은 진단값으로 항상 남긴다. 짝 판정은 두 원자료가 같은 판정 파일
    (finops_judge.py --out)에서 요약 있는 회차마다 오류 없는 판정을 가졌을 때만 확인으로 본다 —
    경로가 있다는 것만으로는 부족하다.
    """
    mismatches: list[str] = []
    if candidate.get("fixed_set") != v2.get("fixed_set") or candidate.get("fixed_set") is None:
        mismatches.append("fixed_set")
    if candidate.get("label") != v2.get("label"):
        mismatches.append("label(model_and_knobs)")
    cand_plan, v2_plan = _plan(candidate), _plan(v2)
    if cand_plan[:2] != v2_plan[:2]:
        mismatches.append("planned_cases_or_repeats")
    pending: list[str] = []
    paired: Optional[bool] = None
    coverage_detail: Optional[dict[str, Any]] = None
    if judged is None:
        pending.append("judged_file_not_supplied")
    else:
        coverage_detail = {
            "candidate": judgment_coverage(candidate, judged),
            "v2": judgment_coverage(v2, judged),
        }
        paired = all(item["complete"] for item in coverage_detail.values())
        if not paired:
            pending.append("paired_judgment_incomplete")
    cand, old = no_proposal_metrics(candidate), no_proposal_metrics(v2)
    comparable = not mismatches
    eligible = comparable and paired is True
    return {
        "candidate": cand,
        "v2": old,
        "comparable": comparable,
        "mismatches": mismatches,
        "paired_judging": paired,
        "paired_judging_coverage": coverage_detail,
        "verdict_eligible": eligible,
        "pending": pending,
        "candidate_not_greater_than_v2": cand["count"] <= old["count"] if eligible else None,
    }


def judgment_coverage(raw: dict[str, Any], judged: dict[str, Any]) -> dict[str, Any]:
    """요약이 있는 회차(judge_cli가 판정하는 대상)마다 오류 없는 복원·결함 판정이 있는지 센다."""
    key = _same_file_key(raw["_path"])
    source = next((s for s in judged["sources"] if _same_file_key(s["path"]) == key), None)
    judgeable = [index for index, run in enumerate(raw["runs"]) if run.get("summary_lines")]
    by_index = {item["run_index"]: item for item in source.get("runs", ())} if source else {}
    ok = errored = missing = 0
    for index in judgeable:
        item = by_index.get(index)
        if item is None or item["case_id"] != raw["runs"][index]["case_id"]:
            missing += 1
        elif (
            not item["restoration"] or not item["defects"]
            or "error" in item["restoration"][0] or "error" in item["defects"][0]
        ):
            errored += 1
        else:
            ok += 1
    return {
        "source_found": source is not None,
        "judgeable_runs": len(judgeable),
        "judged_ok": ok,
        "judgment_errors": errored,
        "judgment_missing": missing,
        "complete": source is not None and bool(judgeable) and ok == len(judgeable),
    }


# --- 호출 계상 ---------------------------------------------------------------------


def call_accounting(raw: dict[str, Any]) -> dict[str, Any]:
    """논리 호출과 물리 시도를 구분해 합친다. 시도 수는 trace가 있는 호출만 관측된다."""
    records = [rec for run in raw["runs"] for rec in run.get("call_records", ())]
    attempts = [rec["attempts"] for rec in records]
    return {
        "logical_calls": len(records),
        "runs_without_call_records": sum(1 for run in raw["runs"] if "call_records" not in run),
        "calls_by_step": dict(Counter(rec["response_model"] for rec in records)),
        "errors_by_class_phase": dict(
            Counter(f"{rec['error_class']}/{rec['error_phase']}" for rec in records if rec["outcome"] == "error")
        ),
        "usage_status_counts": dict(Counter(rec.get("usage_status", "unlabeled") for rec in records)),
        "physical_attempts_observed": sum(item for item in attempts if item is not None),
        "calls_without_attempt_trace": sum(1 for item in attempts if item is None),
        "attempts_without_usage": sum(rec["attempts_without_usage"] or 0 for rec in records),
        # 보고된 값만 더한 하한이다. missing 호출 수가 0이 아니면 그 필드의 실제 합은 더 클 수 있다
        "known_usage_subtotals": {
            name: {
                "known_subtotal": sum(rec[name] for rec in records if rec.get(name) is not None),
                "calls_missing_field": sum(1 for rec in records if rec.get(name) is None),
            }
            for name in ("prompt_tokens", "completion_tokens", "cached_prompt_tokens")
        },
        # 미보고 필드를 0으로 채운 기존 합계 — 위 하한과 구분해 읽는다
        "zero_filled_run_totals": {
            "prompt_tokens": sum(run["prompt_tokens"] for run in raw["runs"]),
            "completion_tokens": sum(run["completion_tokens"] for run in raw["runs"]),
        },
        "attempt_details_calls": sum(1 for rec in records if rec.get("attempt_details") is not None),
    }


def metrics_report(
    candidate: dict[str, Any], v2: Optional[dict[str, Any]], judged: Optional[dict[str, Any]]
) -> dict[str, Any]:
    report: dict[str, Any] = {
        "candidate": {
            "decision_flip": decision_flip_metrics(candidate),
            "decision_mismatch": decision_mismatch_metrics(candidate),
            "call_accounting": call_accounting(candidate),
        }
    }
    if v2 is not None:
        report["v2"] = {
            "decision_flip": decision_flip_metrics(v2),
            "decision_mismatch": decision_mismatch_metrics(v2),
        }
        report["no_proposal_vs_v2"] = compare_no_proposal(candidate, v2, judged)
    return report


# --- ④ rationale_supports_decision 의미 검토 ---------------------------------------


def build_review_packet(sources: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """검토 대상 묶음. 유효 결정과 요약이 있는 회차만 담고, 나머지는 unreviewable로 센다.

    item_id는 출처 라벨·케이스·회차 순번이다. output_sha256은 요약과 후보 집합의 지문이라
    검토 파일이 다른 출력에 대한 판정을 섞어 넣으면 걸러진다. v2도 자신의 후보 집합으로 읽는다.
    """
    items: list[dict[str, Any]] = []
    unreviewable: dict[str, int] = {}
    for label, raw in sources.items():
        seen: Counter = Counter()
        unreviewable[label] = 0
        for run in raw["runs"]:
            seen[run["case_id"]] += 1
            decision = decision_set(run)
            if decision is None or not run.get("summary_lines"):
                unreviewable[label] += 1
                continue
            content = {
                "summary_lines": list(run["summary_lines"]),
                "decision_runbooks": list(decision),
            }
            items.append(
                {
                    "item_id": f"{label}:{run['case_id']}#{seen[run['case_id']]}",
                    "source": label,
                    "case_id": run["case_id"],
                    **content,
                    "output_sha256": _digest(content),
                }
            )
    body = {"criterion_id": CRITERION_ID, "criterion": CRITERION, "items": items}
    return {
        **body,
        "criterion_sha256": _digest(CRITERION),
        "packet_sha256": _digest(body),
        "unreviewable_runs": unreviewable,
        "data_sources": {label: raw.get("data_source", "unlabeled") for label, raw in sources.items()},
    }


class ReviewEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    item_id: str
    output_sha256: str
    verdict: Literal["PASS", "FAIL", "UNSURE"]
    # 요약 한 줄에서 그대로 인용한다. PASS는 인용이 있어야 한다
    quote: str = ""
    reason: str = Field(min_length=1)


class ReviewFile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    packet_sha256: str
    reviewer: str = Field(min_length=1)
    reviewer_model: str = Field(min_length=1)
    reasoning_effort: str = Field(min_length=1)
    reviews: list[ReviewEntry]


def review_template(packet: dict[str, Any], reviewer: str, model: str, effort: str) -> dict[str, Any]:
    """UNSURE로 채운 빈 양식. 검토자가 판정으로 바꾸기 전에는 통과로 세지 않는다."""
    return {
        "packet_sha256": packet["packet_sha256"],
        "reviewer": reviewer,
        "reviewer_model": model,
        "reasoning_effort": effort,
        "reviews": [
            {
                "item_id": item["item_id"],
                "output_sha256": item["output_sha256"],
                "verdict": "UNSURE",
                "quote": "",
                "reason": "검토 대기",
            }
            for item in packet["items"]
        ],
    }


def _score_one(packet: dict[str, Any], review: ReviewFile) -> dict[str, Any]:
    items = {item["item_id"]: item for item in packet["items"]}
    status: dict[str, str] = {item_id: "MISSING" for item_id in items}
    problems: dict[str, str] = {}
    unplanned: list[str] = []
    if review.packet_sha256 != packet["packet_sha256"]:
        return {
            "reviewer": review.reviewer, "reviewer_model": review.reviewer_model,
            "reasoning_effort": review.reasoning_effort, "rejected": "packet_sha256_mismatch",
            "status": status, "problems": problems, "unplanned_entries": unplanned,
        }
    seen: set[str] = set()
    for entry in review.reviews:
        item = items.get(entry.item_id)
        if item is None:
            unplanned.append(entry.item_id)
            continue
        if entry.item_id in seen:
            status[entry.item_id], problems[entry.item_id] = "INVALID", "duplicate_entry"
            continue
        seen.add(entry.item_id)
        if entry.output_sha256 != item["output_sha256"]:
            status[entry.item_id], problems[entry.item_id] = "INVALID", "output_sha256_mismatch"
        elif not entry.reason.strip():
            status[entry.item_id], problems[entry.item_id] = "INVALID", "reason_missing"
        elif entry.quote and not any(entry.quote in line for line in item["summary_lines"]):
            status[entry.item_id], problems[entry.item_id] = "INVALID", "quote_not_in_summary"
        elif entry.verdict == "PASS" and not entry.quote.strip():
            status[entry.item_id], problems[entry.item_id] = "INVALID", "pass_without_quote"
        else:
            status[entry.item_id] = entry.verdict
    return {
        "reviewer": review.reviewer,
        "reviewer_model": review.reviewer_model,
        "reasoning_effort": review.reasoning_effort,
        "rejected": None,
        "status": status,
        "problems": problems,
        "unplanned_entries": unplanned,
    }


def score_reviews(packet: dict[str, Any], reviews: list[ReviewFile]) -> dict[str, Any]:
    """검토자별 집계와 검토자 간 일치. 분모는 packet의 전체 항목이며 MISSING·INVALID·UNSURE는
    통과로 세지 않고 그대로 센다. 합격 문턱은 판정하지 않는다."""
    scored = [_score_one(packet, review) for review in reviews]
    total = len(packet["items"])
    by_source = {item["item_id"]: item["source"] for item in packet["items"]}
    reviewers = []
    for result in scored:
        counts = Counter(result["status"].values())
        per_source: dict[str, Counter] = {}
        for item_id, state in result["status"].items():
            per_source.setdefault(by_source[item_id], Counter())[state] += 1
        reviewers.append(
            {
                **{k: result[k] for k in ("reviewer", "reviewer_model", "reasoning_effort", "rejected")},
                "denominator": total,
                "counts": {name: counts.get(name, 0) for name in ("PASS", "FAIL", "UNSURE", "INVALID", "MISSING")},
                "by_source": {source: dict(c) for source, c in per_source.items()},
                "problems": result["problems"],
                "unplanned_entries": result["unplanned_entries"],
            }
        )
    agreement: Optional[dict[str, Any]] = None
    if len(scored) >= 2:
        agree, disagree, incomplete = 0, [], 0
        for item_id in by_source:
            states = {result["status"][item_id] for result in scored}
            if states <= {"PASS", "FAIL"}:
                if len(states) == 1:
                    agree += 1
                else:
                    disagree.append(item_id)
            else:
                incomplete += 1
        agreement = {
            "agree": agree, "disagree": len(disagree), "incomplete": incomplete,
            "disagreeing_items": disagree,
        }
    models = [review.reviewer_model for review in reviews]
    return {
        "criterion_id": CRITERION_ID,
        "items": total,
        "reviewers": reviewers,
        "independent_reviewers": len(set(models)) == len(models) and len(models) >= 2,
        "agreement": agreement,
        "data_sources": packet["data_sources"],
    }


# --- CLI ---------------------------------------------------------------------------


def _emit(value: Any, out: Optional[Path]) -> None:
    text = json.dumps(value, ensure_ascii=False, indent=1)
    if out:
        out.write_text(text, encoding="utf-8")
        print(f"written: {out}")
    else:
        print(text)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="FinOps stage 3 offline metrics and semantic review")
    sub = parser.add_subparsers(dest="command", required=True)
    metrics = sub.add_parser("metrics")
    metrics.add_argument("candidate", type=Path)
    metrics.add_argument("--v2", type=Path)
    metrics.add_argument("--judged", type=Path, help="finops_judge.py --out file for the paired-judging check")
    packet = sub.add_parser("packet")
    packet.add_argument("candidate", type=Path)
    packet.add_argument("v2", type=Path)
    template = sub.add_parser("review-template")
    template.add_argument("packet", type=Path)
    template.add_argument("--reviewer", required=True)
    template.add_argument("--model", required=True)
    template.add_argument("--effort", required=True)
    score = sub.add_parser("review-score")
    score.add_argument("packet", type=Path)
    score.add_argument("reviews", nargs="+", type=Path)
    for item in (metrics, packet, template, score):
        item.add_argument("--out", type=Path)
    args = parser.parse_args(argv)

    if args.command == "metrics":
        judged = json.loads(args.judged.read_text("utf-8")) if args.judged else None
        v2 = load_raw(args.v2) if args.v2 else None
        _emit(metrics_report(load_raw(args.candidate), v2, judged), args.out)
    elif args.command == "packet":
        _emit(build_review_packet({"candidate": load_raw(args.candidate), "v2": load_raw(args.v2)}), args.out)
    elif args.command == "review-template":
        packet_data = json.loads(args.packet.read_text("utf-8"))
        _emit(review_template(packet_data, args.reviewer, args.model, args.effort), args.out)
    else:
        packet_data = json.loads(args.packet.read_text("utf-8"))
        files = [ReviewFile.model_validate_json(path.read_text("utf-8")) for path in args.reviews]
        _emit(score_reviews(packet_data, files), args.out)
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
