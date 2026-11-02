# ==============================================================================
# [파일 설명]
# 인시던트 · 조치 경로의 테넌트 범위와 기록의 소속 (#533 · #492 PR 5) — 실제 PostgreSQL ·
# 실제 라우터 · 실제 접수(incident_intake).
#
# 계약: packages/schemas/api/tenants.py 머리말 「기존 경로의 테넌트 범위」.
#   - 범위 밖은 없는 것이다 — 목록에서 빠지고 상세 · 실행 · 종료는 404 INCIDENT_NOT_FOUND. 전체 범위는 명시
#     CENTRAL_ADMIN 뿐이며 소속 없는 MVP 기록(tenant_id NULL)까지 본다. TENANT_MANAGER 는 배정된 고객사만(여럿이면
#     각각, 배정 없음 = 아무것도 없음 — 호환 값 role=ADMIN 은 근거가 아니다), 고객사 계정은 자기 소속만 본다.
#   - 판정 순서: 역할(403 · 대상을 읽기 전) → 범위(404) → 같은 Key 재요청(200 재생 · 다른 조치면 409
#     IDEMPOTENCY_KEY_CONFLICT) → 대상 인시던트 테넌트의 정지(409) → 인시던트 · 제안 상태(409).
#   - 정지된 테넌트: 새 실행(복구 포함)은 플랫폼 계정도 409, 종료는 고객사 사용자만 409 — 플랫폼 계정(중앙 관리자 ·
#     그 고객사를 맡은 담당자)은 받는다. 조회는 그대로다. 거절은 기록 · 발행을 남기지 않는다.
#   - 쓰기 직전 재판정: 실행 · 종료는 Workflow 가 users 잠금을 잡고 세션 · 등급 · 역할 · 담당을 다시 판정한다 —
#     의존성이 통과시킨 뒤 회수된 권한으로는 쓰지 못한다. 감사 기록(T-2 · #544)의 행위자도 그 재판정 신원이다.
#   - 소속: 접수가 대상 계정을 등록한 테넌트를 인시던트에 붙이고, 실행은 모든 접수 경로(관제자 승인 ·
#     자동 격리 · 관제자 복구 · 자동 원복)에서 인시던트의 소속을 물려받는다. 고객별 자동 조치 끄기(T-6)와
#     조회 범위가 그 소속을 본다.
# ==============================================================================

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import incident_intake
import pytest
import workflows
from auth import sessions as auth_sessions
from db import models
from db.repositories import audit as audit_repo
from db.repositories import auth as auth_repo
from db.repositories import executions as executions_repo
from db.repositories import incidents as incidents_repo
from db.repositories import notification_events as events_repo
from db.repositories import tenants as tenants_repo
from schemas.api.actions import ExecutionStatus
from schemas.api.audit import AuditActorKind, AuditEventAction
from schemas.api.auth import PlatformRole, UserRole
from schemas.api.incidents import IncidentStatus
from schemas.api.tenants import TenantUpdateRequest
from schemas.api.ws import WsEventType
from schemas.backups import BackupType
from schemas.evidence import EvidenceType
from schemas.incidents import AgentInvocationStatus
from schemas.intake import SecOpsIncidentIntake
from schemas.runbooks import ROLLBACK_RUNBOOK_BY_MAIN_ID, RunbookId
from security import soar
from tests.test_auto_isolation_workflow import PRE, SKIP, _command, _pass_result

EXECUTE = "/api/v1/actions/execute"
REGION = "ap-northeast-2"
SUMMARY = ["요약 1", "요약 2", "요약 3"]


def _code(resp) -> str:
    return resp.json()["error"]["code"]


def _key() -> str:
    return str(uuid.uuid4())


def _incident(db, make_incident, make_executable, tenant_id: str | None) -> models.Incident:
    """분석이 끝나 승인 대기인 인시던트 + 실행 가능한 제안(NACL 차단) 하나 — 상세 · 실행 · 종료가 모두 받는 모양.

    종료는 SecOps 분석이 끝나야 받는다(workflows.resolve_incident) — 분석 대기로 두면 종료가 범위 · 정지와
    무관하게 INCIDENT_NOT_RESOLVABLE 이 된다.
    """
    incident = make_incident(db, summary_lines=SUMMARY)
    incident.agent_invocation_status = AgentInvocationStatus.SUCCEEDED
    make_executable(db, incident=incident)
    incident.tenant_id = tenant_id
    db.commit()
    return incident


