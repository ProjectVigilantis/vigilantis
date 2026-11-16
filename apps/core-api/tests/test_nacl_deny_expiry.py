"""차단 24시간 고정 만료 통합 테스트 — 실제 PostgreSQL 필요. (Issue #597 · T-11)

SUCCESS로 끝난 지 24시간이 지난 NACL 차단(NACL_ADD_DENY)을 dispatcher가 시스템 발동 해제
(NACL_RESTORE, trigger NACL_DENY_EXPIRED_24H)로 되돌리는가를 본다. 가짜 EC2는 해제 왕복 테스트와
같은 StatefulEc2다 — 규칙이 실제로 슬롯에서 사라지는지 본다. 24시간은 시계를 건드리지 않고
차단 행의 finished_at을 25시간 전으로 옮겨 만든다.
"""

import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

CORE_API = Path(__file__).resolve().parents[1]
REPO_ROOT = Path(__file__).resolve().parents[3]
for p in (str(CORE_API), str(REPO_ROOT / "packages"), str(Path(__file__).resolve().parent)):
    if p not in sys.path:
        sys.path.insert(0, p)

import dispatcher  # noqa: E402
import workflows  # noqa: E402
from db import models  # noqa: E402
from db.repositories import audit as audit_repo  # noqa: E402,F401
from db.repositories import executions as exec_repo  # noqa: E402
from db.repositories import incidents as incidents_repo  # noqa: E402
from schemas.api.actions import ExecuteActionRequest, ExecutionStatus  # noqa: E402
from schemas.api.incidents import IncidentCategory, IncidentStatus, ResolutionJudgement  # noqa: E402
from schemas.api.tenants import TenantStatus  # noqa: E402
from schemas.api.ws import WsEventType  # noqa: E402
from schemas.candidates import CandidateStatus  # noqa: E402
from schemas.runbooks import RunbookId, TriggerSource  # noqa: E402
from services.aws import executor as ex  # noqa: E402
from audit_cleanup import purge_audit_events  # noqa: E402
from sqlalchemy import delete, func, select, text, update  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from test_nacl_release_offer import (  # noqa: E402,F401  (fixtures + helpers)
    ACL_ARN,
    ADD_DENY,
    BLOCK_PARAMS,
    CIDR,
    RESTORE,
    aws,
    block_of,
    incident_status,
    release_candidates,
    reserved_block,
    seed_nacl_asset,
)

EXPIRED = TriggerSource.NACL_DENY_EXPIRED_24H


def age_block(db, block_id, *, hours=25):
    block = exec_repo.get_execution(db, block_id)
    block.finished_at = datetime.now(timezone.utc) - timedelta(hours=hours)
    db.commit()


def restores(db, incident_id):
    return [r for r in exec_repo.list_by_incident(db, incident_id) if r.runbook_id is RESTORE]


def deletes(aws):
    return aws["calls"].count("delete_network_acl_entry")


def platform_event_count(db):
    """플랫폼 활동 로그(#589) 전체 행 수 — 건너뜀 · 중복 · 경합이 행을 늘리지 않는지 본다."""
    db.expire_all()
    return db.scalar(select(func.count()).select_from(models.PlatformEvent))


def platform_facts(db, *target_ids):
    db.expire_all()
    return sorted(
        (row.action, row.outcome.value, row.actor_kind.value, row.target_id)
        for row in db.scalars(
            select(models.PlatformEvent).where(models.PlatformEvent.target_id.in_(target_ids))
        )
    )


@pytest.fixture()
def blocked(db, reserved_block, aws):
    """차단이 실제 dispatcher 주기로 SUCCESS까지 닫힌 상태(해제 제안도 그 주기에 선다) → (incident_id, block_id)."""

    def _make(*, hours=25):
        incident_id, block_id = reserved_block()
        dispatcher.dispatch_pending(db)
        assert exec_repo.get_execution(db, block_id).status is ExecutionStatus.SUCCESS
        assert len(aws["entries"]) == 1
        age_block(db, block_id, hours=hours)
        return incident_id, block_id

    return _make


