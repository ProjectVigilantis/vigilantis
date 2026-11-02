# ==============================================================================
# [파일 설명]
# 테넌트 정지 관문 (Post-MVP T-4 2단계 D · #533 · ADR-0012 §3) — 실제 PostgreSQL.
#
# 지키는 것.
#   ① 정지된 테넌트에는 자동 발동(High 선차단 · Medium 1분 자동 격리)이 접수되지 않는다 — 사전 판정은
#      AWS(가드레일 ④)를 부르기 전에, 접수 관문은 잠근 뒤 같은 함수로 다시 본다. 사유는 TENANT_SUSPENDED
#      이고 끄기(AUTO_ACTION_DISABLED)보다 먼저다. 인시던트 상세의 auto_isolation_block 도 같은 값이다.
#   ② 1분 감시 목록이 정지 테넌트의 건을 고르지 않는다.
#   ③ 시작 전 예약(단계 0건)은 시작하지 않는다 — runner 에 넘기기 전이라 AWS 호출이 없고, FAILED(변경 없이
#      실패)로 확정된다. 예약 뒤 첫 단계 전에 정지돼도 첫 단계를 쓰는 트랜잭션이 다시 보고 멈춘다.
#   ④ 정지와 시작의 경합 — 정지 UPDATE 는 첫 단계 트랜잭션의 FOR SHARE 를 기다리고(pg_stat_activity 로
#      관측), 먼저 commit 된 정지는 관문에서 보인다. runner 진입 전 사전 판정은 잠그지 않아 AWS 읽기 동안
#      정지를 기다리게 하지 않는다.
#   ⑤ 단계를 남긴 실행과 그 자동 원복(AUTO_ON_FAILURE · 정지 뒤에 생겨도)은 끝까지 돈다.
#   ⑥ 테넌트 없음(NULL)은 지금 동작 그대로다.
# ==============================================================================

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime, timedelta

import pytest
import workflows
from db import models
from db.repositories import executions as exec_repo
from db.repositories import incidents as incidents_repo
from db.repositories import tenants as tenants_repo
from schemas.api.actions import ExecutionStatus
from schemas.api.errors import ErrorCode
from schemas.api.incidents import AutoIsolationBlock, IncidentStatus
from schemas.api.tenants import TenantStatus, TenantUpdateRequest
from schemas.executions import (
    EXECUTION_RECOVERABLE_STATUSES,
    ExecutionStepResult,
    ExecutionStepStatus,
)
from schemas.runbooks import RunbookId, TriggerSource
from security import soar
from services.aws import backup as bk
from services.aws import executor as ex
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session
from tests.test_auto_isolation_workflow import (
    PRE,
    SKIP,
    TIMEOUT,
    _attempt,
    _auto_evaluations,
    _executions,
)
from tests.test_auto_rollback_workflow import (
    APPLIED_TYPE,
    BACKUP_TYPE,
    INSTANCE,
    INSTANCE_ARN,
    cycle,
    only_child,
    operations,
    reserve_manual_revert,
    statuses,
)
from tests.test_auto_rollback_workflow import aws, rolled_back_origin  # noqa: F401 — 픽스처는 이름으로 등록된다
from tests.test_incident_auto_isolation_block import _block
from tests.test_tenant_auto_action import _seeded, _tenant, _toggle

NOW = datetime(2026, 10, 30, 6, 0, tzinfo=UTC)
SUSPENDED_PREFIX = ErrorCode.TENANT_SUSPENDED.value


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _suspend(db, tenant_id: str) -> None:
    assert tenants_repo.suspend_tenant(db, tenant_id, reason="해지", now=_utcnow())
    db.commit()


def _attach_all(db, incident_id: str, tenant_id: str) -> None:
    """인시던트와 그 실행 전부를 테넌트에 소속시킨다 — 시드 팩토리는 테넌트를 모른다."""
    db.get(models.Incident, incident_id).tenant_id = tenant_id
    for row in exec_repo.list_by_incident(db, incident_id):
        row.tenant_id = tenant_id
    db.commit()