def _execute(client, incident, key: str, runbook_id: RunbookId = RunbookId.RUNBOOK_NACL_ADD_DENY):
    return client.post(EXECUTE, json={
        "incident_id": incident.incident_id,
        "runbook_id": runbook_id.value,
        "idempotency_key": key,
    })


def _resolve(client, incident):
    return client.post(f"/api/v1/incidents/{incident.incident_id}/resolve",
                       json={"resolution": "NO_FURTHER_ACTION"})


def _change(client, incident, call: str):
    return _execute(client, incident, _key()) if call == "execute" else _resolve(client, incident)


def _suspend(db, tenant_id: str) -> None:
    assert tenants_repo.suspend_tenant(db, tenant_id, reason="해지", now=datetime.now(UTC))
    db.commit()


def _executions(db, incident: models.Incident) -> list[models.ActionExecution]:
    db.expire_all()
    return executions_repo.list_by_incident(db, incident.incident_id)


def _effects(db, incident: models.Incident) -> tuple:
    """거절이 남기면 안 되는 것 — 인시던트 상태 · 판단 · 시각, 제안 상태, 실행 행, 알림 사건, 감사 기록."""
    db.expire_all()
    row = db.get(models.Incident, incident.incident_id)
    return (
        row.status, row.resolution, row.resolved_at, row.updated_at,
        sorted((c.candidate_id, c.status) for c in incidents_repo.list_candidates(db, row.incident_id)),
        [e.execution_id for e in executions_repo.list_by_incident(db, row.incident_id)],
        [e.event_id for e in events_repo.list_by_incident(db, row.incident_id)],
        [e.event_id for e in audit_repo.list_by_incident(db, row.incident_id)],
    )


def _central(login_as):
    """전체 범위 — 명시 CENTRAL_ADMIN(client_pg 의 기본 세션과 같은 등급)."""
    return login_as(UserRole.ADMIN, platform_role=PlatformRole.CENTRAL_ADMIN)


def _manager(login_as, *tenant_ids: str):
    """담당자 — role=ADMIN 은 PLATFORM 의 호환 값일 뿐이고, 범위는 명시 배정뿐이다(생략 = 배정 없음)."""
    return login_as(
        UserRole.ADMIN, platform_role=PlatformRole.TENANT_MANAGER, assigned_tenant_ids=tenant_ids
    )


@pytest.fixture()
def two_tenants(db, make_tenant, make_incident, make_executable):
    """테넌트 A · B 와 각자의 인시던트 하나, 그리고 소속 없는 MVP 기록 하나."""
    a, b = make_tenant(), make_tenant()
    return {
        "a": a, "b": b,
        "mine": _incident(db, make_incident, make_executable, a),
        "theirs": _incident(db, make_incident, make_executable, b),
        "mvp": _incident(db, make_incident, make_executable, None),
    }


@pytest.fixture()
def published(client_pg, monkeypatch) -> list:
    events: list = []
    monkeypatch.setattr(client_pg.app.state.realtime, "publish", events.append)
    return events


# ----- 실제 접수의 소속 -------------------------------------------------------------

def _account_of(db, tenant_id: str) -> str:
    return db.get(models.Tenant, tenant_id).aws_account_id


def _unregistered_account() -> str:
    return f"{uuid.uuid4().int % 10**12:012d}"


def _high_intake(account: str) -> tuple[SecOpsIncidentIntake, str]:
    """그 계정 EC2 인스턴스의 High 위협 접수 입력 → (입력, 인스턴스 ID)."""
    instance = "i-0" + uuid.uuid4().hex[:16]
    arn = f"arn:aws:ec2:{REGION}:{account}:instance/{instance}"
    threat_id = str(uuid.uuid4())
    now = datetime.now(UTC).isoformat()
    intake = SecOpsIncidentIntake.model_validate({
        "title": "SSH 브루트포스 시도",
        "threat_event": {
            "threat_event_id": threat_id, "source_event_id": f"evt-{threat_id}",
            "event_type": "SSH_BRUTE_FORCE", "target_arn": arn, "occurred_at": now,
            "payload": {"source_ip": "203.0.113.10", "failed_attempt_count": 120,
                        "window_seconds": 300},
            "deduplication_key": f"SSH_BRUTE_FORCE:{instance}:203.0.113.10",
            "collected_at": now,
        },
        "initial_risk": {
            "threat_event_id": threat_id, "initial_risk_level": "HIGH",
            "response_mode": "PRE_MITIGATION_0_5S", "reason_codes": ["RISK_SSH_BRUTEFORCE"],
        },
    })
    return intake, instance


