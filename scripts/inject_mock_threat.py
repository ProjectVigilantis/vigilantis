# ==============================================================================
# [파일 설명]  담당: 김승철 (Data & Rule Engine)
# 모의 위협 주입 — 골든 SecOps 입력을 정형화하고 초기 위험 판정까지 돌려 결과를 보인다.
# (Issue #268)
#
# 실행 (repo 루트):
#   uv run python scripts/inject_mock_threat.py                 # 골든 전량
#   uv run python scripts/inject_mock_threat.py evt_open_ip_001 # 1건(확장자 생략 가능)
#   uv run python scripts/inject_mock_threat.py --json          # 기계가 읽을 형식
#
# 지나는 경로: 골든 입력 → 입력 계약 검증 → security/threat_normalizer.normalize_mock_input
#              → security/risk_evaluator.evaluate_threat
#
# **DB 에 쓰지 않는다.** Incident 생성(ThreatEvent 저장 → Incident → THREAT 근거)은
# incident_intake.create_incident_from_intake 가 할 일인데 아직 NotImplementedError 다
# (#254). 그 구현이 서면 이 스크립트의 다음 줄이 저장이고, 그때가 E2E 설계서
# §대조 필요 1번이 완전히 풀리는 지점이다 — 이 스크립트는 그 앞까지를 실행 가능하게
# 만들어, 판정 결과를 눈으로 확인할 수 있게 한다.
#
# AWS 를 부르지 않으므로 LocalStack·자격증명이 필요 없다. 골든 정답(expected)이 있으면
# 함께 대조해 어긋나면 종료 코드 1 로 알린다 — 정답지와 구현이 갈린 것을 주입 시점에
# 바로 드러내려는 것이다.
# ==============================================================================

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
for _p in (str(REPO_ROOT / "apps" / "core-api"), str(REPO_ROOT / "packages")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from security.risk_evaluator import evaluate_threat  # noqa: E402
from security.threat_normalizer import normalize_mock_input  # noqa: E402

GOLDEN = REPO_ROOT / "datasets" / "golden" / "secops"
INPUT_DIR = GOLDEN / "input"
EXPECTED_DIR = GOLDEN / "expected"


def _load(path: Path) -> dict:
    with path.open(encoding="utf-8") as fp:
        return json.load(fp)


def _expected_for(name: str) -> dict | None:
    """같은 이름의 정답 파일. 없으면 대조하지 않는다."""
    path = EXPECTED_DIR / name
    return _load(path) if path.exists() else None


def _targets(names: list[str]) -> list[Path]:
    if not names:
        return sorted(INPUT_DIR.glob("*.json"))
    paths = []
    for name in names:
        path = INPUT_DIR / (name if name.endswith(".json") else f"{name}.json")
        if not path.exists():
            raise SystemExit(f"입력이 없습니다: {path}")
        paths.append(path)
    return paths


def inject(path: Path) -> dict:
    """1건을 정형화 → 판정한다. 정답이 있으면 대조 결과도 함께 담는다."""
    event = normalize_mock_input(_load(path))
    result = evaluate_threat(event)

    row = {
        "file": path.name,
        "source_event_id": event.source_event_id,
        "event_type": event.event_type.value,
        "target_arn": event.target_arn,
        "threat_event_id": event.threat_event_id,
        "deduplication_key": event.deduplication_key,
        "initial_risk_level": result.initial_risk_level.value,
        "response_mode": result.response_mode.value,
        "reason_codes": sorted(code.value for code in result.reason_codes),
    }

    expected = _expected_for(path.name)
    if expected is not None:
        # 정답 3축 전부 대조 — reason_codes 를 빼면 사유 코드만 어긋난 오구현을 놓친다
        # (박지현 지적, #269 리뷰). row·expected 양쪽을 sorted 로 맞춰 순서 무관 비교.
        want = (
            expected["initial_risk_level"],
            expected["response_mode"],
            sorted(expected["reason_codes"]),
        )
        got = (row["initial_risk_level"], row["response_mode"], row["reason_codes"])
        row["expected_match"] = want == got
        if not row["expected_match"]:
            row["expected"] = {
                "initial_risk_level": want[0],
                "response_mode": want[1],
                "reason_codes": want[2],
            }
    return row


def _use_utf8_output() -> None:
    """Windows 기본 콘솔 코드페이지(cp949)는 이 파일의 한국어·em dash 를 못 낸다.
    팀 개발 환경이 Windows 라 출력 스트림을 UTF-8 로 고정한다."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, OSError):  # 파이프·리다이렉트 등 재설정 불가
            pass


def main() -> int:
    _use_utf8_output()
    parser = argparse.ArgumentParser(description="골든 SecOps 위협을 정형화·판정한다")
    parser.add_argument("names", nargs="*", help="입력 파일명(생략 시 전량)")
    parser.add_argument("--json", action="store_true", help="결과를 JSON 배열로 출력")
    args = parser.parse_args()

    rows = [inject(path) for path in _targets(args.names)]

    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
    else:
        for row in rows:
            mark = "" if row.get("expected_match", True) else "  <-- 정답 불일치"
            print(
                f"[{row['file']}] {row['event_type']} {row['target_arn']}\n"
                f"    위험도={row['initial_risk_level']} 대응={row['response_mode']}"
                f" 사유={','.join(row['reason_codes'])}{mark}\n"
                f"    중복키={row['deduplication_key']}"
            )

    mismatched = [row for row in rows if row.get("expected_match") is False]
    if mismatched:
        print(
            f"\n정답과 어긋난 건이 {len(mismatched)}건 있습니다: "
            f"{', '.join(row['file'] for row in mismatched)}",
            file=sys.stderr,
        )
        return 1
    # 성공 요약은 stderr 로 — --json 의 stdout 은 순수 JSON 이어야 기계가 파싱한다
    # (안성일 지적, #269 리뷰: `--json | json.load` 가 이 줄에 Extra data 로 깨졌다).
    print(f"\n{len(rows)}건 처리 — 정답 대조 통과(대조 대상만).", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
