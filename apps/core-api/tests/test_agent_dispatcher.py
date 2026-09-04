"""Agent Dispatcher 통합 테스트 — 실제 PostgreSQL 필요(미기동 시 skip). (Issue #285)

**이 파일이 지키는 것은 오케스트레이션이다** — 선점·트랜잭션 경계·계약 검증 ⓐⓑ·
상태 전이·회수·발행. 아래 넷은 다른 자리가 이미 지키므로 여기서 다시 보지 않는다.
  - 그래프 내부 분기(SUCCEEDED·NO_PROPOSAL·FAILED)  → ai/tests/test_finops_graph.py
  - 가드레일 단계별 판정과 거절 사유              → ai/tests/test_guardrail_steps.py
  - AWS Dry-Run 판정                              → services/tests/test_precheck_dispatch.py
  - 계약 불변식(출력 3갈래·근거 유형)             → packages/schemas/tests
그래서 가드레일 ④만 Test Double로 바꾸고 ①②③은 실제로 돌린다 — ③이 대조하는 자산 행도
실제로 적재한다. 모델은 FakeAIModelClient로만 부른다(실호출 0회).
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

CORE_API = Path(__file__).resolve().parents[1]
REPO_ROOT = Path(__file__).resolve().parents[3]
for p in (str(CORE_API), str(REPO_ROOT / "packages")):
    if p not in sys.path:
        sys.path.insert(0, p)

import agent_dispatcher  # noqa: E402
import incident_intake  # noqa: E402
import workflows  # noqa: E402
from ai.agent import (  # noqa: E402
    CandidateProposalOutput,
    EvidenceSummaryOutput,
    ProposedCandidate,
)
from ai.model_client import FakeAIModelClient  # noqa: E402
from db.repositories import assets as assets_repo  # noqa: E402
from db.repositories import guardrails as guardrails_repo  # noqa: E402
from db.repositories import incidents as incidents_repo  # noqa: E402
from schemas.agents import AgentGraphOutput, RunbookCandidateDraft  # noqa: E402
from schemas.api.assets import AssetType  # noqa: E402
from schemas.api.incidents import IncidentStatus, RiskLevel  # noqa: E402
from schemas.candidates import CandidateStatus  # noqa: E402
from schemas.evidence import EvidenceType  # noqa: E402
from schemas.guardrails import GuardrailStep  # noqa: E402
from schemas.incidents import (  # noqa: E402
    AGENT_TERMINAL_STATUSES,
    AgentInvocationStatus,
)
from schemas.intake import FinOpsIncidentIntake, SecOpsIncidentIntake  # noqa: E402
from schemas.precheck import (  # noqa: E402
    PrecheckOutcome,
    PrecheckReasonCode,
    VerificationMethod,
    build_verification_summary,
)
from schemas.runbooks import RunbookId  # noqa: E402

ACCOUNT = "123456789012"
REGION = "ap-northeast-2"
INSTANCE_ID = "i-0abc123456789def0"
GROUP_ID = "sg-0abc123456789def0"
EC2_ARN = f"arn:aws:ec2:{REGION}:{ACCOUNT}:instance/{INSTANCE_ID}"
SG_ARN = f"arn:aws:ec2:{REGION}:{ACCOUNT}:security-group/{GROUP_ID}"
RUN_ID = "1f2e3d4c-5b6a-4978-8899-aabbccddee00"
COLLECTED_AT = "2026-09-02T09:00:00Z"
EVALUATED_AT = "2026-09-02T09:00:05Z"

SUMMARY = EvidenceSummaryOutput(
    situation="t3.xlarge 인스턴스의 3일 평균 CPU가 4.9%다.",
    analysis="규칙 판정은 COST_CANDIDATE이고 health_score는 4다.",
    recommendation="t3.medium으로 다운사이징한다.",
)


# ------------------------------------------------------------------------------
# 시드
# ------------------------------------------------------------------------------


def _ec2_asset(**over):
    base = {
        "arn": EC2_ARN,
        "resource_id": INSTANCE_ID,
        "asset_type": "EC2",
        "resource_role": "PRIMARY",
        "name": "batch-dev",
        "account_id": ACCOUNT,
        "region": REGION,
        "state": "running",
        "spec": {"instance_type": "t3.xlarge"},
        "relationships": [],
        "evaluation_status": "COMPLETED",
        "health_score": 4,
        "verdict": "COST_CANDIDATE",
        "skip_reason_code": None,
        "collected_at": COLLECTED_AT,
    }
    base.update(over)
    return base


def _sg_asset():
    return {
        "arn": SG_ARN,
        "resource_id": GROUP_ID,
        "asset_type": "SG",
        "resource_role": "PRIMARY",
        "name": "orphan-sg",
        "account_id": ACCOUNT,
        "region": REGION,
        "state": None,
        "spec": {"attached": False, "open_to_world": []},
        "relationships": [],
        "evaluation_status": "COMPLETED",
        "health_score": None,
        "verdict": "UNUSED",
        "skip_reason_code": None,
        "collected_at": COLLECTED_AT,
    }


def _intake(asset, *, verdict="COST_CANDIDATE", health_score=4) -> FinOpsIncidentIntake:
    return FinOpsIncidentIntake.model_validate(
        {
            "asset_snapshot": {"collection_run_id": RUN_ID, "asset": asset},
            "rule_evaluation": {
                "asset_arn": asset["arn"],
                "collection_run_id": RUN_ID,
                "evaluation_status": "COMPLETED",
                "verdict": verdict,
                "health_score": health_score,
                "skip_reason_code": None,
                "reason": f"{asset['asset_type']} rule evaluation: verdict={verdict}",
                "evaluated_at": EVALUATED_AT,
            },
        }
    )


def _secops_intake() -> SecOpsIncidentIntake:
    return SecOpsIncidentIntake.model_validate(
        {
            "title": "SSH 브루트포스 시도",
            "threat_event": {
                "threat_event_id": "9a8b7c6d-5e4f-4a3b-8c2d-1e0f9a8b7c60",
                "source_event_id": "evt-mock-001",
                "event_type": "SSH_BRUTE_FORCE",
                "target_arn": EC2_ARN,
                "occurred_at": "2026-09-02T09:00:00Z",
                "payload": {
                    "source_ip": "203.0.113.10",
                    "failed_attempt_count": 120,
                    "window_seconds": 300,
                },
                "deduplication_key": "SSH_BRUTE_FORCE:i-0abc123456789def0:203.0.113.10",
                "collected_at": "2026-09-02T09:00:01Z",
            },
            "initial_risk": {
                "threat_event_id": "9a8b7c6d-5e4f-4a3b-8c2d-1e0f9a8b7c60",
                "initial_risk_level": "HIGH",
                "response_mode": "PRE_MITIGATION_0_5S",
                "reason_codes": ["RISK_SSH_BRUTEFORCE"],
            },
        }
    )


def _seed_asset_row(db, *, arn, asset_type, resource_id, spec, state):
    """③ ARN Match가 대조하는 자산 행. 근거와 별개 계층이라 따로 적재한다."""
    run = assets_repo.start_collection_run(
        db,
        account_id=ACCOUNT,
        region=REGION,
        mode="localstack",
        lookback_days=3,
        period_seconds=3600,
    )
    assets_repo.upsert_asset(
        db,
        arn=arn,
        asset_type=asset_type,
        resource_id=resource_id,
        account_id=ACCOUNT,
        region=REGION,
        spec=spec,
        collection_run_id=run.collection_run_id,
        collected_at=datetime.now(timezone.utc),
        state=state,
    )
    db.commit()


def _pending_incident(db, *, sg=False) -> str:
    """ANALYZING · PENDING 인시던트 1건 + 그 자산 행. incident_id를 돌려준다."""
    if sg:
        _seed_asset_row(
            db,
            arn=SG_ARN,
            asset_type=AssetType.SG,
            resource_id=GROUP_ID,
            spec={"attached": False, "open_to_world": []},
            state=None,
        )
        intake = _intake(_sg_asset(), verdict="UNUSED", health_score=None)
    else:
        _seed_asset_row(
            db,
            arn=EC2_ARN,
            asset_type=AssetType.EC2,
            resource_id=INSTANCE_ID,
            spec={"instance_type": "t3.xlarge"},
            state="running",
        )
        intake = _intake(_ec2_asset())
    return incident_intake.create_incident_from_intake(db, intake).incident_id


def _rule_evidence_id(db, incident_id: str) -> str:
    return next(
        row.evidence_id
        for row in incidents_repo.list_evidence(db, incident_id)
        if row.evidence_type is EvidenceType.RULE
    )


# ------------------------------------------------------------------------------
# Test Double
# ------------------------------------------------------------------------------


def _passing_precheck(_command) -> PrecheckOutcome:
    return PrecheckOutcome(
        passed=True,
        verification_summary=build_verification_summary(
            VerificationMethod.DRY_RUN,
            verified=["AWS 대상 상태"],
            unverified=["IAM 권한(테스트 대역)"],
        ),
    )


def _failing_precheck(_command) -> PrecheckOutcome:
    return PrecheckOutcome(
        passed=False,
        reason_code=PrecheckReasonCode.PRECHECK_TARGET_NOT_FOUND,
        verification_summary=build_verification_summary(
            VerificationMethod.DRY_RUN,
            verified=["없음(DryRun 거절)"],
            unverified=["IAM 권한(테스트 대역)"],
        ),
    )


@pytest.fixture()
def precheck_pass(monkeypatch):
    """④만 대역으로 바꾼다 — ①②③은 실제로 돈다."""
    monkeypatch.setattr(workflows, "_candidate_precheck", _passing_precheck)


def _client(*outputs) -> FakeAIModelClient:
    return FakeAIModelClient(list(outputs))


def _proposal(evidence_id: str, **over) -> ProposedCandidate:
    base = {
        "runbook_id": "RUNBOOK_EC2_RIGHTSIZING",
        "target_arn": EC2_ARN,
        "evidence_ids": [evidence_id],
        "target_instance_type": "t3.medium",
    }
    base.update(over)
    return ProposedCandidate.model_validate(base)


def _cycle(db, client, publish=None):
    return agent_dispatcher.dispatch_pending_analysis(db, publish, client=client)


# ------------------------------------------------------------------------------
# Golden — 정상 입력, 기대값은 조회 계약과 카드 결정에서 도출한다
# ------------------------------------------------------------------------------


def test_successful_analysis_moves_the_incident_to_awaiting_approval(db, precheck_pass):
    incident_id = _pending_incident(db)
    evidence_id = _rule_evidence_id(db, incident_id)

    report = _cycle(
        db, _client(SUMMARY, CandidateProposalOutput(candidates=[_proposal(evidence_id)]))
    )

    assert (report.scanned, report.claimed, report.succeeded) == (1, 1, 1)
    incident = incidents_repo.get_incident(db, incident_id)
    db.refresh(incident)
    # AWAITING_APPROVAL은 실행 가능한 제안 1개 이상을 요구한다(api/incidents.py)
    assert incident.status is IncidentStatus.AWAITING_APPROVAL
    assert incident.agent_invocation_status is AgentInvocationStatus.SUCCEEDED
    assert len(incident.summary_lines) == 3

    candidates = incidents_repo.list_candidates(db, incident_id)
    assert [c.status for c in candidates] == [CandidateStatus.EXECUTABLE]
    # 거절이든 통과든 판정은 남는다 — 관제 화면이 "왜 사라졌나"를 답할 근거다
    assert guardrails_repo.latest_for_candidate(db, candidates[0].candidate_id) is not None


def test_detail_and_list_stay_readable_after_success(db, client_pg, precheck_pass):
    incident_id = _pending_incident(db)
    evidence_id = _rule_evidence_id(db, incident_id)
    _cycle(
        db, _client(SUMMARY, CandidateProposalOutput(candidates=[_proposal(evidence_id)]))
    )

    detail = client_pg.get(f"/api/v1/incidents/{incident_id}")
    assert detail.status_code == 200
    body = detail.json()
    assert body["status"] == IncidentStatus.AWAITING_APPROVAL.value
    assert len(body["summary_lines"]) == 3
    assert [r["runbook_id"] for r in body["recommendations"]] == [
        RunbookId.RUNBOOK_EC2_RIGHTSIZING.value
    ]
    assert client_pg.get("/api/v1/incidents").status_code == 200


def test_unattached_sg_incident_gets_a_menu(db):
    """미부착 SG의 조치는 Registry에서 SECOPS다 — 도메인으로 거르면 메뉴가 빈다."""
    incident_id = _pending_incident(db, sg=True)

    graph_input = agent_dispatcher.build_graph_input(db, incident_id)

    assert [c.runbook_id for c in graph_input.capabilities] == [
        RunbookId.RUNBOOK_SG_DELETE_ISOLATED
    ]


def test_graph_input_reads_the_rule_and_asset_evidence_rows(db):
    """최상위 rule_evaluation은 RULE 근거에서, 자산 문맥은 ASSET 근거에서 (불변식 ⓐⓑ)."""
    incident_id = _pending_incident(db)

    graph_input = agent_dispatcher.build_graph_input(db, incident_id)

    rule_evidence = next(
        item for item in graph_input.evidences if item.evidence_type is EvidenceType.RULE
    )
    assert rule_evidence.content.evaluation == graph_input.rule_evaluation
    assert graph_input.asset_context.arn == EC2_ARN
    assert graph_input.asset_context.spec.instance_type == "t3.xlarge"
    # ASSET 근거는 자산 문맥으로 이미 들어갔다 — 근거로도 실으면 같은 값이 두 번 간다
    assert all(
        item.evidence_type is not EvidenceType.ASSET for item in graph_input.evidences
    )


# ------------------------------------------------------------------------------
# 계약 거부 — 계약이 Workflow 몫으로 못 박은 둘
# ------------------------------------------------------------------------------


def test_candidate_citing_evidence_outside_the_input_fails_the_whole_output(
    db, precheck_pass
):
    incident_id = _pending_incident(db)

    report = _cycle(
        db,
        _client(
            SUMMARY,
            CandidateProposalOutput(
                candidates=[_proposal("2f0d2f2e-0000-4000-8000-000000000000")]
            ),
        ),
    )

    assert report.failed == 1
    incident = incidents_repo.get_incident(db, incident_id)
    db.refresh(incident)
    assert incident.status is IncidentStatus.FAILED
    assert incident.summary_lines == []
    # 성한 후보만 골라 남기지 않는다 — 출력 전체가 FAILED다
    assert incidents_repo.list_candidates(db, incident_id) == []


def test_reviewed_risk_level_on_a_finops_output_is_rejected(db):
    """FINOPS 출력에는 사후 위험도가 올 수 없다(계약 원칙 ⓑ).

    그래프가 이 값을 채우는 경로는 없어(ai/agent.py _validate_output_contract) 검증기를
    직접 부른다 — 통합 경로로는 만들 수 없는 입력이다.
    """
    incident_id = _pending_incident(db)
    graph_input = agent_dispatcher.build_graph_input(db, incident_id)

    violating = AgentGraphOutput(
        invocation_status=AgentInvocationStatus.NO_PROPOSAL,
        summary_lines=[SUMMARY.situation, SUMMARY.analysis, SUMMARY.recommendation],
        reviewed_risk_level=RiskLevel.HIGH,
    )

    verified = agent_dispatcher._verified_output(graph_input, violating, incident_id)

    assert verified.invocation_status is AgentInvocationStatus.FAILED
    assert verified.summary_lines == []


def test_evidence_subset_check_reads_the_input_not_the_incident(db):
    """ⓐ의 기준은 그래프 입력에 실린 Evidence다 — ASSET 근거는 거기 없다."""
    incident_id = _pending_incident(db)
    graph_input = agent_dispatcher.build_graph_input(db, incident_id)
    asset_evidence_id = next(
        row.evidence_id
        for row in incidents_repo.list_evidence(db, incident_id)
        if row.evidence_type is EvidenceType.ASSET
    )

    citing_asset = AgentGraphOutput(
        invocation_status=AgentInvocationStatus.SUCCEEDED,
        summary_lines=[SUMMARY.situation, SUMMARY.analysis, SUMMARY.recommendation],
        candidates=[
            RunbookCandidateDraft.model_validate(
                {
                    "runbook_id": "RUNBOOK_EC2_RIGHTSIZING",
                    "target_arn": EC2_ARN,
                    "parameters": {"target_instance_type": "t3.medium"},
                    "evidence_ids": [asset_evidence_id],
                }
            )
        ],
    )

    verified = agent_dispatcher._verified_output(graph_input, citing_asset, incident_id)

    assert verified.invocation_status is AgentInvocationStatus.FAILED


# ------------------------------------------------------------------------------
# 정책 — 카드가 결정한 처분
# ------------------------------------------------------------------------------


def test_no_proposal_closes_as_failed_with_an_empty_summary(db, client_pg):
    """요약만 있고 후보가 0개인 것은 정상 종착이 아니라 분석 실패다."""
    incident_id = _pending_incident(db)

    report = _cycle(db, _client(SUMMARY, CandidateProposalOutput(candidates=[])))

    assert report.no_proposal == 1
    incident = incidents_repo.get_incident(db, incident_id)
    db.refresh(incident)
    assert incident.status is IncidentStatus.FAILED
    assert incident.summary_lines == []
    # 그래프 오류와 구분해 남긴다 — 결함 계측·감사가 그 둘을 갈라 봐야 한다
    assert incident.agent_invocation_status is AgentInvocationStatus.NO_PROPOSAL
    assert client_pg.get(f"/api/v1/incidents/{incident_id}").status_code == 200
    assert client_pg.get("/api/v1/incidents").status_code == 200


def test_all_candidates_rejected_closes_as_failed(db, monkeypatch):
    """가드레일이 후보를 전부 거절하면 실행 가능한 제안이 0개다 — 같은 처분이다."""
    monkeypatch.setattr(workflows, "_candidate_precheck", _failing_precheck)
    incident_id = _pending_incident(db)
    evidence_id = _rule_evidence_id(db, incident_id)

    report = _cycle(
        db, _client(SUMMARY, CandidateProposalOutput(candidates=[_proposal(evidence_id)]))
    )

    assert report.succeeded == 1  # 그래프는 성공했다
    incident = incidents_repo.get_incident(db, incident_id)
    db.refresh(incident)
    assert incident.status is IncidentStatus.FAILED
    assert incident.summary_lines == []
    candidates = incidents_repo.list_candidates(db, incident_id)
    assert [c.status for c in candidates] == [CandidateStatus.REJECTED]
    # 어느 단계가 왜 막았는지가 남아야 관제 화면이 "왜 사라졌나"를 답한다
    evaluation = guardrails_repo.latest_for_candidate(db, candidates[0].candidate_id)
    assert evaluation.failed_step is GuardrailStep.AWS_DRY_RUN
    assert evaluation.steps[-1]["reason_code"] == (
        PrecheckReasonCode.PRECHECK_TARGET_NOT_FOUND.value
    )


def test_dropped_summary_is_logged_for_no_proposal(db, caplog):
    """인시던트에 안 쓰는 요약은 로그로 남긴다 — 후보 0개의 이유를 볼 자리가 그것뿐이다."""
    incident_id = _pending_incident(db)

    with caplog.at_level("INFO", logger="vigilantis.workflow"):
        _cycle(db, _client(SUMMARY, CandidateProposalOutput(candidates=[])))

    dropped = [r for r in caplog.records if r.msg == "agent_summary_dropped"]
    assert len(dropped) == 1
    assert dropped[0].incident_id == incident_id
    assert dropped[0].summary_lines == [
        SUMMARY.situation,
        SUMMARY.analysis,
        SUMMARY.recommendation,
    ]
    # 로그로 남겼다고 인시던트에 쓰지는 않는다(조회 계약)
    incident = incidents_repo.get_incident(db, incident_id)
    db.refresh(incident)
    assert incident.summary_lines == []


def test_guardrails_run_outside_a_transaction(db, monkeypatch):
    """④ AWS Dry-Run이 트랜잭션에 걸치면 AWS 응답·재시도 동안 커넥션이 묶인다."""
    seen = []

    def _spy(command):
        seen.append(db.in_transaction())
        return _passing_precheck(command)

    monkeypatch.setattr(workflows, "_candidate_precheck", _spy)
    incident_id = _pending_incident(db)
    evidence_id = _rule_evidence_id(db, incident_id)

    _cycle(
        db, _client(SUMMARY, CandidateProposalOutput(candidates=[_proposal(evidence_id)]))
    )

    assert seen == [False]


def test_graph_is_called_outside_a_transaction(db, monkeypatch, precheck_pass):
    """모델 호출이 트랜잭션에 걸치면 커넥션 1개가 그 시간만큼 묶인다."""
    seen = {}
    real = agent_dispatcher.run_finops_graph

    def _spy(graph_input, *, client):
        seen["in_transaction"] = db.in_transaction()
        return real(graph_input, client=client)

    monkeypatch.setattr(agent_dispatcher, "run_finops_graph", _spy)
    incident_id = _pending_incident(db)
    evidence_id = _rule_evidence_id(db, incident_id)

    _cycle(
        db, _client(SUMMARY, CandidateProposalOutput(candidates=[_proposal(evidence_id)]))
    )

    assert seen["in_transaction"] is False


def test_incident_updated_is_published_after_commit(db, precheck_pass):
    incident_id = _pending_incident(db)
    evidence_id = _rule_evidence_id(db, incident_id)
    published = []

    _cycle(
        db,
        _client(SUMMARY, CandidateProposalOutput(candidates=[_proposal(evidence_id)])),
        publish=published.append,
    )

    assert [event.data.incident_id for event in published] == [incident_id]
    incident = incidents_repo.get_incident(db, incident_id)
    db.refresh(incident)
    # occurred_at은 새 시각이 아니라 저장된 updated_at이다(realtime.incident_event)
    assert published[0].occurred_at == incident.updated_at


@pytest.mark.parametrize(
    "summary,proposal_over,자리",
    [
        (SUMMARY, {"target_instance_type": "t3.med\x00ium"}, "후보 parameters"),
        (
            EvidenceSummaryOutput(
                situation="상황\x00", analysis="분석", recommendation="권고"
            ),
            {},
            "요약",
        ),
    ],
    ids=["candidate-parameter", "summary-line"],
)
def test_output_with_unstorable_characters_closes_as_failed(
    db, precheck_pass, summary, proposal_over, 자리
):
    """PostgreSQL이 담지 못하는 NUL은 거절이지 예외가 아니다.

    막지 않으면 저장이 DataError로 터져 그 건이 ANALYZING·IN_PROGRESS에 남고, 회수를 거쳐
    **같은 출력을 다시 받는다** — 모델 호출만 되풀이된다.
    """
    incident_id = _pending_incident(db)
    evidence_id = _rule_evidence_id(db, incident_id)

    report = _cycle(
        db,
        _client(
            summary,
            CandidateProposalOutput(
                candidates=[_proposal(evidence_id, **proposal_over)]
            ),
        ),
    )

    assert (report.failed, report.errored) == (1, 0)
    incident = incidents_repo.get_incident(db, incident_id)
    db.refresh(incident)
    assert incident.status is IncidentStatus.FAILED
    assert incident.agent_invocation_status is AgentInvocationStatus.FAILED
    assert incident.summary_lines == []
    assert incidents_repo.list_candidates(db, incident_id) == []


def test_unstorable_output_is_closed_beyond_the_reclaimer_s_reach(
    db, precheck_pass, client_pg
):
    """회수가 되살릴 수 없어야 한다 — 되살아나면 같은 출력으로 모델 호출만 되풀이한다.

    갇힌 IN_PROGRESS도 PENDING 스캔에는 안 잡히므로 `scanned == 0`만으로는 구분되지
    않는다. 상한을 넘긴 시각을 심어 **회수 경로에 직접 걸어 본다.**
    """
    incident_id = _pending_incident(db)
    evidence_id = _rule_evidence_id(db, incident_id)
    _cycle(
        db,
        _client(
            SUMMARY,
            CandidateProposalOutput(
                candidates=[_proposal(evidence_id, target_instance_type="t3.\x00")]
            ),
        ),
    )
    incident = incidents_repo.get_incident(db, incident_id)
    db.refresh(incident)
    assert incident.agent_invocation_status in AGENT_TERMINAL_STATUSES

    ceiling = agent_dispatcher.stale_claim_ceiling_seconds()
    incident.agent_invocation_started_at = datetime.now(timezone.utc) - timedelta(
        seconds=ceiling + 60
    )
    db.commit()

    again = agent_dispatcher.dispatch_pending_analysis(db, client=_client())

    assert (again.scanned, again.reclaimed, again.errored) == (0, 0, 0)
    assert client_pg.get(f"/api/v1/incidents/{incident_id}").status_code == 200


# ------------------------------------------------------------------------------
# 방어 — 손상·도달 불가 상태의 안전 동작
# ------------------------------------------------------------------------------


def test_stale_in_progress_claim_is_returned_to_pending(db):
    incident_id = _pending_incident(db)
    ceiling = agent_dispatcher.stale_claim_ceiling_seconds()
    incidents_repo.claim_agent_invocation(
        db,
        incident_id,
        started_at=datetime.now(timezone.utc) - timedelta(seconds=ceiling + 60),
    )
    db.commit()

    report = agent_dispatcher.dispatch_pending_analysis(
        db, client=_client(SUMMARY, CandidateProposalOutput(candidates=[]))
    )

    assert report.reclaimed == 1
    # 같은 주기에서 곧바로 다시 집어 간다 — 회수가 스캔보다 앞선다
    assert report.scanned == 1


def test_a_claim_within_the_ceiling_is_left_alone(db):
    incident_id = _pending_incident(db)
    incidents_repo.claim_agent_invocation(
        db, incident_id, started_at=datetime.now(timezone.utc)
    )
    db.commit()

    report = agent_dispatcher.dispatch_pending_analysis(db, client=_client())

    assert report.reclaimed == 0
    assert report.scanned == 0  # IN_PROGRESS는 스캔 대상이 아니다
    incident = incidents_repo.get_incident(db, incident_id)
    db.refresh(incident)
    assert incident.agent_invocation_status is AgentInvocationStatus.IN_PROGRESS


def test_secops_incident_is_counted_but_never_claimed(db):
    """SecOps 그래프가 아직 없다 — 선점하면 주기마다 선점·해제를 반복한다."""
    incident_id = incident_intake.create_incident_from_intake(
        db, _secops_intake()
    ).incident_id

    report = agent_dispatcher.dispatch_pending_analysis(db, client=_client())

    assert (report.scanned, report.unsupported, report.claimed) == (1, 1, 0)
    incident = incidents_repo.get_incident(db, incident_id)
    db.refresh(incident)
    # 그래프가 생기면 그대로 집어 갈 수 있어야 한다
    assert incident.agent_invocation_status is AgentInvocationStatus.PENDING
    assert incident.status is IncidentStatus.ANALYZING


def test_incident_claimed_by_another_scanner_is_skipped(db, monkeypatch):
    _pending_incident(db)
    monkeypatch.setattr(
        incidents_repo, "claim_agent_invocation", lambda *a, **k: False
    )

    report = agent_dispatcher.dispatch_pending_analysis(db, client=_client())

    assert (report.scanned, report.skipped, report.claimed) == (1, 1, 0)


def test_missing_asset_evidence_closes_the_incident_as_failed(db):
    """근거가 빠진 건은 다음 주기에도 결과가 같다 — PENDING으로 두면 영원히 반복한다."""
    incident_id = _pending_incident(db)
    asset_evidence = next(
        row
        for row in incidents_repo.list_evidence(db, incident_id)
        if row.evidence_type is EvidenceType.ASSET
    )
    db.delete(asset_evidence)
    db.commit()

    report = agent_dispatcher.dispatch_pending_analysis(db, client=_client())

    assert report.failed == 1
    incident = incidents_repo.get_incident(db, incident_id)
    db.refresh(incident)
    assert incident.status is IncidentStatus.FAILED
    assert incident.agent_invocation_status is AgentInvocationStatus.FAILED