def _intake(db, account: str) -> tuple[str, SecOpsIncidentIntake, str]:
    intake, instance = _high_intake(account)
    outcome = incident_intake.create_incident_from_intake(db, intake)
    assert outcome.created
    return outcome.incident_id, intake, instance


def _isolate(db, incident_id: str, intake: SecOpsIncidentIntake, instance: str):
    """가드레일 PASS 판정을 실은 High 선차단 접수 — AWS 를 부르지 않는다."""
    evidence_id = incidents_repo.list_evidence(db, incident_id)[0].evidence_id
    return workflows.reserve_auto_isolation(
        db, incident_id, trigger_source=PRE, now=datetime.now(UTC), execution_id=str(uuid.uuid4()),
        validated_command=_command(evidence_id, target=intake.subject_arn, instance_id=instance),
        guardrail_result=_pass_result(),
    )


def _listed(client) -> set[str]:
    resp = client.get("/api/v1/incidents")
    assert resp.status_code == 200, resp.json()
    return {item["incident_id"] for item in resp.json()["items"]}


def test_accounts_see_what_the_intake_assigned_to_their_scope(db, client_pg, login_as, make_tenant):
    a, b = make_tenant(), make_tenant()
    mine, *_ = _intake(db, _account_of(db, a))
    theirs, *_ = _intake(db, _account_of(db, b))
    unowned, *_ = _intake(db, _unregistered_account())

    login_as(UserRole.VIEWER, tenant_id=a)
    assert _listed(client_pg) == {mine}
    assert client_pg.get(f"/api/v1/incidents/{mine}").status_code == 200
    for hidden in (theirs, unowned):
        resp = client_pg.get(f"/api/v1/incidents/{hidden}")
        assert (resp.status_code, _code(resp)) == (404, "INCIDENT_NOT_FOUND")

    _manager(login_as, b)
    assert _listed(client_pg) == {theirs}

    _central(login_as)
    assert {mine, theirs, unowned} <= _listed(client_pg)


def test_disabled_tenant_suppresses_automatic_isolation_of_its_intake(db, make_tenant, monkeypatch):
    tenant_id = make_tenant()
    assert tenants_repo.update_tenant(
        db, tenant_id, TenantUpdateRequest(auto_action_enabled=False), now=datetime.now(UTC))
    db.commit()
    incident_id, intake, instance = _intake(db, _account_of(db, tenant_id))

    # 사전 판정은 테넌트 관문에서 멈춘다 — 그 뒤의 대상 해석 · 가드레일(④ AWS)에 닿지 않는다
    reached: list[str] = []
    real_parse = soar.executor.parse_arn
    monkeypatch.setattr(soar.executor, "parse_arn", lambda arn: reached.append(arn) or real_parse(arn))
    assert soar.prepare_isolation(db, incident_id, PRE) is None
    assert reached == []

    result = _isolate(db, incident_id, intake, instance)
    assert (result.skipped_reason, result.execution_id) == (SKIP.AUTO_ACTION_DISABLED, None)
    assert executions_repo.list_by_incident(db, incident_id) == []


# ----- 실행은 인시던트의 소속을 물려받는다 ----------------------------------------------

OWNERSHIP = pytest.mark.parametrize("owned", [True, False], ids=["tenant", "mvp-record"])


def _execution_tenant(db, execution_id: str) -> str | None:
    db.expire_all()
    return db.get(models.ActionExecution, execution_id).tenant_id


@OWNERSHIP
def test_manual_execution_takes_the_incident_tenant(db, client_pg, make_tenant, make_incident, make_executable, owned):
    tenant_id = make_tenant() if owned else None
    incident = _incident(db, make_incident, make_executable, tenant_id)

    resp = _execute(client_pg, incident, _key())

    assert resp.status_code == 202, resp.json()
    assert _execution_tenant(db, resp.json()["execution_id"]) == tenant_id


