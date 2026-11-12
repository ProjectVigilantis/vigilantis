# ==============================================================================
# [파일 설명]
# 실행 직전 재검증 · 같은 대상 동시 실행 거절 (Post-MVP T-10 · #595). 실제 PostgreSQL.
#
# 지키는 것.
#   ① 같은 대상(target_arn)에 끝나지 않은 실행이 있으면 관제자 승인 · 복구 접수가 409
#      TARGET_EXECUTION_IN_PROGRESS 로 거절되고, 실행 행 · 후보 선점 · 인시던트 전이가 남지 않는다.
#      끝난 실행은 막지 않는다.
#   ② 진행 중인 실행 자신의 원복(원본이 ROLLBACK_INITIATED 인 관제자 복구)은 막지 않는다.
#   ③ 자동 격리는 같은 인스턴스가 바쁘면 접수하지 않고 TARGET_EXECUTION_IN_PROGRESS 로 건너뛴다.
#   ④ 동시에 들어온 같은 대상 접수는 대상 잠금에서 기다렸다가, 앞선 실행이 commit 되면 409 로 떨어진다.
#   ⑤ 승인 대기 중 대상 자산이 사라졌거나 최신 판정이 더는 후보가 아니면, FinOps 실행이 AWS 호출 없이
#      FAILED 로 닫힌다. 확인할 근거(자산 · 판정)가 없으면 막지 않는다.
#   ⑥ 다운사이징은 실행 직전 타입(새 백업 값, 재개면 다시 읽은 값)이 탐지 당시 타입과 다르면 변경 호출 전에
#      멈춘다. 재개 때 원복용 백업은 바꾸지 않는다.
# ==============================================================================

from __future__ import annotations

import threading
import time
import uuid
from datetime import UTC, datetime, timedelta

import pytest
import workflows
from db import models
from db.repositories import assets as assets_repo
from db.repositories import executions as exec_repo
from db.repositories import incidents as incidents_repo
from exceptions import ApiError
from schemas.api.actions import ExecuteActionRequest, ExecutionStatus
from schemas.api.assets import AssetType
from schemas.api.errors import ErrorCode
from schemas.api.incidents import IncidentCategory, IncidentStatus
from schemas.candidates import CandidateStatus
from schemas.evidence import EvidenceType
from schemas.platform_events import PlatformActorKind, PlatformEventOutcome, PlatformEventTargetType
from schemas.runbooks import RunbookId, TriggerSource
from sqlalchemy import delete, select, text
from sqlalchemy.orm import Session
from tests.audit_cleanup import purge_audit_events
from tests.test_auto_isolation_workflow import TIMEOUT, _attempt, _executions, _seed
from tests.test_auto_isolation_workflow import SKIP as AUTO_SKIP
from tests.test_auto_isolation_workflow import SUBJECT as ISOLATION_SUBJECT
from tests.test_auto_rollback_workflow import (
    ACCOUNT,
    APPLIED_TYPE,
    BACKUP_TYPE,
    INSTANCE,
    INSTANCE_ARN,
    REGION,
    cycle,
    operations,
    reserve_manual_revert,
)
from tests.test_auto_rollback_workflow import aws, rolled_back_origin  # noqa: F401 (픽스처는 이름으로 등록된다)

NACL_ARN = "arn:aws:ec2:ap-northeast-2:123456789012:network-acl/acl-0t10race"
CHANGED = workflows.TARGET_CHANGED_BEFORE_START
NOW = datetime(2026, 11, 9, 3, 0, tzinfo=UTC)


def _approve(incident_id: str, runbook_id: RunbookId) -> ExecuteActionRequest:
    return ExecuteActionRequest(
        incident_id=incident_id, runbook_id=runbook_id, idempotency_key=str(uuid.uuid4())
    )


