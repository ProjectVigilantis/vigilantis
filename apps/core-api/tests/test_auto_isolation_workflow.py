# ==============================================================================
# [파일 설명]
# 자동 격리 접수(workflows.reserve_auto_isolation) 통합 검증(PostgreSQL). (Issue #447)
#
#   - 범위는 **접수까지**다. 가드레일 판정은 테스트가 계약 모양대로 만든 값(네 단계
#     PASS·④ FAIL)이며 AWS·LocalStack을 부르지 않는다 — 실제 가드레일·격리 실행과의
#     연결을 검증하지 않는다.
#   - 경합은 독립 세션 2개로 재현한다. 첫 트랜잭션을 commit 직전에 멈춘 채 두 번째가
#     Incident 잠금에 막힌 것을 pg_blocking_pids로 확인한 뒤 놓는다(test_actions_api의
#     동시 접수 검증과 같은 방식). 이 테스트들은 rollback 픽스처 밖이라 commit한 데이터를
#     직접 정리한다.
# ==============================================================================

from __future__ import annotations

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from time import monotonic, sleep

import pytest
import workflows
from tests.audit_cleanup import purge_audit_events
from db import models
from db.repositories import executions as executions_repo
from db.repositories import incidents as incidents_repo
from db.repositories import notification_events as events_repo
from exceptions import ApiError
from schemas.agents import AgentGraphOutput
from schemas.api.actions import ExecuteActionRequest, ExecutionStatus
from schemas.api.errors import ErrorCode
from schemas.api.incidents import (
    IncidentCategory,
    IncidentStatus,
    ResolutionJudgement,
    ResponseMode,
    RiskLevel,
)
from schemas.candidates import CandidateStatus
from schemas.evidence import EvidenceType
from schemas.guardrails import (
    GUARDRAIL_STEP_ORDER,
    GuardrailDecision,
    GuardrailStep,
    GuardrailStepResult,
    GuardrailStepStatus,
    GuardrailValidationContext,
    GuardrailValidationResult,
    PrecheckReasonCode,
)
from schemas.incidents import AgentInvocationStatus
from schemas.runbooks import RunbookId, TriggerSource
from sqlalchemy import delete, event, or_, select, text
from sqlalchemy.orm import Session

ACCOUNT_REGION = "ap-northeast-2:123456789012"
INSTANCE_ID = "i-0123456789abcdef0"
SUBJECT = f"arn:aws:ec2:{ACCOUNT_REGION}:instance/{INSTANCE_ID}"
OTHER_INSTANCE_ID = "i-0fedcba9876543210"
OTHER_SUBJECT = f"arn:aws:ec2:{ACCOUNT_REGION}:instance/{OTHER_INSTANCE_ID}"
SG_SUBJECT = f"arn:aws:ec2:{ACCOUNT_REGION}:security-group/sg-0123456789abcdef0"
TARGET_GROUP_ARN = (
    "arn:aws:elasticloadbalancing:ap-northeast-2:123456789012:targetgroup/vig/abc"
)
ISOLATION_GROUP_ID = "sg-0abc123456789def0"
SUMMARY = ["요약 1", "요약 2", "요약 3"]
REJECTION_SUMMARY = "가드레일 거절(AWS_DRY_RUN/PRECHECK_INVALID_STATE)"

PRE = TriggerSource.PRE_MITIGATION_0_5S
TIMEOUT = TriggerSource.TIMEOUT_ISOLATION_1M
SKIP = workflows.AutoIsolationSkipReason
ROLLED_BACK = "rolled_back"


def _utcnow() -> datetime:
    return datetime.now(UTC)


# --- 입력: 테스트가 계약 모양대로 만든 가드레일 판정 ----------------------------------


def _pass_result() -> GuardrailValidationResult:
    return GuardrailValidationResult(
        result=GuardrailDecision.PASS,
        steps=[
            GuardrailStepResult(
                step=step,
                result=GuardrailStepStatus.PASS,
                verification_summary=(
                    "테스트 구성 판정(AWS 미호출)" if step is GuardrailStep.AWS_DRY_RUN else None
                ),
            )
            for step in GUARDRAIL_STEP_ORDER
        ],
        validated_at=_utcnow(),
    )


def _fail_result() -> GuardrailValidationResult:
    return GuardrailValidationResult(
        result=GuardrailDecision.FAIL,
        failed_step=GuardrailStep.AWS_DRY_RUN,
        steps=[
            GuardrailStepResult(step=step, result=GuardrailStepStatus.PASS)
            for step in GUARDRAIL_STEP_ORDER[:-1]
        ] + [
            GuardrailStepResult(
                step=GuardrailStep.AWS_DRY_RUN,
                result=GuardrailStepStatus.FAIL,
                reason_code=PrecheckReasonCode.PRECHECK_INVALID_STATE,
            )
        ],
        validated_at=_utcnow(),
    )


def _command(
    evidence_id: str,
    *,
    target: str = SUBJECT,
    instance_id: str = INSTANCE_ID,
    runbook_id: str = RunbookId.RUNBOOK_EC2_ISOLATE.value,
    evidence_ids: list[str] | None = None,
) -> dict:
    return {
        "runbook_id": runbook_id,
        "target_arn": target,
        "parameters": {
            "instance_id": instance_id,
            "target_group_arn": TARGET_GROUP_ARN,
            "isolation_group_id": ISOLATION_GROUP_ID,
            "evidence_id": evidence_id,
        },
        "evidence_ids": [evidence_id] if evidence_ids is None else evidence_ids,
    }


def _attempt(
    session,
    seeded: Seeded,
    trigger: TriggerSource,
    *,
    execution_id: str | None = None,
    rejected: bool = False,
    now: datetime | None = None,
) -> workflows.AutoIsolationReservation:
    return workflows.reserve_auto_isolation(
        session,
        seeded.incident_id,
        trigger_source=trigger,
        now=now or _utcnow(),
        execution_id=execution_id or str(uuid.uuid4()),
        validated_command=None if rejected else _command(seeded.evidence_id),
        guardrail_result=_fail_result() if rejected else _pass_result(),
    )


# --- 시드 -----------------------------------------------------------------------


@dataclass(frozen=True)
class Seeded:
    incident_id: str
    evidence_id: str
    started: datetime | None
    deadline: datetime | None


_WAIT_STARTED_AGO = {"due": 120, "overdue_hour": 3600, "future": 30}


