"""감사 기록 쓰기(T-2 A · #544) — 실제 PostgreSQL.

지키는 것: 새 실행 행 1건 = 감사 기록 1건(관제자 승인 → 사람 · 자동 격리 · 자동 원복 → SYSTEM,
가드레일이 거절한 자동 시도 포함) · 종료 확정 1건 = 기록 1건 · 같은 Key 재생 · 거절 · 이미 종료된
재요청은 기록 없음 · 조치가 되돌아가면 기록도 함께 되돌아가고 기록이 실패하면 조치도 남지 않음 · 행위자
종류는 요청자의 계정 종류 · 행위자 인자는 HTTP 자격(actor) 또는 신뢰 경로의 사람(audit_actor) 하나 ·
행위자/발동 원천/종료 판단의 짝은 DB CHECK 가 막고 UPDATE · DELETE 는 트리거가 막는다.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
import workflows
from auth.admin_service import Actor as Credential
from db import models
from db.repositories import audit as audit_repo
from db.repositories import executions as executions_repo
from exceptions import ApiError
from schemas.api.audit import AuditActorKind, AuditEventAction
from schemas.api.auth import AccountType, UserRole
from schemas.api.errors import ErrorCode
from schemas.api.incidents import IncidentCategory, IncidentStatus, ResolutionJudgement
from schemas.runbooks import TriggerSource
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from tenancy import TenantScope
from tests.test_auto_isolation_workflow import (
    PRE,
    SKIP,
    TIMEOUT,
    _approve_request,
    _attempt,
    _seed,
)

STARTED = AuditEventAction.ACTION_EXECUTION_STARTED
RESOLVED = AuditEventAction.INCIDENT_RESOLVED
WORKFLOWS = Path(__file__).resolve().parents[1] / "workflows.py"


def _events(db, incident_id):
    db.expire_all()
    return audit_repo.list_by_incident(db, incident_id)


def _resolvable(db, make_incident, make_tenant=None):
    """종료할 수 있는 최소 상태 — 분석 게이트가 없는 FINOPS 승인 대기 1건."""
    incident = make_incident(
        db, category=IncidentCategory.FINOPS, status=IncidentStatus.AWAITING_APPROVAL
    )
    if make_tenant is not None:
        incident.tenant_id = make_tenant()
    db.commit()
    return incident


# --- 행위자 ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("account_type", "kind"),
    [
        (AccountType.PLATFORM, AuditActorKind.PLATFORM_USER),
        (AccountType.TENANT, AuditActorKind.TENANT_USER),
    ],
)
def test_person_kind_follows_the_account_type(account_type, kind):
    user_id = str(uuid.uuid4())
    assert audit_repo.person(user_id, account_type) == audit_repo.Actor(kind, user_id)


def test_actor_shape_is_rejected_before_the_database():
    with pytest.raises(ValueError):
        audit_repo.person(str(uuid.uuid4()), AccountType.UNASSIGNED)
    with pytest.raises(ValueError):
        audit_repo.Actor(AuditActorKind.SYSTEM, str(uuid.uuid4()))
    with pytest.raises(ValueError):
        audit_repo.Actor(AuditActorKind.PLATFORM_USER, None)


_CREDENTIAL = Credential(token="token", idle_seconds=60, clock=lambda: datetime.now(UTC))
_PERSON = audit_repo.person(str(uuid.uuid4()), AccountType.PLATFORM)
_BAD_ACTOR_ARGS = {
    # 기록 행위자는 재판정한 자격의 사용자다 — 따로 받으면 두 신원이 갈린다
    "credential-and-audit-actor": ({"actor": _CREDENTIAL, "audit_actor": _PERSON}, TypeError),
    # 범위도 재판정 신원이 정한다 — 명시 범위를 함께 받으면 어느 쪽이 기준인지 갈린다
    "credential-with-explicit-scope": ({"actor": _CREDENTIAL, "scope": TenantScope(tenant_ids=())}, ValueError),
    # 감사 행위자는 자격이 아니다 — 권한 판정에 쓰지 않는다
    "audit-actor-as-credential": ({"actor": _PERSON}, TypeError),
    "no-identity": ({}, TypeError),
    # 관제자 승인 · 종료는 사람이 한다
    "system-for-a-human-decision": ({"audit_actor": audit_repo.SYSTEM}, ValueError),
}


@pytest.mark.parametrize("case", sorted(_BAD_ACTOR_ARGS))
@pytest.mark.parametrize("call", ["execute", "resolve"])
def test_actor_arguments_are_rejected_before_the_database(call, case):
    kwargs, error = _BAD_ACTOR_ARGS[case]
    incident_id = str(uuid.uuid4())

    # db=None — 거절 전에 DB 에 닿으면 AttributeError 로 이 검사가 실패한다
    with pytest.raises(error):
        if call == "execute":
            workflows.reserve_execution(None, _approve_request(incident_id), **kwargs)
        else:
            workflows.resolve_incident(None, incident_id, ResolutionJudgement.JUSTIFIED, **kwargs)


# --- 관제자 승인 -----------------------------------------------------------------


def test_user_approval_records_one_person_event(
    db, make_incident, make_candidate, make_tenant, audit_actor
):
    incident = make_incident(db)
    incident.tenant_id = make_tenant()
    make_candidate(db, incident)
    db.commit()
    request = _approve_request(incident.incident_id)

    reservation = workflows.reserve_execution(db, request, audit_actor=audit_actor)
    replay = workflows.reserve_execution(db, request, audit_actor=audit_actor)

    assert (reservation.created, replay.created) == (True, False)
    execution = executions_repo.get_execution(db, reservation.response.execution_id)
    [event] = _events(db, incident.incident_id)
    assert (event.action, event.actor_kind, event.actor_user_id) == (
        STARTED, AuditActorKind.PLATFORM_USER, audit_actor.user_id,
    )
    assert (event.execution_id, event.trigger_source, event.resolution) == (
        execution.execution_id, TriggerSource.USER_APPROVAL, None,
    )
    # 시각 · 테넌트는 원래 행에서 옮긴다
    assert event.occurred_at == execution.created_at
    assert event.tenant_id == incident.tenant_id


def test_rejected_approval_leaves_no_event(db, make_incident, audit_actor):
    incident = make_incident(db)
    db.commit()

    with pytest.raises(ApiError) as rejected:
        workflows.reserve_execution(db, _approve_request(incident.incident_id), audit_actor=audit_actor)
    db.rollback()

    assert rejected.value.code is ErrorCode.PROPOSAL_NOT_EXECUTABLE
    assert _events(db, incident.incident_id) == []


def test_reservation_rolled_back_after_the_write_takes_the_event_with_it(
    db, make_incident, make_candidate, audit_actor, monkeypatch
):
    """기록은 실행 행과 같은 트랜잭션이다 — 후보 선점이 실패해 예약이 되돌아가면 기록도 없다."""
    incident = make_incident(db)
    make_candidate(db, incident)
    db.commit()
    monkeypatch.setattr(
        workflows.incidents_repo, "update_candidate_status", lambda *args, **kwargs: False
    )

    with pytest.raises(ApiError) as lost:
        workflows.reserve_execution(db, _approve_request(incident.incident_id), audit_actor=audit_actor)
    db.rollback()

    assert lost.value.code is ErrorCode.PROPOSAL_NOT_EXECUTABLE
    assert executions_repo.list_by_incident(db, incident.incident_id) == []
    assert _events(db, incident.incident_id) == []


# --- 자동 발동 -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "trigger", "rejected"),
    [
        ("high", PRE, False),
        ("high", PRE, True),
        ("medium", TIMEOUT, False),
        ("medium", TIMEOUT, True),
    ],
    ids=["pre", "pre-rejected", "timeout", "timeout-rejected"],
)
def test_auto_isolation_records_one_system_event(
    db, make_incident, make_candidate, kind, trigger, rejected
):
    seeded = _seed(db, make_incident, make_candidate, kind)
    db.commit()

    first = _attempt(db, seeded, trigger, rejected=rejected)
    again = _attempt(db, seeded, trigger)

    assert first.created is True
    assert again.skipped_reason is SKIP.ALREADY_RESERVED
    [event] = _events(db, seeded.incident_id)
    assert (event.action, event.actor_kind, event.actor_user_id) == (
        STARTED, AuditActorKind.SYSTEM, None,
    )
    assert (event.execution_id, event.trigger_source) == (first.execution_id, trigger)


# --- 종료 판단 -------------------------------------------------------------------


def test_resolution_records_one_event_and_a_repeat_adds_none(
    db, make_incident, make_tenant, audit_actor
):
    incident = _resolvable(db, make_incident, make_tenant)

    assert workflows.resolve_incident(
        db, incident.incident_id, ResolutionJudgement.JUSTIFIED, audit_actor=audit_actor
    ) is True
    assert workflows.resolve_incident(
        db, incident.incident_id, ResolutionJudgement.NO_FURTHER_ACTION, audit_actor=audit_actor
    ) is False

    [event] = _events(db, incident.incident_id)
    assert (event.action, event.actor_kind, event.actor_user_id) == (
        RESOLVED, AuditActorKind.PLATFORM_USER, audit_actor.user_id,
    )
    # 처음 저장된 판단만 남는다
    assert (event.resolution, event.execution_id, event.trigger_source) == (
        ResolutionJudgement.JUSTIFIED, None, None,
    )
    db.refresh(incident)
    assert event.occurred_at == incident.resolved_at
    assert event.tenant_id == incident.tenant_id


def test_unresolvable_request_leaves_no_event(db, make_incident, make_execution, audit_actor):
    incident = _resolvable(db, make_incident)
    make_execution(db, incident)  # 진행 중 실행이 종료를 막는다
    db.commit()

    with pytest.raises(ApiError) as gated:
        workflows.resolve_incident(
            db, incident.incident_id, ResolutionJudgement.JUSTIFIED, audit_actor=audit_actor
        )
    db.rollback()

    assert gated.value.code is ErrorCode.INCIDENT_NOT_RESOLVABLE
    assert _events(db, incident.incident_id) == []


class _AuditDown(Exception):
    pass


def _audit_down(*args, **kwargs):
    raise _AuditDown


def test_a_failed_audit_write_leaves_no_action_behind(
    db, make_incident, make_candidate, audit_actor, monkeypatch
):
    """반대 방향 — 기록이 실패하면 같은 트랜잭션의 실행 행 · 종료 전이도 commit 되지 않는다."""
    approval = make_incident(db)
    make_candidate(db, approval)
    finops = _resolvable(db, make_incident)
    monkeypatch.setattr(workflows.audit_repo, "record_execution_started", _audit_down)
    monkeypatch.setattr(workflows.audit_repo, "record_incident_resolved", _audit_down)

    with pytest.raises(_AuditDown):
        workflows.reserve_execution(db, _approve_request(approval.incident_id), audit_actor=audit_actor)
    db.rollback()
    with pytest.raises(_AuditDown):
        workflows.resolve_incident(
            db, finops.incident_id, ResolutionJudgement.JUSTIFIED, audit_actor=audit_actor
        )
    db.rollback()

    db.expire_all()
    assert executions_repo.list_by_incident(db, approval.incident_id) == []
    row = db.get(models.Incident, finops.incident_id)
    assert (row.status, row.resolution, row.resolved_at) == (IncidentStatus.AWAITING_APPROVAL, None, None)
    assert _events(db, approval.incident_id) == _events(db, finops.incident_id) == []


# --- HTTP: 행위자는 세션의 사용자 -------------------------------------------------


def test_http_requests_record_the_session_user(
    client_pg, db, platform_session, make_incident, make_candidate
):
    incident = make_incident(db)
    make_candidate(db, incident)
    finops = _resolvable(db, make_incident)

    executed = client_pg.post(
        "/api/v1/actions/execute",
        json=_approve_request(incident.incident_id).model_dump(mode="json"),
    )
    resolved = client_pg.post(
        f"/api/v1/incidents/{finops.incident_id}/resolve",
        json={"resolution": "NO_FURTHER_ACTION"},
    )

    assert (executed.status_code, resolved.status_code) == (202, 200)
    [started] = _events(db, incident.incident_id)
    [closed] = _events(db, finops.incident_id)
    for event in (started, closed):
        assert (event.actor_kind, event.actor_user_id) == (
            AuditActorKind.PLATFORM_USER, platform_session.user_id,
        )
    assert started.execution_id == executed.json()["execution_id"]


def test_http_requests_of_a_customer_account_record_a_tenant_user_with_its_tenant(
    client_pg, db, login_as, make_tenant, make_incident, make_candidate
):
    # 업무 관문이 걷힌 뒤(#533 C) 고객사 OPERATOR 의 승인 · 종료가 실제로 TENANT_USER 기록을 남긴다
    tenant_id = make_tenant()
    seed = login_as(UserRole.OPERATOR, tenant_id=tenant_id)
    incident = make_incident(db)
    make_candidate(db, incident)
    finops = _resolvable(db, make_incident)
    incident.tenant_id = finops.tenant_id = tenant_id
    db.commit()

    executed = client_pg.post(
        "/api/v1/actions/execute",
        json=_approve_request(incident.incident_id).model_dump(mode="json"),
    )
    resolved = client_pg.post(
        f"/api/v1/incidents/{finops.incident_id}/resolve",
        json={"resolution": "NO_FURTHER_ACTION"},
    )

    assert (executed.status_code, resolved.status_code) == (202, 200), (executed.json(), resolved.json())
    [started] = _events(db, incident.incident_id)
    [closed] = _events(db, finops.incident_id)
    for event in (started, closed):
        assert (event.actor_kind, event.actor_user_id, event.tenant_id) == (
            AuditActorKind.TENANT_USER, seed.user_id, tenant_id,
        )
    assert started.execution_id == executed.json()["execution_id"]


# --- DB 최후 관문 ----------------------------------------------------------------


def _row(incident, execution, **over):
    values = {
        "action": STARTED,
        "occurred_at": execution.created_at,
        "actor_kind": AuditActorKind.SYSTEM,
        "actor_user_id": None,
        "incident_id": incident.incident_id,
        "execution_id": execution.execution_id,
        "trigger_source": TriggerSource.PRE_MITIGATION_0_5S,
        "resolution": None,
    }
    values.update(over)
    return models.AuditEvent(**values)


_BAD_ROWS = {
    # 사람이 승인하지 않은 자동 발동에 사람 이름이 붙는다
    "person_on_auto_trigger": lambda user: {
        "actor_kind": AuditActorKind.PLATFORM_USER, "actor_user_id": user,
    },
    # 관제자 승인인데 행위자가 SYSTEM 이다
    "system_on_user_approval": lambda user: {"trigger_source": TriggerSource.USER_APPROVAL},
    # SYSTEM 은 사용자가 없다
    "system_with_user": lambda user: {"actor_user_id": user},
    "person_without_user": lambda user: {
        "actor_kind": AuditActorKind.TENANT_USER, "trigger_source": TriggerSource.USER_APPROVAL,
    },
    "started_without_trigger": lambda user: {"trigger_source": None},
    "started_with_resolution": lambda user: {"resolution": ResolutionJudgement.JUSTIFIED},
    # 종료 판단은 사람이 내린다 · 실행을 가리키지 않는다
    "resolved_by_system": lambda user: {
        "action": RESOLVED, "execution_id": None, "trigger_source": None,
        "resolution": ResolutionJudgement.JUSTIFIED,
    },
    "resolved_with_execution": lambda user: {
        "action": RESOLVED, "trigger_source": None, "resolution": ResolutionJudgement.JUSTIFIED,
        "actor_kind": AuditActorKind.PLATFORM_USER, "actor_user_id": user,
    },
}


@pytest.mark.parametrize("case", sorted(_BAD_ROWS))
def test_check_constraints_reject_mismatched_rows(
    db, make_incident, make_execution, audit_actor, case
):
    incident = make_incident(db)
    execution = make_execution(db, incident, trigger_source=TriggerSource.PRE_MITIGATION_0_5S)
    db.flush()

    with pytest.raises(IntegrityError, match="ck_audit_events"), db.begin_nested():
        db.add(_row(incident, execution, **_BAD_ROWS[case](audit_actor.user_id)))
        db.flush()


def test_one_execution_has_at_most_one_started_event(db, make_incident, make_execution):
    incident = make_incident(db)
    execution = make_execution(db, incident, trigger_source=TriggerSource.PRE_MITIGATION_0_5S)
    db.add(_row(incident, execution))
    db.flush()

    with pytest.raises(IntegrityError, match="uq_audit_events_execution_id"), db.begin_nested():
        db.add(_row(incident, execution))
        db.flush()


def test_rows_reject_update_and_delete_but_insert_is_allowed(db, make_incident, make_execution):
    incident = make_incident(db)
    execution = make_execution(db, incident, trigger_source=TriggerSource.PRE_MITIGATION_0_5S)
    db.add(_row(incident, execution))
    db.flush()

    with pytest.raises(DBAPIError, match="append-only"), db.begin_nested():
        db.execute(text("UPDATE audit_events SET occurred_at = now()"))
    with pytest.raises(DBAPIError, match="append-only"), db.begin_nested():
        db.execute(text("DELETE FROM audit_events"))
    other = make_execution(db, incident, trigger_source=TriggerSource.TIMEOUT_ISOLATION_1M)
    db.add(_row(incident, other, trigger_source=TriggerSource.TIMEOUT_ISOLATION_1M))
    db.flush()  # INSERT는 계속 허용
    assert len(audit_repo.list_by_incident(db, incident.incident_id)) == 2


# --- 쓰는 자리 -------------------------------------------------------------------


def test_every_execution_insert_in_workflows_is_recorded():
    """실행 행을 만드는 자리마다 같은 함수 안에서 감사 기록을 남긴다 — 새 자리가 기록을 빠뜨리지 않게."""
    source = WORKFLOWS.read_text(encoding="utf-8")
    blocks = source.split("\ndef ")
    creating = [block for block in blocks if "executions_repo.create_execution(" in block]
    assert len(creating) == 3  # 관제자 승인 · 자동 격리 · 자동 원복
    for block in creating:
        name = block.split("(", 1)[0]
        assert block.count("executions_repo.create_execution(") == 1, name
        assert block.count("audit_repo.record_execution_started(") == 1, name
    assert source.count("audit_repo.record_incident_resolved(") == 1