def _row(status: TenantStatus, *, enabled: bool = True) -> models.Tenant:
    return models.Tenant(status=status, auto_action_enabled=enabled, auto_action_resumed_at=None)


@pytest.fixture()
def committed_tenant(pg_engine):
    """테스트 트랜잭션(db) 밖에 실제로 commit 된 ACTIVE 테넌트 → tenant_id. 다른 세션이 정지시킬 수 있다.

    db 픽스처보다 먼저 요청해야 한다 — 그래야 db 의 외부 트랜잭션이 되돌아간 뒤(참조 행이 사라진 뒤)에
    이 행을 지운다.
    """
    with Session(pg_engine) as setup:
        tenant_id = _tenant(setup, account="999988887775").tenant_id
        setup.commit()
    yield tenant_id
    with Session(pg_engine) as cleanup:
        cleanup.delete(cleanup.get(models.Tenant, tenant_id))
        cleanup.commit()


# ----- ① 관문 함수 · 접수 · 사전 판정 · 상세 응답 ------------------------------------

@pytest.mark.parametrize("trigger", [PRE, TIMEOUT])
@pytest.mark.parametrize("enabled", [True, False], ids=["enabled", "disabled-too"])
def test_gate_reports_suspension_before_the_auto_action_switch(trigger, enabled):
    reason = workflows.automatic_trigger_block(
        _row(TenantStatus.SUSPENDED, enabled=enabled),
        trigger_source=trigger, received_at=NOW, response_deadline_at=NOW)
    assert reason is SKIP.TENANT_SUSPENDED


@pytest.mark.parametrize("status", [TenantStatus.ACTIVE, TenantStatus.PENDING_CONNECTION])
def test_gate_is_unchanged_for_tenants_that_are_not_suspended(status):
    assert workflows.automatic_trigger_block(
        _row(status), trigger_source=TIMEOUT, received_at=NOW, response_deadline_at=NOW) is None
    assert workflows.automatic_trigger_block(
        _row(status, enabled=False), trigger_source=TIMEOUT, received_at=NOW, response_deadline_at=NOW,
    ) is SKIP.AUTO_ACTION_DISABLED


@pytest.mark.parametrize("kind, trigger", [("high", PRE), ("medium", TIMEOUT)])
def test_suspended_tenant_never_reserves_an_automatic_isolation(
    db, make_incident, make_candidate, kind, trigger
):
    tenant = _tenant(db)
    seeded = _seeded(db, make_incident, make_candidate, kind, tenant)
    _suspend(db, tenant.tenant_id)

    for rejected in (False, True):  # 거절 판정을 넘겨도 기록하지 않는다
        result = _attempt(db, seeded, trigger, rejected=rejected)
        assert (result.skipped_reason, result.execution_id) == (SKIP.TENANT_SUSPENDED, None)

    assert _executions(db, seeded.incident_id) == []
    assert _auto_evaluations(db, seeded.incident_id) == []


def test_preparation_stops_before_any_aws_step_when_suspended(
    db, make_incident, make_candidate, monkeypatch
):
    tenant = _tenant(db)
    seeded = _seeded(db, make_incident, make_candidate, "high", tenant)
    _suspend(db, tenant.tenant_id)
    # 관문 바로 뒤가 대상 파싱이고, 그 뒤가 파라미터 조회 · 가드레일(④ AWS Dry-Run)이다
    monkeypatch.setattr(soar.executor, "parse_arn", lambda arn: pytest.fail("관문을 지나 대상을 읽었다"))
    monkeypatch.setattr(soar.guardrails, "run_guardrail_validation",
                        lambda *a, **k: pytest.fail("가드레일이 불렸다"))

    assert soar.prepare_isolation(db, seeded.incident_id, PRE) is None