@pytest.fixture()
def bare_block(db, make_incident, make_execution, aws):
    """후보 없이 실행된 SUCCESS 차단(자동 차단) + 결속된 백업 + AWS의 규칙 → (incident, block)."""

    def _make(*, status=IncidentStatus.ACTION_IN_PROGRESS, hours=25, rule_in_aws=True):
        incident = make_incident(
            db, category=IncidentCategory.SECOPS, subject_arn=ACL_ARN, status=status
        )
        block = make_execution(
            db,
            incident,
            runbook_id=ADD_DENY,
            target_arn=ACL_ARN,
            status=ExecutionStatus.SUCCESS,
            trigger_source=TriggerSource.USER_APPROVAL,
        )
        record = exec_repo.create_backup_record(
            db,
            execution_id=block.execution_id,
            target_arn=ACL_ARN,
            backup_type=ex.BACKUP_NACL_RULE_INDEX,
            payload={
                "rule_number": 100,
                "egress": False,
                "cidr_block": CIDR,
                "protocol": "6",
                "rule_action": "deny",
            },
        )
        assert exec_repo.bind_backup_record(db, block.execution_id, record.backup_record_id)
        if rule_in_aws:
            aws["entries"].append(
                {"RuleNumber": 100, "Egress": False, "CidrBlock": CIDR, "Protocol": "6", "RuleAction": "deny"}
            )
        db.commit()
        age_block(db, block.execution_id, hours=hours)
        return incident, block

    return _make


# ------------------------------------------------------------------ 1. 만료 → 해제 SUCCESS


def test_an_expired_block_is_released_in_one_cycle(db, blocked, aws):
    incident_id, block_id = blocked()
    offers_before = len(release_candidates(db, incident_id))
    events = []

    report = dispatcher.dispatch_pending(db, events.append)

    assert report.expiry_reserved == 1 and report.expiry_skipped == 0
    [restore] = restores(db, incident_id)
    assert restore.trigger_source is EXPIRED
    assert restore.status is ExecutionStatus.SUCCESS
    assert restore.candidate_id is None and restore.parent_execution_id is None
    assert restore.deduplication_key == workflows.nacl_deny_expiry_deduplication_key(block_id)
    assert restore.backup_record_id == exec_repo.get_execution(db, block_id).backup_record_id
    assert restore.validated_command["evidence_ids"] == [block_id]
    assert aws["entries"] == []  # 차단 규칙이 사라졌다
    assert deletes(aws) == 1
    # 만료는 해제 후보를 새로 만들지 않는다
    assert len(release_candidates(db, incident_id)) == offers_before
    assert all(c.status is not CandidateStatus.CLAIMED for c in release_candidates(db, incident_id))
    # 시스템 발동 감사 기록
    [audit] = db.execute(
        select(models.AuditEvent).where(models.AuditEvent.execution_id == restore.execution_id)
    ).scalars()
    assert audit.trigger_source is EXPIRED
    assert audit.actor_kind == audit_repo.SYSTEM.kind
    # 시스템이 닫지 않는다 — 접수가 차단 때 선 [해제] 제안을 무효화하므로 close_execution이
    # 인시던트를 종료 대기로 내린다
    assert incident_status(db, incident_id) is IncidentStatus.AWAITING_CLOSURE
    [offer] = release_candidates(db, incident_id)
    assert offer.status is CandidateStatus.INVALIDATED
    types = [e.event_type for e in events]
    assert WsEventType.EXECUTION_UPDATED in types and WsEventType.INCIDENT_UPDATED in types


def test_the_expired_block_is_not_picked_up_twice(db, blocked, aws):
    incident_id, block_id = blocked()
    dispatcher.dispatch_pending(db)
    events = platform_event_count(db)

    again = dispatcher.dispatch_pending(db)
    # 스캔이 다시 집지 않아도 직접 부르면 중복 방지 키 조회가 막는다
    direct = workflows.reserve_nacl_deny_expiry(db, block_id)

    assert (again.expiry_reserved, again.expiry_skipped) == (0, 0)
    assert direct.execution_id is None and "이미 접수" in direct.skipped_reason
    assert len(restores(db, incident_id)) == 1
    assert deletes(aws) == 1
    assert platform_event_count(db) == events


