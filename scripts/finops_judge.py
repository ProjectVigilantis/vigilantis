# ==============================================================================
# [파일 설명]
# FinOps 요약 3줄의 LLM 판정 실행기입니다. (Issue #243)
#
# scripts/finops_eval.py가 --json으로 남긴 원자료를 읽어, 요약이 있는 회차마다 판정자를
# 두 번 부른다 — 복원 대조(화면 조합만 보여 줌)와 결함 2·3·4 판정(압축 사실표 포함).
# 채점·요청 구성은 ai/evaluation/judge.py에 있고 여기는 파일·호출·집계만 한다.
#
# 실행 (repo 루트, .env에 실제 OPENAI_API_KEY 필요):
#   규모만 보기      : uv run python scripts/finops_judge.py v0.json v1.json --estimate
#   판정             : uv run python scripts/finops_judge.py v0.json v1.json --out judged.json
#   보정 자료 만들기 : ... --calibration-sheet sheet.md --per-case 1 --seed 243
#       원자료마다 케이스당 회차 하나를 뽑아 섞고 출처(어느 판)를 가린 목록을 쓴다.
#       사람이 먼저 매긴 뒤 LLM 라벨(sheet.key.json)과 대조한다 — 순서를 바꾸면 사람이
#       LLM 라벨에 끌린다.
#   판정자 자기일치  : ... --repeat 2   (같은 회차를 두 번 판정해 라벨 일치를 본다)
#
# **실제 모델을 부르므로 과금이 발생한다**(요약 1회차 = 판정 호출 2회 × --repeat). CI 인자에
# 넣지 않으며, 키가 없으면 대체 경로로 떨어지지 않고 거절한다.
#
# 판정자 모델은 finops_eval.py와 같은 환경변수(OPENAI_MODEL 등)로 정해진다 — 기본값은
# 생성 모델과 같은 확정 모델이다. 판정 프롬프트 전문과 모델 원문 응답은 출력하지 않는다
# (ADR-0005 미보존 대상). 남기는 것은 라벨·이유·채점·토큰이다.
# ==============================================================================

from __future__ import annotations

import argparse
import json
import random
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Optional

sys.stdout.reconfigure(encoding="utf-8")