@pytest.mark.parametrize("kind, trigger", [("medium", TIMEOUT), ("high", PRE)])
def test_detail_shows_tenant_suspended_and_the_gate_agrees(
    db, client_pg, make_incident, make_candidate, kind, trigger
):
    tenant = _tenant(db)
    seeded = _seeded(db, make_incident, make_candidate, kind, tenant)
    assert _block(client_pg, seeded.incident_id) is None
    _suspend(db, tenant.tenant_id)
    assert _block(client_pg, seeded.incident_id) == AutoIsolationBlock.TENANT_SUSPENDED.value
    # 화면이 본 값과 서버가 실제로 접수하지 않는 이유가 같다
    assert _attempt(db, seeded, trigger).skipped_reason is SKIP.TENANT_SUSPENDED


def test_detail_prefers_suspension_over_the_auto_action_switch(
    db, client_pg, make_incident, make_candidate
):
    tenant = _tenant(db, enabled=False)
    seeded = _seeded(db, make_incident, make_candidate, "medium", tenant)
    assert _block(client_pg, seeded.incident_id) == AutoIsolationBlock.AUTO_ACTION_DISABLED.value
    _suspend(db, tenant.tenant_id)
    assert _block(client_pg, seeded.incident_id) == AutoIsolationBlock.TENANT_SUSPENDED.value
    # 정지 중에 자동 조치를 다시 켜도 정지가 남아 있다
    assert tenants_repo.update_tenant(
        db, tenant.tenant_id, TenantUpdateRequest(auto_action_enabled=True), now=_utcnow())
    db.commit()
    assert _block(client_pg, seeded.incident_id) == AutoIsolationBlock.TENANT_SUSPENDED.value


# ----- ② 감시 목록 --------------------------------------------------------------

def test_watch_list_skips_suspended_tenants(db, make_incident, make_candidate):
    tenant = _tenant(db)
    seeded = _seeded(db, make_incident, make_candidate, "medium", tenant)
    assert seeded.incident_id in incidents_repo.list_expired_agent_waits(db, now=_utcnow())
    _suspend(db, tenant.tenant_id)
    assert seeded.incident_id not in incidents_repo.list_expired_agent_waits(db, now=_utcnow())


# ----- ③ 시작 전 예약 ------------------------------------------------------------

def _reserved(db, make_incident, make_candidate, make_execution, tenant_id: str | None):
    """승인 접수 직후 상태 — IN_PROGRESS RIGHTSIZING 실행 1건 · 단계 0건 (tests.test_dispatcher 의 조합)."""
    from schemas.api.incidents import IncidentCategory
    from schemas.candidates import CandidateStatus

    incident = make_incident(
        db, category=IncidentCategory.FINOPS, subject_arn=INSTANCE_ARN, status=IncidentStatus.ANALYZING,
    )
    candidate = make_candidate(
        db, incident, runbook_id=RunbookId.RUNBOOK_EC2_RIGHTSIZING, target_arn=INSTANCE_ARN,
        parameters={"target_instance_type": APPLIED_TYPE}, status=CandidateStatus.CLAIMED,
    )
    incidents_repo.update_incident_status(
        db, incident.incident_id, expected=incident.status, next_status=IncidentStatus.ACTION_IN_PROGRESS,
    )
    execution = make_execution(
        db, incident, runbook_id=RunbookId.RUNBOOK_EC2_RIGHTSIZING, target_arn=INSTANCE_ARN,
        candidate=candidate,
    )
    db.commit()
    if tenant_id is not None:
        _attach_all(db, incident.incident_id, tenant_id)
    return incident.incident_id, execution.execution_id


