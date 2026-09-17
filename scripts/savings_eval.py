"""독립 단가·서비스 계측 JSON의 절감 예상만 채점한다. 네트워크·모델 호출은 없다."""

# 저장소 경로를 등록한 뒤 실제 채점 구현을 가져온다.
# ruff: noqa: E402

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "core-api"))
sys.path.insert(0, str(ROOT / "packages"))

from ai.evaluation.savings.scoring import score_report

SPEC = ROOT / "apps/core-api/ai/evaluation/savings/spec.json"


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        epilog=("이 채점기는 모델 호출 0회다. 생성 예산: 요약 60실행 최대 120호출, "
                "단가만 60실행 최대 60호출, Workflow 전체는 통과 RIGHTSIZING당 최대 3호출. "
                "현재 finops_eval.py의 요약 전용 결과에는 절감 예상이 없다."),
    )
    parser.add_argument("input", type=Path, help="절감 예상이 포함된 계측 원자료 JSON")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    source = args.input.read_bytes()
    specification = SPEC.read_bytes()
    report = score_report(json.loads(source), json.loads(specification))
    report["source_sha256"] = hashlib.sha256(source).hexdigest()
    report["spec_sha256"] = hashlib.sha256(specification).hexdigest()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: report[k] for k in (
        "complete", "passed", "runs", "estimated", "not_estimated", "status_counts",
    )}, ensure_ascii=False))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