def _seed(
    session,
    make_incident,
    make_candidate,
    kind: str,
    *,
    status: IncidentStatus | None = None,
    wait: str | None = "due",
    candidates: tuple[RunbookId, ...] = (RunbookId.RUNBOOK_NACL_ADD_DENY,),
    subject: str = SUBJECT,
) -> Seeded:
    """high = 분석 전 High, medium·low = 분석이 끝나 승인 대기 중인 건(기본: 기한 지남)."""
    if kind == "high":
        incident = make_incident(
            session,
            subject_arn=subject,
            status=status or IncidentStatus.ANALYZING,
            initial_risk_level=RiskLevel.HIGH,
            response_mode=ResponseMode.PRE_MITIGATION_0_5S,
        )
        started = None
    else:
        incident = make_incident(
            session,
            subject_arn=subject,
            status=status or IncidentStatus.AWAITING_APPROVAL,
            summary_lines=SUMMARY,
            initial_risk_level=RiskLevel.MEDIUM if kind == "medium" else RiskLevel.LOW,
            response_mode=ResponseMode.AGENT_WAIT,
        )
        incident.agent_invocation_status = AgentInvocationStatus.SUCCEEDED
        started = (
            None if wait is None else _utcnow() - timedelta(seconds=_WAIT_STARTED_AGO[wait])
        )
        if started is not None:
            incident.agent_wait_started_at = started
            incident.response_deadline_at = started + timedelta(seconds=60)
        for runbook_id in candidates:
            make_candidate(session, incident, runbook_id=runbook_id)
    evidence = models.Evidence(
        incident_id=incident.incident_id,
        evidence_type=EvidenceType.THREAT,
        source_type="threat_event",
        source_id="seed",
        content={},
        occurred_at=_utcnow(),
    )
    session.add(evidence)
    session.flush()
    return Seeded(
        incident_id=incident.incident_id,
        evidence_id=evidence.evidence_id,
        started=started,
        deadline=None if started is None else started + timedelta(seconds=60),
    )


# --- 조회 -----------------------------------------------------------------------


def _incident(session, incident_id: str) -> models.Incident:
    session.expire_all()  # Core UPDATE는 세션이 든 객체를 갱신하지 않는다
    return incidents_repo.get_incident(session, incident_id)


def _executions(session, incident_id: str) -> list[models.ActionExecution]:
    session.expire_all()
    return executions_repo.list_by_incident(session, incident_id)


def _auto_evaluations(session, incident_id: str) -> list[models.GuardrailEvaluation]:
    session.expire_all()
    return list(session.execute(
        select(models.GuardrailEvaluation)
        .join(
            models.ActionExecution,
            models.ActionExecution.execution_id == models.GuardrailEvaluation.execution_id,
        )
        .where(
            models.ActionExecution.incident_id == incident_id,
            models.GuardrailEvaluation.validation_context
            == GuardrailValidationContext.AUTO_ISOLATION,
        )
    ).scalars())


def _candidate_statuses(session, incident_id: str) -> list[CandidateStatus]:
    session.expire_all()
    return sorted(
        (row.status for row in incidents_repo.list_candidates(session, incident_id)),
        key=lambda status: status.value,
    )


def _approve_request(incident_id: str) -> ExecuteActionRequest:
    return ExecuteActionRequest(
        incident_id=incident_id,
        runbook_id=RunbookId.RUNBOOK_NACL_ADD_DENY,
        idempotency_key="auto-iso-" + uuid.uuid4().hex,
    )


# ==============================================================================
# 접수 결과 — 단일 세션
# ==============================================================================


def test_high_pass_reserves_validated_in_progress_execution(
    db, client_pg, make_incident, make_candidate
):
    seeded = _seed(db, make_incident, make_candidate, "high")
    db.commit()
    execution_id = str(uuid.uuid4())

    result = _attempt(db, seeded, PRE, execution_id=execution_id)

    assert result.created is True and result.skipped_reason is None
    assert result.execution_id == execution_id
    assert result.execution_status is ExecutionStatus.IN_PROGRESS
    assert result.response_deadline_at is None
    incident = _incident(db, seeded.incident_id)
    assert result.occurred_at == incident.updated_at
    assert incident.status is IncidentStatus.ACTION_IN_PROGRESS
    assert incident.response_mode is ResponseMode.PRE_MITIGATION_0_5S
    assert incident.agent_invocation_status is AgentInvocationStatus.PENDING

    [execution] = _executions(db, seeded.incident_id)
    assert execution.execution_id == execution_id
    assert execution.runbook_id is RunbookId.RUNBOOK_EC2_ISOLATE
    assert execution.status is ExecutionStatus.IN_PROGRESS
    assert execution.trigger_source is PRE
    assert execution.target_arn == SUBJECT
    assert execution.candidate_id is None
    assert execution.deduplication_key == workflows.auto_isolation_deduplication_key(
        PRE, seeded.incident_id
    )
    # 저장된 명령은 입력과 같은 값이다(정규화가 값을 바꾸지 않는다)
    assert execution.validated_command == _command(seeded.evidence_id)
    [evaluation] = _auto_evaluations(db, seeded.incident_id)
    assert evaluation.execution_id == execution_id
    assert evaluation.result is GuardrailDecision.PASS
    assert evaluation.validated_command == execution.validated_command

    # 선차단 뒤에도 분석은 계속 대상이다
    assert (seeded.incident_id, IncidentCategory.SECOPS) in (
        incidents_repo.list_pending_agent_analysis(db)
    )
    detail = client_pg.get(f"/api/v1/incidents/{seeded.incident_id}")
    assert detail.status_code == 200
    body = detail.json()
    assert body["status"] == "ACTION_IN_PROGRESS"
    assert [(item["execution_id"], item["status"]) for item in body["executions"]] == [
        (execution_id, "IN_PROGRESS")
    ]
    [fact] = events_repo.list_by_incident(db, seeded.incident_id)
    assert (fact.kind, fact.phase, fact.outcome) == ("AUTO_ISOLATION", "REQUESTED", None)
    assert fact.execution_id == execution_id and fact.trigger_source is PRE


def test_timeout_pass_invalidates_wait_and_preserves_timing(
    db, client_pg, make_incident, make_candidate
):
    seeded = _seed(
        db, make_incident, make_candidate, "medium",
        candidates=(RunbookId.RUNBOOK_NACL_ADD_DENY, RunbookId.RUNBOOK_EBS_DELETE_UNATTACHED),
    )
    db.commit()
    assert seeded.incident_id in incidents_repo.list_expired_agent_waits(db, now=_utcnow())

    result = _attempt(db, seeded, TIMEOUT)

    assert result.created is True
    assert result.execution_status is ExecutionStatus.IN_PROGRESS
    assert result.response_deadline_at == seeded.deadline
    incident = _incident(db, seeded.incident_id)
    assert incident.status is IncidentStatus.ACTION_IN_PROGRESS
    assert incident.response_mode is ResponseMode.TIMEOUT_ISOLATION_1M
    # 기한은 새로 쓰지도 지우지도 않는다
    assert (incident.agent_wait_started_at, incident.response_deadline_at) == (
        seeded.started, seeded.deadline,
    )
    assert _candidate_statuses(db, seeded.incident_id) == [CandidateStatus.INVALIDATED] * 2
    [execution] = _executions(db, seeded.incident_id)
    assert (execution.trigger_source, execution.status) == (TIMEOUT, ExecutionStatus.IN_PROGRESS)
    assert len(_auto_evaluations(db, seeded.incident_id)) == 1
    assert seeded.incident_id not in incidents_repo.list_expired_agent_waits(db, now=_utcnow())

    body = client_pg.get(f"/api/v1/incidents/{seeded.incident_id}").json()
    assert body["status"] == "ACTION_IN_PROGRESS"
    assert body["response_mode"] == "TIMEOUT_ISOLATION_1M"
    assert body["recommendations"] == []
    [fact] = events_repo.list_by_incident(db, seeded.incident_id)
    assert (fact.phase, fact.outcome, fact.trigger_source) == ("REQUESTED", None, TIMEOUT)