@OWNERSHIP
def test_operator_recovery_takes_the_incident_tenant(
    db, client_pg, make_tenant, make_incident, make_executable, make_execution, owned
):
    tenant_id = make_tenant() if owned else None
    incident = _incident(db, make_incident, make_executable, tenant_id)
    # ORM 시드라 원본 행에는 소속이 없다 — 자식의 소속은 원본이 아니라 인시던트에서 온다
    origin = make_execution(db, incident, runbook_id=RunbookId.RUNBOOK_EC2_RIGHTSIZING,
                            status=ExecutionStatus.SUCCESS)
    db.commit()

    resp = _execute(client_pg, incident, _key(),
                    RunbookId(ROLLBACK_RUNBOOK_BY_MAIN_ID[origin.runbook_id.value]))

    assert resp.status_code == 202, resp.json()
    child = db.get(models.ActionExecution, resp.json()["execution_id"])
    assert child.parent_execution_id == origin.execution_id
    assert _execution_tenant(db, child.execution_id) == tenant_id


@OWNERSHIP
def test_automatic_isolation_of_a_real_intake_takes_the_incident_tenant(db, make_tenant, owned):
    tenant_id = make_tenant() if owned else None
    account = _account_of(db, tenant_id) if owned else _unregistered_account()
    incident_id, intake, instance = _intake(db, account)

    result = _isolate(db, incident_id, intake, instance)

    assert result.created and result.execution_status is ExecutionStatus.IN_PROGRESS
    assert _execution_tenant(db, result.execution_id) == tenant_id


@OWNERSHIP
def test_automatic_rollback_takes_the_incident_tenant(db, make_tenant, make_incident, make_execution, owned):
    tenant_id = make_tenant() if owned else None
    incident = make_incident(db, status=IncidentStatus.ACTION_IN_PROGRESS)
    incident.tenant_id = tenant_id
    db.add(models.Evidence(
        incident_id=incident.incident_id, evidence_type=EvidenceType.THREAT,
        source_type="threat_event", source_id="seed", content={}, occurred_at=datetime.now(UTC),
    ))
    origin = make_execution(db, incident, runbook_id=RunbookId.RUNBOOK_EC2_RIGHTSIZING,
                            status=ExecutionStatus.ROLLBACK_INITIATED)
    record = executions_repo.create_backup_record(
        db, execution_id=origin.execution_id, target_arn=origin.target_arn,
        backup_type=BackupType.SAVE_INSTANCE_SPEC_JSON.value,
        payload={"instance_id": "i-0aaa", "instance_type": "t3.large", "state": "running"},
    )
    assert executions_repo.bind_backup_record(db, origin.execution_id, record.backup_record_id)
    db.commit()

    started = workflows.initiate_auto_rollback(db, origin.execution_id)

    assert started.execution_id is not None, started.skipped_reason
    assert _execution_tenant(db, started.execution_id) == tenant_id


# ----- 범위 밖은 없는 것이다 ------------------------------------------------------

def test_customer_can_read_and_resolve_its_active_tenant_incident(
    db, client_pg, login_as, two_tenants, published
):
    mine = two_tenants["mine"]
    login_as(UserRole.OPERATOR, tenant_id=two_tenants["a"])
    detail = client_pg.get(f"/api/v1/incidents/{mine.incident_id}")
    assert detail.status_code == 200
    assert detail.json()["status"] == "AWAITING_APPROVAL"

    resolved = _resolve(client_pg, mine)

    assert resolved.status_code == 200, resolved.json()
    assert resolved.json()["status"] == "RESOLVED"
    assert client_pg.get(f"/api/v1/incidents/{mine.incident_id}").json()["status"] == "RESOLVED"
    db.expire_all()
    row = db.get(models.Incident, mine.incident_id)
    assert row.status is IncidentStatus.RESOLVED
    assert row.resolved_at is not None
    assert [event.event_type for event in published] == [WsEventType.INCIDENT_UPDATED]


def test_tenant_account_lists_only_its_own_incidents(client_pg, login_as, two_tenants):
    login_as(UserRole.OPERATOR, tenant_id=two_tenants["a"])
    assert _listed(client_pg) == {two_tenants["mine"].incident_id}


def test_central_admin_lists_every_incident_including_mvp_records(client_pg, login_as, two_tenants):
    _central(login_as)
    assert {two_tenants[k].incident_id for k in ("mine", "theirs", "mvp")} <= _listed(client_pg)