def test_reserving_invalidates_only_the_restore_candidates(db, blocked, make_candidate, aws):
    incident_id, block_id = blocked()
    [offer] = release_candidates(db, incident_id)
    assert offer.status is CandidateStatus.EXECUTABLE
    other = make_candidate(db, incident_id, runbook_id=RunbookId.RUNBOOK_SG_DELETE_ISOLATED)
    db.commit()
    assert other.status is CandidateStatus.EXECUTABLE

    reservation = workflows.reserve_nacl_deny_expiry(db, block_id)

    assert reservation.execution_id is not None
    db.refresh(offer)
    db.refresh(other)
    assert offer.status is CandidateStatus.INVALIDATED
    assert other.status is CandidateStatus.EXECUTABLE


def test_a_restored_expired_block_gets_no_release_offer_afterwards(
    db, bare_block, aws
):
    """종료된 인시던트의 차단은 제안 없이 만료된다 — 해제된 뒤 다음 주기가 제안을 새로 세우지 않는다."""
    incident, block = bare_block(status=IncidentStatus.RESOLVED)
    first = dispatcher.dispatch_pending(db)
    assert first.expiry_reserved == 1
    [restore] = restores(db, incident.incident_id)
    assert restore.status is ExecutionStatus.SUCCESS and aws["entries"] == []
    describes = aws["calls"].count("describe_network_acls")

    second = dispatcher.dispatch_pending(db)

    assert (second.release_offered, second.release_rejected) == (0, 0)
    assert release_candidates(db, incident.incident_id) == []
    assert aws["calls"].count("describe_network_acls") == describes


# --------------------------------------------------------------------- 2. 24시간 전에는 가만히


def test_a_block_younger_than_24_hours_is_left_alone(db, blocked, aws):
    incident_id, block_id = blocked(hours=23)
    calls = list(aws["calls"])
    events = platform_event_count(db)

    report = dispatcher.dispatch_pending(db)

    assert (report.expiry_reserved, report.expiry_skipped) == (0, 0)
    assert restores(db, incident_id) == []
    assert deletes(aws) == 0 and aws["calls"] == calls
    assert len(aws["entries"]) == 1
    assert platform_event_count(db) == events


def test_reserving_a_young_block_directly_reserves_nothing(db, blocked, aws):
    incident_id, block_id = blocked(hours=23)
    events = platform_event_count(db)

    reservation = workflows.reserve_nacl_deny_expiry(db, block_id)

    assert reservation.execution_id is None and reservation.skipped_reason
    assert restores(db, incident_id) == []
    assert platform_event_count(db) == events


# ------------------------------------------------------- 3. 종료(RESOLVED)된 인시던트의 차단도


def test_a_resolved_incident_goes_through_in_progress_and_the_block_still_expires(
    db, blocked, aws, audit_actor
):
    incident_id, block_id = blocked()
    assert workflows.resolve_incident(
        db, incident_id, ResolutionJudgement.JUSTIFIED, audit_actor=audit_actor
    )
    incident = incidents_repo.get_incident(db, incident_id)
    assert incident.status is IncidentStatus.RESOLVED and incident.resolution is not None

    reservation = workflows.reserve_nacl_deny_expiry(db, block_id)

    assert reservation.execution_id is not None
    db.refresh(incident)
    assert incident.status is IncidentStatus.ACTION_IN_PROGRESS
    assert incident.resolution is None and incident.resolved_at is None
    # 예약만 했다 — 규칙은 아직 서 있고, 이어지는 주기가 비종료 실행을 마저 돌린다
    assert len(aws["entries"]) == 1
    dispatcher.dispatch_pending(db)
    assert aws["entries"] == []
    assert exec_repo.get_execution(db, reservation.execution_id).status is ExecutionStatus.SUCCESS