def test_rejected_high_guardrail_is_durable_and_never_retried(
    db, client_pg, make_incident, make_candidate
):
    seeded = _seed(db, make_incident, make_candidate, "high")
    db.commit()
    execution_id = str(uuid.uuid4())

    result = _attempt(db, seeded, PRE, execution_id=execution_id, rejected=True)

    assert result.created is True
    assert result.execution_status is ExecutionStatus.FAILED
    [execution] = _executions(db, seeded.incident_id)
    assert execution.execution_id == execution_id
    assert (execution.trigger_source, execution.runbook_id) == (
        PRE, RunbookId.RUNBOOK_EC2_ISOLATE,
    )
    assert execution.status is ExecutionStatus.FAILED
    assert execution.finished_at is not None
    assert execution.error_summary == REJECTION_SUMMARY
    assert execution.validated_command is None
    [evaluation] = _auto_evaluations(db, seeded.incident_id)
    assert evaluation.result is GuardrailDecision.FAIL
    assert evaluation.failed_step is GuardrailStep.AWS_DRY_RUN
    assert evaluation.validated_command is None
    # 거절은 진행 중 실행으로 보이지 않는다
    assert execution_id not in {row.execution_id for row in executions_repo.list_non_terminal(db)}
    incident = _incident(db, seeded.incident_id)
    assert incident.status is IncidentStatus.FAILED
    assert incident.agent_invocation_status is AgentInvocationStatus.PENDING

    # 다음 시도는 같은 키에 막히고, 같은 ID 재시도는 저장된 거절을 그대로 돌려준다
    again = _attempt(db, seeded, PRE)
    assert again.skipped_reason is SKIP.ALREADY_RESERVED and again.execution_id is None
    replay = _attempt(db, seeded, PRE, execution_id=execution_id, rejected=True)
    assert (replay.created, replay.execution_id, replay.execution_status) == (
        False, execution_id, ExecutionStatus.FAILED,
    )
    assert len(_executions(db, seeded.incident_id)) == 1
    assert len(_auto_evaluations(db, seeded.incident_id)) == 1

    body = client_pg.get(f"/api/v1/incidents/{seeded.incident_id}").json()
    assert body["status"] == "FAILED"
    assert [item["status"] for item in body["executions"]] == ["FAILED"]
    facts = events_repo.list_by_incident(db, seeded.incident_id)
    assert [(row.phase, row.outcome) for row in facts] == [
        ("REQUESTED", None), ("RESULT", "REJECTED"),
    ]
    assert facts[1].reason_code == "PRECHECK_INVALID_STATE"


def test_rejected_timeout_closes_the_wait_without_retry(
    db, make_incident, make_candidate, audit_actor
):
    seeded = _seed(db, make_incident, make_candidate, "medium")
    db.commit()

    result = _attempt(db, seeded, TIMEOUT, rejected=True)

    assert result.created is True
    assert result.execution_status is ExecutionStatus.FAILED
    incident = _incident(db, seeded.incident_id)
    assert incident.status is IncidentStatus.FAILED
    assert incident.response_mode is ResponseMode.TIMEOUT_ISOLATION_1M
    assert (incident.agent_wait_started_at, incident.response_deadline_at) == (
        seeded.started, seeded.deadline,
    )
    assert _candidate_statuses(db, seeded.incident_id) == [CandidateStatus.INVALIDATED]
    [evaluation] = _auto_evaluations(db, seeded.incident_id)
    assert evaluation.result is GuardrailDecision.FAIL
    # 감시가 같은 기한을 다시 잡지 않고, 잡더라도 키가 막는다
    assert seeded.incident_id not in incidents_repo.list_expired_agent_waits(db, now=_utcnow())
    assert _attempt(db, seeded, TIMEOUT).skipped_reason is SKIP.ALREADY_RESERVED

    # 이전 대기에 대한 뒤늦은 승인은 기존 충돌 코드다
    with pytest.raises(ApiError) as stale:
        workflows.reserve_execution(
            db, _approve_request(seeded.incident_id), audit_actor=audit_actor
        )
    assert stale.value.code is ErrorCode.PROPOSAL_NOT_EXECUTABLE
    db.rollback()
    # 진행 중 실행이 없으므로 관제자 종료는 막지 않는다
    assert workflows.resolve_incident(
        db, seeded.incident_id, ResolutionJudgement.NO_FURTHER_ACTION, audit_actor=audit_actor
    ) is True
    assert len(_executions(db, seeded.incident_id)) == 1


@pytest.mark.parametrize("outcome", ["guardrail_rejected", "run_failed"])
def test_failed_high_pre_mitigation_keeps_agent_analysis_reachable(
    db, client_pg, make_incident, make_candidate, audit_actor, outcome
):
    """선차단이 거절·실패로 끝나 FAILED가 된 High도 분석이 끝나야 종료할 수 있다."""
    seeded = _seed(db, make_incident, make_candidate, "high")
    db.commit()
    if outcome == "guardrail_rejected":
        _attempt(db, seeded, PRE, rejected=True)
    else:
        reserved = _attempt(db, seeded, PRE)
        assert workflows.close_execution(
            db, reserved.execution_id,
            next_status=ExecutionStatus.FAILED, error_summary="격리 실행 실패",
        ) is not None
    incident = _incident(db, seeded.incident_id)
    assert incident.status is IncidentStatus.FAILED
    assert incident.agent_invocation_status is AgentInvocationStatus.PENDING
    # 분석 입력이 "시도 후 실패"를 볼 수 있는 기록이 남는다
    [prior] = [row for row in _executions(db, seeded.incident_id) if row.trigger_source is PRE]
    assert (prior.runbook_id, prior.status) == (
        RunbookId.RUNBOOK_EC2_ISOLATE, ExecutionStatus.FAILED,
    )
    assert prior.error_summary

    assert (seeded.incident_id, IncidentCategory.SECOPS) in (
        incidents_repo.list_pending_agent_analysis(db)
    )
    # 분석이 끝나기 전에는 기존 종료 게이트가 그대로다
    with pytest.raises(ApiError) as gated:
        workflows.resolve_incident(
            db, seeded.incident_id, ResolutionJudgement.NO_FURTHER_ACTION, audit_actor=audit_actor
        )
    assert gated.value.code is ErrorCode.INCIDENT_NOT_RESOLVABLE
    db.rollback()

    assert incidents_repo.claim_agent_invocation(db, seeded.incident_id, started_at=_utcnow())
    db.commit()
    recorded = workflows.record_agent_analysis(db, seeded.incident_id, AgentGraphOutput(
        invocation_status=AgentInvocationStatus.NO_PROPOSAL,
        summary_lines=SUMMARY,
        reviewed_risk_level=RiskLevel.HIGH,
    ))
    assert recorded.next_status is IncidentStatus.FAILED
    incident = _incident(db, seeded.incident_id)
    assert incident.agent_invocation_status is AgentInvocationStatus.NO_PROPOSAL
    assert (seeded.incident_id, IncidentCategory.SECOPS) not in (
        incidents_repo.list_pending_agent_analysis(db)
    )
    body = client_pg.get(f"/api/v1/incidents/{seeded.incident_id}").json()
    assert body["status"] == "FAILED"
    assert body["analysis_result"]["status"] == "NO_PROPOSAL"
    assert workflows.resolve_incident(
        db, seeded.incident_id, ResolutionJudgement.NO_FURTHER_ACTION, audit_actor=audit_actor
    ) is True