def _busy_target(db, make_incident, make_execution, target_arn: str, *, status=None):
    """다른 인시던트가 같은 대상에 남긴 실행. 기본은 접수 직후(IN_PROGRESS)."""
    other = make_incident(db)
    return make_execution(
        db, other, runbook_id=RunbookId.RUNBOOK_NACL_ADD_DENY, target_arn=target_arn, status=status
    )


# ----- ① · ② 관제자 접수 ---------------------------------------------------------

@pytest.mark.parametrize("busy", [ExecutionStatus.IN_PROGRESS, ExecutionStatus.ROLLBACK_INITIATED])
def test_approval_on_a_busy_target_is_refused_without_side_effects(
    db, make_incident, make_candidate, make_execution, audit_actor, busy
):
    _busy_target(db, make_incident, make_execution, NACL_ARN, status=busy)
    incident = make_incident(db)
    candidate = make_candidate(db, incident, target_arn=NACL_ARN)
    db.commit()  # 거절 뒤 rollback 이 시드까지 되돌리지 않게 한다

    with pytest.raises(ApiError) as refused:
        workflows.reserve_execution(
            db, _approve(incident.incident_id, candidate.runbook_id), audit_actor=audit_actor
        )

    assert refused.value.code is ErrorCode.TARGET_EXECUTION_IN_PROGRESS
    db.rollback()
    assert exec_repo.list_by_incident(db, incident.incident_id) == []
    assert incidents_repo.get_candidate(db, candidate.candidate_id).status is CandidateStatus.EXECUTABLE
    assert incidents_repo.get_incident(db, incident.incident_id).status is IncidentStatus.AWAITING_APPROVAL


@pytest.mark.parametrize(
    "settled", [ExecutionStatus.SUCCESS, ExecutionStatus.FAILED, ExecutionStatus.ROLLED_BACK]
)
def test_finished_executions_on_the_target_do_not_block(
    db, make_incident, make_candidate, make_execution, audit_actor, settled
):
    _busy_target(db, make_incident, make_execution, NACL_ARN, status=settled)
    incident = make_incident(db)
    candidate = make_candidate(db, incident, target_arn=NACL_ARN)

    reserved = workflows.reserve_execution(
        db, _approve(incident.incident_id, candidate.runbook_id), audit_actor=audit_actor
    )

    assert reserved.created is True


def test_a_different_target_is_not_blocked(
    db, make_incident, make_candidate, make_execution, audit_actor
):
    _busy_target(db, make_incident, make_execution, NACL_ARN)
    incident = make_incident(db)
    candidate = make_candidate(db, incident, target_arn=NACL_ARN + "-other")

    assert workflows.reserve_execution(
        db, _approve(incident.incident_id, candidate.runbook_id), audit_actor=audit_actor
    ).created is True


def test_second_candidate_of_the_same_incident_waits_for_the_first(
    db, make_incident, make_candidate, audit_actor
):
    """같은 인시던트의 두 제안이 같은 대상을 가리키면, 앞의 실행이 끝나기 전 뒤의 승인은 거절된다."""
    incident = make_incident(db, category=IncidentCategory.FINOPS, subject_arn=INSTANCE_ARN)
    make_candidate(
        db, incident, runbook_id=RunbookId.RUNBOOK_EC2_RIGHTSIZING,
        parameters={"target_instance_type": APPLIED_TYPE},
    )
    make_candidate(db, incident, runbook_id=RunbookId.RUNBOOK_EC2_ENABLE_AUTOSCALING)
    workflows.reserve_execution(
        db, _approve(incident.incident_id, RunbookId.RUNBOOK_EC2_RIGHTSIZING), audit_actor=audit_actor
    )

    with pytest.raises(ApiError) as refused:
        workflows.reserve_execution(
            db,
            _approve(incident.incident_id, RunbookId.RUNBOOK_EC2_ENABLE_AUTOSCALING),
            audit_actor=audit_actor,
        )
    assert refused.value.code is ErrorCode.TARGET_EXECUTION_IN_PROGRESS