@pytest.mark.parametrize("target", ["theirs", "mvp"])
def test_out_of_scope_detail_is_404(client_pg, login_as, two_tenants, target):
    login_as(UserRole.VIEWER, tenant_id=two_tenants["a"])
    resp = client_pg.get(f"/api/v1/incidents/{two_tenants[target].incident_id}")
    assert (resp.status_code, _code(resp)) == (404, "INCIDENT_NOT_FOUND")


@pytest.mark.parametrize("target", ["theirs", "mvp"])
@pytest.mark.parametrize("call", ["execute", "resolve"])
def test_out_of_scope_change_is_404_and_leaves_no_trace(
    db, client_pg, login_as, two_tenants, published, call, target
):
    login_as(UserRole.OPERATOR, tenant_id=two_tenants["a"])
    incident = two_tenants[target]
    before = _effects(db, incident)

    resp = _change(client_pg, incident, call)

    assert (resp.status_code, _code(resp)) == (404, "INCIDENT_NOT_FOUND")
    assert _effects(db, incident) == before
    assert published == []


# ----- 담당자(TENANT_MANAGER)는 배정된 고객사만 -------------------------------------

@pytest.mark.parametrize(("assigned", "visible"), [
    (("a",), {"mine"}),
    (("a", "b"), {"mine", "theirs"}),
    ((), set()),
], ids=["one-assignment", "multiple-assignments", "unassigned"])
def test_manager_reads_only_its_assigned_tenants(client_pg, login_as, two_tenants, assigned, visible):
    _manager(login_as, *(two_tenants[key] for key in assigned))

    assert _listed(client_pg) == {two_tenants[key].incident_id for key in visible}
    for key in ("mine", "theirs", "mvp"):
        resp = client_pg.get(f"/api/v1/incidents/{two_tenants[key].incident_id}")
        expected = (200, None) if key in visible else (404, "INCIDENT_NOT_FOUND")
        assert (resp.status_code, resp.json().get("error", {}).get("code")) == expected, key


def test_manager_executes_on_its_assigned_tenant(db, client_pg, login_as, two_tenants):
    _manager(login_as, two_tenants["a"])

    resp = _execute(client_pg, two_tenants["mine"], _key())

    assert resp.status_code == 202, resp.json()
    assert _execution_tenant(db, resp.json()["execution_id"]) == two_tenants["a"]


def test_manager_with_multiple_assignments_resolves_each(db, client_pg, login_as, two_tenants, published):
    _manager(login_as, two_tenants["a"], two_tenants["b"])

    for key in ("mine", "theirs"):
        resp = _resolve(client_pg, two_tenants[key])
        assert resp.status_code == 200, resp.json()
        assert resp.json()["status"] == "RESOLVED"
    assert [event.event_type for event in published] == [WsEventType.INCIDENT_UPDATED] * 2


@pytest.mark.parametrize(("assigned", "target"), [
    (("a",), "theirs"),
    (("a",), "mvp"),
    ((), "mine"),
    ((), "theirs"),
    ((), "mvp"),
], ids=["a-vs-b", "a-vs-mvp", "unassigned-vs-a", "unassigned-vs-b", "unassigned-vs-mvp"])
@pytest.mark.parametrize("call", ["execute", "resolve"])
def test_manager_change_outside_its_assignments_is_404_and_leaves_no_trace(
    db, client_pg, login_as, two_tenants, published, call, assigned, target
):
    _manager(login_as, *(two_tenants[key] for key in assigned))
    incident = two_tenants[target]
    before = _effects(db, incident)

    resp = _change(client_pg, incident, call)

    assert (resp.status_code, _code(resp)) == (404, "INCIDENT_NOT_FOUND")
    assert _effects(db, incident) == before
    assert published == []


# ----- 판정 순서 --------------------------------------------------------------

def _record_incident_reads(monkeypatch) -> list[str]:
    reads: list[str] = []
    for name in ("get_incident", "lock_incident"):
        real = getattr(incidents_repo, name)

        def spy(*args, _real=real, _name=name, **kwargs):
            reads.append(_name)
            return _real(*args, **kwargs)

        monkeypatch.setattr(incidents_repo, name, spy)
    return reads


