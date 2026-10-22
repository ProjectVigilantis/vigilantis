# ==============================================================================
# [파일 설명]  담당: 김승철 (API 계약 · BE)
# 고객별 자동 조치 끄기 (Post-MVP T-6 · #514) — 실제 PostgreSQL.
#
# 지키는 것.
#   ① 꺼진 테넌트에는 서버가 사람 없이 시작하는 발동(High 선차단 · Medium 1분 자동 격리)이 접수되지 않는다
#      — 사전 판정은 AWS(가드레일 ④)를 부르기 전에, 접수 관문은 잠근 뒤 다시 본다.
#   ② 꺼진 동안 지난 Medium 기한은 다시 켜도 발동하지 않고, 켠 뒤 지나는 기한은 발동한다.
#   ③ 1분 감시 목록이 꺼진 테넌트의 건을 고르지 않는다 — 주기마다 잠그고 건너뛰지 않게.
#   ④ 끄기와 접수가 겹치면 끄기가 접수 commit 을 기다린다(FOR SHARE).
#   ⑤ 사람 승인 실행은 그대로다 · 테넌트 없음(NULL)은 지금 동작 그대로다.
# ==============================================================================

from __future__ import annotations

import threading
import uuid
from datetime import UTC, datetime, timedelta

import pytest
import workflows
from db import models
from db.repositories import incidents as incidents_repo
from db.repositories import tenants as tenants_repo
from schemas.api.actions import ExecuteActionRequest, ExecutionStatus
from schemas.api.tenants import TenantUpdateRequest, TenantVerificationResult
from schemas.candidates import CandidateStatus
from schemas.runbooks import RunbookId, TriggerSource
from security import soar
from sqlalchemy.orm import Session
from tests.test_auto_isolation_workflow import (
    PRE,
    SKIP,
    TIMEOUT,
    _attempt,
    _auto_evaluations,
    _candidate_statuses,
    _executions,
    _seed,
)

ACCOUNT = "123456789012"  # tests.test_auto_isolation_workflow 의 대상 ARN 계정
NOW = datetime(2026, 10, 22, 6, 0, tzinfo=UTC)


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _tenant(db, *, enabled: bool = True, account: str = ACCOUNT) -> models.Tenant:
    tenant = tenants_repo.create_tenant(
        db, name="학원", aws_account_id=account, regions=["ap-northeast-2"], now=NOW
    )
    assert tenants_repo.record_verification(
        db, tenant.tenant_id, result=TenantVerificationResult.OK, reason_code=None, now=NOW)
    if not enabled:
        _toggle(db, tenant, False)
    return tenant


def _toggle(db, tenant: models.Tenant, enabled: bool, *, now: datetime | None = None) -> None:
    assert tenants_repo.update_tenant(
        db, tenant.tenant_id, TenantUpdateRequest(auto_action_enabled=enabled), now=now or _utcnow())


def _attach(db, seeded, tenant: models.Tenant) -> None:
    db.get(models.Incident, seeded.incident_id).tenant_id = tenant.tenant_id
    db.flush()


def _seeded(db, make_incident, make_candidate, kind: str, tenant: models.Tenant | None, **kw):
    seeded = _seed(db, make_incident, make_candidate, kind, **kw)
    if tenant is not None:
        _attach(db, seeded, tenant)
    db.commit()
    return seeded


# ----- 관문 함수 ----------------------------------------------------------------

def _row(enabled: bool, resumed_at: datetime | None) -> models.Tenant:
    return models.Tenant(auto_action_enabled=enabled, auto_action_resumed_at=resumed_at)


@pytest.mark.parametrize("tenant, trigger, deadline, blocked", [
    pytest.param(None, TIMEOUT, NOW, False, id="no-tenant-is-mvp-record"),
    pytest.param(_row(True, None), TIMEOUT, NOW, False, id="enabled-never-turned-off"),
    pytest.param(_row(False, None), PRE, None, True, id="disabled-high"),
    pytest.param(_row(False, None), TIMEOUT, NOW, True, id="disabled-medium"),
    pytest.param(_row(True, NOW), TIMEOUT, NOW, True, id="deadline-passed-at-resume"),
    pytest.param(_row(True, NOW), TIMEOUT, NOW - timedelta(hours=1), True, id="deadline-passed-while-off"),
    pytest.param(_row(True, NOW), TIMEOUT, NOW + timedelta(seconds=1), False, id="deadline-after-resume"),
    pytest.param(_row(True, NOW), PRE, None, False, id="high-has-no-deadline"),
])
def test_gate(tenant, trigger, deadline, blocked):
    reason = workflows.automatic_trigger_block(
        tenant, trigger_source=trigger, response_deadline_at=deadline)
    assert reason is (SKIP.AUTO_ACTION_DISABLED if blocked else None)