def test_same_key_replay_is_still_a_replay(db, make_incident, make_candidate, audit_actor):
    """앞선 접수가 대상을 차지해도, 같은 Key 재요청은 409 가 아니라 200 재생이다."""
    incident = make_incident(db)
    candidate = make_candidate(db, incident, target_arn=NACL_ARN)
    request = _approve(incident.incident_id, candidate.runbook_id)
    first = workflows.reserve_execution(db, request, audit_actor=audit_actor)

    replay = workflows.reserve_execution(db, request, audit_actor=audit_actor)

    assert replay.created is False
    assert replay.response.execution_id == first.response.execution_id


def test_recovery_of_a_rollback_initiated_origin_is_not_blocked_by_itself(
    db, rolled_back_origin, audit_actor
):
    """원본이 ROLLBACK_INITIATED 면 그 자신이 같은 대상의 끝나지 않은 실행이다. 복구는 그것을 끝내는 일이다."""
    incident_id, origin_id = rolled_back_origin()

    child_id = reserve_manual_revert(db, incident_id, audit_actor)

    child = exec_repo.get_execution(db, child_id)
    assert child.parent_execution_id == origin_id
    assert child.status is ExecutionStatus.IN_PROGRESS


def test_recovery_is_refused_while_another_execution_holds_the_target(
    db, rolled_back_origin, make_incident, make_execution, audit_actor
):
    incident_id, _ = rolled_back_origin(origin_status=ExecutionStatus.SUCCESS)
    _busy_target(db, make_incident, make_execution, INSTANCE_ARN)
    db.commit()

    with pytest.raises(ApiError) as refused:
        reserve_manual_revert(db, incident_id, audit_actor)

    assert refused.value.code is ErrorCode.TARGET_EXECUTION_IN_PROGRESS


# ----- ③ 자동 격리 ---------------------------------------------------------------

def test_automatic_isolation_skips_a_busy_instance(db, make_incident, make_candidate, make_execution):
    seeded = _seed(db, make_incident, make_candidate, "medium")
    _busy_target(db, make_incident, make_execution, ISOLATION_SUBJECT)
    db.commit()

    result = _attempt(db, seeded, TIMEOUT)

    assert result.skipped_reason is AUTO_SKIP.TARGET_EXECUTION_IN_PROGRESS
    assert result.execution_id is None
    assert _executions(db, seeded.incident_id) == []


def test_same_source_attempt_holding_the_target_is_already_reserved(
    db, make_incident, make_candidate, monkeypatch
):
    """대상을 차지한 것이 같은 출처의 앞선 시도면 다른 실행이 아니다. 대상 사유가 아니라 ALREADY_RESERVED 로
    돌려야 호출부(security/soar.py)가 승자 실행을 이어 보낸다. 키 조회가 그 시도를 놓친 경우를 재현한다."""
    seeded = _seed(db, make_incident, make_candidate, "medium")
    winner = exec_repo.create_execution(
        db, incident_id=seeded.incident_id, runbook_id=RunbookId.RUNBOOK_EC2_ISOLATE,
        target_arn=ISOLATION_SUBJECT, trigger_source=TIMEOUT,
        deduplication_key=workflows.auto_isolation_deduplication_key(TIMEOUT, seeded.incident_id),
    )
    db.commit()
    monkeypatch.setattr(workflows.executions_repo, "get_by_deduplication_key", lambda *a, **k: None)

    result = _attempt(db, seeded, TIMEOUT)

    assert result.skipped_reason is AUTO_SKIP.ALREADY_RESERVED
    assert [row.execution_id for row in _executions(db, seeded.incident_id)] == [winner.execution_id]


def test_automatic_isolation_proceeds_once_the_other_execution_finished(
    db, make_incident, make_candidate, make_execution
):
    seeded = _seed(db, make_incident, make_candidate, "medium")
    _busy_target(db, make_incident, make_execution, ISOLATION_SUBJECT, status=ExecutionStatus.SUCCESS)
    db.commit()

    result = _attempt(db, seeded, TIMEOUT)

    assert result.created is True