@pytest.mark.parametrize("case", ["secops_without_pre_mitigation", "finops"])
def test_failed_state_without_pre_mitigation_stays_out_of_analysis(
    db, make_incident, make_candidate, case
):
    """FAILED 수용은 선차단이 있었던 SecOps에만 걸린다 — 다른 FAILED는 그대로 거절한다."""
    if case == "finops":
        incident = make_incident(
            db, category=IncidentCategory.FINOPS, status=IncidentStatus.FAILED
        )
        incident_id = incident.incident_id
    else:
        seeded = _seed(
            db, make_incident, make_candidate, "medium",
            status=IncidentStatus.FAILED, candidates=(),
        )
        incident_id = seeded.incident_id
        execution = executions_repo.create_execution(
            db, incident_id=incident_id, runbook_id=RunbookId.RUNBOOK_EC2_ISOLATE,
            target_arn=SUBJECT, trigger_source=TriggerSource.USER_APPROVAL,
        )
        execution.status = ExecutionStatus.FAILED
        incidents_repo.get_incident(db, incident_id).agent_invocation_status = (
            AgentInvocationStatus.PENDING
        )
    db.commit()

    assert incident_id not in {
        row[0] for row in incidents_repo.list_pending_agent_analysis(db)
    }
    with pytest.raises(ValueError, match="분석 결과를 저장할 수 없는 Incident 상태"):
        workflows.record_agent_analysis(db, incident_id, AgentGraphOutput(
            invocation_status=AgentInvocationStatus.NO_PROPOSAL, summary_lines=SUMMARY,
        ))


# --- 발동 조건 제외 -------------------------------------------------------------

_EXCLUSIONS = [
    # (id, 시드 종류, 시드 인자, 발동 출처, 기대 사유)
    ("low_with_stored_deadline", "low", {}, TIMEOUT, SKIP.RISK_NOT_ELIGIBLE),
    ("medium_not_due", "medium", {"wait": "future"}, TIMEOUT, SKIP.WAIT_NOT_DUE),
    ("medium_without_wait", "medium", {"wait": None}, TIMEOUT, SKIP.WAIT_NOT_STARTED),
    *[
        (f"medium_{status.value.lower()}", "medium", {"status": status}, TIMEOUT,
         SKIP.STATUS_NOT_ELIGIBLE)
        for status in (
            IncidentStatus.ANALYZING,
            IncidentStatus.ACTION_IN_PROGRESS,
            IncidentStatus.AWAITING_CLOSURE,
            IncidentStatus.RESOLVED,
            IncidentStatus.FAILED,
        )
    ],
    ("high_after_analysis", "high", {"status": IncidentStatus.AWAITING_APPROVAL}, PRE,
     SKIP.STATUS_NOT_ELIGIBLE),
    ("pre_mitigation_on_medium", "medium", {}, PRE, SKIP.RISK_NOT_ELIGIBLE),
    ("timeout_on_high", "high", {}, TIMEOUT, SKIP.RISK_NOT_ELIGIBLE),
]


@pytest.mark.parametrize(
    "kind, seed_kwargs, trigger, reason",
    [row[1:] for row in _EXCLUSIONS],
    ids=[row[0] for row in _EXCLUSIONS],
)
def test_ineligible_incidents_are_skipped_without_writes(
    db, make_incident, make_candidate, kind, seed_kwargs, trigger, reason
):
    seeded = _seed(db, make_incident, make_candidate, kind, **seed_kwargs)
    db.commit()
    before = _incident(db, seeded.incident_id)
    snapshot = (
        before.status, before.response_mode, before.agent_wait_started_at,
        before.response_deadline_at, before.updated_at,
    )
    candidates = _candidate_statuses(db, seeded.incident_id)

    if trigger is TIMEOUT:  # 감시 조회도 같은 건을 집지 않는다
        assert seeded.incident_id not in incidents_repo.list_expired_agent_waits(
            db, now=_utcnow()
        )
    # 거절 판정을 넘겨도 조건 밖이면 기록하지 않는다
    for rejected in (False, True):
        result = _attempt(db, seeded, trigger, rejected=rejected)
        assert (result.skipped_reason, result.execution_id, result.created) == (
            reason, None, False,
        )

    after = _incident(db, seeded.incident_id)
    assert (
        after.status, after.response_mode, after.agent_wait_started_at,
        after.response_deadline_at, after.updated_at,
    ) == snapshot
    assert _candidate_statuses(db, seeded.incident_id) == candidates
    assert _executions(db, seeded.incident_id) == []
    assert _auto_evaluations(db, seeded.incident_id) == []


def test_prior_user_approval_blocks_the_old_deadline(
    db, make_incident, make_candidate, audit_actor
):
    """승인한 실행이 끝나 다른 제안이 남아도 지난 기한으로 자동 격리하지 않는다."""
    seeded = _seed(
        db, make_incident, make_candidate, "medium", wait="future",
        candidates=(RunbookId.RUNBOOK_NACL_ADD_DENY, RunbookId.RUNBOOK_EBS_DELETE_UNATTACHED),
    )
    db.commit()
    approved = workflows.reserve_execution(
        db, _approve_request(seeded.incident_id), audit_actor=audit_actor
    )
    closure = workflows.close_execution(
        db, approved.response.execution_id, next_status=ExecutionStatus.SUCCESS
    )
    assert closure.incident_status is IncidentStatus.AWAITING_APPROVAL
    after_deadline = seeded.deadline + timedelta(seconds=1)

    assert seeded.incident_id not in incidents_repo.list_expired_agent_waits(
        db, now=after_deadline
    )
    result = _attempt(db, seeded, TIMEOUT, now=after_deadline)
    assert result.skipped_reason is SKIP.USER_RESPONDED
    incident = _incident(db, seeded.incident_id)
    assert incident.response_mode is ResponseMode.AGENT_WAIT
    assert sorted(_candidate_statuses(db, seeded.incident_id)) == sorted(
        [CandidateStatus.CLAIMED, CandidateStatus.EXECUTABLE]
    )
    assert [row.trigger_source for row in _executions(db, seeded.incident_id)] == [
        TriggerSource.USER_APPROVAL
    ]