def test_a_resolved_incident_expires_in_a_single_cycle(db, blocked, aws, audit_actor):
    incident_id, _ = blocked()
    assert workflows.resolve_incident(
        db, incident_id, ResolutionJudgement.JUSTIFIED, audit_actor=audit_actor
    )

    report = dispatcher.dispatch_pending(db)

    assert report.expiry_reserved == 1
    [restore] = restores(db, incident_id)
    assert restore.status is ExecutionStatus.SUCCESS
    assert aws["entries"] == []
    assert incident_status(db, incident_id) is IncidentStatus.AWAITING_CLOSURE


# ------------------------------------------------------- 4. 사람이 이미 해제했거나 시도한 차단


def test_a_block_released_by_an_operator_is_not_picked_up(db, blocked, aws, audit_actor):
    incident_id, block_id = blocked(hours=1)
    reservation = workflows.reserve_execution(
        db,
        ExecuteActionRequest(
            incident_id=incident_id, runbook_id=RESTORE, idempotency_key="00000000-0000-4000-8000-000000000597"
        ),
        audit_actor=audit_actor,
    )
    dispatcher.dispatch_pending(db)
    release = exec_repo.get_execution(db, reservation.response.execution_id)
    assert release.status is ExecutionStatus.SUCCESS and aws["entries"] == []
    age_block(db, block_id)
    events = platform_event_count(db)

    report = dispatcher.dispatch_pending(db)

    assert (report.expiry_reserved, report.expiry_skipped) == (0, 0)
    assert [r.execution_id for r in restores(db, incident_id)] == [release.execution_id]
    assert deletes(aws) == 1
    assert platform_event_count(db) == events
    assert block_id not in exec_repo.list_nacl_deny_expired(
        db, finished_before=datetime.now(timezone.utc)
    )


@pytest.mark.parametrize("bound", [True, False], ids=["bound-to-record", "unbound-same-incident-target"])
def test_a_failed_operator_release_attempt_is_not_picked_up(
    db, blocked, make_execution, aws, bound
):
    incident_id, block_id = blocked()
    block = exec_repo.get_execution(db, block_id)
    attempt = make_execution(
        db,
        incident_id,
        runbook_id=RESTORE,
        target_arn=ACL_ARN,
        status=ExecutionStatus.FAILED,
    )
    attempt.backup_record_id = block.backup_record_id if bound else None
    db.commit()
    assert block_id not in exec_repo.list_nacl_deny_expired(
        db, finished_before=datetime.now(timezone.utc)
    )
    events = platform_event_count(db)

    report = dispatcher.dispatch_pending(db)

    assert (report.expiry_reserved, report.expiry_skipped) == (0, 0)
    assert [r.execution_id for r in restores(db, incident_id)] == [attempt.execution_id]
    assert len(aws["entries"]) == 1 and deletes(aws) == 0
    assert platform_event_count(db) == events


# ------------------------------------------------------- 5. 규칙을 손으로 먼저 지웠다


def test_a_rule_deleted_by_hand_is_recorded_once_and_never_retried(db, blocked, aws):
    incident_id, _ = blocked()
    aws["entries"].clear()

    first = dispatcher.dispatch_pending(db)

    assert first.expiry_reserved == 1
    [restore] = restores(db, incident_id)
    assert restore.trigger_source is EXPIRED
    # 되돌릴 규칙이 이미 없으면 변경 호출 없이 닫는다(execute_nacl_restore ①)
    assert restore.status is ExecutionStatus.SUCCESS
    assert deletes(aws) == 0
    calls = list(aws["calls"])
    events = platform_event_count(db)

    second = dispatcher.dispatch_pending(db)

    assert (second.expiry_reserved, second.expiry_skipped) == (0, 0)
    assert [r.execution_id for r in restores(db, incident_id)] == [restore.execution_id]
    assert deletes(aws) == 0 and aws["calls"] == calls
    assert platform_event_count(db) == events


# ------------------------------------------------------- 6. 후보 없는 차단(자동 차단)


def test_a_block_without_a_candidate_expires_too(db, bare_block, aws):
    incident, block = bare_block()
    assert block.candidate_id is None

    report = dispatcher.dispatch_pending(db)

    assert report.expiry_reserved == 1
    [restore] = restores(db, incident.incident_id)
    assert restore.trigger_source is EXPIRED
    assert restore.status is ExecutionStatus.SUCCESS
    assert restore.candidate_id is None
    assert restore.validated_command["parameters"]["evidence_id"] == block.execution_id
    assert aws["entries"] == []


