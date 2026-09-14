"""저장된 SecOps Incident 입력으로 실제 모델 그래프 1회. DB 결과 저장·AWS 실행 없음.

실행 전에 모델 1열·그래프 1회(최대 3회 모델 호출, SDK 재시도 별도)의 비용과
예상 시간을 확인하고 승인을 받는다. 입력 ID는 --incident-id로 명시한다.
"""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT / "apps/core-api", ROOT / "packages"):
    sys.path.insert(0, str(path))

from agent_dispatcher import build_graph_input, _verified_output  # noqa: E402
from ai.agent import run_secops_graph  # noqa: E402
from ai.openai_client import build_openai_model_client  # noqa: E402
from db.session import get_session_factory  # noqa: E402
from schemas.agents import SecOpsGraphInput  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--incident-id", required=True)
    parser.add_argument("--check-input", action="store_true", help="모델 호출 없이 입력 조립만 확인")
    args = parser.parse_args()
    with get_session_factory()() as db:
        graph_input = build_graph_input(db, args.incident_id)
        if not isinstance(graph_input, SecOpsGraphInput):
            parser.error("SECOPS Incident가 필요합니다")
    if args.check_input:
        print(json.dumps({"input_valid": True, "model_calls": 0,
                          "capabilities": [item.runbook_id.value for item in graph_input.capabilities]}))
        return
    output = _verified_output(graph_input,
                              run_secops_graph(graph_input, client=build_openai_model_client()),
                              args.incident_id)
    # 원본 모델 응답·프롬프트 대신 공개 계약으로 검증한 분석 결과만 출력한다.
    print(output.model_dump_json(indent=2))
    if output.invocation_status.value == "FAILED":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