def test_post_isolation_new_action_is_distinct_from_stale_candidate(
    db, make_incident, make_candidate, audit_actor
):
    seeded = _seed(db, make_incident, make_candidate, "medium")
    db.commit()
    isolation = _attempt(db, seeded, TIMEOUT)

    # 이전 대기의 승인은 충돌이다
    with pytest.raises(ApiError) as stale:
        workflows.reserve_execution(
            db, _approve_request(seeded.incident_id), audit_actor=audit_actor
        )
    assert stale.value.code is ErrorCode.PROPOSAL_NOT_EXECUTABLE
    db.rollback()

    workflows.close_execution(
        db, isolation.execution_id, next_status=ExecutionStatus.SUCCESS
    )
    # 격리 뒤 새로 생긴 제안은 승인할 수 있다
    fresh = make_candidate(db, seeded.incident_id, runbook_id=RunbookId.RUNBOOK_NACL_ADD_DENY)
    assert incidents_repo.update_incident_status(
        db, seeded.incident_id,
        expected=IncidentStatus.AWAITING_CLOSURE, next_status=IncidentStatus.AWAITING_APPROVAL,
    )
    db.commit()
    approved = workflows.reserve_execution(
        db, _approve_request(seeded.incident_id), audit_actor=audit_actor
    )
    assert approved.created is True
    db.expire_all()
    assert incidents_repo.get_candidate(db, fresh.candidate_id).status is CandidateStatus.CLAIMED
    workflows.close_execution(
        db, approved.response.execution_id, next_status=ExecutionStatus.SUCCESS
    )
    # 격리 실행의 복구도 막지 않는다
    recovery = workflows.reserve_execution(db, ExecuteActionRequest(
        incident_id=seeded.incident_id,
        runbook_id=RunbookId.RUNBOOK_EC2_UNISOLATE,
        idempotency_key="auto-iso-" + uuid.uuid4().hex,
    ), audit_actor=audit_actor)
    assert recovery.created is True
    # 같은 대기의 타임아웃은 다시 발동하지 않는다
    assert _attempt(db, seeded, TIMEOUT).skipped_reason is SKIP.ALREADY_RESERVED
    assert sorted(
        (row.trigger_source.value, row.runbook_id.value)
        for row in _executions(db, seeded.incident_id)
    ) == sorted([
        (TIMEOUT.value, RunbookId.RUNBOOK_EC2_ISOLATE.value),
        (TriggerSource.USER_APPROVAL.value, RunbookId.RUNBOOK_NACL_ADD_DENY.value),
        (TriggerSource.USER_APPROVAL.value, RunbookId.RUNBOOK_EC2_UNISOLATE.value),
    ])


# --- 입력 계약 거부 -------------------------------------------------------------


def _foreign_id() -> str:
    return str(uuid.uuid4())


_INVALID_INPUTS = {
    "fail_with_command": lambda ev: {
        "validated_command": _command(ev), "guardrail_result": _fail_result()},
    "pass_without_command": lambda ev: {"validated_command": None},
    "not_isolate_runbook": lambda ev: {
        "validated_command": _command(ev, runbook_id=RunbookId.RUNBOOK_NACL_ADD_DENY.value)},
    "instance_differs_from_target": lambda ev: {
        "validated_command": _command(ev, instance_id=OTHER_INSTANCE_ID)},
    "sg_command_target": lambda ev: {"validated_command": _command(ev, target=SG_SUBJECT)},
    "evidence_id_not_first": lambda ev: {
        "validated_command": _command(ev, evidence_ids=[_foreign_id(), ev])},
    "other_instance_than_incident": lambda ev: {
        "validated_command": _command(ev, target=OTHER_SUBJECT, instance_id=OTHER_INSTANCE_ID)},
    "evidence_of_other_incident": lambda ev: {"validated_command": _command(_foreign_id())},
    "non_canonical_execution_id": lambda ev: {"execution_id": str(uuid.uuid4()).upper()},
}


@pytest.mark.parametrize("case", sorted(_INVALID_INPUTS))
def test_invalid_inputs_are_rejected_without_writes(db, make_incident, make_candidate, case):
    seeded = _seed(db, make_incident, make_candidate, "high")
    db.commit()
    kwargs = {
        "trigger_source": PRE,
        "now": _utcnow(),
        "execution_id": str(uuid.uuid4()),
        "validated_command": _command(seeded.evidence_id),
        "guardrail_result": _pass_result(),
    } | _INVALID_INPUTS[case](seeded.evidence_id)

    with pytest.raises(ValueError):
        workflows.reserve_auto_isolation(db, seeded.incident_id, **kwargs)
    db.rollback()

    assert _executions(db, seeded.incident_id) == []
    assert _auto_evaluations(db, seeded.incident_id) == []
    assert _incident(db, seeded.incident_id).status is IncidentStatus.ANALYZING


@pytest.mark.parametrize("rejected", [False, True], ids=["pass", "guardrail_rejected"])
def test_non_instance_incident_is_rejected_explicitly(db, make_incident, make_candidate, rejected):
    """SG 대상 Incident를 다른 인스턴스로 옮겨 격리하지 않는다 — 판정과 무관하게 예외다."""
    seeded = _seed(db, make_incident, make_candidate, "medium", subject=SG_SUBJECT)
    db.commit()
    assert seeded.incident_id not in incidents_repo.list_expired_agent_waits(db, now=_utcnow())

    with pytest.raises(ValueError, match="EC2 인스턴스"):
        _attempt(db, seeded, TIMEOUT, rejected=rejected)
    db.rollback()

    assert _executions(db, seeded.incident_id) == []
    incident = _incident(db, seeded.incident_id)
    assert (incident.status, incident.response_mode) == (
        IncidentStatus.AWAITING_APPROVAL, ResponseMode.AGENT_WAIT,
    )


def test_dedup_constraint_backstop_discards_the_loser(
    db, make_incident, make_candidate, monkeypatch
):
    """키 조회가 앞선 시도를 보지 못해도 유니크 제약이 막고, 진 쪽은 아무것도 남기지 않는다."""
    seeded = _seed(db, make_incident, make_candidate, "high")
    winner = executions_repo.create_execution(
        db, incident_id=seeded.incident_id, runbook_id=RunbookId.RUNBOOK_EC2_ISOLATE,
        target_arn=SUBJECT, trigger_source=PRE,
        deduplication_key=workflows.auto_isolation_deduplication_key(PRE, seeded.incident_id),
    )
    db.commit()
    real_lookup = executions_repo.get_by_deduplication_key
    calls = {"count": 0}

    def _blind_first(*args, **kwargs):
        calls["count"] += 1
        return None if calls["count"] == 1 else real_lookup(*args, **kwargs)

    monkeypatch.setattr(workflows.executions_repo, "get_by_deduplication_key", _blind_first)
    loser_id = str(uuid.uuid4())

    result = _attempt(db, seeded, PRE, execution_id=loser_id)

    assert result.skipped_reason is SKIP.ALREADY_RESERVED and result.execution_id is None
    assert calls["count"] == 2  # 최초 조회 + 제약 위반 뒤 재조회
    assert [row.execution_id for row in _executions(db, seeded.incident_id)] == [
        winner.execution_id
    ]
    assert _auto_evaluations(db, seeded.incident_id) == []
    assert _incident(db, seeded.incident_id).status is IncidentStatus.ANALYZING


# ==============================================================================
# 재시작·반복 — commit된 DB 값만으로 판정한다
# ==============================================================================


@contextmanager
def _committed(pg_engine):
    """rollback 픽스처 밖에서 commit한 Incident와 그 자식을 지운다."""
    incident_ids: list[str] = []
    try:
        yield incident_ids
    finally:
        if incident_ids:
            with Session(pg_engine) as cleanup:
                purge_audit_events(cleanup, incident_ids=incident_ids)
                executions = select(models.ActionExecution.execution_id).where(
                    models.ActionExecution.incident_id.in_(incident_ids)
                )
                candidates = select(models.RunbookCandidate.candidate_id).where(
                    models.RunbookCandidate.incident_id.in_(incident_ids)
                )
                cleanup.execute(delete(models.GuardrailEvaluation).where(or_(
                    models.GuardrailEvaluation.execution_id.in_(executions),
                    models.GuardrailEvaluation.candidate_id.in_(candidates),
                )))
                for model in (
                    models.ActionExecution, models.RunbookCandidate, models.Evidence,
                ):
                    cleanup.execute(delete(model).where(model.incident_id.in_(incident_ids)))
                cleanup.execute(
                    delete(models.Incident).where(models.Incident.incident_id.in_(incident_ids))
                )
                cleanup.commit()