# ------------------------------------------------------- 7. 정지 테넌트


def test_a_suspended_tenant_block_is_skipped(db, bare_block, make_tenant, aws):
    incident, block = bare_block()
    tenant_id = make_tenant(TenantStatus.SUSPENDED)
    incident.tenant_id = tenant_id
    block.tenant_id = tenant_id
    db.commit()
    events = platform_event_count(db)

    report = dispatcher.dispatch_pending(db)

    assert (report.expiry_reserved, report.expiry_skipped) == (0, 1)
    assert restores(db, incident.incident_id) == []
    assert deletes(aws) == 0 and len(aws["entries"]) == 1
    # 다음 주기에 다시 본다 — 영구히 빠지지 않는다
    assert dispatcher.dispatch_pending(db).expiry_skipped == 1
    # 주기마다 다시 보는 판정이라 활동 로그를 남기지 않는다(#589)
    assert platform_event_count(db) == events


# ------------------------------------------------------- 7b. 플랫폼 활동 로그 (#589)


def test_expiry_records_a_system_trigger_and_a_completion_not_a_rollback(db, bare_block, aws):
    incident, block = bare_block()

    dispatcher.dispatch_pending(db)
    dispatcher.dispatch_pending(db)  # 다시 집지 않는다

    [restore] = restores(db, incident.incident_id)
    assert restore.status is ExecutionStatus.SUCCESS
    # 원본을 가리키지 않는 해제다 — 원복(ACTION_ROLLBACK)이 아니라 완료다
    assert platform_facts(db, restore.execution_id, block.execution_id, incident.incident_id) == [
        ("ACTION_AUTO_TRIGGER", "SUCCEEDED", "SYSTEM", restore.execution_id),
        ("ACTION_COMPLETE", "SUCCEEDED", "SYSTEM", restore.execution_id),
    ]


def test_a_stale_dedup_lookup_hits_the_real_constraint_and_keeps_the_first_reservation(
    db, bare_block, aws, monkeypatch
):
    """중복 방지 키 조회가 앞선 접수를 보지 못한 경합 창을 재현한다(첫 조회만 비운다) — INSERT 는 실제 유니크
    제약에 막히고(IntegrityError), 재조회로 물러난다. 처음 접수된 해제와 그 활동 로그만 남고 진 쪽 행은 없다.

    경계: 실제 동시 접수는 인시던트 잠금이 직렬화해 이 분기까지 오지 않는다(8b가 그 대기를 본다). 여기서
    보는 것은 그 잠금 뒤의 최후 관문 — 제약 위반 처리 분기 — 뿐이다.
    """
    incident, block = bare_block()
    assert dispatcher.dispatch_pending(db).expiry_reserved == 1  # 처음 접수 → 같은 주기에 SUCCESS
    [original] = restores(db, incident.incident_id)
    facts = platform_facts(db, original.execution_id, block.execution_id, incident.incident_id)
    assert facts == [
        ("ACTION_AUTO_TRIGGER", "SUCCEEDED", "SYSTEM", original.execution_id),
        ("ACTION_COMPLETE", "SUCCEEDED", "SYSTEM", original.execution_id),
    ]
    status_before = incident_status(db, incident.incident_id)
    real_lookup = exec_repo.get_by_deduplication_key
    calls = {"n": 0}

    def stale_first(session, key):
        calls["n"] += 1
        return None if calls["n"] == 1 else real_lookup(session, key)

    monkeypatch.setattr(workflows.executions_repo, "get_by_deduplication_key", stale_first)

    reservation = workflows.reserve_nacl_deny_expiry(db, block.execution_id)

    assert reservation.execution_id is None and "이미 접수" in reservation.skipped_reason
    assert calls["n"] == 2  # 최초 조회(비움) + 제약 위반 뒤 재조회 — 재조회는 IntegrityError 분기에만 있다
    assert [r.execution_id for r in restores(db, incident.incident_id)] == [original.execution_id]
    assert exec_repo.get_execution(db, original.execution_id).status is ExecutionStatus.SUCCESS
    assert incident_status(db, incident.incident_id) is status_before
    assert platform_facts(db, original.execution_id, block.execution_id, incident.incident_id) == facts