def test_role_is_judged_before_the_target_is_read(db, client_pg, login_as, two_tenants, monkeypatch):
    login_as(UserRole.VIEWER, tenant_id=two_tenants["a"])
    reads = _record_incident_reads(monkeypatch)
    missing = SimpleNamespace(incident_id=str(uuid.uuid4()))

    for target in (two_tenants["mine"], two_tenants["theirs"], two_tenants["mvp"], missing):
        for resp in (_execute(client_pg, target, _key()), _resolve(client_pg, target)):
            assert (resp.status_code, _code(resp)) == (403, "AUTH_FORBIDDEN")
    assert reads == []


OUTSIDERS = pytest.mark.parametrize("outsider", [
    lambda login_as, tenants: login_as(UserRole.OPERATOR, tenant_id=tenants["a"]),
    lambda login_as, tenants: _manager(login_as, tenants["a"]),
    lambda login_as, tenants: _manager(login_as),
], ids=["customer-a", "manager-a", "manager-unassigned"])


@OUTSIDERS
def test_scope_is_judged_before_replaying_another_tenants_key(db, client_pg, login_as, two_tenants, outsider):
    theirs, key = two_tenants["theirs"], _key()
    accepted = _execute(client_pg, theirs, key)  # CENTRAL_ADMIN 이 B 의 인시던트에 접수했다
    assert accepted.status_code == 202

    outsider(login_as, two_tenants)
    replay = _execute(client_pg, theirs, key)  # 같은 Key · 같은 요청이어도 범위 밖 실행 응답은 재생되지 않는다

    assert (replay.status_code, _code(replay)) == (404, "INCIDENT_NOT_FOUND")
    assert accepted.json()["execution_id"] not in replay.text
    assert [e.execution_id for e in _executions(db, theirs)] == [accepted.json()["execution_id"]]


@OUTSIDERS
def test_scope_is_judged_before_another_tenants_suspension(db, client_pg, login_as, two_tenants, outsider):
    _suspend(db, two_tenants["b"])
    outsider(login_as, two_tenants)
    for resp in (_execute(client_pg, two_tenants["theirs"], _key()), _resolve(client_pg, two_tenants["theirs"])):
        assert (resp.status_code, _code(resp)) == (404, "INCIDENT_NOT_FOUND")


def test_same_key_is_judged_before_suspension(db, client_pg, login_as, two_tenants):
    mine, key = two_tenants["mine"], _key()
    login_as(UserRole.OPERATOR, tenant_id=two_tenants["a"])
    accepted = _execute(client_pg, mine, key)
    assert accepted.status_code == 202
    _suspend(db, two_tenants["a"])

    # 정지 전에 접수된 실행의 재요청은 새 예약을 만들지 않으므로 200 재생이다
    replay = _execute(client_pg, mine, key)
    assert replay.status_code == 200
    assert replay.json()["execution_id"] == accepted.json()["execution_id"]
    # 같은 Key 로 다른 조치를 요청하면 정지보다 앞의 Key 충돌이다
    conflict = _execute(client_pg, mine, key, RunbookId.RUNBOOK_EBS_DELETE_UNATTACHED)
    assert (conflict.status_code, _code(conflict)) == (409, "IDEMPOTENCY_KEY_CONFLICT")
    # 새 Key 는 정지에 걸린다
    fresh = _execute(client_pg, mine, _key())
    assert (fresh.status_code, _code(fresh)) == (409, "TENANT_SUSPENDED")

    assert [e.execution_id for e in _executions(db, mine)] == [accepted.json()["execution_id"]]


def test_suspension_is_judged_before_proposal_and_incident_state(db, client_pg, login_as, two_tenants):
    mine = two_tenants["mine"]
    # 제안이 없는 조치 — 정지 전에는 제안 상태의 409 다
    unproposed = _execute(client_pg, mine, _key(), RunbookId.RUNBOOK_EBS_DELETE_UNATTACHED)
    assert (unproposed.status_code, _code(unproposed)) == (409, "PROPOSAL_NOT_EXECUTABLE")
    _suspend(db, two_tenants["a"])

    unproposed = _execute(client_pg, mine, _key(), RunbookId.RUNBOOK_EBS_DELETE_UNATTACHED)
    assert (unproposed.status_code, _code(unproposed)) == (409, "TENANT_SUSPENDED")
    # 이미 종료된 건의 재요청(200)은 Key 가 아니라 인시던트 상태로 판정하므로 정지 뒤 자리다
    assert _resolve(client_pg, mine).status_code == 200  # CENTRAL_ADMIN 의 정리
    login_as(UserRole.OPERATOR, tenant_id=two_tenants["a"])
    again = _resolve(client_pg, mine)
    assert (again.status_code, _code(again)) == (409, "TENANT_SUSPENDED")