def test_repeated_timeout_and_restart_use_the_stored_deadline(
    pg_engine, make_incident, make_candidate
):
    with _committed(pg_engine) as cleanup_ids:
        with Session(pg_engine) as setup:
            overdue = _seed(setup, make_incident, make_candidate, "medium", wait="overdue_hour")
            upcoming = _seed(setup, make_incident, make_candidate, "medium", wait="future")
            cleanup_ids += [overdue.incident_id, upcoming.incident_id]
            setup.commit()

        # 재시작한 프로세스는 새 세션에서 저장된 기한만 읽는다
        with Session(pg_engine) as restarted:
            due = incidents_repo.list_expired_agent_waits(restarted, now=_utcnow())
            assert overdue.incident_id in due and upcoming.incident_id not in due
            restarted.rollback()
            execution_id = str(uuid.uuid4())
            first = _attempt(restarted, overdue, TIMEOUT, execution_id=execution_id)
            assert first.created is True
            assert _attempt(restarted, upcoming, TIMEOUT).skipped_reason is SKIP.WAIT_NOT_DUE

        # commit 뒤 응답을 잃고 같은 시도를 다시 보낸 경우와 다음 감시 주기
        with Session(pg_engine) as again:
            replay = _attempt(again, overdue, TIMEOUT, execution_id=execution_id)
            assert (replay.created, replay.execution_id, replay.execution_status) == (
                False, execution_id, ExecutionStatus.IN_PROGRESS,
            )
            assert replay.occurred_at is None
            assert _attempt(again, overdue, TIMEOUT).skipped_reason is SKIP.ALREADY_RESERVED
            assert overdue.incident_id not in incidents_repo.list_expired_agent_waits(
                again, now=_utcnow()
            )
            # 기한을 늘리지 않으므로 그 시각이 지나면 그대로 발동한다
            later = upcoming.deadline + timedelta(seconds=1)
            assert upcoming.incident_id in incidents_repo.list_expired_agent_waits(
                again, now=later
            )
            again.rollback()
            assert _attempt(again, upcoming, TIMEOUT, now=later).created is True

        with Session(pg_engine) as verify:
            for seeded in (overdue, upcoming):
                incident = incidents_repo.get_incident(verify, seeded.incident_id)
                assert (incident.agent_wait_started_at, incident.response_deadline_at) == (
                    seeded.started, seeded.deadline,
                )
                assert len(executions_repo.list_by_incident(verify, seeded.incident_id)) == 1
                assert len(_auto_evaluations(verify, seeded.incident_id)) == 1


# ==============================================================================
# 실제 경합 — 독립 세션 2개, commit 직전 장벽, pg_blocking_pids 관찰
# ==============================================================================


class _InjectedCommitFailure(RuntimeError):
    """잠금을 쥔 첫 트랜잭션이 commit에 실패하는 경우를 재현한다."""


def _race(pg_engine, first_call, second_call, *, fail_first_commit: bool = False):
    """첫 호출을 commit 직전에 세우고, 두 번째가 그 잠금에 막힌 것을 본 뒤 놓는다."""
    pending_commit = threading.Event()
    release_commit = threading.Event()

    def hold_first_commit(session):
        if session.in_nested_transaction():
            return
        pending_commit.set()
        assert release_commit.wait(timeout=45), "첫 트랜잭션의 commit 해제 신호가 오지 않음"
        if fail_first_commit:
            raise _InjectedCommitFailure()

    def run(session, call):
        session.execute(text("SET LOCAL lock_timeout = '30s'"))
        try:
            return call(session)
        except ApiError as exc:
            session.rollback()
            return exc.code
        except _InjectedCommitFailure:
            session.rollback()  # 잠금은 이때 풀린다
            return ROLLED_BACK

    with (
        pg_engine.connect() as first_conn,
        pg_engine.connect() as second_conn,
        pg_engine.connect() as observer,
    ):
        first_pid = first_conn.scalar(text("SELECT pg_backend_pid()"))
        second_pid = second_conn.scalar(text("SELECT pg_backend_pid()"))
        first_conn.rollback()
        second_conn.rollback()
        with (
            Session(first_conn) as first,
            Session(second_conn) as second,
            ThreadPoolExecutor(max_workers=2) as pool,
        ):
            event.listen(first, "before_commit", hold_first_commit)
            first_future = pool.submit(run, first, first_call)
            try:
                deadline = monotonic() + 15
                while not pending_commit.wait(timeout=0.02):
                    if first_future.done():
                        pytest.fail(f"commit 대기 전 첫 트랜잭션 종료: {first_future.result()}")
                    assert monotonic() < deadline, "첫 트랜잭션이 commit 직전까지 도달하지 않음"
                second_future = pool.submit(run, second, second_call)
                deadline = monotonic() + 15
                while True:
                    blockers = observer.scalar(
                        text("SELECT pg_blocking_pids(:pid)"), {"pid": second_pid}
                    )
                    if first_pid in blockers:
                        break
                    if second_future.done():
                        pytest.fail(f"잠금 대기 없이 두 번째 종료: {second_future.result()}")
                    assert monotonic() < deadline, (
                        f"PostgreSQL 잠금 대기 미관찰: first={first_pid}, "
                        f"second={second_pid}, blockers={blockers}"
                    )
                    sleep(0.02)
            finally:
                release_commit.set()
            return first_future.result(timeout=30), second_future.result(timeout=30)


def _approve(incident_id: str, actor):
    request = _approve_request(incident_id)
    return lambda session: workflows.reserve_execution(session, request, audit_actor=actor)


def _reject(incident_id: str, actor):
    return lambda session: workflows.resolve_incident(
        session, incident_id, ResolutionJudgement.NO_FURTHER_ACTION, audit_actor=actor
    )


def _isolate(seeded: Seeded, trigger: TriggerSource, execution_id: str, *, rejected=False):
    return lambda session: _attempt(
        session, seeded, trigger, execution_id=execution_id, rejected=rejected
    )


@dataclass(frozen=True)
class _Final:
    status: IncidentStatus
    response_mode: ResponseMode
    candidate: CandidateStatus
    user_approvals: int
    timeouts: tuple[ExecutionStatus, ...]
    evaluations: tuple[GuardrailDecision, ...]
    wait_kept: bool


def _final(pg_engine, seeded: Seeded) -> _Final:
    with Session(pg_engine) as verify:
        incident = incidents_repo.get_incident(verify, seeded.incident_id)
        executions = executions_repo.list_by_incident(verify, seeded.incident_id)
        [candidate] = incidents_repo.list_candidates(verify, seeded.incident_id)
        return _Final(
            status=incident.status,
            response_mode=incident.response_mode,
            candidate=candidate.status,
            user_approvals=sum(
                row.trigger_source is TriggerSource.USER_APPROVAL for row in executions
            ),
            timeouts=tuple(row.status for row in executions if row.trigger_source is TIMEOUT),
            evaluations=tuple(
                row.result for row in _auto_evaluations(verify, seeded.incident_id)
            ),
            wait_kept=(incident.agent_wait_started_at, incident.response_deadline_at)
            == (seeded.started, seeded.deadline),
        )