# ------------------------------------------------------- 8. 같은 대상이 바쁘다


def test_a_busy_target_defers_the_expiry(db, bare_block, make_incident, make_execution, aws):
    incident, block = bare_block()
    other = make_incident(db, category=IncidentCategory.SECOPS, subject_arn=ACL_ARN)
    make_execution(db, other, runbook_id=ADD_DENY, target_arn=ACL_ARN)  # IN_PROGRESS
    db.commit()
    events = platform_event_count(db)

    reservation = workflows.reserve_nacl_deny_expiry(db, block.execution_id)

    assert reservation.execution_id is None
    assert "진행 중" in reservation.skipped_reason
    assert restores(db, incident.incident_id) == []
    assert len(aws["entries"]) == 1 and deletes(aws) == 0
    assert platform_event_count(db) == events


# ------------------------------------------------------- 8b. 관제자 해제 접수와의 직렬화


def _waiting_on_lock(engine, pid):
    """그 백엔드가 잠금을 기다리는가. 시간 제한은 관측 상한일 뿐 순서의 증거가 아니다 — 전체
    스위트가 DB를 함께 쓰는 동안은 첫 질의까지가 느릴 수 있어 넉넉히 둔다."""
    deadline = time.monotonic() + 30
    with engine.connect() as probe:
        while time.monotonic() < deadline:
            row = probe.execute(
                text("SELECT wait_event_type FROM pg_stat_activity WHERE pid = :pid"),
                {"pid": pid},
            ).one_or_none()
            if row is not None and row[0] == "Lock":
                return True
            time.sleep(0.02)
    return False