# ----- ④ 동시 접수 경합(독립 세션) -------------------------------------------------

def _waiting_on_lock(engine, pid: int) -> bool:
    """그 백엔드가 advisory lock 을 기다리는가. 시간 제한은 관측 상한일 뿐 순서의 증거가 아니다."""
    deadline = time.monotonic() + 10
    with engine.connect() as probe:
        while time.monotonic() < deadline:
            row = probe.execute(
                text("SELECT wait_event_type, wait_event FROM pg_stat_activity WHERE pid = :pid"),
                {"pid": pid},
            ).one_or_none()
            if row is not None and row[0] == "Lock" and row[1] == "advisory":
                return True
            time.sleep(0.02)
    return False


def test_concurrent_reservation_waits_for_the_target_lock_and_then_refuses(
    pg_engine, make_incident, make_candidate, committed_actor
):
    """앞선 트랜잭션이 대상 잠금을 쥔 채 실행 행을 넣는 동안 뒤의 접수는 기다리고, commit 뒤에는 그 행을
    보고 409 로 떨어진다. 잠금이 없으면 둘 다 서로의 미커밋 행을 못 본 채 통과한다."""
    target = NACL_ARN + "-" + uuid.uuid4().hex[:8]  # 다른 테스트와 대상을 공유하지 않는다
    with Session(pg_engine) as setup:
        holder_incident = make_incident(setup)
        racer_incident = make_incident(setup)
        candidate = make_candidate(setup, racer_incident, target_arn=target)
        incident_ids = [holder_incident.incident_id, racer_incident.incident_id]
        candidate_id = candidate.candidate_id
        setup.commit()

    outcome: dict = {}
    try:
        holder, racer = Session(pg_engine), Session(pg_engine)
        try:
            exec_repo.lock_target(holder, target)
            racer_pid = racer.scalar(text("SELECT pg_backend_pid()"))
            racer.execute(text("SET LOCAL lock_timeout = '30s'"))

            def reserve():
                try:
                    outcome["result"] = workflows.reserve_execution(
                        racer,
                        _approve(incident_ids[1], RunbookId.RUNBOOK_NACL_ADD_DENY),
                        audit_actor=committed_actor,
                    )
                except ApiError as exc:
                    outcome["result"] = exc.code
                    racer.rollback()

            thread = threading.Thread(target=reserve)
            thread.start()
            assert _waiting_on_lock(pg_engine, racer_pid), "뒤의 접수가 대상 잠금을 기다리지 않았다"
            holder.add(models.ActionExecution(
                incident_id=incident_ids[0],
                runbook_id=RunbookId.RUNBOOK_NACL_ADD_DENY,
                target_arn=target,
                trigger_source=TriggerSource.USER_APPROVAL,
            ))
            holder.commit()  # 잠금이 풀리고 뒤의 접수가 이 행을 본다
            thread.join(timeout=30)
            assert not thread.is_alive()
        finally:
            holder.close()
            racer.close()

        assert outcome["result"] is ErrorCode.TARGET_EXECUTION_IN_PROGRESS
        with Session(pg_engine) as verify:
            assert exec_repo.list_by_incident(verify, incident_ids[1]) == []
            assert incidents_repo.get_candidate(verify, candidate_id).status is CandidateStatus.EXECUTABLE
    finally:
        with Session(pg_engine) as cleanup:
            purge_audit_events(cleanup, incident_ids=incident_ids)
            cleanup.execute(
                delete(models.ActionExecution).where(models.ActionExecution.incident_id.in_(incident_ids))
            )
            cleanup.execute(
                delete(models.RunbookCandidate).where(models.RunbookCandidate.incident_id.in_(incident_ids))
            )
            cleanup.execute(delete(models.Incident).where(models.Incident.incident_id.in_(incident_ids)))
            cleanup.commit()


# ----- ⑤ · ⑥ 시작 직전 재검증 ------------------------------------------------------

