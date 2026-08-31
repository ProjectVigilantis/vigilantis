# ==============================================================================
# [파일 설명]
# LangGraph FinOps 그래프의 실제 모델 왕복 스모크입니다. (Issue #209)
#
# 합성 인시던트 1건을 그래프에 넣어 실제 GPT-4o를 부르고, 나온 결과가 출력 계약
# (packages/schemas/agents.py)을 통과하는지 본다. 확인하는 것은 **왕복이 서느냐**이며
# 요약 문장의 품질 판정은 프롬프트 카드 몫이다.
#
# **주입 Test Double 테스트로는 이 경로를 덮을 수 없다.** 테스트는 사람이 만든 후보를
# 돌려주므로, 그래프가 모델에게 필요한 정보를 주지 않아 모델이 그 후보를 만들어 낼 수
# 없는 상황을 드러내지 못한다(#209에서 실제로 그 결함이 이 스크립트로 잡혔다).
#
# 실행 (repo 루트, .env에 실제 OPENAI_API_KEY 필요):
#   PowerShell: uv run python scripts/smoke_finops_graph.py
#   bash      : uv run python scripts/smoke_finops_graph.py
#
# **실제 모델을 부르므로 과금이 발생한다**(1회 실행 = 모델 호출 2회). CI 인자에 넣지
# 않으며, 키가 없으면 대체 경로로 떨어지지 않고 거절한다 — Fake로 돌아 초록불이 뜨면
# 실경로를 확인한 것으로 오독된다.
#
# Prompt 전문과 모델 원문 응답은 출력하지 않는다(ADR-0005 미보존 대상). 남기는 것은
# 최종 요약 3줄·후보·토큰 사용량이며, 그 셋은 저장·노출 대상이다.
# ==============================================================================

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
for path in (REPO_ROOT / "apps" / "core-api", REPO_ROOT / "packages"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from ai.agent import run_finops_graph  # noqa: E402
from ai.model_client import AIModelRequest, AIModelResponse, build_outbound_payload  # noqa: E402
from ai.openai_client import build_openai_model_client  # noqa: E402
from config import Settings  # noqa: E402
from schemas.agents import FinOpsGraphInput  # noqa: E402

# ------------------------------------------------------------------------------
# 합성 입력 — 저평가된 EC2 1건 + 거기 붙은 EBS 볼륨
# ------------------------------------------------------------------------------
# 실제 수집 자산이 아니다. 대상 ARN·자원 ID는 문서 예시용 값이며, rule_evaluation의
# reason에는 전송 직전 마스킹이 도는지 보려고 자격증명 형태 문자열을 일부러 심었다.

REGION = "ap-northeast-2"
ACCOUNT = "123456789012"
EC2_ARN = f"arn:aws:ec2:{REGION}:{ACCOUNT}:instance/i-0abc123456789def0"
VOLUME_ARN = f"arn:aws:ec2:{REGION}:{ACCOUNT}:volume/vol-0abc123456789def0"
PLANTED_SECRET = "AKIAIOSFODNN7EXAMPLE"

RULE_EVALUATION = {
    "asset_arn": EC2_ARN,
    "collection_run_id": "run-smoke-001",
    "evaluation_status": "COMPLETED",
    "verdict": "COST_CANDIDATE",
    "health_score": 3,
    "reason": f"3일 평균 CPU 3%, 최대 7% (수집 키 {PLANTED_SECRET})",
    "evaluated_at": "2026-08-31T09:00:00Z",
}

GRAPH_INPUT = FinOpsGraphInput.model_validate(
    {
        "domain": "FINOPS",
        "incident_id": "inc-smoke-001",
        "asset_context": {
            "arn": EC2_ARN,
            "resource_id": "i-0abc123456789def0",
            "asset_type": "EC2",
            "resource_role": "PRIMARY",
            "account_id": ACCOUNT,
            "region": REGION,
            "state": "running",
            "spec": {"instance_type": "t3.xlarge"},
            "relationships": [{"relation_type": "ATTACHED_TO", "target_arn": VOLUME_ARN}],
            "evaluation_status": "COMPLETED",
            "health_score": 3,
            "verdict": "COST_CANDIDATE",
            "collected_at": "2026-08-31T09:00:00Z",
        },
        "rule_evaluation": RULE_EVALUATION,
        "evidences": [
            {
                "evidence_id": "ev-0001",
                "evidence_type": "RULE",
                "content": {"evaluation": RULE_EVALUATION},
            }
        ],
        "capabilities": [
            {
                "runbook_id": "RUNBOOK_EC2_RIGHTSIZING",
                "purpose": "과대 스펙 EC2 다운사이징",
                "allowed_target_asset_types": ["EC2"],
            },
            {
                "runbook_id": "RUNBOOK_EBS_DELETE_UNATTACHED",
                "purpose": "미연결 EBS 볼륨 삭제",
                "allowed_target_asset_types": ["EBS"],
            },
        ],
    }
)


class _RecordingClient:
    """실제 경계 구현에 위임하면서 전송될 페이로드와 호출 메타만 따로 모은다."""

    def __init__(self, inner) -> None:
        self._inner = inner
        self.sent: list[dict] = []
        self.usage: list[tuple[str, object]] = []

    def complete(self, request: AIModelRequest, response_model) -> AIModelResponse:
        self.sent.append(build_outbound_payload(request))
        response = self._inner.complete(request, response_model)
        self.usage.append((response.model, response.usage))
        return response


def _settings() -> Settings:
    # 이 스크립트는 DB를 쓰지 않지만 Settings가 DATABASE_URL을 필수로 받는다. 자리값을
    # 넘겨 OPENAI_* 만 .env·환경변수에서 읽게 한다 — 연결은 열지 않는다.
    return Settings(DATABASE_URL="postgresql+psycopg://unused/unused")


def main() -> int:
    settings = _settings()
    if not settings.OPENAI_API_KEY or settings.OPENAI_API_KEY == "sk-...":
        print(
            "OPENAI_API_KEY가 없습니다. repo 루트 .env의 OPENAI_API_KEY= 에 실제 키를 넣으세요"
            "(.env.example 참고). 환경변수로 넘겨도 됩니다.\n"
            "실제 모델을 부르지 않고 출력 계약과 세 갈래 분기만 보려면 테스트를 돌리세요:\n"
            "  uv run pytest apps/core-api/ai/tests/test_finops_graph.py -q",
            file=sys.stderr,
        )
        return 1

    client = _RecordingClient(build_openai_model_client(settings))
    output = run_finops_graph(GRAPH_INPUT, client=client)

    print("=" * 68)
    print(f"invocation_status  : {output.invocation_status.value}")
    print(f"모델 호출 횟수     : {len(client.sent)}")
    print(f"reviewed_risk_level: {output.reviewed_risk_level}")
    print("-" * 68)
    for index, line in enumerate(output.summary_lines, 1):
        print(f"요약 {index}: {line}")
    if not output.summary_lines:
        print("(요약 없음 — FAILED)")
    print("-" * 68)
    for candidate in output.candidates:
        print(f"후보: {candidate.runbook_id.value}")
        print(f"  target_arn  : {candidate.target_arn}")
        print(f"  parameters  : {candidate.parameters.model_dump()}")
        print(f"  evidence_ids: {candidate.evidence_ids}")
    if not output.candidates:
        print("(후보 없음)")
    print("-" * 68)
    leaked = [s for s in client.sent if PLANTED_SECRET in s["user_json"]]
    redacted = [s for s in client.sent if "[REDACTED]" in s["user_json"]]
    print(
        f"마스킹: 심어 둔 자격증명 유출 {len(leaked)}건 "
        f"| [REDACTED] 치환된 호출 {len(redacted)}/{len(client.sent)}건"
    )
    for model, usage in client.usage:
        print(
            f"호출 메타: {model} prompt={usage.prompt_tokens} "
            f"completion={usage.completion_tokens} total={usage.total_tokens}"
        )
    print("=" * 68)

    # 마스킹이 뚫렸으면 결과와 무관하게 실패로 끝낸다 — 전송이 이미 일어난 뒤라 경고가
    # 조용히 스크롤 밖으로 밀려나면 안 된다
    return 1 if leaked else 0


if __name__ == "__main__":
    raise SystemExit(main())