def test_expiry_waits_for_an_operator_release_reservation_and_then_skips(
    pg_engine, make_incident, make_candidate, make_execution
):
    """관제자가 인시던트를 잠근 채 해제 실행을 넣는 동안 만료 접수는 기다리고, commit 뒤에는 그 해제를
    보고 물러난다. 인시던트 잠금이 없으면 둘 다 서로의 미커밋 행을 못 본 채 해제를 두 번 접수한다."""
    with Session(pg_engine) as setup:
        incident = make_incident(
            setup,
            category=IncidentCategory.SECOPS,
            subject_arn=ACL_ARN,
            status=IncidentStatus.AWAITING_APPROVAL,
        )
        incident_id = incident.incident_id
        block = make_execution(
            setup,
            incident,
            runbook_id=ADD_DENY,
            target_arn=ACL_ARN,
            status=ExecutionStatus.SUCCESS,
            trigger_source=TriggerSource.USER_APPROVAL,
        )
        block_id = block.execution_id
        record = exec_repo.create_backup_record(
            setup,
            execution_id=block_id,
            target_arn=ACL_ARN,
            backup_type=ex.BACKUP_NACL_RULE_INDEX,
            payload={
                "rule_number": 100,
                "egress": False,
                "cidr_block": CIDR,
                "protocol": "6",
                "rule_action": "deny",
            },
        )
        assert exec_repo.bind_backup_record(setup, block_id, record.backup_record_id)
        offer = make_candidate(
            setup, incident_id, runbook_id=RESTORE, target_arn=ACL_ARN,
            parameters={"rule_number": 100, "egress": False},
        )
        offer_id = offer.candidate_id
        block.finished_at = datetime.now(timezone.utc) - timedelta(hours=25)
        setup.commit()

    outcome = {}
    try:
        operator, expiry = Session(pg_engine), Session(pg_engine)
        try:
            incidents_repo.lock_incident(operator, incident_id)
            operator_execution = exec_repo.create_execution(
                operator,
                incident_id=incident_id,
                runbook_id=RESTORE,
                target_arn=ACL_ARN,
                trigger_source=TriggerSource.USER_APPROVAL,
                candidate_id=offer_id,
            )  # 미커밋
            operator_execution_id = operator_execution.execution_id
            # 트랜잭션을 연 채 pid를 읽는다 — 사이에 rollback이 들어가면 연결이 풀로 돌아가 다음
            # 문장이 다른 연결(다른 pid)로 나갈 수 있어 감시 대상이 어긋난다. SET LOCAL이라 이 트랜잭션
            # 안에서만 유효하고 풀의 다른 사용자에게 남지 않는다
            expiry.execute(text("SET LOCAL lock_timeout = '30s'"))
            expiry_pid = expiry.scalar(text("SELECT pg_backend_pid()"))

            def reserve():
                outcome["result"] = workflows.reserve_nacl_deny_expiry(expiry, block_id)

            thread = threading.Thread(target=reserve)
            thread.start()
            try:
                assert _waiting_on_lock(pg_engine, expiry_pid), "만료 접수가 잠금을 기다리지 않았다"
                operator.commit()  # 잠금이 풀리고 만료 접수가 이 행을 본다
            finally:
                # 단언이 실패해도 스레드를 끝낸 뒤 세션을 닫는다 — 살아 있는 스레드가 잠금을 쥔 채
                # 남으면 정리와 교착하고 뒤 테스트가 남은 행을 본다
                operator.rollback()
                thread.join(timeout=30)
            assert not thread.is_alive()
        finally:
            operator.close()
            expiry.close()

        result = outcome["result"]
        assert result.execution_id is None and result.skipped_reason
        with Session(pg_engine) as verify:
            assert [r.execution_id for r in restores(verify, incident_id)] == [operator_execution_id]
            assert block_id not in exec_repo.list_nacl_deny_expired(
                verify, finished_before=datetime.now(timezone.utc)
            )
            # 물러난 만료 접수는 발동 활동 로그를 남기지 않는다(#589 — 관제자 행은 저장소로 직접 넣었다)
            assert platform_facts(verify, incident_id, block_id, operator_execution_id) == []
    finally:
        with Session(pg_engine) as cleanup:
            purge_audit_events(cleanup, incident_ids=[incident_id])
            cleanup.execute(
                update(models.ActionExecution)
                .where(models.ActionExecution.incident_id == incident_id)
                .values(backup_record_id=None, candidate_id=None)
            )
            cleanup.execute(
                delete(models.BackupRecord).where(
                    models.BackupRecord.target_arn == ACL_ARN,
                    models.BackupRecord.execution_id.in_(
                        select(models.ActionExecution.execution_id).where(
                            models.ActionExecution.incident_id == incident_id
                        )
                    ),
                )
            )
            cleanup.execute(
                delete(models.ActionExecution).where(
                    models.ActionExecution.incident_id == incident_id
                )
            )
            cleanup.execute(
                delete(models.RunbookCandidate).where(
                    models.RunbookCandidate.incident_id == incident_id
                )
            )
            cleanup.execute(delete(models.Incident).where(models.Incident.incident_id == incident_id))
            cleanup.commit()


# ------------------------------------------------------- 8c. 다른 인시던트 접수와의 직렬화


