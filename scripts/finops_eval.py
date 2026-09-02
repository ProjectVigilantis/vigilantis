# ==============================================================================
# [파일 설명]
# FinOps 그래프 응답 품질 계측 실행기입니다. (Issue #237)
#
# 고정 입력 세트(Golden Dataset FinOps의 낭비 후보)를 같은 값으로 N회 넣어, 한 조합
# (모델 + 파라미터)의 지표를 표 한 줄로 낸다.
#   FAILED(계약 위반 · 호출 실패) · NO_PROPOSAL · 사실 정합성 실패 · 필드별 일치율 ·
#   토큰(캐시분 포함) · 추정 비용 · 응답이 밝힌 모델 스냅샷
#
# 조합은 **환경변수로 바꾼다** — OPENAI_MODEL·OPENAI_TEMPERATURE·OPENAI_REASONING_EFFORT.
# 코드를 고치지 않고 같은 경로로 여러 조합을 재기 위한 것이며, 모델 계열마다 받는
# 파라미터가 달라 안 쓰는 노브는 비워 둔다(apps/core-api/config.py).
#
# 실행 (repo 루트, .env에 실제 OPENAI_API_KEY 필요):
#   예상 호출 수만 보기 : uv run python scripts/finops_eval.py --estimate
#   조합 수용 프로브    : uv run python scripts/finops_eval.py --case A1 --repeats 1
#   기본 조합(luna low) : uv run python scripts/finops_eval.py --repeats 4
#   추론량 바꾸기       : OPENAI_REASONING_EFFORT=high uv run python scripts/finops_eval.py --repeats 4
#   다른 추론 모델      : OPENAI_MODEL=gpt-5.6-terra uv run python scripts/finops_eval.py --repeats 4
#   (PowerShell은 `$env:OPENAI_REASONING_EFFORT='high'; uv run python ...`)
#
# temperature 계열(gpt-4o 등)은 환경변수만으로 재지 못한다 — reasoning_effort 기본값을
# 끄는 환경변수 표기가 없어서, apps/core-api/config.py의 기본값 두 줄을 함께 고쳐야 한다.
#
# **실제 모델을 부르므로 과금이 발생한다**(케이스 1건 = 모델 호출 2회). CI 인자에 넣지
# 않으며, 키가 없으면 대체 경로로 떨어지지 않고 거절한다 — Fake로 돌아 초록불이 뜨면
# 실경로를 확인한 것으로 오독된다(scripts/smoke_finops_graph.py와 같은 규약).
#
# 단가는 코드에 박지 않고 실행 인자로 받는다(--price-in·--price-out). 값이 계속 낡고,
# LLM 운영비 지표를 제품에서 빼고 토큰 사용량으로 오프라인 추정한다는 ADR-0005
# §Consequences를 따르기 위해서다. 인자를 주지 않으면 비용 칸을 비운다.
#
# Prompt 전문과 모델 원문 응답은 출력하지 않는다(ADR-0005 미보존 대상). 남기는 것은
# 최종 요약 3줄·후보·토큰 사용량이다.
#
# 종료 코드: 계측이 한 바퀴 끝나면 0이다. 지표가 나쁜 것은 실패가 아니라 결과다 —
# 키 누락·인자 오류처럼 계측 자체가 서지 못한 경우만 1이다.
# ==============================================================================

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Optional

sys.stdout.reconfigure(encoding="utf-8")

