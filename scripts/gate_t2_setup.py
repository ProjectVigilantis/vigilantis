# ==============================================================================
# [파일 설명]  담당: 박지현 (QA & Scenario)
# 9/11 게이트 T2 전용 — SECOPS Incident 하나에 AI 분석 결과를 넣어
# 가드레일 4단계를 태우고 조치 후보를 EXECUTABLE 로 올린다. (Issue #301)
#
# 실행 (repo 루트, 앱이 떠 있는 상태):
#   uv run python scripts/gate_t2_setup.py --incident-id <SECOPS Incident id> \
#       --target-arn <시드 NACL ARN>
#
# ── 이 파일은 게이트 전용이고, 지울 조건이 정해져 있다 ──────────────────
# **SecOps 그래프가 아직 없어서 AI 출력을 사람이 손으로 작성한다.**
# `ai/agent.py` 에는 FinOps 그래프만 있고 Agent Dispatcher 도 FINOPS 만
# 처리하므로(`agent_dispatcher.py:426-429`), 저장된 SECOPS Incident 는 분석으로
# 이어지지 않는다. 그 빈자리를 이 스크립트가 한 번 메운다.
#
#   **#323(LangGraph SecOps 그래프)이 머지되면 이 파일을 지운다.**
#   그래프가 근거 요약과 조치 후보를 내면 이 스크립트가 하는 일이 통째로
#   프로덕션 경로로 들어간다.
#
# Incident 생성은 **이 스크립트의 몫이 아니다** — #322(PR #325 · 2026-09-11
# 머지)가 실경로를 세웠다. `scripts/inject_mock_threat.py --prepare-inbox` 로
# 관측을 넣으면 앱이 정형화·판정·Intake 를 거쳐 SECOPS Incident 를 저장한다.
# **그 Incident 의 id 를 --incident-id 로 넘긴다.**
#
# ── 왜 add_candidate 로 질러가지 않는가 ───────────────────────────────────
# `incidents_repo.add_candidate` 로 후보를 직접 넣어도 `POST /actions/execute`
# 는 202 를 내고 실행도 SUCCESS 가 된다. **그런데 guardrail_evaluations 가
# 0 행이다** — 가드레일 4단계가 후보 생성과 같은 함수에서 돌기 때문이다
# (`_guard_candidate` `workflows.py:2040`). 그 경로로 넣으면 **T2-4 가 통째로
# 사라진 채 5·6 만 선다.** 넣는 값은 같은데 들어가는 문이 다르다.
#
# ⚠️ 가드레일 ③ ARN Match 는 **DB 에 수집된 자산만** 통과시킨다
# (`_managed_arns` `workflows.py:1946`). 대상 NACL 이 자산이 되려면 대본
# §3-4 「골든 → 실수집 전환」 컷(수집 1회)이 **먼저** 돌아야 한다.
# ==============================================================================

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