def _collect(db, *, instance_type: str, verdict: str | None, skip_reason: str | None = None,
             evaluated_at: datetime | None = None) -> models.Asset:
    """수집 1회차. 자산 upsert 와 그 회차의 판정. 판정은 회차마다 쌓인다."""
    run = assets_repo.start_collection_run(
        db, account_id=ACCOUNT, region=REGION, mode="localstack", lookback_days=14,
        period_seconds=3600,
    )
    asset = assets_repo.upsert_asset(
        db, arn=INSTANCE_ARN, asset_type=AssetType.EC2, resource_id=INSTANCE, account_id=ACCOUNT,
        region=REGION, spec={"instance_type": instance_type}, collection_run_id=run.collection_run_id,
        collected_at=datetime.now(UTC),
    )
    if verdict is not None or skip_reason is not None:
        db.add(models.RuleEvaluation(
            asset_id=asset.asset_id,
            collection_run_id=run.collection_run_id,
            evaluation_status="COMPLETED",
            verdict=verdict,
            skip_reason_code=skip_reason,
            evaluated_at=evaluated_at or datetime.now(UTC),
        ))
    db.flush()
    return asset


def _approved_rightsizing(db, make_incident, make_candidate, *, detected_type: str | None = BACKUP_TYPE):
    """관제자가 승인한 다운사이징 1건(단계 0건). 탐지 당시 ASSET 근거에 타입을 고정한다."""
    incident = make_incident(
        db, category=IncidentCategory.FINOPS, subject_arn=INSTANCE_ARN,
        status=IncidentStatus.ACTION_IN_PROGRESS,
    )
    if detected_type is not None:
        db.add(models.Evidence(
            incident_id=incident.incident_id,
            evidence_type=EvidenceType.ASSET,
            source_type="asset_snapshot",
            source_id=INSTANCE_ARN,
            content={
                "collection_run_id": "run-detect",
                "asset": {"arn": INSTANCE_ARN, "asset_type": "EC2",
                          "spec": {"instance_type": detected_type}},
            },
            occurred_at=NOW,
        ))
    candidate = make_candidate(
        db, incident, runbook_id=RunbookId.RUNBOOK_EC2_RIGHTSIZING, target_arn=INSTANCE_ARN,
        parameters={"target_instance_type": APPLIED_TYPE}, status=CandidateStatus.CLAIMED,
    )
    execution = exec_repo.create_execution(
        db, incident_id=incident.incident_id, runbook_id=RunbookId.RUNBOOK_EC2_RIGHTSIZING,
        target_arn=INSTANCE_ARN, trigger_source=TriggerSource.USER_APPROVAL,
        candidate_id=candidate.candidate_id, idempotency_key=str(uuid.uuid4()),
    )
    db.commit()
    return incident.incident_id, execution.execution_id


def test_gate_passes_while_the_latest_verdict_is_still_a_candidate(db, make_incident, make_candidate):
    _collect(db, instance_type=BACKUP_TYPE, verdict="COST_CANDIDATE")
    _, execution_id = _approved_rightsizing(db, make_incident, make_candidate)

    workflows.refuse_start_if_target_changed(db, exec_repo.get_execution(db, execution_id))


@pytest.mark.parametrize("verdict, skip_reason", [
    ("SKIP", "SKIP_ACTIVE"),          # 승인 대기 중 부하가 올라 저활성이 아니게 됐다
    (None, "SKIP_PROD_PROTECTED"),    # 판정 보류
])
def test_gate_refuses_when_the_latest_verdict_is_no_longer_a_candidate(
    db, make_incident, make_candidate, verdict, skip_reason
):
    _collect(db, instance_type=BACKUP_TYPE, verdict="COST_CANDIDATE",
             evaluated_at=datetime.now(UTC) - timedelta(hours=2))
    _collect(db, instance_type=BACKUP_TYPE, verdict=verdict, skip_reason=skip_reason)
    _, execution_id = _approved_rightsizing(db, make_incident, make_candidate)

    with pytest.raises(workflows.TargetChangedBeforeStart) as refused:
        workflows.refuse_start_if_target_changed(db, exec_repo.get_execution(db, execution_id))

    assert str(refused.value).startswith(f"{CHANGED}: ")
    assert (verdict or skip_reason) in str(refused.value)