def test_reservation_on_a_suspended_tenant_is_not_started(
    db, make_incident, make_candidate, make_execution, make_tenant, aws
):
    tenant_id = make_tenant()
    incident_id, execution_id = _reserved(db, make_incident, make_candidate, make_execution, tenant_id)
    _suspend(db, tenant_id)

    report = cycle(db)

    assert (report.scanned, report.started, report.closed) == (1, 0, 1)
    assert aws.calls == []  # 백업 캡처(AWS 읽기)도 없다
    assert exec_repo.list_steps(db, execution_id) == []
    assert statuses(db, incident_id, execution_id) == (IncidentStatus.FAILED, ExecutionStatus.FAILED)
    row = exec_repo.get_execution(db, execution_id)
    assert row.finished_at is not None
    assert row.error_summary.startswith(SUSPENDED_PREFIX + ": ")
    # 확정은 한 번이다 — 다음 주기는 집지 않는다
    assert cycle(db).scanned == 0


def test_suspension_between_reservation_and_first_step_stops_before_the_first_change(
    committed_tenant, db, pg_engine, make_incident, make_candidate, make_execution, aws, monkeypatch
):
    """runner 진입(관문 통과) 뒤, 백업 캡처(AWS 읽기) 동안 **다른 세션**이 정지를 commit 한다 —
    첫 단계를 쓰는 트랜잭션이 그 정지를 보고 멈춘다. 정지 세션은 lock_timeout 으로 묶는다 — 사전 판정이
    잠금을 들고 AWS 를 부르면 여기서 멈추지 않고 실패한다."""
    tenant_id = committed_tenant
    incident_id, execution_id = _reserved(db, make_incident, make_candidate, make_execution, tenant_id)
    fake_factory = bk.aws_client

    class SuspendDuringCapture:
        def __init__(self, inner):
            self._inner = inner

        def __getattr__(self, operation):
            call = getattr(self._inner, operation)
            if operation != "describe_instances":
                return call

            def hooked(**kwargs):
                # 백업 캡처의 읽기 — 첫 단계 전이다. 디스패치 세션의 트랜잭션은 열린 채다
                with Session(pg_engine) as admin:
                    admin.execute(text("SET lock_timeout = '5s'"))
                    assert tenants_repo.suspend_tenant(admin, tenant_id, reason="해지", now=_utcnow())
                    admin.commit()
                return call(**kwargs)

            return hooked

    monkeypatch.setattr(
        bk, "aws_client", lambda service, region=None, **_: SuspendDuringCapture(fake_factory(service, region)))

    report = cycle(db)

    assert (report.started, report.closed, report.errored) == (1, 1, 0)
    assert operations(aws) == ["describe_instances"]  # 변경 호출(stop_instances)은 없다
    assert exec_repo.list_steps(db, execution_id) == []
    assert statuses(db, incident_id, execution_id) == (IncidentStatus.FAILED, ExecutionStatus.FAILED)
    assert exec_repo.get_execution(db, execution_id).error_summary.startswith(SUSPENDED_PREFIX)
    assert tenants_repo.get_tenant_status(db, tenant_id) is TenantStatus.SUSPENDED


def test_reservation_on_an_active_or_unassigned_tenant_starts_as_before(
    db, make_incident, make_candidate, make_execution, make_tenant, aws
):
    for tenant_id in (make_tenant(), None):
        aws.calls.clear()
        _, execution_id = _reserved(db, make_incident, make_candidate, make_execution, tenant_id)
        report = cycle(db)
        assert (report.started, report.closed) == (1, 0)
        assert "stop_instances" in operations(aws)
        assert exec_repo.list_steps(db, execution_id) != []
        # 다음 반복이 이 건을 다시 집지 않게 닫는다
        workflows.close_execution(db, execution_id, next_status=ExecutionStatus.SUCCESS)


# ----- ④ 정지와 시작의 경합 ---------------------------------------------------------