# Windows 콘솔(cp949)은 em dash 등 출력 시 UnicodeEncodeError로 죽는다 — UTF-8로 강제
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parents[1]
for _path in (ROOT / "apps" / "core-api", ROOT / "packages"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import workflows  # noqa: E402
from db.repositories import incidents as incidents_repo  # noqa: E402
from db.session import get_session_factory  # noqa: E402
from schemas.agents import AgentGraphOutput, RunbookCandidateDraft  # noqa: E402
from schemas.api.incidents import IncidentCategory, RiskLevel  # noqa: E402
from schemas.incidents import AgentInvocationStatus  # noqa: E402
from schemas.runbooks import RunbookId  # noqa: E402


def _summary(cidr: str) -> list[str]:
    """AI 가 냈어야 할 근거 3줄. 그래프가 없어 사람이 쓴다(#323 이 대신할 자리)."""
    source = cidr.split("/")[0]
    return [
        f"{source}에서 300초간 SSH 인증 실패 120회가 관측됐다.",
        "대상 서브넷의 NACL에 해당 출발지를 막는 규칙이 없다.",
        "출발지 주소만 /32로 막으면 다른 트래픽에 영향이 없다.",
    ]


def main() -> int:
    parser = argparse.ArgumentParser(
        description="게이트 T2 — SECOPS Incident에 AI 분석 결과를 넣어 가드레일을 태운다"
    )
    parser.add_argument("--incident-id", required=True, help="실경로로 저장된 SECOPS Incident id")
    parser.add_argument("--target-arn", required=True, help="조치 대상 NACL ARN(사전 준비 ③이 찍은 값)")
    parser.add_argument("--cidr", default="203.0.113.10/32", help="차단할 출발지")
    parser.add_argument("--rule-number", type=int, default=100, help="NACL 규칙 번호")
    args = parser.parse_args()

    session_factory = get_session_factory()

    with session_factory() as db:
        incident = incidents_repo.get_incident(db, args.incident_id)
        if incident is None:
            raise SystemExit(f"중단: Incident를 찾을 수 없다({args.incident_id})")
        if incident.category is not IncidentCategory.SECOPS:
            raise SystemExit(
                f"중단: SECOPS Incident가 아니다(category={incident.category.value}).\n"
                "  이 스크립트는 T2 전용이다 — FinOps는 프로덕션 경로가 이미 분석한다."
            )
        if not incidents_repo.claim_agent_invocation(
            db, args.incident_id, started_at=datetime.now(timezone.utc)
        ):
            raise SystemExit(
                "중단: AI 호출 Claim에 실패했다 — 이미 분석됐거나 다른 주체가 가져갔다.\n"
                f"  현재 상태: incident={incident.status.value} "
                f"invocation={incident.agent_invocation_status.value}"
            )
        db.commit()
        print(f"[1] Claim: {args.incident_id} ({incident.status.value})")

    output = AgentGraphOutput(
        invocation_status=AgentInvocationStatus.SUCCEEDED,
        summary_lines=_summary(args.cidr),
        reviewed_risk_level=RiskLevel.HIGH,
        candidates=[
            RunbookCandidateDraft(
                runbook_id=RunbookId.RUNBOOK_NACL_ADD_DENY,
                target_arn=args.target_arn,
                parameters={
                    "rule_number": args.rule_number,
                    "cidr_block": args.cidr,
                    "protocol": "tcp",
                },
                # 실경로가 저장해 둔 THREAT 근거를 그대로 쓴다(#322 / PR #325)
                evidence_ids=list(_evidence_ids(session_factory, args.incident_id)),
            )
        ],
    )

    with session_factory() as db:
        outcome = workflows.record_agent_analysis(db, args.incident_id, output)
        print(
            f"[2] 분석 기록: next_status={outcome.next_status.value} "
            f"executable={outcome.executable} rejected={outcome.rejected}"
        )

    with session_factory() as db:
        incident = incidents_repo.get_incident(db, args.incident_id)
        print(
            f"[3] Incident: {incident.status.value} / "
            f"invocation={incident.agent_invocation_status.value}"
        )
        for candidate in incidents_repo.list_candidates(db, args.incident_id):
            print(f"[4] 후보: {candidate.candidate_id} status={candidate.status.value}")

    if outcome.executable == 0:
        print("\n중단: 실행 가능한 후보가 0건이다 — 가드레일에서 거절됐다.", file=sys.stderr)
        print("  ARN Match 거절이면 대본 §3-4 「골든 → 실수집 전환」 컷을 먼저 돌린다.", file=sys.stderr)
        return 1

    print("\n가드레일 기록 확인 —")
    print("  select g.validation_context, g.result, g.steps")
    print("    from guardrail_evaluations g")
    print("    join runbook_candidates c on c.candidate_id = g.candidate_id")
    print(f"   where c.incident_id = '{args.incident_id}';")
    return 0


def _evidence_ids(session_factory, incident_id: str) -> list[str]:
    """실경로가 남긴 근거 id. 없으면 계약이 요구하는 최소 1건을 만들어 쓴다."""
    with session_factory() as db:
        incident = incidents_repo.get_incident(db, incident_id)
        found = list(getattr(incident, "evidence_ids", None) or [])
    return found or [f"gate-t2-{incident_id[:8]}"]


if __name__ == "__main__":
    raise SystemExit(main())