def test_gate_refuses_when_the_asset_vanished(db, make_incident, make_candidate):
    asset = _collect(db, instance_type=BACKUP_TYPE, verdict="COST_CANDIDATE")
    asset.absent_since = datetime.now(UTC)
    _, execution_id = _approved_rightsizing(db, make_incident, make_candidate)

    with pytest.raises(workflows.TargetChangedBeforeStart):
        workflows.refuse_start_if_target_changed(db, exec_repo.get_execution(db, execution_id))


@pytest.mark.parametrize("collected", [False, True], ids=["no-asset", "no-evaluation"])
def test_gate_does_not_block_without_evidence_of_change(db, make_incident, make_candidate, collected):
    if collected:
        _collect(db, instance_type=BACKUP_TYPE, verdict=None)
    _, execution_id = _approved_rightsizing(db, make_incident, make_candidate)

    workflows.refuse_start_if_target_changed(db, exec_repo.get_execution(db, execution_id))


def test_gate_ignores_threat_blocks_and_automatic_paths(db, make_incident, make_candidate, make_execution):
    """NACL 차단(위협 출처) · 자동 원복은 이 관문의 대상이 아니다. 판정이 바뀌어도 막지 않는다."""
    _collect(db, instance_type=BACKUP_TYPE, verdict="SKIP", skip_reason="SKIP_ACTIVE")
    incident = make_incident(db, category=IncidentCategory.FINOPS, subject_arn=INSTANCE_ARN)
    for runbook_id, trigger in (
        (RunbookId.RUNBOOK_NACL_ADD_DENY, TriggerSource.USER_APPROVAL),
        (RunbookId.RUNBOOK_EC2_REVERT_SIZE, TriggerSource.AUTO_ON_FAILURE),
    ):
        execution = make_execution(db, incident, runbook_id=runbook_id, target_arn=INSTANCE_ARN,
                                   trigger_source=trigger)
        workflows.refuse_start_if_target_changed(db, execution)


def _assert_target_changed_event(db, incident_id, execution_id):
    rows = db.scalars(
        select(models.PlatformEvent).where(
            models.PlatformEvent.target_id.in_([str(incident_id), str(execution_id)])
        )
    ).all()
    assert [
        (row.action, row.outcome, row.actor_kind, row.target_type, row.target_id, row.details)
        for row in rows
    ] == [(
        "ACTION_COMPLETE", PlatformEventOutcome.FAILED, PlatformActorKind.SYSTEM,
        PlatformEventTargetType.EXECUTION, str(execution_id),
        {"reason_code": "TARGET_CHANGED_BEFORE_START"},
    )]


def test_dispatch_closes_a_stale_approval_without_touching_aws(db, make_incident, make_candidate, aws):
    _collect(db, instance_type=BACKUP_TYPE, verdict="COST_CANDIDATE",
             evaluated_at=datetime.now(UTC) - timedelta(hours=2))
    _collect(db, instance_type=BACKUP_TYPE, verdict="SKIP", skip_reason="SKIP_ACTIVE")
    incident_id, execution_id = _approved_rightsizing(db, make_incident, make_candidate)

    cycle(db)

    db.expire_all()
    execution = exec_repo.get_execution(db, execution_id)
    assert execution.status is ExecutionStatus.FAILED
    assert execution.error_summary.startswith(f"{CHANGED}: ")
    assert execution.backup_record_id is None  # 백업 캡처(AWS 읽기)도 하지 않았다
    assert operations(aws) == []
    assert exec_repo.list_steps(db, execution_id) == []
    assert incidents_repo.get_incident(db, incident_id).status is not IncidentStatus.ACTION_IN_PROGRESS
    _assert_target_changed_event(db, incident_id, execution_id)