REPO_ROOT = Path(__file__).resolve().parents[1]
for path in (REPO_ROOT / "apps" / "core-api", REPO_ROOT / "packages"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from ai.agent import _incident_payload, prompt_fingerprint  # noqa: E402
from ai.evaluation import (  # noqa: E402
    JUDGE_VERSION,
    DefectJudgement,
    RestorationOutput,
    defect_request,
    judge_fingerprint,
    proposal_cards,
    restoration_request,
    score_restoration,
)
from ai.model_client import AIModelError  # noqa: E402
from ai.openai_client import build_openai_model_client  # noqa: E402
from config import Settings  # noqa: E402
from finops_eval import (  # noqa: E402
    _fixed_set,
    _label,
    _UsageRecordingClient,
    load_cases,
)
from schemas.agents import RunbookCandidateDraft  # noqa: E402

DEFECTS = ("defect_2", "defect_3", "defect_4")


def _load_raw(path: Path) -> dict[str, Any]:
    raw = json.loads(path.read_text("utf-8"))
    raw["_path"] = str(path)
    return raw


def _judgeable(raw: dict[str, Any]) -> list[tuple[int, dict[str, Any]]]:
    """요약이 있는 회차만 — FAILED는 판정할 문장이 없다. 원자료 안의 순번을 보존한다."""
    return [(index, run) for index, run in enumerate(raw["runs"]) if run.get("summary_lines")]


def _cards(run: dict[str, Any]) -> list[dict[str, Any]]:
    drafts = [RunbookCandidateDraft.model_validate(item) for item in run.get("candidates", ())]
    return proposal_cards(drafts)


def _judge_once(client, request, model):
    try:
        return client.complete(request, model).output, None
    except AIModelError as exc:
        return None, type(exc).__name__


def judge_file(
    raw: dict[str, Any], payloads: dict[str, dict[str, Any]], client, repeat: int
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    entries = _judgeable(raw)
    for position, (index, run) in enumerate(entries, start=1):
        payload = payloads[run["case_id"]]
        cards = _cards(run)
        restoration_runs: list[dict[str, Any]] = []
        defect_runs: list[dict[str, Any]] = []
        for _ in range(repeat):
            output, error = _judge_once(
                client, restoration_request(run["summary_lines"], cards), RestorationOutput
            )
            if output is None:
                restoration_runs.append({"error": error})
            else:
                score = score_restoration(output, payload)
                restoration_runs.append(
                    {
                        "output": output.model_dump(),
                        "verdict_ok": score.verdict_ok,
                        "skip_reason_ok": score.skip_reason_ok,
                        "restored": list(score.restored),
                        "missing": list(score.missing),
                        "unexpected": list(score.unexpected),
                    }
                )
            output, error = _judge_once(
                client, defect_request(payload, run["summary_lines"], cards), DefectJudgement
            )
            defect_runs.append({"error": error} if output is None else output.model_dump())
        results.append(
            {
                "case_id": run["case_id"],
                "run_index": index,
                "restoration": restoration_runs,
                "defects": defect_runs,
            }
        )
        first = restoration_runs[0]
        restored = len(first.get("restored", []))
        expected = restored + len(first.get("missing", []))
        flags = "".join(
            "F" if (defect_runs[0].get(name) or {}).get("flagged") else "." for name in DEFECTS
        )
        print(
            f"  [{position}/{len(entries)}] {run['case_id']} #{index}: 복원 {restored}/{expected}"
            f" 결함 {flags}" + (f" [{first['error']}]" if "error" in first else ""),
            flush=True,
        )
    return results


def summarize(label: str, results: list[dict[str, Any]], repeat: int) -> dict[str, Any]:
    judged = len(results)
    verdict_ok = sum(1 for r in results if r["restoration"][0].get("verdict_ok"))
    skip_ok = sum(1 for r in results if r["restoration"][0].get("skip_reason_ok"))
    restored: Counter = Counter()
    expected: Counter = Counter()
    for r in results:
        first = r["restoration"][0]
        for name in first.get("restored", []):
            restored[name] += 1
            expected[name] += 1
        for name in first.get("missing", []):
            expected[name] += 1
    flagged = {
        name: sum(1 for r in results if (r["defects"][0].get(name) or {}).get("flagged"))
        for name in DEFECTS
    }
    errors = sum(1 for r in results if "error" in r["restoration"][0] or "error" in r["defects"][0])

    self_agreement: Optional[dict[str, Any]] = None
    if repeat > 1:
        restoration_same = sum(
            1
            for r in results
            if len({json.dumps(x.get("restored"), sort_keys=True) for x in r["restoration"]}) == 1
        )
        defect_same = {
            name: sum(
                1
                for r in results
                if len({(x.get(name) or {}).get("flagged") for x in r["defects"]}) == 1
            )
            for name in DEFECTS
        }
        self_agreement = {"restoration": restoration_same, "defects": defect_same}

    print("-" * 72)
    print(f"[{label}] 판정 {judged}회차 · 오류 {errors}")
    print(
        f"복원  verdict {verdict_ok}/{judged} · skip_reason {skip_ok}/{judged} · 가른 값 "
        f"{sum(restored.values())}/{sum(expected.values())} ("
        + " · ".join(f"{name} {restored[name]}/{expected[name]}" for name in expected)
        + ")"
    )
    print(
        "결함  "
        + " · ".join(f"{name.replace('defect_', '')} {flagged[name]}/{judged}" for name in DEFECTS)
    )
    if self_agreement:
        print(
            f"판정자 자기일치 (repeat {repeat})  복원 {self_agreement['restoration']}/{judged} · 결함 "
            + " · ".join(
                f"{name.replace('defect_', '')} {self_agreement['defects'][name]}/{judged}"
                for name in DEFECTS
            )
        )
    return {
        "label": label,
        "judged": judged,
        "errors": errors,
        "verdict_ok": verdict_ok,
        "skip_reason_ok": skip_ok,
        "restored": dict(restored),
        "expected": dict(expected),
        "flagged": flagged,
        "self_agreement": self_agreement,
    }


def write_calibration_sheet(
    sources: list[dict[str, Any]],
    judged: dict[str, list[dict[str, Any]]],
    sheet: Path,
    per_case: int,
    seed: int,
) -> None:
    """사람 보정 자료. 출처(어느 판)를 가리고 섞는다. 키 파일에 출처와 LLM 라벨을 둔다."""
    rng = random.Random(seed)
    items: list[dict[str, Any]] = []
    for raw in sources:
        by_case: dict[str, list[tuple[int, dict[str, Any]]]] = {}
        for index, run in _judgeable(raw):
            by_case.setdefault(run["case_id"], []).append((index, run))
        for case_id, runs in by_case.items():
            for index, run in rng.sample(runs, min(per_case, len(runs))):
                llm = next((r for r in judged[raw["_path"]] if r["run_index"] == index), None)
                items.append(
                    {
                        "source": raw["_path"],
                        "label": raw["label"],
                        "case_id": case_id,
                        "run_index": index,
                        "summary_lines": run["summary_lines"],
                        "cards": _cards(run),
                        "llm": (llm or {}).get("defects", [None])[0],
                    }
                )
    rng.shuffle(items)

    lines = [
        "# 요약 3줄 결함 보정 자료",
        "",
        "각 건의 3줄과 조치 카드만 보고 결함 2·3·4를 매긴다. 출처(어느 판)는 가려져 있다.",
        "기준은 apps/core-api/ai/evaluation/summary_defects.md의 항목 본문이다.",
        "",
    ]
    for number, item in enumerate(items, start=1):
        lines.append(f"## {number}")
        for line_no, text in enumerate(item["summary_lines"], start=1):
            lines.append(f"{line_no}. {text}")
        if item["cards"]:
            for card in item["cards"]:
                params = ", ".join(f"{k}={v}" for k, v in card["display_parameters"].items())
                lines.append(
                    f"- 카드: {card['runbook_id']} · {card['target_arn']}"
                    + (f" · {params}" if params else "")
                )
        else:
            lines.append("- 카드: (없음 — NO_PROPOSAL)")
        lines.append("- 결함 2 단정/추정 [ ] · 결함 3 진단↔조치 [ ] · 결함 4 같은 말 [ ]")
        lines.append("- 메모:")
        lines.append("")
    sheet.write_text("\n".join(lines), encoding="utf-8")
    key = sheet.with_suffix(".key.json")
    key.write_text(
        json.dumps(
            [
                {
                    "number": number,
                    "source": item["source"],
                    "label": item["label"],
                    "case_id": item["case_id"],
                    "run_index": item["run_index"],
                    "llm": item["llm"],
                }
                for number, item in enumerate(items, start=1)
            ],
            ensure_ascii=False,
            indent=1,
        ),
        encoding="utf-8",
    )
    print(f"보정 자료 {len(items)}건: {sheet}  (출처·LLM 라벨: {key} — 매기기 전에 열지 않는다)")


def main() -> int:
    parser = argparse.ArgumentParser(description="FinOps 요약 3줄 LLM 판정")
    parser.add_argument(
        "raw", nargs="+", type=Path, help="finops_eval.py --json 원자료 (여러 개 가능)"
    )
    parser.add_argument("--out", type=Path, default=None, help="판정 결과를 쓸 파일 경로")
    parser.add_argument(
        "--repeat", type=int, default=1, help="회차당 판정 반복 수 (자기일치 확인용)"
    )
    parser.add_argument("--estimate", action="store_true", help="호출하지 않고 규모만 출력")
    parser.add_argument(
        "--calibration-sheet", type=Path, default=None, help="사람 보정 자료(markdown) 경로"
    )
    parser.add_argument(
        "--per-case", type=int, default=1, help="보정 자료에 원자료마다 케이스당 담을 회차 수"
    )
    parser.add_argument("--seed", type=int, default=243, help="보정 자료 추출·셔플 seed")
    args = parser.parse_args()

    if args.repeat < 1:
        print("--repeat는 1 이상이어야 합니다", file=sys.stderr)
        return 1

    sources = [_load_raw(path) for path in args.raw]
    # 다른 입력에서 잰 판끼리는 짝 비교가 안 된다 — 골든이 바뀌었으면 이전 판을 새 세트에서
    # 다시 만든 뒤 온다(docs/AI_SUMMARY_BASELINE.md §기준선). CI로는 막지 않고 여기서 거절한다
    if any(raw.get("fixed_set") is None for raw in sources):
        print("원자료에 fixed_set이 없는 것이 있다(구 버전 원자료) — 세트가 같은지는 확인하지 못한다")
    elif len({json.dumps(raw["fixed_set"], sort_keys=True) for raw in sources}) > 1:
        print(
            "원자료의 고정 세트가 서로 다르다 — 다른 입력에서 잰 판끼리는 짝 비교가 안 된다. "
            "이전 판을 새 세트에서 다시 만든 뒤 판정한다(docs/AI_SUMMARY_BASELINE.md §기준선)",
            file=sys.stderr,
        )
        return 1
    # 정답(복원 슬롯)과 사실표는 현재 골든에서 만든다 — 원자료가 다른 입력에서 나왔으면 옛 산출에
    # 지금 정답을 대입하게 된다. 그때 골든을 복원하거나(git) 원자료를 다시 만든 뒤 온다
    current_set = _fixed_set(load_cases())
    for raw in sources:
        fixed_set = raw.get("fixed_set")
        if fixed_set is None:
            continue
        if fixed_set["case_ids"] != current_set["case_ids"] or fixed_set["input_sha256"] != current_set["input_sha256"]:
            print(
                f"{Path(raw['_path']).name}: 원자료의 입력이 현재 골든과 다르다 — 정답·사실표는 현재 골든에서 만들므로 "
                "재채점이 옛 산출에 지금 정답을 대입한다. 그때 골든을 복원하거나 원자료를 다시 만든다",
                file=sys.stderr,
            )
            return 1
    # 승인 스냅샷의 해시와 지표는 같은 문구의 실측을 가리켜야 한다 — 현재 문구로 만든 원자료가
    # 하나도 없으면 문구를 고친 뒤 생성을 건너뛴 것이다(이전 판 원자료만 재판정하는 경우는 예외)
    current_prompt = prompt_fingerprint()
    if not any(raw.get("prompt_sha256") == current_prompt for raw in sources):
        print(
            "현재 프롬프트로 만든 원자료가 없다 — 승인할 문구는 그 문구로 생성·판정한 결과여야 한다. "
            "문구를 고쳤으면 finops_eval.py를 다시 돌린다(구 버전 원자료는 prompt_sha256이 없어 여기 걸린다)"
        )
    outputs = sum(len(_judgeable(raw)) for raw in sources)
    calls = outputs * 2 * args.repeat
    print(
        f"원자료 {len(sources)}개 · 요약 있는 회차 {outputs} · 판정 호출 {calls}회"
        f" (복원 1 + 결함 1) × 반복 {args.repeat}"
    )
    print(f"판정자 판 {JUDGE_VERSION} · 해시 {judge_fingerprint()[:16]}…")
    if args.estimate:
        return 0

    settings = Settings(DATABASE_URL="postgresql+psycopg://unused/unused")
    if not settings.OPENAI_API_KEY or settings.OPENAI_API_KEY == "sk-...":
        print(
            "OPENAI_API_KEY가 없습니다. repo 루트 .env의 OPENAI_API_KEY= 에 실제 키를 넣으세요. "
            "규모만 보려면 --estimate를 쓰세요 — 모델을 부르지 않습니다.",
            file=sys.stderr,
        )
        return 1

    payloads = {case.case_id: _incident_payload(case.graph_input) for case in load_cases()}
    client = _UsageRecordingClient(build_openai_model_client(settings))
    label = _label(settings)
    print(f"판정자: {label}")

    judged: dict[str, list[dict[str, Any]]] = {}
    summaries: list[dict[str, Any]] = []
    for raw in sources:
        print("=" * 72)
        print(f"원자료 {raw['_path']}  ({raw['label']})")
        judged[raw["_path"]] = judge_file(raw, payloads, client, args.repeat)
        summaries.append(summarize(raw["label"], judged[raw["_path"]], args.repeat))
    print("=" * 72)
    print(
        f"토큰  입력 {client.prompt_tokens:,} (캐시 {client.cached_prompt_tokens:,})"
        f" · 출력 {client.completion_tokens:,}"
        f" · 모델 스냅샷 {', '.join(sorted(client.model_snapshots)) or '(응답 없음)'}"
    )

    if args.out:
        args.out.write_text(
            json.dumps(
                {
                    "judge_version": JUDGE_VERSION,
                    "judge_prompt_sha256": judge_fingerprint(),
                    "judge_label": label,
                    "model_snapshots": sorted(client.model_snapshots),
                    "repeat": args.repeat,
                    "sources": [
                        {
                            "path": raw["_path"],
                            "label": raw["label"],
                            "summary": summary,
                            "runs": judged[raw["_path"]],
                        }
                        for raw, summary in zip(sources, summaries)
                    ],
                },
                ensure_ascii=False,
                indent=1,
            ),
            encoding="utf-8",
        )
        print(f"판정 결과: {args.out}")

    if args.calibration_sheet:
        write_calibration_sheet(sources, judged, args.calibration_sheet, args.per_case, args.seed)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
