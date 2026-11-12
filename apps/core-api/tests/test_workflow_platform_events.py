"""플랫폼 활동 로그(#589 B) — 업무 흐름이 실제 platform_events 행을 남기는가 · 실제 PostgreSQL.

지키는 것.
  - 관제자 승인(원복 접수 포함) · 종료는 HTTP 재판정 신원(계정 종류 · 플랫폼 등급 · 세션)으로 같은 트랜잭션에 남는다.
    actor 없는 신뢰 경로는 사람 행을 남기지 않는다.
  - 정지 테넌트 거절(409)은 요청을 되돌린 뒤 따로 남고 원래 409 는 그대로다. 운영과 같은 독립 세션 · commit 된
    사용자로도 잠금을 기다리지 않고 commit 된다(lock_timeout 으로 묶어 멈추지 않고 실패하게 한다).
  - 분석 · 자동 격리 · 확정 · 원복 · 시작 관문은 SYSTEM 행이고 사유는 enum 값이다(원문 문구가 아니다).
  - 재생 · 경합 패배 · 건너뜀 · 이미 종료 · 판정 보류 재시도는 행을 늘리지 않는다. 업무가 되돌아가면 행도 없고,
    행을 못 쓰면 업무도 commit 되지 않는다.
외부 AWS 는 tests.test_auto_rollback_workflow 의 가짜 EC2(aws 픽스처)만 쓴다. 모델은 부르지 않는다.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
import workflows
from db import models
from db.repositories import executions as executions_repo
from db.repositories import incidents as incidents_repo
from exceptions import ApiError
from fastapi.testclient import TestClient
from schemas.agents import AgentGraphOutput
from schemas.api.actions import ExecuteActionRequest, ExecutionStatus
from schemas.api.analysis import AnalysisFailureReason
from schemas.api.auth import PlatformRole, UserRole
from schemas.api.errors import ErrorCode
from schemas.api.incidents import IncidentCategory, IncidentStatus, ResolutionJudgement, RiskLevel
from schemas.api.tenants import TenantStatus
from schemas.candidates import CandidateStatus
from schemas.executions import ExecutionEffect, ExecutionStepResult, ExecutionStepStatus
from schemas.incidents import AgentInvocationStatus
from schemas.platform_events import PlatformActorKind, PlatformEventOutcome, PlatformEventTargetType
from schemas.precheck import PrecheckReasonCode
from schemas.runbooks import RunbookId
from sqlalchemy import create_engine, delete, event, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from tests.test_auto_isolation_workflow import (
    PRE,
    SKIP,
    SUBJECT,
    TIMEOUT,
    _approve_request,
    _attempt,
    _fail_result,
    _seed,
)
from tests.test_auto_rollback_workflow import aws, rolled_back_origin  # noqa: F401 — 픽스처는 이름으로 등록된다
from tests.test_auto_rollback_workflow import cycle, dry_run_error, only_child
from tests.test_tenant_auto_action import _tenant
from tests.test_tenant_suspension_gate import _attach_all, _reserved, _suspend

S = PlatformEventOutcome.SUCCEEDED
D = PlatformEventOutcome.DENIED
F = PlatformEventOutcome.FAILED
SYSTEM = PlatformActorKind.SYSTEM
EXECUTION = PlatformEventTargetType.EXECUTION
INCIDENT = PlatformEventTargetType.INCIDENT
SUMMARY = ["관측", "추정", "근거"]


def _utcnow() -> datetime:
    return datetime.now(UTC)


# --- 조회 -----------------------------------------------------------------------


def _rows(session, *target_ids) -> list[models.PlatformEvent]:
    session.expire_all()
    return list(session.scalars(
        select(models.PlatformEvent)
        .where(models.PlatformEvent.target_id.in_([str(value) for value in target_ids]))
        .order_by(models.PlatformEvent.action, models.PlatformEvent.target_id)
    ))


def _facts(session, *target_ids) -> list[tuple]:
    """(행동, 결과, 대상, 사유) — 순서와 무관하게 비교한다."""
    return sorted(
        (row.action, row.outcome.value, row.target_id, row.details.get("reason_code"))
        for row in _rows(session, *target_ids)
    )


def _total(session) -> int:
    session.expire_all()
    return session.scalar(select(func.count()).select_from(models.PlatformEvent))


def _is_system(row, tenant_id) -> None:
    assert row.actor_kind is SYSTEM
    assert (row.actor_user_id, row.actor_account_type, row.actor_platform_role, row.session_id) == (
        None, None, None, None,
    )
    assert row.tenant_id == tenant_id


def _is_person(row, seed, tenant_id) -> None:
    assert row.actor_kind is PlatformActorKind.USER
    assert (row.actor_user_id, row.actor_account_type, row.actor_platform_role, row.session_id) == (
        seed.user_id, seed.account_type, seed.platform_role, seed.session_id,
    )
    assert row.tenant_id == tenant_id


def _code(response) -> str:
    return response.json()["error"]["code"]


# --- 시드 -----------------------------------------------------------------------


def _approvable(db, make_incident, make_candidate, tenant_id):
    """EXECUTABLE 후보 1개가 선 SecOps 승인 대기 1건(테넌트 소속)."""
    incident = make_incident(db)
    incident.tenant_id = tenant_id
    make_candidate(db, incident)
    db.commit()
    return incident


def _execute(client, incident_id, runbook_id=RunbookId.RUNBOOK_NACL_ADD_DENY, key=None):
    return client.post("/api/v1/actions/execute", json=ExecuteActionRequest(
        incident_id=incident_id, runbook_id=runbook_id,
        idempotency_key=key or "events-" + uuid.uuid4().hex,
    ).model_dump(mode="json"))


# ==============================================================================
# 관제자 승인 — ACTION_APPROVE (USER)
# ==============================================================================


@pytest.mark.parametrize("account", ["central-admin", "tenant-manager", "tenant-operator"])
def test_http_approval_records_the_rechecked_person_once(
    client_pg, db, platform_session, login_as, make_tenant, make_incident, make_candidate, account
):
    tenant_id = make_tenant()
    seed = {
        "central-admin": lambda: platform_session,
        "tenant-manager": lambda: login_as(
            UserRole.ADMIN, platform_role=PlatformRole.TENANT_MANAGER, assigned_tenant_ids=(tenant_id,)
        ),
        "tenant-operator": lambda: login_as(UserRole.OPERATOR, tenant_id=tenant_id),
    }[account]()
    incident = _approvable(db, make_incident, make_candidate, tenant_id)
    key = "events-" + uuid.uuid4().hex

    first = _execute(client_pg, incident.incident_id, key=key)
    before_replay = _total(db)
    replay = _execute(client_pg, incident.incident_id, key=key)

    assert (first.status_code, replay.status_code) == (202, 200), first.json()
    assert _total(db) == before_replay  # 같은 Key 재생은 새 행이 없다
    execution_id = first.json()["execution_id"]
    [row] = _rows(db, execution_id, incident.incident_id)
    assert (row.action, row.outcome, row.target_type, row.target_id, row.details) == (
        "ACTION_APPROVE", S, EXECUTION, execution_id, {},
    )
    _is_person(row, seed, tenant_id)
    assert row.request_id is not None  # 요청 문맥은 기록 함수가 채운다


def test_http_rollback_reservation_is_an_approval_and_its_result_a_rollback(
    client_pg, db, platform_session, rolled_back_origin, aws
):
    incident_id, origin_id = rolled_back_origin(origin_status=ExecutionStatus.UNVERIFIED)

    reserved = _execute(client_pg, incident_id, RunbookId.RUNBOOK_EC2_REVERT_SIZE)
    assert reserved.status_code == 202, reserved.json()
    cycle(db)

    child_id = only_child(db, origin_id).execution_id
    assert reserved.json()["execution_id"] == child_id
    assert _facts(db, child_id, origin_id, incident_id) == [
        ("ACTION_APPROVE", "SUCCEEDED", child_id, None),
        ("ACTION_ROLLBACK", "SUCCEEDED", child_id, None),
    ]
    approve, rollback = _rows(db, child_id)
    _is_person(approve, platform_session, None)
    _is_system(rollback, None)  # 원복의 결과는 사람이 아니라 실행이 낸다


def test_rejected_and_raced_approvals_add_no_event(
    client_pg, db, make_tenant, make_incident, make_candidate, monkeypatch
):
    tenant_id = make_tenant()
    without_candidate = make_incident(db)
    incident = _approvable(db, make_incident, make_candidate, tenant_id)
    key = "events-" + uuid.uuid4().hex
    assert _execute(client_pg, incident.incident_id, key=key).status_code == 202
    before = _total(db)

    refused = _execute(client_pg, without_candidate.incident_id)
    # 첫 Key 조회와 접수 근거 확인 사이에 앞선 요청이 commit 되고 테넌트가 정지된 경합 — 재확인이 재생한다
    _suspend(db, tenant_id)
    real = executions_repo.get_by_idempotency_key
    calls = {"n": 0}

    def late_commit(session, idempotency_key):
        calls["n"] += 1
        return None if calls["n"] == 1 else real(session, idempotency_key)

    monkeypatch.setattr(executions_repo, "get_by_idempotency_key", late_commit)
    raced = _execute(client_pg, incident.incident_id, key=key)

    assert (refused.status_code, _code(refused)) == (409, "PROPOSAL_NOT_EXECUTABLE")
    assert (raced.status_code, calls["n"]) == (200, 2)
    assert _total(db) == before


def test_a_failed_platform_event_write_leaves_no_approval_behind(
    client_pg, db, make_tenant, make_incident, make_candidate
):
    incident = _approvable(db, make_incident, make_candidate, make_tenant())
    before = _total(db)

    def corrupt(_mapper, _connection, target):
        target.details = {"reason_code": "X"}  # SUCCEEDED + reason_code → DB CHECK 위반

    event.listen(models.PlatformEvent, "before_insert", corrupt)
    try:
        response = _execute(client_pg, incident.incident_id)  # 미처리 예외는 미들웨어가 500 봉투로 바꾼다
    finally:
        event.remove(models.PlatformEvent, "before_insert", corrupt)
        db.rollback()

    assert (response.status_code, _code(response)) == (500, "INTERNAL_ERROR")
    assert executions_repo.list_by_incident(db, incident.incident_id) == []
    [candidate] = incidents_repo.list_candidates(db, incident.incident_id)
    assert candidate.status is CandidateStatus.EXECUTABLE
    assert _total(db) == before


# ==============================================================================
# 정지 테넌트 거절 — ACTION_BLOCK (USER · 따로 남기기)
# ==============================================================================


@pytest.mark.parametrize("rollback", [False, True], ids=["main-runbook", "rollback-runbook"])
def test_suspended_tenant_refusal_is_recorded_apart_and_keeps_the_409(
    client_pg, db, platform_session, make_tenant, make_incident, make_candidate,
    rolled_back_origin, aws, rollback,
):
    tenant_id = make_tenant(TenantStatus.SUSPENDED)
    if rollback:
        incident_id, _ = rolled_back_origin(origin_status=ExecutionStatus.UNVERIFIED)
        _attach_all(db, incident_id, tenant_id)
        runbook_id = RunbookId.RUNBOOK_EC2_REVERT_SIZE
    else:
        incident_id = _approvable(db, make_incident, make_candidate, tenant_id).incident_id
        runbook_id = RunbookId.RUNBOOK_NACL_ADD_DENY
    executions_before = len(executions_repo.list_by_incident(db, incident_id))

    response = _execute(client_pg, incident_id, runbook_id)

    assert (response.status_code, _code(response)) == (409, "TENANT_SUSPENDED")
    [row] = _rows(db, incident_id)
    assert (row.action, row.outcome, row.target_type, row.details) == (
        "ACTION_BLOCK", D, INCIDENT, {"reason_code": "TENANT_SUSPENDED"},
    )
    _is_person(row, platform_session, tenant_id)
    assert len(executions_repo.list_by_incident(db, incident_id)) == executions_before


def test_detached_write_failure_keeps_the_original_refusal(
    client_pg, db, make_tenant, make_incident, make_candidate
):
    import db.session as db_session

    incident = _approvable(db, make_incident, make_candidate, make_tenant(TenantStatus.SUSPENDED))

    def unavailable():
        raise RuntimeError("detached session unavailable")

    client_pg.app.dependency_overrides[db_session.get_session_factory] = lambda: unavailable
    response = _execute(client_pg, incident.incident_id)

    assert (response.status_code, _code(response)) == (409, "TENANT_SUSPENDED")
    assert _rows(db, incident.incident_id) == []


def _credential(seed):
    """HTTP 의존성(get_actor)이 만드는 것과 같은 자격 — 라우터 없이 actor 경로를 부를 때 쓴다."""
    from auth.admin_service import Actor
    from config import get_auth_settings

    return Actor(
        token=seed.token, idle_seconds=get_auth_settings().AUTH_SESSION_IDLE_SECONDS, clock=_utcnow,
    )


def test_actor_only_reservation_without_a_factory_works_as_before(
    db, platform_session, make_tenant, make_incident, make_candidate
):
    """session_factory 는 정지 거절의 따로 남기기에만 쓴다 — 없어도 actor 만의 접수 · 재생은 그대로 되고,
    정지 거절은 기록 없이 원래 409 다."""
    tenant_id = make_tenant()
    incident = _approvable(db, make_incident, make_candidate, tenant_id)
    suspended = _approvable(db, make_incident, make_candidate, make_tenant(TenantStatus.SUSPENDED))
    request = _approve_request(incident.incident_id)
    credential = _credential(platform_session)

    reservation = workflows.reserve_execution(db, request, actor=credential)
    before = _total(db)
    replay = workflows.reserve_execution(db, request, actor=credential)
    with pytest.raises(ApiError) as refused:
        workflows.reserve_execution(db, _approve_request(suspended.incident_id), actor=credential)
    db.rollback()

    assert (reservation.created, replay.created) == (True, False)
    assert replay.response.execution_id == reservation.response.execution_id
    assert refused.value.code is ErrorCode.TENANT_SUSPENDED
    assert _total(db) == before  # 재생도, factory 없는 정지 거절도 새 행이 없다
    execution_id = reservation.response.execution_id
    [row] = _rows(db, execution_id, incident.incident_id, suspended.incident_id)
    assert (row.action, row.outcome, row.target_id) == ("ACTION_APPROVE", S, execution_id)
    _is_person(row, platform_session, tenant_id)


@pytest.mark.parametrize("call", ["execute", "resolve"])
def test_identity_changed_before_the_write_lock_is_recorded_as_rechecked(
    client_pg, db, login_as, make_tenant, make_incident, make_candidate, monkeypatch, call
):
    """의존성이 본 TENANT_MANAGER 가 쓰기 잠금 직전 CENTRAL_ADMIN 으로 바뀌었다 — 권한은 그대로라 쓰기는 되고,
    행의 등급은 의존성 스냅샷이 아니라 잠금 안에서 다시 판정한 신원이다."""
    from db.repositories import auth as auth_repo

    tenant_id = make_tenant()
    seed = login_as(
        UserRole.ADMIN, platform_role=PlatformRole.TENANT_MANAGER, assigned_tenant_ids=(tenant_id,)
    )
    if call == "execute":
        incident = _approvable(db, make_incident, make_candidate, tenant_id)
    else:
        incident = make_incident(db, category=IncidentCategory.FINOPS)
        incident.tenant_id = tenant_id
        db.commit()
    changed: list[str] = []
    real_lock = auth_repo.lock_users_table

    def promote_then_lock(session):
        if not changed:
            auth_repo.set_platform_role(
                session, user_id=seed.user_id, platform_role=PlatformRole.CENTRAL_ADMIN, now=_utcnow()
            )
            changed.append(seed.user_id)
        real_lock(session)

    monkeypatch.setattr(auth_repo, "lock_users_table", promote_then_lock)
    if call == "execute":
        response = _execute(client_pg, incident.incident_id)
        target_id = response.json().get("execution_id")
    else:
        response = client_pg.post(
            f"/api/v1/incidents/{incident.incident_id}/resolve", json={"resolution": "JUSTIFIED"}
        )
        target_id = incident.incident_id

    assert changed == [seed.user_id]  # 의존성을 지나 Workflow 의 쓰기 잠금까지 왔다
    assert response.status_code == (202 if call == "execute" else 200), response.json()
    [row] = _rows(db, target_id)
    assert (row.actor_user_id, row.actor_account_type, row.actor_platform_role, row.session_id) == (
        seed.user_id, seed.account_type, PlatformRole.CENTRAL_ADMIN, seed.session_id,
    )
    assert row.tenant_id == tenant_id


def test_trusted_calls_record_no_person_event(
    db, make_tenant, make_incident, make_candidate, audit_actor
):
    """actor 없는 신뢰 경로는 등급 · 세션을 모른다 — 사람 행을 지어내지 않고 업무는 예전 그대로다."""
    incident = _approvable(db, make_incident, make_candidate, make_tenant())
    finops = make_incident(db, category=IncidentCategory.FINOPS)
    suspended = _approvable(db, make_incident, make_candidate, make_tenant(TenantStatus.SUSPENDED))

    reservation = workflows.reserve_execution(db, _approve_request(incident.incident_id), audit_actor=audit_actor)
    assert workflows.resolve_incident(
        db, finops.incident_id, ResolutionJudgement.JUSTIFIED, audit_actor=audit_actor
    ) is True
    with pytest.raises(ApiError) as refused:
        workflows.reserve_execution(db, _approve_request(suspended.incident_id), audit_actor=audit_actor)
    db.rollback()

    assert reservation.created is True
    assert refused.value.code is ErrorCode.TENANT_SUSPENDED
    assert _rows(
        db, reservation.response.execution_id, incident.incident_id, finops.incident_id,
        suspended.incident_id,
    ) == []


def _committed_app(sessions):
    """운영과 같은 갈래 — 요청마다 독립 세션, 따로 남기기도 독립 세션(테스트 트랜잭션 밖)."""
    import api_usage
    import db.session as db_session
    from config import get_settings
    from main import create_app

    get_settings.cache_clear()
    app = create_app()

    def _get_db():
        session = sessions()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[db_session.get_db] = _get_db
    app.dependency_overrides[db_session.get_session_factory] = lambda: sessions
    app.dependency_overrides[api_usage.get_usage_session_factory] = lambda: sessions
    return app


def test_suspended_refusal_commits_apart_from_the_request_without_waiting(
    pg_engine, committed_auth, make_incident
):
    """요청이 쥔 app_users 잠금을 놓지 않고 따로 쓰면 FK 확인이 그 잠금을 기다린다 — 5초 뒤 실패해 행이 없다."""
    engine = create_engine(pg_engine.url, connect_args={"options": "-c lock_timeout=5000"})
    sessions = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    tenant_id = committed_auth.make_tenant(TenantStatus.SUSPENDED)
    seed = committed_auth.make()
    with sessions() as setup:
        incident = make_incident(setup)
        incident.tenant_id = tenant_id
        setup.commit()
        incident_id = incident.incident_id
    try:
        with TestClient(_committed_app(sessions), headers=seed.headers) as client:
            response = _execute(client, incident_id)

        assert (response.status_code, _code(response)) == (409, "TENANT_SUSPENDED")
        with sessions() as check:
            [row] = _rows(check, incident_id)
            assert (row.action, row.outcome, row.details) == (
                "ACTION_BLOCK", D, {"reason_code": "TENANT_SUSPENDED"},
            )
            _is_person(row, seed, tenant_id)
            assert executions_repo.list_by_incident(check, incident_id) == []
    finally:
        # 사람 행은 committed_auth 정리가 사용자와 함께 지운다
        with sessions() as cleanup:
            cleanup.execute(delete(models.Incident).where(models.Incident.incident_id == incident_id))
            cleanup.commit()
        engine.dispose()


# ==============================================================================
# 종료 판단 — INCIDENT_RESOLVE (USER)
# ==============================================================================


def test_http_resolution_records_the_transition_once(
    client_pg, db, platform_session, make_tenant, make_incident, make_execution
):
    tenant_id = make_tenant()
    finops = make_incident(db, category=IncidentCategory.FINOPS)
    finops.tenant_id = tenant_id
    busy = make_incident(db, category=IncidentCategory.FINOPS)
    make_execution(db, busy)  # 진행 중 실행이 종료를 막는다
    db.commit()
    url = "/api/v1/incidents/{}/resolve"

    first = client_pg.post(url.format(finops.incident_id), json={"resolution": "NO_FURTHER_ACTION"})
    before = _total(db)
    again = client_pg.post(url.format(finops.incident_id), json={"resolution": "JUSTIFIED"})
    gated = client_pg.post(url.format(busy.incident_id), json={"resolution": "JUSTIFIED"})

    assert (first.status_code, again.status_code) == (200, 200)
    assert (gated.status_code, _code(gated)) == (409, "INCIDENT_NOT_RESOLVABLE")
    assert _total(db) == before  # 이미 종료 · 거절은 새 행이 없다
    [row] = _rows(db, finops.incident_id, busy.incident_id)
    assert (row.action, row.outcome, row.target_type, row.target_id, row.details) == (
        "INCIDENT_RESOLVE", S, INCIDENT, finops.incident_id,
        {"before": "AWAITING_APPROVAL", "after": "RESOLVED"},
    )
    _is_person(row, platform_session, tenant_id)


# ==============================================================================
# 분석 — INCIDENT_ANALYZE (SYSTEM)
# ==============================================================================


def _analyzing(db, make_incident, tenant_id) -> str:
    incident = make_incident(db, status=IncidentStatus.ANALYZING)
    incident.tenant_id = tenant_id
    db.flush()
    assert incidents_repo.claim_agent_invocation(db, incident.incident_id, started_at=_utcnow())
    db.commit()
    return incident.incident_id


_ANALYSES = {
    "no-proposal": (
        AgentGraphOutput(
            invocation_status=AgentInvocationStatus.NO_PROPOSAL,
            summary_lines=SUMMARY, reviewed_risk_level=RiskLevel.MEDIUM,
        ),
        S, "AWAITING_CLOSURE", None,
    ),
    "failed": (
        AgentGraphOutput(
            invocation_status=AgentInvocationStatus.FAILED,
            failure_reason=AnalysisFailureReason.MODEL_UNAVAILABLE,
        ),
        F, "FAILED", "MODEL_UNAVAILABLE",
    ),
    # 사유를 싣지 않는 FAILED 생산자도 계약상 있다 — 호출 상태를 사유로 싣는다
    "failed-without-reason": (
        AgentGraphOutput(invocation_status=AgentInvocationStatus.FAILED),
        F, "FAILED", "FAILED",
    ),
}


@pytest.mark.parametrize("case", sorted(_ANALYSES))
def test_analysis_records_its_transition_and_failure_reason(db, make_incident, make_tenant, case):
    output, outcome, after, reason = _ANALYSES[case]
    tenant_id = make_tenant()
    incident_id = _analyzing(db, make_incident, tenant_id)

    workflows.record_agent_analysis(db, incident_id, output)

    [row] = _rows(db, incident_id)
    expected = {"before": "ANALYZING", "after": after}
    if reason is not None:
        expected["reason_code"] = reason
    assert (row.action, row.outcome, row.target_type, row.details) == (
        "INCIDENT_ANALYZE", outcome, INCIDENT, expected,
    )
    _is_system(row, tenant_id)


def test_refused_analysis_records_nothing(db, make_incident):
    incident = make_incident(db, category=IncidentCategory.FINOPS)  # 분석 저장을 받지 않는 승인 대기
    db.commit()

    with pytest.raises(ValueError, match="분석 결과를 저장할 수 없는"):
        workflows.record_agent_analysis(db, incident.incident_id, _ANALYSES["no-proposal"][0])
    db.rollback()

    assert _rows(db, incident.incident_id) == []


# ==============================================================================
# 자동 격리 — ACTION_AUTO_TRIGGER · 가드레일 거절은 ACTION_BLOCK (SYSTEM)
# ==============================================================================


def _seeded(db, make_incident, make_candidate, kind, tenant_id=None, **kwargs):
    seeded = _seed(db, make_incident, make_candidate, kind, **kwargs)
    db.get(models.Incident, seeded.incident_id).tenant_id = tenant_id
    db.commit()
    return seeded


@pytest.mark.parametrize(("kind", "trigger"), [("high", PRE), ("medium", TIMEOUT)])
def test_auto_isolation_records_one_trigger_and_repeats_add_none(
    db, make_incident, make_candidate, make_tenant, kind, trigger
):
    tenant_id = make_tenant()
    seeded = _seeded(db, make_incident, make_candidate, kind, tenant_id)
    execution_id = str(uuid.uuid4())

    first = _attempt(db, seeded, trigger, execution_id=execution_id)
    before = _total(db)
    retry = _attempt(db, seeded, trigger, execution_id=execution_id)  # 같은 시도의 재전달
    loser = _attempt(db, seeded, trigger)  # 다른 시도 — 이미 접수됨

    assert (first.created, retry.created, retry.execution_id) == (True, False, execution_id)
    assert loser.skipped_reason is SKIP.ALREADY_RESERVED
    assert _total(db) == before
    [row] = _rows(db, execution_id, seeded.incident_id)
    assert (row.action, row.outcome, row.target_type, row.target_id, row.details) == (
        "ACTION_AUTO_TRIGGER", S, EXECUTION, execution_id, {},
    )
    _is_system(row, tenant_id)


@pytest.mark.parametrize("coded", [True, False], ids=["step-reason", "no-step-reason"])
def test_rejected_auto_isolation_is_a_guardrail_block_not_a_trigger(
    db, make_incident, make_candidate, make_tenant, coded
):
    tenant_id = make_tenant()
    seeded = _seeded(db, make_incident, make_candidate, "high", tenant_id)
    result = _fail_result()
    if not coded:
        raw = result.model_dump(mode="json")
        raw["steps"][-1]["reason_code"] = None  # FAIL 단계의 사유는 계약상 선택이다
        result = type(result).model_validate(raw)

    reservation = workflows.reserve_auto_isolation(
        db, seeded.incident_id, trigger_source=PRE, now=_utcnow(),
        execution_id=str(uuid.uuid4()), validated_command=None, guardrail_result=result,
    )

    assert reservation.execution_status is ExecutionStatus.FAILED
    [row] = _rows(db, seeded.incident_id, reservation.execution_id)
    assert (row.action, row.outcome, row.target_type, row.target_id, row.details) == (
        "ACTION_BLOCK", D, INCIDENT, seeded.incident_id,
        {"reason_code": "PRECHECK_INVALID_STATE" if coded else "AWS_DRY_RUN"},
    )
    _is_system(row, tenant_id)


@pytest.mark.parametrize(
    ("case", "reason"),
    [
        ("tenant-suspended", SKIP.TENANT_SUSPENDED),
        ("auto-action-off", SKIP.AUTO_ACTION_DISABLED),
        ("wait-not-due", SKIP.WAIT_NOT_DUE),
        ("target-busy", SKIP.TARGET_EXECUTION_IN_PROGRESS),
    ],
)
def test_skipped_auto_isolation_records_nothing(
    db, make_incident, make_candidate, make_tenant, make_execution, case, reason
):
    tenant_id = None
    if case == "tenant-suspended":
        tenant_id = make_tenant(TenantStatus.SUSPENDED)
    elif case == "auto-action-off":
        tenant_id = _tenant(db, enabled=False).tenant_id
    if case == "target-busy":
        make_execution(db, make_incident(db, subject_arn=SUBJECT), target_arn=SUBJECT)
    seeded = _seeded(
        db, make_incident, make_candidate, "medium", tenant_id,
        wait="future" if case == "wait-not-due" else "due",
    )
    before = _total(db)

    for rejected in (False, True):
        assert _attempt(db, seeded, TIMEOUT, rejected=rejected).skipped_reason is reason

    assert _total(db) == before


def test_isolation_commit_failure_takes_its_event_with_it(db, make_incident, make_candidate):
    seeded = _seeded(db, make_incident, make_candidate, "high")
    before = _total(db)

    def fail_commit(session):
        if not session.in_nested_transaction():
            raise RuntimeError("injected business commit failure")

    event.listen(db, "before_commit", fail_commit)
    try:
        with pytest.raises(RuntimeError, match="injected business commit failure"):
            _attempt(db, seeded, PRE)
    finally:
        event.remove(db, "before_commit", fail_commit)
        db.rollback()

    assert executions_repo.list_by_incident(db, seeded.incident_id) == []
    assert _total(db) == before


# ==============================================================================
# 확정 — ACTION_COMPLETE · ACTION_ROLLBACK (SYSTEM)
# ==============================================================================


def _running(db, make_incident, make_execution, tenant_id, *, runbook_id=RunbookId.RUNBOOK_EC2_RIGHTSIZING,
             parent=None):
    incident = make_incident(db, category=IncidentCategory.FINOPS, status=IncidentStatus.ACTION_IN_PROGRESS)
    incident.tenant_id = tenant_id
    execution = make_execution(db, incident, runbook_id=runbook_id, parent=parent)
    execution.tenant_id = tenant_id
    db.commit()
    return incident, execution


@pytest.mark.parametrize(
    ("next_status", "outcome", "reason"),
    [
        (ExecutionStatus.SUCCESS, S, None),
        (ExecutionStatus.FAILED, F, "FAILED"),
        # 원래 조치의 실패다 — 원복이 끝났다는 기록이 아니다
        (ExecutionStatus.ROLLBACK_INITIATED, F, "ROLLBACK_INITIATED"),
    ],
)
def test_closing_an_execution_records_its_completion_once(
    db, make_incident, make_execution, make_tenant, next_status, outcome, reason
):
    tenant_id = make_tenant()
    incident, execution = _running(db, make_incident, make_execution, tenant_id)

    closure = workflows.close_execution(
        db, execution.execution_id, next_status=next_status,
        error_summary=None if next_status is ExecutionStatus.SUCCESS else "원문 오류 — 사유로 쓰지 않는다",
    )

    assert closure is not None
    [row] = _rows(db, execution.execution_id, incident.incident_id)
    assert (row.action, row.outcome, row.target_type, row.target_id) == (
        "ACTION_COMPLETE", outcome, EXECUTION, execution.execution_id,
    )
    assert row.details == ({} if reason is None else {"reason_code": reason})
    _is_system(row, tenant_id)


def test_a_repeated_closure_adds_no_event(db, make_incident, make_execution):
    _, execution = _running(db, make_incident, make_execution, None)
    assert workflows.close_execution(db, execution.execution_id, next_status=ExecutionStatus.SUCCESS)
    before = _total(db)

    again = workflows.close_execution(db, execution.execution_id, next_status=ExecutionStatus.FAILED)

    assert again is None  # 이미 다른 주체가 확정했다
    assert _total(db) == before


@pytest.mark.parametrize(
    ("next_status", "outcome", "reason"),
    [(ExecutionStatus.SUCCESS, S, None), (ExecutionStatus.FAILED, F, "ROLLBACK_FAILED")],
)
def test_closing_a_rollback_child_records_a_rollback_of_the_child_only(
    db, make_incident, make_execution, make_tenant, next_status, outcome, reason
):
    tenant_id = make_tenant()
    incident, origin = _running(db, make_incident, make_execution, tenant_id)
    assert executions_repo.update_execution_status(
        db, origin.execution_id, expected=ExecutionStatus.IN_PROGRESS, next_status=ExecutionStatus.SUCCESS,
        finished_at=_utcnow(),
    )
    child = make_execution(db, incident, runbook_id=RunbookId.RUNBOOK_EC2_REVERT_SIZE, parent=origin)
    child.tenant_id = tenant_id
    db.commit()

    workflows.close_execution(
        db, child.execution_id, next_status=next_status,
        error_summary=None if next_status is ExecutionStatus.SUCCESS else "원복 실패",
    )

    # 원본 확정(ROLLED_BACK · ROLLBACK_FAILED)은 자식 행이 잇는 같은 사실이다 — 따로 남지 않는다
    assert executions_repo.get_execution(db, origin.execution_id).status is (
        ExecutionStatus.ROLLED_BACK if next_status is ExecutionStatus.SUCCESS else ExecutionStatus.ROLLBACK_FAILED
    )
    assert _facts(db, child.execution_id, origin.execution_id, incident.incident_id) == [
        ("ACTION_ROLLBACK", outcome.value, child.execution_id, reason),
    ]
    _is_system(_rows(db, child.execution_id)[0], tenant_id)


def test_held_verification_keeps_unverified_apart_from_a_failure_with_the_same_cause(
    db, make_incident, make_candidate
):
    """같은 보류 사유(PRECHECK_AWS_ERROR)로 확정돼도 자산을 만지지 않은 실행은 FAILED(사유 = 그 보류 사유), 만진
    실행은 UNVERIFIED(사유 = UNVERIFIED)다 — 활동 로그 행만으로 둘이 갈린다. 재시도로 미룬 판정은 행이 없다."""
    policy = workflows.VerificationRetryPolicy(max_attempts=2, interval_seconds=0)
    closed = {}
    for applied in (False, True):
        seeded = _seeded(db, make_incident, make_candidate, "high")
        reserved = _attempt(db, seeded, PRE)
        if applied:
            executions_repo.add_step(db, ExecutionStepResult(
                sequence=1, step_type="ISOLATE", status=ExecutionStepStatus.SUCCESS,
                effect=ExecutionEffect.APPLIED, affected_arn=SUBJECT,
                aws_operation="modify_network_interface_attribute", occurred_at=_utcnow(),
            ), execution_id=reserved.execution_id)
        db.commit()
        before = _total(db)

        deferred = workflows.record_verification_failure(
            db, reserved.execution_id, reason_code=PrecheckReasonCode.PRECHECK_AWS_ERROR, policy=policy,
        )
        assert deferred.held is False and _total(db) == before
        held = workflows.record_verification_failure(
            db, reserved.execution_id, reason_code=PrecheckReasonCode.PRECHECK_AWS_ERROR, policy=policy,
        )
        closed[applied] = (reserved.execution_id, held.closure.execution_status)

    (untouched, untouched_status), (applied, applied_status) = closed[False], closed[True]
    assert (untouched_status, applied_status) == (ExecutionStatus.FAILED, ExecutionStatus.UNVERIFIED)
    assert _facts(db, untouched, applied) == sorted([
        ("ACTION_AUTO_TRIGGER", "SUCCEEDED", untouched, None),
        ("ACTION_COMPLETE", "FAILED", untouched, "PRECHECK_AWS_ERROR"),
        ("ACTION_AUTO_TRIGGER", "SUCCEEDED", applied, None),
        ("ACTION_COMPLETE", "FAILED", applied, "UNVERIFIED"),
    ])


def test_an_origin_that_cannot_start_its_rollback_records_a_failed_rollback(
    db, make_incident, make_execution, make_tenant
):
    tenant_id = make_tenant()
    incident, origin = _running(db, make_incident, make_execution, tenant_id)
    workflows.close_execution(db, origin.execution_id, next_status=ExecutionStatus.ROLLBACK_INITIATED)

    workflows.close_execution(
        db, origin.execution_id, next_status=ExecutionStatus.ROLLBACK_FAILED, error_summary="원복 불가",
    )

    assert _facts(db, origin.execution_id, incident.incident_id) == [
        ("ACTION_COMPLETE", "FAILED", origin.execution_id, "ROLLBACK_INITIATED"),
        ("ACTION_ROLLBACK", "FAILED", origin.execution_id, "ROLLBACK_FAILED"),
    ]


def test_a_failed_closure_event_write_leaves_the_execution_open(
    db, make_incident, make_execution
):
    incident, execution = _running(db, make_incident, make_execution, None)

    def corrupt(_mapper, _connection, target):
        target.details = {"reason_code": "X"}  # SUCCEEDED + reason_code → DB CHECK 위반

    event.listen(models.PlatformEvent, "before_insert", corrupt)
    try:
        with pytest.raises(IntegrityError):
            workflows.close_execution(db, execution.execution_id, next_status=ExecutionStatus.SUCCESS)
    finally:
        event.remove(models.PlatformEvent, "before_insert", corrupt)
        db.rollback()

    assert executions_repo.get_execution(db, execution.execution_id).status is ExecutionStatus.IN_PROGRESS
    assert incidents_repo.get_incident(db, incident.incident_id).status is IncidentStatus.ACTION_IN_PROGRESS
    assert _rows(db, execution.execution_id) == []


def test_start_gate_refusal_records_a_block_and_a_failed_completion_once(
    db, make_incident, make_candidate, make_execution, make_tenant, aws
):
    tenant_id = make_tenant()
    incident_id, execution_id = _reserved(db, make_incident, make_candidate, make_execution, tenant_id)
    _suspend(db, tenant_id)

    cycle(db)
    before = _total(db)
    cycle(db)

    assert aws.calls == []
    assert _total(db) == before
    assert _facts(db, execution_id, incident_id) == [
        ("ACTION_BLOCK", "DENIED", incident_id, "TENANT_SUSPENDED"),
        ("ACTION_COMPLETE", "FAILED", execution_id, "TENANT_SUSPENDED"),
    ]
    for row in _rows(db, execution_id, incident_id):
        _is_system(row, tenant_id)


# ==============================================================================
# 자동 원복 — 발동 · 가드레일 거절 · 시작 불가 (SYSTEM)
# ==============================================================================


def test_auto_rollback_records_its_trigger_and_result_once(db, rolled_back_origin, aws):
    incident_id, origin_id = rolled_back_origin(origin_status=ExecutionStatus.IN_PROGRESS)
    workflows.close_execution(db, origin_id, next_status=ExecutionStatus.ROLLBACK_INITIATED)

    cycle(db)  # 자식 접수
    cycle(db)  # 자식 실행 → SUCCESS · 원본 ROLLED_BACK
    before = _total(db)
    cycle(db)  # 원본당 1회 — 다시 발동하지 않는다

    child_id = only_child(db, origin_id).execution_id
    assert _total(db) == before
    assert _facts(db, origin_id, child_id, incident_id) == [
        ("ACTION_AUTO_TRIGGER", "SUCCEEDED", child_id, None),
        ("ACTION_COMPLETE", "FAILED", origin_id, "ROLLBACK_INITIATED"),
        ("ACTION_ROLLBACK", "SUCCEEDED", child_id, None),
    ]


def test_rollback_guardrail_rejection_is_a_block_and_a_failed_rollback(db, rolled_back_origin, aws):
    incident_id, origin_id = rolled_back_origin(collected=False)  # ③ ARN Match 가 거절한다

    cycle(db)
    cycle(db)
    cycle(db)

    child_id = only_child(db, origin_id).execution_id
    assert _facts(db, origin_id, child_id, incident_id) == [
        ("ACTION_AUTO_TRIGGER", "SUCCEEDED", child_id, None),
        ("ACTION_BLOCK", "DENIED", incident_id, "ARN_TARGET_NOT_MANAGED"),
        ("ACTION_ROLLBACK", "FAILED", child_id, "ROLLBACK_FAILED"),
    ]


def test_deferred_rollback_guardrail_is_not_a_block(db, rolled_back_origin, aws):
    incident_id, origin_id = rolled_back_origin()
    aws(dry_run=dry_run_error("RequestLimitExceeded", 503))  # ④ 판정 불가 — 거절이 아니라 보류

    cycle(db)
    cycle(db)

    child_id = only_child(db, origin_id).execution_id
    assert _facts(db, origin_id, child_id, incident_id) == [
        ("ACTION_AUTO_TRIGGER", "SUCCEEDED", child_id, None),
    ]


def test_abandoned_auto_rollback_is_a_failed_rollback_of_the_origin(db, rolled_back_origin, aws):
    incident_id, origin_id = rolled_back_origin(with_backup=False)

    cycle(db)
    cycle(db)

    assert executions_repo.list_rollback_children(db, origin_id) == []
    assert _facts(db, origin_id, incident_id) == [
        ("ACTION_ROLLBACK", "FAILED", origin_id, "ROLLBACK_FAILED"),
    ]