def test_rightsizing_stops_when_the_type_changed_while_waiting(db, make_incident, make_candidate, aws):
    """탐지 때 t3.xlarge 였는데 승인 대기 중 누가 t3.2xlarge 로 올렸다. 목표 타입은 지금의 인스턴스에 대한
    계산이 아니다. 백업은 잡되 정지 · 타입 변경은 부르지 않는다."""
    _collect(db, instance_type=BACKUP_TYPE, verdict="COST_CANDIDATE")
    incident_id, execution_id = _approved_rightsizing(db, make_incident, make_candidate)
    aws(current_type="t3.2xlarge")

    cycle(db)

    db.expire_all()
    execution = exec_repo.get_execution(db, execution_id)
    assert execution.status is ExecutionStatus.FAILED
    assert execution.error_summary.startswith(f"{CHANGED}: ")
    assert "t3.2xlarge" in execution.error_summary and BACKUP_TYPE in execution.error_summary
    assert "describe_instances" in operations(aws)
    assert "stop_instances" not in operations(aws)
    assert "modify_instance_attribute" not in operations(aws)
    assert exec_repo.list_steps(db, execution_id) == []
    _assert_target_changed_event(db, incident_id, execution_id)


@pytest.mark.parametrize("detected_type", [BACKUP_TYPE, None], ids=["same-type", "no-snapshot"])
def test_rightsizing_proceeds_when_the_type_is_unchanged_or_unknown(
    db, make_incident, make_candidate, aws, detected_type
):
    _collect(db, instance_type=BACKUP_TYPE, verdict="COST_CANDIDATE")
    _, execution_id = _approved_rightsizing(db, make_incident, make_candidate, detected_type=detected_type)
    aws(current_type=BACKUP_TYPE)

    cycle(db)

    assert "stop_instances" in operations(aws)
    db.expire_all()
    assert exec_repo.list_steps(db, execution_id) != []


@pytest.mark.parametrize("resumed_type, proceeds", [
    ("t3.2xlarge", False),   # 중단된 사이 누가 올렸다. 지난 백업 값으로 통과하면 안 된다
    (BACKUP_TYPE, True),     # 그대로면 재개한다
], ids=["changed-while-down", "unchanged"])
def test_resumed_rightsizing_rechecks_the_current_type(
    db, make_incident, make_candidate, aws, resumed_type, proceeds
):
    """백업 commit 뒤 첫 단계 전에 프로세스가 죽었다(단계 0건). 디스패처가 다시 돌릴 때 결속된 백업은 지난
    관측이므로 지금 타입을 다시 읽는다. 원복용 백업은 바꾸지 않는다."""
    _collect(db, instance_type=BACKUP_TYPE, verdict="COST_CANDIDATE")
    _, execution_id = _approved_rightsizing(db, make_incident, make_candidate)
    aws(current_type=BACKUP_TYPE)
    first = workflows.store_instance_spec_backup(db, execution_id)
    assert first.created and exec_repo.list_steps(db, execution_id) == []
    aws(current_type=resumed_type)

    cycle(db)

    db.expire_all()
    execution = exec_repo.get_execution(db, execution_id)
    assert execution.backup_record_id == first.record.backup_record_id
    assert exec_repo.get_backup_record(db, execution.backup_record_id).payload["instance_type"] == BACKUP_TYPE
    if proceeds:
        assert "stop_instances" in operations(aws)
        return
    assert execution.status is ExecutionStatus.FAILED
    assert execution.error_summary.startswith(f"{CHANGED}: ")
    assert "t3.2xlarge" in execution.error_summary
    assert "stop_instances" not in operations(aws)
    assert "modify_instance_attribute" not in operations(aws)
    assert exec_repo.list_steps(db, execution_id) == []
