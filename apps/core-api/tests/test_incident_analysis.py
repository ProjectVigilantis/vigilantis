"""분석 결과와 사용자 종료의 독립성. PostgreSQL 저장 후 공개 API를 재조회한다."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from time import monotonic, sleep
import uuid

import pytest
from sqlalchemy import delete, event, select, text
from sqlalchemy.orm import Session

from schemas.agents import AgentGraphOutput
from schemas.api.actions import ExecuteActionRequest, ExecutionStatus
from schemas.api.errors import ErrorCode
from schemas.api.incidents import IncidentCategory, IncidentStatus, ResolutionJudgement, RiskLevel
from schemas.candidates import CandidateStatus
from schemas.guardrails import (
    GUARDRAIL_STEP_ORDER, GuardrailDecision, GuardrailStep, GuardrailStepStatus,
    GuardrailStepResult, GuardrailValidationContext, GuardrailValidationResult, PrecheckReasonCode,
)
from schemas.incidents import AgentInvocationStatus
from schemas.runbooks import RunbookId

import workflows
from db import models
from db.repositories import guardrails as guardrails_repo
from db.repositories import incidents as incidents_repo
from exceptions import ApiError


def _evaluate(db, candidate, *, passed):
    return guardrails_repo.add_evaluation(
        db, candidate_id=candidate.candidate_id,
        validation_context=GuardrailValidationContext.AI_CANDIDATE,
        result=GuardrailValidationResult(
            result=GuardrailDecision.PASS if passed else GuardrailDecision.FAIL,
            failed_step=None if passed else GuardrailStep.AWS_DRY_RUN,
            steps=[GuardrailStepResult(
                step=step,
                result=(GuardrailStepStatus.FAIL if not passed and step is GuardrailStep.AWS_DRY_RUN
                        else GuardrailStepStatus.PASS),
                reason_code=(PrecheckReasonCode.PRECHECK_AWS_ERROR
                             if not passed and step is GuardrailStep.AWS_DRY_RUN else None),
                verification_summary="internal provider detail" if step is GuardrailStep.AWS_DRY_RUN else None,
            ) for step in GUARDRAIL_STEP_ORDER],
            validated_at=datetime.now(timezone.utc),
        ),
    )


@pytest.mark.parametrize("decision", ["approve", "reject"])
def test_original_analysis_survives_consumption_of_proposals(
    db, client_pg, make_incident, make_candidate, decision,
):
    incident = make_incident(db)
    incident.agent_invocation_status = AgentInvocationStatus.SUCCEEDED
    passed = make_candidate(db, incident)
    rejected = make_candidate(
        db, incident, runbook_id=RunbookId.RUNBOOK_EC2_ISOLATE,
        parameters={}, status=CandidateStatus.REJECTED,
    )
    _evaluate(db, passed, passed=True)
    _evaluate(db, rejected, passed=False)
    db.flush()
    url = f"/api/v1/incidents/{incident.incident_id}"
    before = client_pg.get(url).json()
    assert before["analysis_result"]["status"] == "PROPOSALS_GENERATED"
    assert len(before["analysis_result"]["guardrail_rejections"]) == 1
    assert "internal provider detail" not in str(before)
    if decision == "reject":
        response = client_pg.post(url + "/resolve", json={"resolution": "NO_FURTHER_ACTION"})
        assert response.status_code == 200
        db.refresh(passed)
        assert passed.status is CandidateStatus.INVALIDATED
        denied = client_pg.post("/api/v1/actions/execute", json={
            "incident_id": incident.incident_id, "runbook_id": passed.runbook_id.value,
            "idempotency_key": uuid.uuid4().hex,
        })
        assert denied.status_code == 409
        assert db.scalars(select(models.ActionExecution)).all() == []
    else:
        response = client_pg.post("/api/v1/actions/execute", json={
            "incident_id": incident.incident_id, "runbook_id": passed.runbook_id.value,
            "idempotency_key": uuid.uuid4().hex,
        })
        assert response.status_code == 202
    after = client_pg.get(url).json()
    assert after["recommendations"] == []
    assert after["analysis_result"] == before["analysis_result"]
    assert after["initial_risk_level"] == before["initial_risk_level"]
    db.refresh(rejected)
    assert rejected.status is CandidateStatus.REJECTED


@pytest.mark.parametrize("analysis_passed", [True, False])
@pytest.mark.parametrize("release_passed", [True, False])
def test_release_evaluation_does_not_change_original_analysis(
    db, client_pg, make_incident, make_candidate, analysis_passed, release_passed,
):
    incident = make_incident(db, status=IncidentStatus.FAILED)
    incident.agent_invocation_status = AgentInvocationStatus.SUCCEEDED
    original = make_candidate(db, incident, status=(
        CandidateStatus.CLAIMED if analysis_passed else CandidateStatus.REJECTED
    ))
    _evaluate(db, original, passed=analysis_passed)
    db.flush()
    url = f"/api/v1/incidents/{incident.incident_id}"
    before = client_pg.get(url).json()["analysis_result"]
    assert before["status"] == ("PROPOSALS_GENERATED" if analysis_passed else "GUARDRAIL_REJECTED")

    release = make_candidate(db, incident, runbook_id=RunbookId.RUNBOOK_NACL_RESTORE,
                             parameters={"rule_number": 100, "egress": False},
                             status=CandidateStatus.INVALIDATED if release_passed else CandidateStatus.REJECTED)
    _evaluate(db, release, passed=release_passed)
    db.flush()

    assert client_pg.get(url).json()["analysis_result"] == before
    [listed] = client_pg.get("/api/v1/incidents").json()["items"]
    assert listed["analysis_result"] == before


@pytest.mark.parametrize("status", [IncidentStatus.FAILED, IncidentStatus.AWAITING_CLOSURE])
def test_unknown_evaluation_is_not_reported_as_no_proposal(
    db, client_pg, make_incident, make_candidate, status,
):
    incident = make_incident(db, status=status)
    incident.agent_invocation_status = AgentInvocationStatus.SUCCEEDED
    candidate = make_candidate(db, incident, status=CandidateStatus.INVALIDATED)
    initial_risk = incident.initial_risk_level
    db.flush()
    listing = client_pg.get("/api/v1/incidents")
    assert listing.status_code == 200
    [listed] = listing.json()["items"]
    response = client_pg.get(f"/api/v1/incidents/{incident.incident_id}")
    assert response.status_code == 200
    data = response.json()
    for item in (listed, data):
        assert item["analysis_result"] == {"status": "UNAVAILABLE", "guardrail_rejections": []}
        assert item["status"] == status.value
        assert item["initial_risk_level"] == initial_risk.value
    assert data["executions"] == []
    assert data["resolution"] is None
    db.refresh(incident)
    db.refresh(candidate)
    assert incident.status is status
    assert incident.agent_invocation_status is AgentInvocationStatus.SUCCEEDED
    assert incident.initial_risk_level is initial_risk
    assert candidate.status is CandidateStatus.INVALIDATED


def test_analysis_listing_has_constant_query_count(db, client_pg, make_incident, make_candidate):
    statements = []
    def capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    counts = []
    for _ in range(2):
        for _ in range(5):
            incident = make_incident(db, status=IncidentStatus.AWAITING_CLOSURE)
            incident.agent_invocation_status = AgentInvocationStatus.SUCCEEDED
            candidate = make_candidate(db, incident, status=CandidateStatus.REJECTED)
            _evaluate(db, candidate, passed=False)
        db.flush()
        statements.clear()
        event.listen(db.bind, "before_cursor_execute", capture)
        try:
            response = client_pg.get("/api/v1/incidents")
        finally:
            event.remove(db.bind, "before_cursor_execute", capture)
        assert response.status_code == 200
        assert all(item["analysis_result"]["status"] == "GUARDRAIL_REJECTED"
                   for item in response.json()["items"])
        counts.append(len(statements))
    assert counts == [2, 2]  # Incident + 후보/평가 일괄 조회. 위협 연결 없는 시드다.


@pytest.mark.parametrize("risk", list(RiskLevel))
def test_close_preserves_first_note_and_cancels_wait(db, client_pg, make_incident, risk):
    incident = make_incident(db, status=IncidentStatus.AWAITING_CLOSURE, initial_risk_level=risk)
    incident.agent_invocation_status = AgentInvocationStatus.NO_PROPOSAL
    incident.agent_wait_started_at = datetime.now(timezone.utc)
    incident.response_deadline_at = incident.agent_wait_started_at + timedelta(seconds=60)
    db.flush()
    url = f"/api/v1/incidents/{incident.incident_id}"
    first = client_pg.post(url + "/resolve", json={
        "resolution": "NO_FURTHER_ACTION", "resolution_note": "  별도 조치 후 관제 종료  ",
    })
    assert first.status_code == 200
    again = client_pg.post(url + "/resolve", json={
        "resolution": "JUSTIFIED", "resolution_note": "덮어쓰지 않음",
    })
    assert again.status_code == 200
    assert again.json() == first.json()
    assert client_pg.get(url).json() == first.json()
    assert first.json()["resolution_note"] == "별도 조치 후 관제 종료"
    db.refresh(incident)
    assert incident.agent_wait_started_at is None and incident.response_deadline_at is None
    assert incident.agent_invocation_status is AgentInvocationStatus.NO_PROPOSAL
    assert incident.initial_risk_level is risk


@pytest.mark.parametrize("note", ["", "   ", "x" * 1001, "bad\x00note", "bad\ud800note"])
def test_invalid_note_is_rejected_before_db_write(client_pg, db, make_incident, note):
    incident = make_incident(db, status=IncidentStatus.FAILED)
    incident.agent_invocation_status = AgentInvocationStatus.FAILED
    db.flush()
    # ensure_ascii는 잘못된 Unicode를 JSON escape로 전달하여 요청 계약을 검증한다.
    import json
    response = client_pg.post(f"/api/v1/incidents/{incident.incident_id}/resolve",
        content=json.dumps({"resolution": "NO_FURTHER_ACTION", "resolution_note": note}),
        headers={"Content-Type": "application/json"})
    assert response.status_code == 422
    db.refresh(incident)
    assert incident.resolution is None


@pytest.mark.parametrize("analysis_status", [AgentInvocationStatus.PENDING, AgentInvocationStatus.IN_PROGRESS])
def test_prior_containment_does_not_allow_close_during_analysis(
    db, client_pg, make_incident, make_execution, analysis_status,
):
    incident = make_incident(db, status=IncidentStatus.AWAITING_CLOSURE)
    incident.agent_invocation_status = analysis_status
    make_execution(db, incident, status=ExecutionStatus.SUCCESS)
    db.flush()
    for resolution in ResolutionJudgement:
        response = client_pg.post(f"/api/v1/incidents/{incident.incident_id}/resolve",
                                  json={"resolution": resolution.value})
        assert response.status_code == 409


@pytest.mark.parametrize("resolution", list(ResolutionJudgement))
@pytest.mark.parametrize("analysis_status", [
    AgentInvocationStatus.FAILED,
    AgentInvocationStatus.NO_PROPOSAL,
    AgentInvocationStatus.SUCCEEDED,
])
def test_closure_without_execution_accepts_legacy_and_new_judgements(
    db, client_pg, make_incident, make_candidate, analysis_status, resolution,
):
    status = (IncidentStatus.FAILED if analysis_status is AgentInvocationStatus.FAILED
              else IncidentStatus.AWAITING_CLOSURE)
    incident = make_incident(db, status=status)
    incident.agent_invocation_status = analysis_status
    if analysis_status is AgentInvocationStatus.SUCCEEDED:
        rejected = make_candidate(db, incident, status=CandidateStatus.REJECTED)
        _evaluate(db, rejected, passed=False)
    db.flush()
    url = f"/api/v1/incidents/{incident.incident_id}"
    before = client_pg.get(url).json()
    response = client_pg.post(url + "/resolve", json={"resolution": resolution.value})
    assert response.status_code == 200
    after = client_pg.get(url).json()
    assert after["status"] == "RESOLVED"
    assert after["resolution"] == resolution.value
    assert after["executions"] == []
    assert after["analysis_result"] == before["analysis_result"]


def test_finops_closure_keeps_the_existing_judgement(db, client_pg, make_incident):
    finops = make_incident(db, category=IncidentCategory.FINOPS, status=IncidentStatus.FAILED)
    url = f"/api/v1/incidents/{finops.incident_id}/resolve"
    assert client_pg.post(url, json={"resolution": "NO_FURTHER_ACTION"}).status_code == 409
    assert client_pg.post(url, json={"resolution": "JUSTIFIED"}).status_code == 200


def test_late_analysis_cannot_reopen_closed_incident(db, client_pg, make_incident):
    incident = make_incident(db, status=IncidentStatus.FAILED)
    incident.agent_invocation_status = AgentInvocationStatus.FAILED
    db.flush()
    url = f"/api/v1/incidents/{incident.incident_id}"
    assert client_pg.post(url + "/resolve", json={"resolution": "NO_FURTHER_ACTION"}).status_code == 200
    with pytest.raises(ValueError, match="분석 결과를 저장할 수 없는"):
        workflows.record_agent_analysis(db, incident.incident_id, AgentGraphOutput(
            invocation_status=AgentInvocationStatus.NO_PROPOSAL,
            summary_lines=["관측", "추정", "근거"], reviewed_risk_level=RiskLevel.LOW,
        ))
    db.rollback()
    data = client_pg.get(url).json()
    assert data["status"] == "RESOLVED"
    assert data["analysis_result"]["status"] == "FAILED"
    assert data["reviewed_risk_level"] is None


@pytest.mark.parametrize("winner", ["close", "approve"])
def test_close_and_approval_are_serialized_by_postgres(
    pg_engine, make_incident, make_candidate, winner,
):
    with Session(pg_engine) as setup:
        incident = make_incident(setup)
        incident.agent_invocation_status = AgentInvocationStatus.SUCCEEDED
        candidate = make_candidate(setup, incident)
        incident_id, candidate_id = incident.incident_id, candidate.candidate_id
        setup.commit()
    request = ExecuteActionRequest(
        incident_id=incident_id, runbook_id=RunbookId.RUNBOOK_NACL_ADD_DENY,
        idempotency_key=uuid.uuid4().hex,
    )
    def act(session, action):
        if action == "close":
            return workflows.resolve_incident(session, incident_id, ResolutionJudgement.NO_FURTHER_ACTION)
        return workflows.reserve_execution(session, request)
    def contender(session):
        session.execute(text("SET LOCAL lock_timeout = '30s'"))
        try:
            return act(session, "approve" if winner == "close" else "close")
        except ApiError as exc:
            return exc.code
    try:
        # 연결 생성과 PID 조회를 먼저 끝내고 실제 PostgreSQL 잠금 대기만 관찰한다.
        with pg_engine.connect() as first_conn, pg_engine.connect() as second_conn, pg_engine.connect() as observer:
            first_pid = first_conn.scalar(text("SELECT pg_backend_pid()"))
            second_pid = second_conn.scalar(text("SELECT pg_backend_pid()"))
            first_conn.rollback()
            second_conn.rollback()
            with Session(first_conn) as first, Session(second_conn) as second, ThreadPoolExecutor(max_workers=1) as pool:
                incidents_repo.lock_incident(first, incident_id)
                future = pool.submit(contender, second)
                try:
                    deadline = monotonic() + 15
                    while True:
                        blockers = observer.scalar(text("SELECT pg_blocking_pids(:pid)"), {"pid": second_pid})
                        if first_pid in blockers:
                            break
                        if future.done():
                            pytest.fail(f"Incident 잠금 대기 없이 요청 종료: {future.result()}")
                        assert monotonic() < deadline, (
                            f"Incident 잠금 대기 미관찰: winner={winner}, "
                            f"first_pid={first_pid}, second_pid={second_pid}, blockers={blockers}"
                        )
                        sleep(0.02)
                    act(first, winner)
                finally:
                    first.rollback()
                assert future.result(timeout=30) is (
                    ErrorCode.PROPOSAL_NOT_EXECUTABLE if winner == "close"
                    else ErrorCode.INCIDENT_NOT_RESOLVABLE
                )
        with Session(pg_engine) as verify:
            row = verify.get(models.Incident, incident_id)
            executions = verify.scalars(select(models.ActionExecution).where(
                models.ActionExecution.incident_id == incident_id)).all()
            assert row.status is (IncidentStatus.RESOLVED if winner == "close" else IncidentStatus.ACTION_IN_PROGRESS)
            assert len(executions) == (0 if winner == "close" else 1)
            assert verify.get(models.RunbookCandidate, candidate_id).status is (
                CandidateStatus.INVALIDATED if winner == "close" else CandidateStatus.CLAIMED
            )
    finally:
        with Session(pg_engine) as cleanup:
            cleanup.execute(delete(models.ActionExecution).where(models.ActionExecution.incident_id == incident_id))
            cleanup.execute(delete(models.RunbookCandidate).where(models.RunbookCandidate.incident_id == incident_id))
            cleanup.execute(delete(models.Incident).where(models.Incident.incident_id == incident_id))
            cleanup.commit()