@pytest.mark.parametrize("lock", [True, False], ids=["first-step", "pre-start"])
@pytest.mark.parametrize("trigger", [
    TriggerSource.USER_APPROVAL, TriggerSource.PRE_MITIGATION_0_5S, TriggerSource.TIMEOUT_ISOLATION_1M,
])
def test_start_gate_refuses_every_new_execution_but_not_auto_rollback(db, make_tenant, trigger, lock):
    tenant_id = make_tenant(TenantStatus.SUSPENDED)
    with pytest.raises(workflows.TenantSuspendedBeforeStart) as raised:
        workflows.refuse_start_if_tenant_suspended(
            db, tenant_id=tenant_id, trigger_source=trigger, lock=lock)
    assert raised.value.tenant_id == tenant_id
    assert str(raised.value).startswith(SUSPENDED_PREFIX + ": ")
    workflows.refuse_start_if_tenant_suspended(
        db, tenant_id=tenant_id, trigger_source=TriggerSource.AUTO_ON_FAILURE, lock=lock)
    workflows.refuse_start_if_tenant_suspended(db, tenant_id=None, trigger_source=trigger, lock=lock)


def _backend_pid(session: Session) -> int:
    return session.scalar(text("SELECT pg_backend_pid()"))


def _lock_wait_of(engine, pid: int) -> tuple | None:
    """그 백엔드가 지금 잠금을 기다리는가 — pg_stat_activity 의 wait_event_type 'Lock' 이 근거다.
    시간 제한은 관측을 기다리는 상한일 뿐 순서의 증거가 아니다."""
    deadline = time.monotonic() + 10
    with engine.connect() as probe:
        while time.monotonic() < deadline:
            row = probe.execute(
                text("SELECT wait_event_type, wait_event FROM pg_stat_activity WHERE pid = :pid"),
                {"pid": pid},
            ).one_or_none()
            if row is not None and row[0] == "Lock":
                return tuple(row)
            time.sleep(0.02)
    return None


def _suspend_in_thread(admin: Session, tenant_id: str) -> tuple[threading.Thread, dict]:
    done: dict = {}
    thread = threading.Thread(target=lambda: done.setdefault("ok", tenants_repo.suspend_tenant(
        admin, tenant_id, reason="해지", now=NOW)))
    thread.start()
    return thread, done


def test_suspension_waits_for_the_first_step_transaction(pg_engine):
    """첫 단계 관문(lock=True)이 FOR SHARE 로 읽은 뒤 commit 전에는 정지 UPDATE 가 행 잠금을 기다린다 —
    pg_stat_activity 로 그 대기를 관측한다. commit 뒤 정지가 들어가고, 그다음 시작은 막힌다."""
    with Session(pg_engine) as setup:
        tenant_id = _tenant(setup, account="999988887777").tenant_id
        setup.commit()
    try:
        starting, admin = Session(pg_engine), Session(pg_engine)
        try:
            admin_pid = _backend_pid(admin)
            workflows.refuse_start_if_tenant_suspended(
                starting, tenant_id=tenant_id, trigger_source=TriggerSource.USER_APPROVAL, lock=True)
            suspend, done = _suspend_in_thread(admin, tenant_id)
            waiting = _lock_wait_of(pg_engine, admin_pid)
            assert waiting is not None and done == {}, "정지가 첫 단계 트랜잭션의 FOR SHARE 를 기다려야 한다"
            starting.commit()  # = 첫 단계 commit
            suspend.join(timeout=10)
            assert not suspend.is_alive() and done == {"ok": True}
            admin.commit()
            # 정지가 commit 된 뒤의 다음 시작은 막힌다
            with pytest.raises(workflows.TenantSuspendedBeforeStart):
                workflows.refuse_start_if_tenant_suspended(
                    starting, tenant_id=tenant_id, trigger_source=TriggerSource.USER_APPROVAL, lock=True)
            starting.rollback()
        finally:
            starting.close()
            admin.close()
    finally:
        with Session(pg_engine) as cleanup:
            cleanup.delete(cleanup.get(models.Tenant, tenant_id))
            cleanup.commit()