def _created(status: ExecutionStatus):
    def check(result):
        return (
            isinstance(result, workflows.AutoIsolationReservation)
            and result.created and result.execution_status is status
        )
    return check


def _skipped(reason):
    def check(result):
        return (
            isinstance(result, workflows.AutoIsolationReservation)
            and result.skipped_reason is reason and result.execution_id is None
        )
    return check


def _approved(result) -> bool:
    return isinstance(result, workflows.ExecutionReservation) and result.created


def _conflict(code: ErrorCode):
    return lambda result: result is code


_ORDERS = {
    "approval_then_timeout": (
        "approve", "timeout", _approved, _skipped(SKIP.STATUS_NOT_ELIGIBLE),
        _Final(IncidentStatus.ACTION_IN_PROGRESS, ResponseMode.AGENT_WAIT,
               CandidateStatus.CLAIMED, 1, (), (), True),
    ),
    "timeout_then_approval": (
        "timeout", "approve", _created(ExecutionStatus.IN_PROGRESS),
        _conflict(ErrorCode.PROPOSAL_NOT_EXECUTABLE),
        _Final(IncidentStatus.ACTION_IN_PROGRESS, ResponseMode.TIMEOUT_ISOLATION_1M,
               CandidateStatus.INVALIDATED, 0, (ExecutionStatus.IN_PROGRESS,),
               (GuardrailDecision.PASS,), True),
    ),
    "rejection_then_timeout": (
        # 기존 종료 동작 그대로 — 종료는 대기 시각을 지운다
        "reject", "timeout", lambda result: result is True, _skipped(SKIP.STATUS_NOT_ELIGIBLE),
        _Final(IncidentStatus.RESOLVED, ResponseMode.AGENT_WAIT,
               CandidateStatus.INVALIDATED, 0, (), (), False),
    ),
    "timeout_then_rejection": (
        "timeout", "reject", _created(ExecutionStatus.IN_PROGRESS),
        _conflict(ErrorCode.INCIDENT_NOT_RESOLVABLE),
        _Final(IncidentStatus.ACTION_IN_PROGRESS, ResponseMode.TIMEOUT_ISOLATION_1M,
               CandidateStatus.INVALIDATED, 0, (ExecutionStatus.IN_PROGRESS,),
               (GuardrailDecision.PASS,), True),
    ),
    "rejected_timeout_then_approval": (
        "rejected_timeout", "approve", _created(ExecutionStatus.FAILED),
        _conflict(ErrorCode.PROPOSAL_NOT_EXECUTABLE),
        _Final(IncidentStatus.FAILED, ResponseMode.TIMEOUT_ISOLATION_1M,
               CandidateStatus.INVALIDATED, 0, (ExecutionStatus.FAILED,),
               (GuardrailDecision.FAIL,), True),
    ),
}


def _call(name: str, seeded: Seeded, actor):
    return {
        "approve": lambda: _approve(seeded.incident_id, actor),
        "reject": lambda: _reject(seeded.incident_id, actor),
        "timeout": lambda: _isolate(seeded, TIMEOUT, str(uuid.uuid4())),
        "rejected_timeout": lambda: _isolate(seeded, TIMEOUT, str(uuid.uuid4()), rejected=True),
    }[name]()


@pytest.mark.parametrize("order", sorted(_ORDERS))
def test_first_committed_response_wins_the_wait(
    pg_engine, make_incident, make_candidate, committed_actor, order
):
    first_name, second_name, first_ok, second_ok, expected = _ORDERS[order]
    with _committed(pg_engine) as cleanup_ids:
        with Session(pg_engine) as setup:
            seeded = _seed(setup, make_incident, make_candidate, "medium")
            cleanup_ids.append(seeded.incident_id)
            setup.commit()

        first, second = _race(
            pg_engine,
            _call(first_name, seeded, committed_actor),
            _call(second_name, seeded, committed_actor),
        )

        assert first_ok(first), first
        assert second_ok(second), second
        assert _final(pg_engine, seeded) == expected


@pytest.mark.parametrize("winner", ["timeout", "approval", "high"])
def test_rolled_back_lock_holder_releases_the_loser(
    pg_engine, make_incident, make_candidate, committed_actor, winner
):
    """잠금을 쥔 쪽이 commit에 실패하면 기다리던 쪽이 처음부터 다시 판정해 이긴다."""
    with _committed(pg_engine) as cleanup_ids:
        with Session(pg_engine) as setup:
            seeded = _seed(
                setup, make_incident, make_candidate,
                "high" if winner == "high" else "medium",
            )
            cleanup_ids.append(seeded.incident_id)
            setup.commit()
        holder_id, loser_id = str(uuid.uuid4()), str(uuid.uuid4())
        approve = _approve(seeded.incident_id, committed_actor)
        if winner == "timeout":
            calls = (_isolate(seeded, TIMEOUT, holder_id), approve)
        elif winner == "approval":
            calls = (approve, _isolate(seeded, TIMEOUT, loser_id))
        else:
            calls = (_isolate(seeded, PRE, holder_id), _isolate(seeded, PRE, loser_id))

        first, second = _race(pg_engine, *calls, fail_first_commit=True)

        assert first == ROLLED_BACK
        with Session(pg_engine) as verify:
            executions = executions_repo.list_by_incident(verify, seeded.incident_id)
            incident = incidents_repo.get_incident(verify, seeded.incident_id)
            evaluations = _auto_evaluations(verify, seeded.incident_id)
            assert holder_id not in {row.execution_id for row in executions}
            facts = events_repo.list_by_incident(verify, seeded.incident_id)
            assert holder_id not in {row.execution_id for row in facts}
            assert len(facts) == (0 if winner == "timeout" else 1)
            if winner == "timeout":
                assert _approved(second)
                assert [row.trigger_source for row in executions] == [
                    TriggerSource.USER_APPROVAL
                ]
                assert evaluations == []
                assert incident.response_mode is ResponseMode.AGENT_WAIT
                verify.rollback()
                # 승인이 이긴 대기는 다시 발동하지 않는다
                assert _attempt(verify, seeded, TIMEOUT).skipped_reason is (
                    SKIP.STATUS_NOT_ELIGIBLE
                )
            else:
                assert _created(ExecutionStatus.IN_PROGRESS)(second)
                assert [row.execution_id for row in executions] == [loser_id]
                assert [row.execution_id for row in evaluations] == [loser_id]
                assert incident.status is IncidentStatus.ACTION_IN_PROGRESS