# ----- 정지된 테넌트 ------------------------------------------------------------

def test_new_execution_on_a_suspended_tenant_is_409_even_for_the_platform(
    db, client_pg, two_tenants, published
):
    mine = two_tenants["mine"]
    _suspend(db, two_tenants["a"])
    before = _effects(db, mine)

    resp = _execute(client_pg, mine, _key())

    assert (resp.status_code, _code(resp)) == (409, "TENANT_SUSPENDED")
    assert _effects(db, mine) == before
    assert published == []


def test_operator_recovery_on_a_suspended_tenant_is_409_even_for_the_platform(
    db, client_pg, make_execution, two_tenants
):
    mine = two_tenants["mine"]
    origin = make_execution(db, mine, runbook_id=RunbookId.RUNBOOK_EC2_RIGHTSIZING,
                            status=ExecutionStatus.SUCCESS)
    db.commit()
    _suspend(db, two_tenants["a"])

    resp = _execute(client_pg, mine, _key(), RunbookId(ROLLBACK_RUNBOOK_BY_MAIN_ID[origin.runbook_id.value]))

    assert (resp.status_code, _code(resp)) == (409, "TENANT_SUSPENDED")
    assert [e.execution_id for e in _executions(db, mine)] == [origin.execution_id]


@pytest.mark.parametrize("platform", [
    lambda login_as, tenants: _central(login_as),
    lambda login_as, tenants: _manager(login_as, tenants["a"]),
], ids=["central-admin", "assigned-manager"])
def test_resolve_on_a_suspended_tenant_is_409_for_its_users_but_allowed_for_platform_support(
    db, client_pg, login_as, two_tenants, published, platform
):
    """정지 종료 예외는 범위가 아니라 계정 종류로 판정한다 — 담당자는 전체 범위가 아니어도 플랫폼 운영자다."""
    mine = two_tenants["mine"]
    _suspend(db, two_tenants["a"])
    before = _effects(db, mine)

    login_as(UserRole.OPERATOR, tenant_id=two_tenants["a"])
    refused = _resolve(client_pg, mine)
    assert (refused.status_code, _code(refused)) == (409, "TENANT_SUSPENDED")
    assert _effects(db, mine) == before
    assert published == []

    platform(login_as, two_tenants)  # 해지한 고객의 열린 인시던트 정리
    resolved = _resolve(client_pg, mine)
    assert resolved.status_code == 200, resolved.json()
    assert resolved.json()["status"] == "RESOLVED"
    assert [event.event_type for event in published] == [WsEventType.INCIDENT_UPDATED]


def test_assigned_manager_cannot_start_a_new_execution_on_a_suspended_tenant(
    db, client_pg, login_as, two_tenants, published
):
    mine = two_tenants["mine"]
    _suspend(db, two_tenants["a"])
    _manager(login_as, two_tenants["a"])
    before = _effects(db, mine)

    resp = _execute(client_pg, mine, _key())

    assert (resp.status_code, _code(resp)) == (409, "TENANT_SUSPENDED")
    assert _effects(db, mine) == before
    assert published == []


def test_suspended_tenant_users_can_still_read_their_records(db, client_pg, login_as, two_tenants):
    _suspend(db, two_tenants["a"])
    login_as(UserRole.VIEWER, tenant_id=two_tenants["a"])
    assert client_pg.get(f"/api/v1/incidents/{two_tenants['mine'].incident_id}").status_code == 200
    assert _listed(client_pg) == {two_tenants["mine"].incident_id}


# ----- 쓰기 직전 재판정 — 의존성 통과 뒤의 회수 ---------------------------------------------
#
# 회수는 Workflow 가 users 잠금을 잡기 직전에 같은 트랜잭션에서 일어난다 — 의존성(역할 · 스냅샷 범위)이 이미
# 통과시킨 뒤 관리 쓰기가 commit 된 것과 같다. 관리 쓰기도 같은 잠금을 잡으므로 잠금 뒤에는 끼어들 수 없다.