def test_first_step_recorder_holds_the_tenant_row_until_the_step_commits(
    committed_tenant, db, pg_engine, make_incident, make_candidate, make_execution, monkeypatch
):
    """첫 단계 저장 전 정지 UPDATE 는 _step_recorder 가 획득한 테넌트 행 잠금을 기다린다."""
    _, execution_id = _reserved(
        db, make_incident, make_candidate, make_execution, committed_tenant
    )
    observed = []
    real_add_step = exec_repo.add_step

    def add_step_while_suspending(session, step, *, execution_id):
        with Session(pg_engine) as admin:
            admin.execute(text("SET lock_timeout = '1s'"))
            with pytest.raises(OperationalError) as raised:
                tenants_repo.suspend_tenant(
                    admin, committed_tenant, reason="해지", now=_utcnow()
                )
            assert raised.value.orig.sqlstate == "55P03"
            observed.append("lock-wait")
            admin.rollback()
        return real_add_step(session, step, execution_id=execution_id)

    monkeypatch.setattr(exec_repo, "add_step", add_step_while_suspending)
    record = workflows._step_recorder(db, execution_id)
    record(ExecutionStepResult(
        sequence=1, affected_arn=INSTANCE_ARN, step_type=ex.STEP_STOP_INSTANCE,
        aws_operation="ec2.stop_instances", status=ExecutionStepStatus.IN_PROGRESS,
        occurred_at=_utcnow(),
    ))
    assert observed == ["lock-wait"]
    assert len(exec_repo.list_steps(db, execution_id)) == 1


def test_pre_start_read_never_makes_a_suspension_wait(pg_engine):
    """runner 진입 전 사전 판정(lock=False)은 잠그지 않는다 — 그 트랜잭션이 열린 채 AWS 를 부르는 동안에도
    정지는 기다리지 않고 commit 되고, 첫 단계 관문이 그 정지를 본다."""
    with Session(pg_engine) as setup:
        tenant_id = _tenant(setup, account="999988887776").tenant_id
        setup.commit()
    try:
        starting, admin = Session(pg_engine), Session(pg_engine)
        try:
            admin.execute(text("SET lock_timeout = '5s'"))  # 기다리면 실패한다 — 증거가 아니라 상한이다
            workflows.refuse_start_if_tenant_suspended(
                starting, tenant_id=tenant_id, trigger_source=TriggerSource.USER_APPROVAL, lock=False)
            assert starting.in_transaction()  # 사전 판정의 트랜잭션은 열린 채다(= 백업 캡처 중)
            suspend, done = _suspend_in_thread(admin, tenant_id)
            suspend.join(timeout=10)
            assert not suspend.is_alive() and done == {"ok": True}
            admin.commit()
            with pytest.raises(workflows.TenantSuspendedBeforeStart):
                workflows.refuse_start_if_tenant_suspended(
                    starting, tenant_id=tenant_id, trigger_source=TriggerSource.USER_APPROVAL, lock=True)
            starting.rollback()
        finally:
            starting.close()
            admin.close()
    finally:
        with Session(pg_engine) as cleanup:
            cleanup.delete(cleanup.get(models.Tenant, tenant_id))
            cleanup.commit()


# ----- ⑤ 단계를 남긴 실행과 자동 원복 -----------------------------------------------