# ----- ① 접수 관문 ---------------------------------------------------------------

@pytest.mark.parametrize("kind, trigger", [("high", PRE), ("medium", TIMEOUT)])
def test_disabled_tenant_never_reserves_an_automatic_isolation(
    db, make_incident, make_candidate, kind, trigger
):
    seeded = _seeded(db, make_incident, make_candidate, kind, _tenant(db, enabled=False))
    before = _candidate_statuses(db, seeded.incident_id)

    for rejected in (False, True):  # 거절 판정을 넘겨도 기록하지 않는다
        result = _attempt(db, seeded, trigger, rejected=rejected)
        assert (result.skipped_reason, result.execution_id) == (SKIP.AUTO_ACTION_DISABLED, None)

    assert _executions(db, seeded.incident_id) == []
    assert _auto_evaluations(db, seeded.incident_id) == []
    # 제안은 그대로 남는다 — 사람이 승인할 수 있다
    assert _candidate_statuses(db, seeded.incident_id) == before


def test_human_approval_still_executes_on_a_disabled_tenant(db, make_incident, make_candidate):
    seeded = _seeded(db, make_incident, make_candidate, "medium", _tenant(db, enabled=False))
    approved = workflows.reserve_execution(db, ExecuteActionRequest(
        incident_id=seeded.incident_id, runbook_id=RunbookId.RUNBOOK_NACL_ADD_DENY,
        idempotency_key="t6-" + uuid.uuid4().hex,
    ))
    assert approved.created
    [execution] = _executions(db, seeded.incident_id)
    assert (execution.trigger_source, execution.status) == (
        TriggerSource.USER_APPROVAL, ExecutionStatus.IN_PROGRESS)


@pytest.mark.parametrize("assigned", [True, False], ids=["enabled-tenant", "no-tenant"])
def test_enabled_and_unassigned_incidents_reserve_as_before(db, make_incident, make_candidate, assigned):
    tenant = _tenant(db) if assigned else None
    seeded = _seeded(db, make_incident, make_candidate, "medium", tenant)
    result = _attempt(db, seeded, TIMEOUT)
    assert result.created and result.execution_status is ExecutionStatus.IN_PROGRESS


# ----- ② 꺼진 동안 지난 기한 -------------------------------------------------------

def test_deadline_that_passed_while_disabled_does_not_fire_after_re_enabling(
    db, make_incident, make_candidate
):
    tenant = _tenant(db, enabled=False)
    seeded = _seeded(db, make_incident, make_candidate, "medium", tenant)  # 기한 이미 지남
    _toggle(db, tenant, True)  # 켠 시각 = 지금 > 기한
    db.commit()

    assert seeded.incident_id not in incidents_repo.list_expired_agent_waits(db, now=_utcnow())
    result = _attempt(db, seeded, TIMEOUT)
    assert result.skipped_reason is SKIP.AUTO_ACTION_DISABLED
    assert _executions(db, seeded.incident_id) == []


def test_deadline_after_re_enabling_fires(db, make_incident, make_candidate):
    tenant = _tenant(db, enabled=False)
    _toggle(db, tenant, True, now=_utcnow() - timedelta(minutes=10))  # 대기 시작보다 먼저 켰다
    seeded = _seeded(db, make_incident, make_candidate, "medium", tenant)

    assert seeded.incident_id in incidents_repo.list_expired_agent_waits(db, now=_utcnow())
    result = _attempt(db, seeded, TIMEOUT)
    assert result.created and result.execution_status is ExecutionStatus.IN_PROGRESS


