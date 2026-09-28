"""기본 동작은 오프라인 확인이며, --run에서만 유료 모델을 호출한다."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from agent_dispatcher import stale_claim_ceiling_seconds
from config import Settings

from ai.agent import SECOPS_MODEL_CALLS
from ai.openai_client import build_openai_model_client

from .dataset import check_sources, load_cases, read_json
from .runner import (
    SETTING_NAMES,
    fingerprints,
    new_run,
    report,
    review_template,
    run_one,
)
from .scoring import ReviewFile, load_answers


def _settings(args: argparse.Namespace, *, paid: bool = False) -> Settings:
    # 워크트리 인계 시 기본 체크아웃의 .env는 인증에만 사용한다.
    # 기준선은 커밋된 서비스 기본값을 쓰며 모델·추론 수준 변경은 별도 실험으로 기록한다.
    values = {name: Settings.model_fields[name].default for name in SETTING_NAMES}
    if args.model:
        values["OPENAI_MODEL"] = args.model
    if args.reasoning_effort:
        values["OPENAI_REASONING_EFFORT"] = args.reasoning_effort
    key = None
    if paid:
        key = os.getenv("OPENAI_API_KEY")
        if args.key_env_file is not None:
            from dotenv import dotenv_values

            key = dotenv_values(args.key_env_file).get("OPENAI_API_KEY")
        if not key:
            raise ValueError("OPENAI_API_KEY is required for --run")
    return Settings(_env_file=None, DATABASE_URL="evaluation-does-not-connect-to-db",
                    OPENAI_API_KEY=key, **values)


def _dump(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2) + "\n"


def _create(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(_dump(value))


def _save_progress(path: Path, run: dict) -> None:
    # path는 이번 실행에서 독점 생성했다. 완료한 관측을 저장한 뒤 다음 건을 진행한다.
    # 중단된 실행은 state=RUNNING으로 남으며 PASS로 처리되지 않는다.
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", newline="\n", dir=path.parent,
                                     prefix=path.name + ".", suffix=".tmp", delete=False) as stream:
        temporary = Path(stream.name)
        stream.write(_dump(run))
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SecOps MVP 모의 시나리오 평가; 기본은 오프라인 입력 확인")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check-inputs", action="store_true")
    mode.add_argument("--estimate", action="store_true", help="호출 상한·조건만 확인; 모델 호출 없음")
    mode.add_argument("--run", action="store_true", help="라운드 승인 후 실제 모델 호출")
    mode.add_argument("--report", type=Path, metavar="RUN_JSON")
    mode.add_argument("--review-template", type=Path, metavar="RUN_JSON")
    parser.add_argument("--output", type=Path, help="새 파일만 허용; 기존 결과를 덮어쓰지 않음")
    parser.add_argument("--review", type=Path, help="요약 의미 검토 파일; --report에서만 사용")
    parser.add_argument("--cases", nargs="+", help="주평가 C01~C07 중 선택; 기본 전체")
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--model", help="생략 시 서비스 코드 기본 모델")
    parser.add_argument("--reasoning-effort", choices=["low", "medium", "high", "unset"])
    parser.add_argument("--key-env-file", type=Path, help="--run 인증 키만 읽을 .env 경로")
    args = parser.parse_args(argv)
    if args.repeats < 1:
        parser.error("--repeats must be positive")
    if args.review is not None and args.report is None:
        parser.error("--review requires --report")
    if (args.run or args.review_template is not None) and args.output is None:
        parser.error("--run/--review-template requires a new --output path")

    cases = load_cases()
    # 원본 변경은 계측 준비 단계에서 알린다. CI의 입력 로딩에는 연결하지 않는다.
    check_sources(cases)
    answers = load_answers(cases)
    selected = args.cases
    saved = None
    if args.report is not None or args.review_template is not None:
        saved = read_json(args.report or args.review_template)
        if selected is not None and selected != saved["case_ids"]:
            parser.error("--cases must match the recorded run")
        selected = saved["case_ids"]
    if selected is not None:
        if len(selected) != len(set(selected)) or not selected or set(selected) - set(answers):
            parser.error("Select unique primary case IDs from C01~C07; N01/N02 require no model call")
        by_id = {case.case_id: case for case in cases}
        cases = [by_id[case_id] for case_id in selected]

    if args.review_template is not None:
        # 검토 양식을 만들기 전에 측정 결과와 입력·답지의 연결을 확인한다.
        report(saved, cases, answers)
        _create(args.output, review_template(saved))
        print(f"검토 양식: {args.output.resolve()}")
        return 0
    if args.report is not None:
        review = ReviewFile.model_validate(read_json(args.review)) if args.review else None
        result = report(saved, cases, answers, review)
        if args.output:
            _create(args.output, result)
        print(_dump(result), end="")
        return 0 if result["quality_pass"] else 2
    settings = _settings(args, paid=args.run)
    if args.estimate:
        calls = len(cases) * args.repeats * SECOPS_MODEL_CALLS
        print(_dump({
            "case_ids": [case.case_id for case in cases], "repeats": args.repeats,
            "graph_runs": len(cases) * args.repeats, "model_calls_upper_bound": calls,
            "http_attempts_upper_bound": calls * settings.OPENAI_MAX_ATTEMPTS,
            "conservative_timeout_budget_seconds": len(cases) * args.repeats * stale_claim_ceiling_seconds(settings),
            "settings": {name: getattr(settings, name) for name in SETTING_NAMES},
            **fingerprints(cases),
            "note": "요약 검토는 별도 모델 호출 없이 근거와 판정자를 기록한다. 금액은 최신 단가와 예상 토큰으로 실행 전에 산정한다.",
        }), end="")
        return 0
    if not args.run:
        print(_dump({
            "scope": "MVP mock scenarios, not collector detection",
            "cases": [{"case_id": case.case_id, "title": case.title,
                       "initial_risk": case.graph_input.initial_risk.initial_risk_level.value}
                      for case in cases],
            "controls_without_model_calls": ["N01", "N02"], **fingerprints(cases),
        }), end="")
        return 0

    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8]
    run = new_run(cases, args.repeats, settings, run_id)
    _create(args.output, run)
    client = build_openai_model_client(settings)
    for repeat in range(1, args.repeats + 1):
        for case in cases:
            record = run_one(case, repeat, client)
            run["records"].append(record)
            _save_progress(args.output, run)
            print(f"{case.case_id} {repeat}/{args.repeats}: {record['output']['invocation_status']} "
                  f"({record['model_calls']} calls)", flush=True)
    run["state"] = "COMPLETE"
    _save_progress(args.output, run)
    print(f"측정 자료: {args.output.resolve()} — 의미 검토와 합격 판정은 아직 수행하지 않음")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