def test_execution_with_a_step_is_judged_and_auto_rolled_back_after_suspension(
    db, rolled_back_origin, aws
):
    """단계가 남은 실행은 정지 뒤에도 판정 → 자동 원복 접수 → 원복 실행까지 그대로 간다."""
    tenant_id = _tenant(db, account=INSTANCE_ARN.split(":")[4]).tenant_id
    incident_id, origin_id = rolled_back_origin(origin_status=ExecutionStatus.IN_PROGRESS)
    _attach_all(db, incident_id, tenant_id)
    exec_repo.add_step(db, ExecutionStepResult(
        sequence=1, affected_arn=INSTANCE_ARN, step_type=ex.STEP_STOP_INSTANCE,
        aws_operation="ec2.stop_instances", status=ExecutionStepStatus.IN_PROGRESS, occurred_at=_utcnow(),
    ), execution_id=origin_id)
    db.commit()
    _suspend(db, tenant_id)

    judged = cycle(db)      # 단계가 남았다 — 재실행이 아니라 판정 → ROLLBACK_INITIATED
    initiated = cycle(db)   # 자동 원복 자식 접수(정지 뒤에 생긴다)
    reverted = cycle(db)    # 자식 실행 — AUTO_ON_FAILURE 는 관문을 지나지 않는다

    assert (judged.judged, judged.rollback_initiated, judged.closed) == (1, 1, 0)
    assert initiated.rollback_started == 1
    assert reverted.started == 1 and reverted.closed == 1
    child = only_child(db, origin_id)
    assert (child.trigger_source, child.tenant_id, child.status) == (
        TriggerSource.AUTO_ON_FAILURE, tenant_id, ExecutionStatus.SUCCESS)
    assert statuses(db, incident_id, origin_id) == (IncidentStatus.AWAITING_CLOSURE, ExecutionStatus.ROLLED_BACK)
    modify = [kw for name, kw in aws.calls if name == "modify_instance_attribute" and not kw.get("DryRun")]
    assert modify == [{"InstanceId": INSTANCE, "InstanceType": {"Value": BACKUP_TYPE}}]


def test_auto_rollback_child_created_after_suspension_still_runs(db, rolled_back_origin, aws):
    tenant_id = _tenant(db, account=INSTANCE_ARN.split(":")[4]).tenant_id
    incident_id, origin_id = rolled_back_origin()
    _attach_all(db, incident_id, tenant_id)
    _suspend(db, tenant_id)

    assert cycle(db).rollback_started == 1
    report = cycle(db)

    assert (report.started, report.closed) == (1, 1)
    assert only_child(db, origin_id).status is ExecutionStatus.SUCCESS
    assert statuses(db, incident_id, origin_id) == (IncidentStatus.AWAITING_CLOSURE, ExecutionStatus.ROLLED_BACK)


def test_operator_recovery_reserved_before_suspension_closes_failed_and_origin_rollback_failed(
    db, rolled_back_origin, aws, audit_actor
):
    """정지 직전 관제자 복구 예약은 자식 FAILED · 원본 ROLLBACK_FAILED 로 끝나며 AWS 변경은 없다.

    ROLLBACK_FAILED 는 복구를 다시 열지 않으므로 재개 뒤의 원복은 사람 몫이다.
    """
    tenant_id = _tenant(db, account=INSTANCE_ARN.split(":")[4]).tenant_id
    incident_id, origin_id = rolled_back_origin()
    _attach_all(db, incident_id, tenant_id)
    child_id = reserve_manual_revert(db, incident_id, audit_actor)
    _suspend(db, tenant_id)

    cycle(db)

    child = exec_repo.get_execution(db, child_id)
    assert child.status is ExecutionStatus.FAILED
    assert child.error_summary.startswith(SUSPENDED_PREFIX + ": ")
    assert exec_repo.list_steps(db, child_id) == []
    assert statuses(db, incident_id, origin_id)[1] is ExecutionStatus.ROLLBACK_FAILED
    assert [
        name for name, kwargs in aws.calls
        if name == "modify_instance_attribute" and not kwargs.get("DryRun")
    ] == []
    assert ExecutionStatus.ROLLBACK_FAILED not in EXECUTION_RECOVERABLE_STATUSES


# ----- ⑥ 다시 켜기와 무관 · 기존 동작 보존 ---------------------------------------------

def test_toggling_auto_action_does_not_lift_the_suspension_for_the_watch_list(
    db, make_incident, make_candidate
):
    tenant = _tenant(db, enabled=False)
    seeded = _seeded(db, make_incident, make_candidate, "medium", tenant)
    _suspend(db, tenant.tenant_id)
    _toggle(db, tenant, True, now=_utcnow() - timedelta(minutes=10))
    db.commit()
    assert seeded.incident_id not in incidents_repo.list_expired_agent_waits(db, now=_utcnow())
    assert _attempt(db, seeded, TIMEOUT).skipped_reason is SKIP.TENANT_SUSPENDED