REPO_ROOT = Path(__file__).resolve().parents[1]
for path in (REPO_ROOT / "apps" / "core-api", REPO_ROOT / "packages"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from ai.agent import _incident_payload, run_finops_graph  # noqa: E402
from ai.evaluation import (  # noqa: E402
    CaseRun,
    EvalCase,
    build_column_report,
    check_summary_facts,
    finops_cases,
)
from ai.model_client import AIModelError, AIModelRequest, AIModelResponse  # noqa: E402
from ai.openai_client import build_openai_model_client  # noqa: E402
from config import Settings  # noqa: E402
from schemas.assets import AssetInventory  # noqa: E402

GOLDEN = REPO_ROOT / "datasets" / "golden" / "finops"
INVENTORY_IDS = ("001", "002", "003")


class _UsageRecordingClient:
    """실제 경계 구현에 위임하면서 호출 메타만 모은다.

    경계 예외는 그대로 올려 보낸다 — 그래프가 FAILED로 접는 것이 정상 경로다. 여기서는
    무엇이 실패했는지 사람이 볼 수 있게 예외 **클래스 이름만** 남긴다(ADR-0005).
    """

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.cached_prompt_tokens = 0
        self.calls = 0
        self.errors: list[str] = []
        # 직전 reset 이후 이 회차에서 경계가 세운 예외 클래스. 그래프는 이것을 삼켜
        # FAILED로 접기 때문에, 여기서 잡아 두지 않으면 "모델이 계약을 어겼다"와
        # "왕복이 못 섰다"가 결과에서 같은 값이 된다
        self.last_error: Optional[str] = None
        self.last_error_phase: Optional[str] = None
        # 응답이 밝힌 스냅샷 ID(별칭이 아니라 날짜가 붙은 실제 버전). 별칭으로 부른
        # 모델이 실제로 무엇이었는지 없이는 나중에 같은 표를 재현할 수 없다
        self.model_snapshots: set[str] = set()

    def reset(self) -> None:
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.cached_prompt_tokens = 0
        self.calls = 0
        self.last_error: Optional[str] = None
        self.last_error_phase: Optional[str] = None

    def complete(self, request: AIModelRequest, response_model) -> AIModelResponse:
        self.calls += 1
        try:
            response = self._inner.complete(request, response_model)
        except AIModelError as exc:
            self.errors.append(type(exc).__name__)
            self.last_error = type(exc).__name__
            self.last_error_phase = exc.phase
            # 응답을 받은 실패(refusal·응답 계약 위반)는 토큰이 이미 발생했다 —
            # 예외에 실려 온 usage를 집계에 보존한다. 버리면 비용이 적게 잡힌다
            if exc.usage is not None:
                self.prompt_tokens += exc.usage.prompt_tokens
                self.completion_tokens += exc.usage.completion_tokens
                self.cached_prompt_tokens += exc.usage.cached_prompt_tokens
            raise
        self.prompt_tokens += response.usage.prompt_tokens
        self.completion_tokens += response.usage.completion_tokens
        self.cached_prompt_tokens += response.usage.cached_prompt_tokens
        self.model_snapshots.add(response.model)
        return response


def load_cases() -> list[EvalCase]:
    cases: list[EvalCase] = []
    for inventory_id in INVENTORY_IDS:
        inventory = AssetInventory.model_validate(
            json.loads(
                (GOLDEN / "input" / f"asset_inventory_{inventory_id}.json").read_text("utf-8")
            )
        )
        expected = json.loads(
            (GOLDEN / "expected" / f"asset_inventory_{inventory_id}.json").read_text("utf-8")
        )
        cases.extend(finops_cases(inventory, expected))
    return cases


def _label(settings: Settings) -> str:
    knobs = []
    if settings.OPENAI_TEMPERATURE is not None:
        knobs.append(f"temperature={settings.OPENAI_TEMPERATURE:g}")
    if settings.OPENAI_REASONING_EFFORT is not None:
        knobs.append(f"reasoning_effort={settings.OPENAI_REASONING_EFFORT}")
    return f"{settings.OPENAI_MODEL} ({' · '.join(knobs) if knobs else '노브 미지정'})"


def _estimate(cases: list[EvalCase], repeats: int) -> None:
    """호출을 하지 않고 규모만 낸다 — 실행 승인을 받을 때 쓰는 숫자다.

    토큰이 아니라 문자 수를 낸다. 토크나이저가 저장소에 없어 토큰으로 바꾸면 그 자체가
    추정 위의 추정이 된다 — 실제 토큰은 한 바퀴 돌린 뒤 사용량으로 확인한다.
    """
    total_chars = 0
    for case in cases:
        payload = _incident_payload(case.graph_input)
        total_chars += len(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    print(f"케이스 {len(cases)}건 × 반복 {repeats}회 = 실행 {len(cases) * repeats}회")
    print(f"모델 호출 {len(cases) * repeats * 2}회 (요약 1 + 후보 1)")
    print(f"요약 호출 입력 문자 수 합계 {total_chars:,}자 (반복 1회 기준)")
    print("후보 호출 입력은 여기에 capabilities·요약 3줄이 더 붙는다")


def _cost(tokens_in: int, tokens_out: int, price_in: Optional[float], price_out: Optional[float]):
    """캐시를 빼지 않은 비용. **모델 선택 판단에 쓸 값은 이쪽이다** — 실경로는 인시던트
    마다 입력이 달라 캐시가 걸리지 않기 때문이다. 계측이 같은 프롬프트를 N회 보내며 받은
    캐시 할인은 이 세션의 청구액을 낮출 뿐 프로덕션 비용을 대표하지 않는다."""
    if price_in is None or price_out is None:
        return None
    return tokens_in / 1_000_000 * price_in + tokens_out / 1_000_000 * price_out


def run(
    settings: Settings, cases: list[EvalCase], repeats: int
) -> tuple[list[CaseRun], _UsageRecordingClient]:
    client = _UsageRecordingClient(build_openai_model_client(settings))
    runs: list[CaseRun] = []
    for case in cases:
        payload = _incident_payload(case.graph_input)
        for attempt in range(1, repeats + 1):
            client.reset()
            output = run_finops_graph(case.graph_input, client=client)
            runs.append(
                CaseRun(
                    case_id=case.case_id,
                    output=output,
                    prompt_tokens=client.prompt_tokens,
                    completion_tokens=client.completion_tokens,
                    fact=check_summary_facts(payload, output.summary_lines),
                    cached_prompt_tokens=client.cached_prompt_tokens,
                    error=client.last_error,
                    error_phase=client.last_error_phase,
                )
            )
            print(
                f"  {case.case_id} {attempt}/{repeats}: {output.invocation_status.value}"
                f" 후보 {len(output.candidates)}건"
                + (f" [{client.last_error}]" if client.last_error else ""),
                flush=True,
            )
    return runs, client


def main() -> int:
    parser = argparse.ArgumentParser(description="FinOps 그래프 응답 품질 계측")
    parser.add_argument("--repeats", type=int, default=4, help="케이스당 반복 실행 수 (기본 4)")
    parser.add_argument(
        "--case",
        action="append",
        dest="cases",
        metavar="CASE_ID",
        help="이 케이스만 실행(반복 지정 가능). 미지정이면 전체 — 조합 프로브는 --case 1건 · --repeats 1로 돌린다",
    )
    parser.add_argument("--estimate", action="store_true", help="호출하지 않고 규모만 출력")
    parser.add_argument("--price-in", type=float, default=None, help="입력 100만 토큰당 단가(USD)")
    parser.add_argument("--price-out", type=float, default=None, help="출력 100만 토큰당 단가(USD)")
    parser.add_argument("--json", type=Path, default=None, help="원자료를 쓸 파일 경로")
    args = parser.parse_args()

    if args.repeats < 1:
        print("--repeats는 1 이상이어야 합니다", file=sys.stderr)
        return 1

    cases = load_cases()
    if args.cases:
        known = {case.case_id for case in cases}
        unknown = set(args.cases) - known
        if unknown:
            print(
                f"없는 케이스입니다: {', '.join(sorted(unknown))}"
                f" (가능: {', '.join(case.case_id for case in cases)})",
                file=sys.stderr,
            )
            return 1
        cases = [case for case in cases if case.case_id in set(args.cases)]
    if args.estimate:
        _estimate(cases, args.repeats)
        return 0

    # DB를 쓰지 않지만 Settings가 DATABASE_URL을 필수로 받는다. 자리값을 넘겨
    # OPENAI_* 만 .env·환경변수에서 읽게 한다 — 연결은 열지 않는다
    settings = Settings(DATABASE_URL="postgresql+psycopg://unused/unused")
    if not settings.OPENAI_API_KEY or settings.OPENAI_API_KEY == "sk-...":
        print(
            "OPENAI_API_KEY가 없습니다. repo 루트 .env의 OPENAI_API_KEY= 에 실제 키를 넣으세요"
            "(.env.example 참고). 환경변수로 넘겨도 됩니다.\n"
            "규모만 보려면 --estimate를 쓰세요 — 모델을 부르지 않습니다.",
            file=sys.stderr,
        )
        return 1

    label = _label(settings)
    print("=" * 72)
    print(f"조합: {label}")
    print(f"케이스 {len(cases)}건 × 반복 {args.repeats}회")
    print("-" * 72)
    runs, client = run(settings, cases, args.repeats)
    report = build_column_report(label, runs)
    cost = _cost(report.prompt_tokens, report.completion_tokens, args.price_in, args.price_out)

    print("=" * 72)
    print(f"실행 {report.runs}회 = 케이스 {report.case_count} × 반복 {report.repeats}")
    print(
        f"FAILED        {report.failed}"
        f"  (계약 위반 {report.failed_contract} · 호출 실패 {report.failed_transport}"
        f" · 요청 거절 {report.failed_request})"
    )
    print(f"NO_PROPOSAL   {report.no_proposal}  ({report.no_proposal_rate:.1%})")
    print(f"사실 정합성    실패 {report.fact_failed} / 판정 {report.fact_checked}")
    print(
        f"필드 안정도    {report.stable_slots}/{report.field_slots}"
        f"  ({report.field_stability:.1%})"
    )
    print(f"흔들린 필드   {', '.join(report.unstable_field_names) or '없음'}")
    print(
        f"토큰          입력 {report.prompt_tokens:,}"
        f" (캐시 {report.cached_prompt_tokens:,}) · 출력 {report.completion_tokens:,}"
    )
    print(f"추정 비용     {f'${cost:.4f}' if cost is not None else '(단가 미지정)'}")
    print(f"모델 스냅샷   {', '.join(sorted(client.model_snapshots)) or '(응답 없음)'}")
    if client.errors:
        print(f"모델 호출 실패 {len(client.errors)}건: {', '.join(sorted(set(client.errors)))}")

    if report.unstable:
        print("-" * 72)
        print("흔들린 값 (케이스별)")
        for case_id, agreements in report.unstable.items():
            for agreement in agreements:
                print(f"  {case_id} {agreement.field}: {' | '.join(agreement.values)}")

    violations = [run_ for run_ in runs if not run_.fact.passed]
    if violations:
        print("-" * 72)
        print("입력에 없는 값을 인용한 회차")
        for run_ in violations:
            parts = []
            if run_.fact.identifier_violations:
                parts.append(f"식별자 {', '.join(run_.fact.identifier_violations)}")
            if run_.fact.number_violations:
                parts.append(f"숫자 {', '.join(run_.fact.number_violations)}")
            print(f"  {run_.case_id}: {' / '.join(parts)}")

    # 파생 수치는 실패가 아니라 "입력에서 계산해 냈다"는 표시다. 세어 두는 것은 이것이
    # 조합을 가르는 실질 차이이기 때문이다 — 관측 기간을 사람 말로 옮기는 조합과
    # 아예 말하지 않는 조합이 여기서 갈린다
    derived = sorted({value for run_ in runs for value in run_.fact.derived_numbers})
    if derived:
        cited = sum(1 for run_ in runs if run_.fact.derived_numbers)
        print("-" * 72)
        print(f"입력에서 계산해 낸 수치 (실패 아님)  {', '.join(derived)}  — {cited}회차")

    print("-" * 72)
    print("PR 본문 표 한 줄:")
    print(
        f"| {label} | {report.failed_contract} | {report.failed_transport} | {report.no_proposal} |"
        f" {report.fact_checked - report.fact_failed}/{report.fact_checked} |"
        f" {report.field_stability:.0%} |"
        f" {report.prompt_tokens + report.completion_tokens:,} |"
        f" {f'${cost:.3f}' if cost is not None else '—'} |"
    )
    print("=" * 72)

    if args.json:
        args.json.write_text(
            json.dumps(
                {
                    "label": label,
                    "model_snapshots": sorted(client.model_snapshots),
                    "repeats": report.repeats,
                    "runs": [
                        {
                            "case_id": run_.case_id,
                            "invocation_status": run_.output.invocation_status.value,
                            "summary_lines": list(run_.output.summary_lines),
                            "candidates": [
                                candidate.model_dump(mode="json")
                                for candidate in run_.output.candidates
                            ],
                            "prompt_tokens": run_.prompt_tokens,
                            "completion_tokens": run_.completion_tokens,
                            "cached_prompt_tokens": run_.cached_prompt_tokens,
                            "error": run_.error,
                            "error_phase": run_.error_phase,
                            "identifier_violations": list(run_.fact.identifier_violations),
                            "number_violations": list(run_.fact.number_violations),
                            "instance_types_outside_input": list(
                                run_.fact.instance_types_outside_input
                            ),
                            "derived_numbers": list(run_.fact.derived_numbers),
                        }
                        for run_ in runs
                    ],
                },
                ensure_ascii=False,
                indent=1,
            ),
            encoding="utf-8",
        )
        print(f"원자료: {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