def test_expiry_waits_for_another_incidents_reservation_on_the_same_target_and_then_skips(
    pg_engine, make_incident, make_execution
):
    """다른 인시던트가 같은 NACL에 실행을 넣는 동안(대상 잠금을 쥔 채 미커밋) 만료 접수는 기다리고,
    commit 뒤에는 그 실행을 보고 물러난다. 인시던트가 달라 인시던트 잠금으로는 줄을 서지 않으므로,
    대상 잠금(T-10 · #595)이 없으면 서로의 미커밋 행을 못 본 채 같은 NACL에 실행이 둘 선다."""
    with Session(pg_engine) as setup:
        incident = make_incident(
            setup,
            category=IncidentCategory.SECOPS,
            subject_arn=ACL_ARN,
            status=IncidentStatus.ACTION_IN_PROGRESS,
        )
        other = make_incident(setup, category=IncidentCategory.SECOPS, subject_arn=ACL_ARN)
        incident_id, other_id = incident.incident_id, other.incident_id
        incident_ids = [incident_id, other_id]
        block = make_execution(
            setup,
            incident,
            runbook_id=ADD_DENY,
            target_arn=ACL_ARN,
            status=ExecutionStatus.SUCCESS,
            trigger_source=TriggerSource.USER_APPROVAL,
        )
        block_id = block.execution_id
        record = exec_repo.create_backup_record(
            setup,
            execution_id=block_id,
            target_arn=ACL_ARN,
            backup_type=ex.BACKUP_NACL_RULE_INDEX,
            payload={
                "rule_number": 100,
                "egress": False,
                "cidr_block": CIDR,
                "protocol": "6",
                "rule_action": "deny",
            },
        )
        assert exec_repo.bind_backup_record(setup, block_id, record.backup_record_id)
        block.finished_at = datetime.now(timezone.utc) - timedelta(hours=25)
        setup.commit()

    outcome = {}
    try:
        operator, expiry = Session(pg_engine), Session(pg_engine)
        try:
            # 관제자 접수(reserve_execution)가 하는 순서 그대로 — 대상 잠금을 잡고 확인한 뒤 행을 넣는다
            exec_repo.lock_target(operator, ACL_ARN)
            exec_repo.create_execution(
                operator,
                incident_id=other_id,
                runbook_id=ADD_DENY,
                target_arn=ACL_ARN,
                trigger_source=TriggerSource.USER_APPROVAL,
            )  # 미커밋
            # pid는 트랜잭션을 연 채 읽는다 — 이유는 8b와 같다
            expiry.execute(text("SET LOCAL lock_timeout = '30s'"))
            expiry_pid = expiry.scalar(text("SELECT pg_backend_pid()"))

            def reserve():
                outcome["result"] = workflows.reserve_nacl_deny_expiry(expiry, block_id)

            thread = threading.Thread(target=reserve)
            thread.start()
            try:
                assert _waiting_on_lock(pg_engine, expiry_pid), "만료 접수가 대상 잠금을 기다리지 않았다"
                operator.commit()  # 잠금이 풀리고 만료 접수가 이 행을 본다
            finally:
                operator.rollback()
                thread.join(timeout=30)
            assert not thread.is_alive()
        finally:
            operator.close()
            expiry.close()

        result = outcome["result"]
        assert result.execution_id is None
        assert "진행 중" in result.skipped_reason
        with Session(pg_engine) as verify:
            assert restores(verify, incident_id) == []
    finally:
        with Session(pg_engine) as cleanup:
            purge_audit_events(cleanup, incident_ids=incident_ids)
            cleanup.execute(
                update(models.ActionExecution)
                .where(models.ActionExecution.incident_id.in_(incident_ids))
                .values(backup_record_id=None, candidate_id=None)
            )
            cleanup.execute(
                delete(models.BackupRecord).where(
                    models.BackupRecord.target_arn == ACL_ARN,
                    models.BackupRecord.execution_id.in_(
                        select(models.ActionExecution.execution_id).where(
                            models.ActionExecution.incident_id.in_(incident_ids)
                        )
                    ),
                )
            )
            cleanup.execute(
                delete(models.ActionExecution).where(
                    models.ActionExecution.incident_id.in_(incident_ids)
                )
            )
            cleanup.execute(delete(models.Incident).where(models.Incident.incident_id.in_(incident_ids)))
            cleanup.commit()


# ------------------------------------------------------- 9. 저장소 수준


def test_list_nacl_deny_expired_filters_by_status_and_age(db, bare_block, make_execution):
    incident, block = bare_block()
    young_incident, young = bare_block(hours=1)
    failed = make_execution(
        db,
        incident,
        runbook_id=ADD_DENY,
        target_arn=ACL_ARN,
        status=ExecutionStatus.FAILED,
    )
    failed.finished_at = datetime.now(timezone.utc) - timedelta(hours=30)
    db.commit()

    found = exec_repo.list_nacl_deny_expired(
        db, finished_before=datetime.now(timezone.utc) - workflows.NACL_DENY_TTL
    )

    assert block.execution_id in found
    assert young.execution_id not in found and failed.execution_id not in found