@pytest.mark.parametrize(
    "trigger, first_rejected",
    [(PRE, False), (PRE, True), (TIMEOUT, False), (TIMEOUT, True)],
    ids=["high", "high_rejected", "timeout", "timeout_rejected"],
)
def test_concurrent_duplicate_attempts_keep_one_record(
    pg_engine, make_incident, make_candidate, trigger, first_rejected
):
    """같은 출처의 동시 시도는 하나만 남고, 진 쪽의 명령·판정은 저장되지 않는다.
    먼저 commit된 거절도 키를 차지하므로 뒤의 PASS가 재시도가 되지 않는다."""
    with _committed(pg_engine) as cleanup_ids:
        with Session(pg_engine) as setup:
            seeded = _seed(
                setup, make_incident, make_candidate,
                "high" if trigger is PRE else "medium",
            )
            cleanup_ids.append(seeded.incident_id)
            setup.commit()
        first_id, second_id = str(uuid.uuid4()), str(uuid.uuid4())

        first, second = _race(
            pg_engine,
            _isolate(seeded, trigger, first_id, rejected=first_rejected),
            _isolate(seeded, trigger, second_id),
        )

        status = ExecutionStatus.FAILED if first_rejected else ExecutionStatus.IN_PROGRESS
        assert _created(status)(first)
        assert _skipped(SKIP.ALREADY_RESERVED)(second)
        with Session(pg_engine) as verify:
            executions = executions_repo.list_by_incident(verify, seeded.incident_id)
            assert [(row.execution_id, row.status) for row in executions] == [
                (first_id, status)
            ]
            assert [row.execution_id for row in _auto_evaluations(
                verify, seeded.incident_id
            )] == [first_id]
            facts = events_repo.list_by_incident(verify, seeded.incident_id)
            assert [(row.execution_id, row.phase, row.outcome) for row in facts] == (
                [(first_id, "REQUESTED", None), (first_id, "RESULT", "REJECTED")]
                if first_rejected else [(first_id, "REQUESTED", None)]
            )


@pytest.mark.parametrize("rejected", [False, True])
def test_outbox_failure_rolls_back_isolation_reservation(
    db, make_incident, make_candidate, rejected,
):
    from sqlalchemy.exc import IntegrityError, PendingRollbackError

    seeded = _seed(db, make_incident, make_candidate, "medium")
    db.commit()

    def corrupt(_mapper, _connection, target):
        if target.phase == ("RESULT" if rejected else "REQUESTED"):
            target.target_arn = None

    event.listen(models.NotificationEvent, "before_insert", corrupt)
    try:
        with pytest.raises(IntegrityError):
            _attempt(db, seeded, TIMEOUT, rejected=rejected)
        with pytest.raises(PendingRollbackError):
            db.commit()
    finally:
        event.remove(models.NotificationEvent, "before_insert", corrupt)
        db.rollback()
    assert _executions(db, seeded.incident_id) == []
    assert _auto_evaluations(db, seeded.incident_id) == []
    assert events_repo.list_by_incident(db, seeded.incident_id) == []
    assert _incident(db, seeded.incident_id).status is IncidentStatus.AWAITING_APPROVAL
    assert _incident(db, seeded.incident_id).response_mode is ResponseMode.AGENT_WAIT
    assert _candidate_statuses(db, seeded.incident_id) == [CandidateStatus.EXECUTABLE]


def test_guardrail_rejection_without_optional_reason_still_records(
    db, make_incident, make_candidate,
):
    seeded = _seed(db, make_incident, make_candidate, "high")
    result = _fail_result().model_dump(mode="json")
    result["steps"][-1]["reason_code"] = None
    workflows.reserve_auto_isolation(
        db, seeded.incident_id, trigger_source=PRE, now=_utcnow(),
        execution_id=str(uuid.uuid4()), validated_command=None,
        guardrail_result=GuardrailValidationResult.model_validate(result),
    )
    [request, rejected] = events_repo.list_by_incident(db, seeded.incident_id)
    assert request.phase == "REQUESTED"
    assert (rejected.outcome, rejected.reason_code) == ("REJECTED", None)


def test_result_outbox_failure_leaves_execution_unsettled(db, make_incident, make_candidate):
    from sqlalchemy.exc import IntegrityError

    seeded = _seed(db, make_incident, make_candidate, "high")
    reserved = _attempt(db, seeded, PRE)

    def corrupt(_mapper, _connection, target):
        target.target_arn = None

    event.listen(models.NotificationEvent, "before_insert", corrupt)
    try:
        with pytest.raises(IntegrityError):
            workflows.close_execution(
                db, reserved.execution_id, next_status=ExecutionStatus.SUCCESS,
            )
    finally:
        event.remove(models.NotificationEvent, "before_insert", corrupt)
        db.rollback()
    execution = executions_repo.get_execution(db, reserved.execution_id)
    assert execution.status is ExecutionStatus.IN_PROGRESS
    assert _incident(db, seeded.incident_id).status is IncidentStatus.ACTION_IN_PROGRESS
    assert [row.phase for row in events_repo.list_by_incident(db, seeded.incident_id)] == [
        "REQUESTED",
    ]


def test_concurrent_closure_records_one_result(pg_engine, make_incident, make_candidate):
    with _committed(pg_engine) as cleanup_ids:
        with Session(pg_engine) as setup:
            seeded = _seed(setup, make_incident, make_candidate, "high")
            cleanup_ids.append(seeded.incident_id)
            reserved = _attempt(setup, seeded, PRE)
            execution_id = reserved.execution_id

        def close(session):
            return workflows.close_execution(
                session, execution_id, next_status=ExecutionStatus.SUCCESS,
            )

        first, second = _race(pg_engine, close, close)
        assert first.execution_status is ExecutionStatus.SUCCESS and second is None
        with Session(pg_engine) as reopened:
            facts = events_repo.list_by_incident(reopened, seeded.incident_id)
            assert [(row.phase, row.outcome) for row in facts] == [
                ("REQUESTED", None), ("RESULT", "SUCCESS"),
            ]
            # 재조회는 커밋 영속성 검증이다. 발송 worker 복구 검증이 아니다.
            assert all(row.execution_id == execution_id for row in facts)


def test_isolation_deferral_then_unverified_is_not_success(
    db, make_incident, make_candidate,
):
    from schemas.executions import (
        ExecutionEffect,
        ExecutionStepResult,
        ExecutionStepStatus,
    )

    seeded = _seed(db, make_incident, make_candidate, "high")
    reserved = _attempt(db, seeded, PRE)
    executions_repo.add_step(db, ExecutionStepResult(
        sequence=1, step_type="ISOLATE", status=ExecutionStepStatus.SUCCESS,
        effect=ExecutionEffect.APPLIED, affected_arn=SUBJECT,
        aws_operation="modify_network_interface_attribute", occurred_at=_utcnow(),
    ), execution_id=reserved.execution_id)
    db.commit()
    policy = workflows.VerificationRetryPolicy(max_attempts=2, interval_seconds=0)
    first = workflows.record_verification_failure(
        db, reserved.execution_id, reason_code=PrecheckReasonCode.PRECHECK_AWS_ERROR,
        policy=policy,
    )
    assert not first.held
    assert [row.phase for row in events_repo.list_by_incident(db, seeded.incident_id)] == [
        "REQUESTED",
    ]
    second = workflows.record_verification_failure(
        db, reserved.execution_id, reason_code=PrecheckReasonCode.PRECHECK_AWS_ERROR,
        policy=policy,
    )
    assert second.held
    [request, result] = events_repo.list_by_incident(db, seeded.incident_id)
    assert request.outcome is None
    assert result.outcome == "UNVERIFIED"
    assert result.execution_status is ExecutionStatus.UNVERIFIED