def test_resumed_at_is_written_only_when_turning_back_on(db):
    tenant = _tenant(db)
    assert tenant.auto_action_resumed_at is None  # 끈 적 없음
    _toggle(db, tenant, True, now=NOW)  # 켜진 것을 다시 켬 — 기록하지 않는다
    assert tenant.auto_action_resumed_at is None
    _toggle(db, tenant, False, now=NOW + timedelta(minutes=1))
    assert tenant.auto_action_resumed_at is None
    _toggle(db, tenant, True, now=NOW + timedelta(minutes=2))
    assert tenant.auto_action_resumed_at == NOW + timedelta(minutes=2)
    _toggle(db, tenant, True, now=NOW + timedelta(minutes=3))  # 이미 켜짐 — 앞 기록을 덮지 않는다
    assert tenant.auto_action_resumed_at == NOW + timedelta(minutes=2)
    # 다른 칸만 바꾸는 수정은 건드리지 않는다
    assert tenants_repo.update_tenant(db, tenant.tenant_id, TenantUpdateRequest(name="x"), now=NOW)
    assert tenant.auto_action_resumed_at == NOW + timedelta(minutes=2)


# ----- ③ 감시 목록 --------------------------------------------------------------

def test_watch_list_skips_disabled_tenants_and_keeps_unassigned(db, make_incident, make_candidate):
    disabled = _seeded(db, make_incident, make_candidate, "medium", _tenant(db, enabled=False))
    unassigned = _seeded(db, make_incident, make_candidate, "medium", None,
                         subject="arn:aws:ec2:ap-northeast-2:123456789012:instance/i-0fedcba9876543210")
    due = incidents_repo.list_expired_agent_waits(db, now=_utcnow())
    assert disabled.incident_id not in due
    assert unassigned.incident_id in due


# ----- ① 사전 판정 — AWS 를 부르기 전 ------------------------------------------------

@pytest.mark.parametrize("enabled", [False, True])
def test_preparation_stops_before_any_aws_step_when_disabled(
    db, make_incident, make_candidate, monkeypatch, enabled
):
    seeded = _seeded(db, make_incident, make_candidate, "high", _tenant(db, enabled=enabled))
    reached: list[str] = []
    # 관문 바로 뒤가 대상 파싱이고, 그 뒤가 파라미터 조회 · 가드레일(④ AWS Dry-Run)이다
    real_parse = soar.executor.parse_arn
    monkeypatch.setattr(soar.executor, "parse_arn", lambda arn: reached.append(arn) or real_parse(arn))
    monkeypatch.setattr(soar.guardrails, "run_guardrail_validation",
                        lambda *a, **k: pytest.fail("가드레일이 불렸다"))
    monkeypatch.setattr(workflows, "resolve_isolation_parameters",
                        lambda db, arn: type("Unresolved", (), {
                            "resolved": False, "reason_code": type("R", (), {"value": "TEST"})()})())

    assert soar.prepare_isolation(db, seeded.incident_id, PRE) is None
    assert bool(reached) is enabled


# ----- ④ 끄기와 접수의 겹침 --------------------------------------------------------

def test_turning_off_waits_for_a_reservation_that_read_the_tenant(pg_engine):
    with Session(pg_engine) as setup:
        tenant_id = _tenant(setup, account="999988887777").tenant_id
        setup.commit()
    try:
        reserving, admin = Session(pg_engine), Session(pg_engine)
        try:
            # 접수가 테넌트를 FOR SHARE 로 읽은 채 commit 전이다 — 끄기는 그 commit 을 기다린다
            assert tenants_repo.read_tenant_for_share(reserving, tenant_id).auto_action_enabled
            done: dict = {}
            off = threading.Thread(target=lambda: done.setdefault("ok", tenants_repo.update_tenant(
                admin, tenant_id, TenantUpdateRequest(auto_action_enabled=False), now=NOW)))
            off.start()
            off.join(timeout=0.5)
            assert off.is_alive(), "끄기가 접수의 FOR SHARE 를 기다려야 한다"
            reserving.commit()
            off.join(timeout=10)
            admin.commit()
            assert done == {"ok": True}
        finally:
            reserving.close()
            admin.close()
        with Session(pg_engine) as check:
            assert check.get(models.Tenant, tenant_id).auto_action_enabled is False
    finally:
        with Session(pg_engine) as cleanup:
            cleanup.delete(cleanup.get(models.Tenant, tenant_id))
            cleanup.commit()


def test_candidate_stays_executable_after_a_disabled_timeout(db, make_incident, make_candidate):
    seeded = _seeded(db, make_incident, make_candidate, "medium", _tenant(db, enabled=False))
    _attempt(db, seeded, TIMEOUT)
    assert _candidate_statuses(db, seeded.incident_id) == [CandidateStatus.EXECUTABLE]