def _now() -> datetime:
    return datetime.now(UTC)


REVOCATIONS = pytest.mark.parametrize(("login", "revoke", "expected"), [
    pytest.param(
        lambda login_as, tenants: _manager(login_as, tenants["a"]),
        lambda db, user_id: auth_repo.replace_assigned_tenants(db, user_id=user_id, tenant_ids=(), now=_now()),
        (404, "INCIDENT_NOT_FOUND"), id="assignment",
    ),
    pytest.param(
        lambda login_as, tenants: _central(login_as),
        lambda db, user_id: auth_repo.set_platform_role(
            db, user_id=user_id, platform_role=PlatformRole.TENANT_MANAGER, now=_now()),
        (404, "INCIDENT_NOT_FOUND"), id="grade",
    ),
    pytest.param(
        lambda login_as, tenants: login_as(UserRole.OPERATOR, tenant_id=tenants["a"]),
        lambda db, user_id: auth_repo.set_role(db, user_id=user_id, role=UserRole.VIEWER, now=_now()),
        (403, "AUTH_FORBIDDEN"), id="customer-role",
    ),
    pytest.param(
        lambda login_as, tenants: _manager(login_as, tenants["a"]),
        lambda db, user_id: auth_sessions.revoke_user_sessions(
            db, user_id=user_id, now=_now(), reason=models.AuthSessionRevokeReason.ADMIN_REVOKED),
        (401, "AUTH_REQUIRED"), id="session",
    ),
])


@REVOCATIONS
@pytest.mark.parametrize("call", ["execute", "resolve"])
def test_authority_revoked_after_the_dependencies_cannot_authorize_the_write(
    db, client_pg, login_as, two_tenants, published, monkeypatch, call, login, revoke, expected
):
    mine = two_tenants["mine"]
    seed = login(login_as, two_tenants)
    revoked: list[str] = []
    real_lock = auth_repo.lock_users_table

    def revoke_then_lock(session):
        if not revoked:
            revoke(session, seed.user_id)
            revoked.append(seed.user_id)
        real_lock(session)

    monkeypatch.setattr(auth_repo, "lock_users_table", revoke_then_lock)
    before = _effects(db, mine)

    resp = _change(client_pg, mine, call)

    assert revoked == [seed.user_id]  # 의존성을 통과해 Workflow 의 쓰기 직전 잠금까지 왔다
    assert (resp.status_code, _code(resp)) == expected
    assert _effects(db, mine) == before
    assert published == []


@pytest.mark.parametrize("call", ["execute", "resolve"])
def test_manager_write_is_audited_as_that_platform_user_only_while_assigned(
    db, client_pg, login_as, two_tenants, published, monkeypatch, call
):
    """담당자의 쓰기는 그 사용자(PLATFORM_USER) · 대상 고객사로 기록되고, 의존성 통과 뒤 그 배정이 회수되면
    쓰기도 기록도 없다 — 기록 행위자와 범위가 같은 재판정 신원에서 나온다."""
    a, b = two_tenants["a"], two_tenants["b"]
    theirs, mine = two_tenants["theirs"], two_tenants["mine"]
    seed = _manager(login_as, a, b)

    allowed = _change(client_pg, theirs, call)

    assert allowed.status_code == (202 if call == "execute" else 200), allowed.json()
    db.expire_all()
    [event] = audit_repo.list_by_incident(db, theirs.incident_id)
    action = AuditEventAction.ACTION_EXECUTION_STARTED if call == "execute" else AuditEventAction.INCIDENT_RESOLVED
    assert (event.action, event.actor_kind, event.actor_user_id, event.tenant_id) == (
        action, AuditActorKind.PLATFORM_USER, seed.user_id, b,
    )

    real_lock = auth_repo.lock_users_table

    def revoke_a_then_lock(session):
        auth_repo.replace_assigned_tenants(session, user_id=seed.user_id, tenant_ids=(b,), now=_now())
        real_lock(session)

    monkeypatch.setattr(auth_repo, "lock_users_table", revoke_a_then_lock)
    before = _effects(db, mine)
    published.clear()

    refused = _change(client_pg, mine, call)

    assert (refused.status_code, _code(refused)) == (404, "INCIDENT_NOT_FOUND")
    assert _effects(db, mine) == before
    assert published == []
