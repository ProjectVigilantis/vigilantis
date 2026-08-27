"""Repository·제약 통합 테스트 — 실제 PostgreSQL 필요(미기동 시 skip). (Issue #60)

Alembic head가 적용된 일회용 DB에서 조건부 갱신·원자 Claim·유니크/CHECK 거절을
확인한다. 트랜잭션은 테스트마다 rollback된다(conftest).
"""

import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

CORE_API = Path(__file__).resolve().parents[2]
REPO_ROOT = Path(__file__).resolve().parents[4]
for p in (str(CORE_API), str(REPO_ROOT / "packages")):
    if p not in sys.path:
        sys.path.insert(0, p)

from sqlalchemy import select, text  # noqa: E402
from sqlalchemy.exc import IntegrityError  # noqa: E402

from db import models  # noqa: E402
from db.repositories import assets as assets_repo  # noqa: E402
from db.repositories import executions as exec_repo  # noqa: E402
from db.repositories import guardrails as guard_repo  # noqa: E402
from db.repositories import incidents as incidents_repo  # noqa: E402
from schemas.api.actions import ExecutionStatus  # noqa: E402
from schemas.api.assets import AssetType, RelationType  # noqa: E402
from schemas.api.incidents import (  # noqa: E402
    IncidentCategory,
    IncidentStatus,
    ResponseMode,
    RiskLevel,
)
from schemas.assets import MetricSummary as MetricSummaryContract  # noqa: E402
from schemas.candidates import CandidateStatus, RunbookCandidateData  # noqa: E402
from schemas.collections import CollectionRunStatus  # noqa: E402
from schemas.executions import (  # noqa: E402
    ExecutionEffect,
    ExecutionStepResult,
    ExecutionStepStatus,
)
from schemas.guardrails import (  # noqa: E402
    GuardrailDecision,
    GuardrailValidationContext,
    GuardrailValidationResult,
)
from schemas.incidents import AgentInvocationStatus, AgentWaitSchedule  # noqa: E402
from schemas.runbooks import RunbookId, TriggerSource  # noqa: E402

NOW = datetime(2026, 8, 18, 12, 0, 0, tzinfo=timezone.utc)


def _secops_incident(db, **overrides):
    kwargs = dict(
        subject_arn="arn:aws:ec2:ap-northeast-2:123456789012:instance/i-1",
        category=IncidentCategory.SECOPS,
        initial_risk_level=RiskLevel.MEDIUM,
        response_mode=ResponseMode.AGENT_WAIT,
        initial_risk_reason_codes=["SSH_FAILED_ATTEMPTS_OVER_THRESHOLD"],
    )
    kwargs.update(overrides)
    return incidents_repo.create_incident(db, **kwargs)


def _finops_incident(db):
    return incidents_repo.create_incident(
        db,
        subject_arn="arn:aws:ec2:ap-northeast-2:123456789012:instance/i-2",
        category=IncidentCategory.FINOPS,
    )


def _execution(db, incident, **overrides):
    kwargs = dict(
        incident_id=incident.incident_id,
        runbook_id=RunbookId.RUNBOOK_EC2_RIGHTSIZING,
        target_arn=incident.subject_arn,
        trigger_source=TriggerSource.USER_APPROVAL,
    )
    kwargs.update(overrides)
    return exec_repo.create_execution(db, **kwargs)


# --- 마이그레이션 적용 상태 ----------------------------------------------------


def test_alembic_head_applied_with_13_tables(db):
    # 리비전 문자열을 적어 두면 마이그레이션마다 이 줄을 고쳐야 하고, 그 수정은
    # 검증이 아니라 손질이다. 확인할 것은 "DB가 head까지 올라와 있는가"다.
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    head = ScriptDirectory.from_config(
        Config(str(CORE_API / "alembic.ini"))
    ).get_current_head()
    version = db.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
    assert version == head
    count = db.execute(
        text(
            "SELECT count(*) FROM information_schema.tables"
            " WHERE table_schema='public' AND table_name != 'alembic_version'"
        )
    ).scalar_one()
    assert count == 13


# --- 자산 계열 -----------------------------------------------------------------


def test_upsert_asset_is_single_row_per_arn(db):
    run = assets_repo.start_collection_run(
        db, account_id="123456789012", region="ap-northeast-2",
        mode="localstack", lookback_days=14, period_seconds=3600,
    )
    arn = "arn:aws:ec2:ap-northeast-2:123456789012:instance/i-up"
    first = assets_repo.upsert_asset(
        db, arn=arn, asset_type=AssetType.EC2, resource_id="i-up",
        account_id="123456789012", region="ap-northeast-2",
        spec={"instance_type": "t3.large"}, collection_run_id=run.collection_run_id,
        collected_at=NOW,
    )
    second = assets_repo.upsert_asset(
        db, arn=arn, asset_type=AssetType.EC2, resource_id="i-up",
        account_id="123456789012", region="ap-northeast-2",
        spec={"instance_type": "t3.medium"}, collection_run_id=run.collection_run_id,
        collected_at=NOW,
    )
    assert first.asset_id == second.asset_id
    rows = db.execute(select(models.Asset).where(models.Asset.arn == arn)).scalars().all()
    assert len(rows) == 1
    assert rows[0].spec["instance_type"] == "t3.medium"


def test_replace_relationships_snapshot_and_reverse_lookup(db):
    run = assets_repo.start_collection_run(
        db, account_id="1", region="r", mode="localstack",
        lookback_days=14, period_seconds=3600,
    )
    asset = assets_repo.upsert_asset(
        db, arn="arn:ec2/i-rel", asset_type=AssetType.EC2, resource_id="i-rel",
        account_id="1", region="r", spec={}, collection_run_id=run.collection_run_id,
        collected_at=NOW,
    )
    assets_repo.replace_relationships(
        db, asset.asset_id,
        [(RelationType.SECURED_BY, "arn:sg/sg-1"), (RelationType.ATTACHED_TO, "arn:ebs/v-1")],
        collection_run_id=run.collection_run_id,
    )
    assets_repo.replace_relationships(
        db, asset.asset_id, [(RelationType.SECURED_BY, "arn:sg/sg-2")],
        collection_run_id=run.collection_run_id,
    )
    reverse = assets_repo.list_relationships_by_target(db, "arn:sg/sg-2")
    assert [r.source_asset_id for r in reverse] == [asset.asset_id]
    assert assets_repo.list_relationships_by_target(db, "arn:sg/sg-1") == []


def test_finish_collection_run_only_from_in_progress(db):
    run = assets_repo.start_collection_run(
        db, account_id="1", region="r", mode="localstack",
        lookback_days=14, period_seconds=3600,
    )
    assert assets_repo.finish_collection_run(
        db, run.collection_run_id, CollectionRunStatus.SUCCESS, finished_at=NOW
    )
    assert not assets_repo.finish_collection_run(
        db, run.collection_run_id, CollectionRunStatus.FAILED, finished_at=NOW
    )


def test_metric_summary_per_run_and_window_ordered(db):
    run = assets_repo.start_collection_run(
        db, account_id="1", region="r", mode="localstack",
        lookback_days=14, period_seconds=3600,
    )
    asset = assets_repo.upsert_asset(
        db, arn="arn:ec2/i-met", asset_type=AssetType.EC2, resource_id="i-met",
        account_id="1", region="r", spec={}, collection_run_id=run.collection_run_id,
        collected_at=NOW,
    )
    summary = MetricSummaryContract(cpu_datapoints=336, cpu_avg=3.2, cpu_max=11.5)
    assets_repo.add_metric_summary(
        db, asset_id=asset.asset_id, collection_run_id=run.collection_run_id,
        summary=summary, window_start=NOW - timedelta(days=14), window_end=NOW,
        collected_at=NOW,
    )
    # 같은 (자산, 회차) 중복은 유니크 제약이 거절한다
    with pytest.raises(IntegrityError, match="uq_metric_summaries_asset_id"):
        with db.begin_nested():
            assets_repo.add_metric_summary(
                db, asset_id=asset.asset_id, collection_run_id=run.collection_run_id,
                summary=summary, window_start=NOW - timedelta(days=14), window_end=NOW,
                collected_at=NOW,
            )
    # 뒤집힌 관측 구간은 CHECK가 거절한다
    with pytest.raises(IntegrityError, match="ck_metric_summaries_window_ordered"):
        with db.begin_nested():
            run2 = assets_repo.start_collection_run(
                db, account_id="1", region="r", mode="localstack",
                lookback_days=14, period_seconds=3600,
            )
            assets_repo.add_metric_summary(
                db, asset_id=asset.asset_id, collection_run_id=run2.collection_run_id,
                summary=summary, window_start=NOW, window_end=NOW - timedelta(days=1),
                collected_at=NOW,
            )


def test_rule_evaluation_success_path_and_latest(db):
    from schemas.rules import RuleEvaluationResult

    run = assets_repo.start_collection_run(
        db, account_id="1", region="r", mode="localstack",
        lookback_days=14, period_seconds=3600,
    )
    asset = assets_repo.upsert_asset(
        db, arn="arn:ec2/i-rule", asset_type=AssetType.EC2, resource_id="i-rule",
        account_id="1", region="r", spec={}, collection_run_id=run.collection_run_id,
        collected_at=NOW,
    )
    contract = RuleEvaluationResult.model_validate(
        {
            "asset_arn": asset.arn,
            "collection_run_id": run.collection_run_id,
            "evaluation_status": "COMPLETED",
            "verdict": "COST_CANDIDATE",
            "health_score": 12,
            "evaluated_at": NOW,
        }
    )
    saved = assets_repo.add_rule_evaluation(db, contract)
    latest = assets_repo.latest_rule_evaluation(db, asset.asset_id)
    assert latest.rule_evaluation_id == saved.rule_evaluation_id
    assert latest.verdict == "COST_CANDIDATE"
    assert latest.health_score == 12
    # 미존재 자산을 참조하는 판정은 저장하지 않는다
    missing = contract.model_copy(update={"asset_arn": "arn:missing"})
    with pytest.raises(LookupError):
        assets_repo.add_rule_evaluation(db, missing)


# --- 위협 이벤트·근거(테이블 실저장 경로) ---------------------------------------


def test_threat_event_insert_dedup_and_evidence_round_trip(db):
    from db import mappers
    from schemas.events import NormalizedThreatEvent, ThreatEventType
    from schemas.evidence import EvidenceItem, EvidenceType

    te_id = str(uuid.uuid4())
    contract = NormalizedThreatEvent.model_validate(
        {
            "threat_event_id": te_id,
            "source_event_id": "src-1",
            "event_type": ThreatEventType.OPEN_IP,
            "target_arn": "arn:sg/sg-threat",
            "occurred_at": NOW,
            "payload": {"protocol": "tcp", "from_port": 22, "to_port": 22,
                        "source_cidr": "0.0.0.0/0"},
            "deduplication_key": "dedup-abc",
            "collected_at": NOW,
        }
    )
    incidents_repo.insert_threat_event(db, contract)
    found = incidents_repo.get_threat_event_by_dedup_key(db, "dedup-abc")
    assert mappers.to_threat_event(found) == contract
    # 같은 중복 키 재삽입은 유니크 제약이 거절한다
    dup = contract.model_copy(update={"threat_event_id": str(uuid.uuid4())})
    with pytest.raises(IntegrityError, match="uq_threat_events_deduplication_key"):
        with db.begin_nested():
            incidents_repo.insert_threat_event(db, dup)

    incident = _secops_incident(db, threat_event_id=te_id)
    ev = EvidenceItem.model_validate(
        {
            "evidence_id": str(uuid.uuid4()),
            "incident_id": incident.incident_id,
            "evidence_type": EvidenceType.THREAT,
            "source_type": "threat_event",
            "source_id": te_id,
            "content": {"event": contract.model_dump(mode="json")},
            "occurred_at": NOW,
            "collected_at": NOW,
        }
    )
    incidents_repo.add_evidence(db, ev)
    listed = incidents_repo.list_evidence(db, incident.incident_id)
    assert [mappers.to_evidence_item(r) for r in listed] == [ev]


def test_list_non_terminal_and_updated_at_bump(db):
    incident = _finops_incident(db)
    running = _execution(db, incident, idempotency_key="nt-1")
    done = _execution(db, incident, idempotency_key="nt-2")
    exec_repo.update_execution_status(
        db, done.execution_id,
        expected=ExecutionStatus.IN_PROGRESS, next_status=ExecutionStatus.SUCCESS,
    )
    non_terminal = {e.execution_id for e in exec_repo.list_non_terminal(db)}
    assert running.execution_id in non_terminal
    assert done.execution_id not in non_terminal

    # expected가 어긋나면 갱신되지 않는다(전이 검증 2층)
    assert not incidents_repo.update_incident_status(
        db, incident.incident_id,
        expected=IncidentStatus.RESOLVED, next_status=IncidentStatus.FAILED,
    )
    # 조건부 UPDATE(Core 경로)에서도 updated_at onupdate가 적용된다
    before = incident.updated_at
    assert incidents_repo.update_incident_status(
        db, incident.incident_id,
        expected=IncidentStatus.ANALYZING, next_status=IncidentStatus.AWAITING_APPROVAL,
    )
    db.expire_all()
    after = incidents_repo.get_incident(db, incident.incident_id).updated_at
    assert after > before

    assert incidents_repo.touch_incident(db, incident.incident_id)
    db.expire_all()
    assert incidents_repo.get_incident(db, incident.incident_id).updated_at > after


# --- Incident 불변식(DB CheckConstraint 실거절) --------------------------------


def test_category_risk_shape_rejected_by_db(db):
    # FINOPS인데 위험도 있음
    with pytest.raises(IntegrityError, match="ck_incidents_category_risk_shape"):
        with db.begin_nested():
            incidents_repo.create_incident(
                db, subject_arn="arn:x", category=IncidentCategory.FINOPS,
                initial_risk_level=RiskLevel.HIGH,
            )
    # SECOPS인데 사유 코드 0개
    with pytest.raises(IntegrityError, match="ck_incidents_category_risk_shape"):
        with db.begin_nested():
            incidents_repo.create_incident(
                db, subject_arn="arn:x", category=IncidentCategory.SECOPS,
                initial_risk_level=RiskLevel.HIGH,
                response_mode=ResponseMode.PRE_MITIGATION_0_5S,
                initial_risk_reason_codes=[],
            )


# --- AI 호출 원자 Claim ---------------------------------------------------------


def test_agent_claim_is_atomic_and_single_winner(db):
    incident = _secops_incident(db)
    assert incidents_repo.claim_agent_invocation(db, incident.incident_id, started_at=NOW)
    assert not incidents_repo.claim_agent_invocation(
        db, incident.incident_id, started_at=NOW
    )


def test_agent_finish_only_from_in_progress_and_terminal_only(db):
    incident = _secops_incident(db)
    with pytest.raises(ValueError):
        incidents_repo.finish_agent_invocation(
            db, incident.incident_id, AgentInvocationStatus.PENDING
        )
    assert not incidents_repo.finish_agent_invocation(
        db, incident.incident_id, AgentInvocationStatus.SUCCEEDED
    )
    incidents_repo.claim_agent_invocation(db, incident.incident_id, started_at=NOW)
    assert incidents_repo.finish_agent_invocation(
        db, incident.incident_id, AgentInvocationStatus.SUCCEEDED,
        summary_lines=["a", "b", "c"], reviewed_risk_level=RiskLevel.LOW,
    )
    db.expire_all()
    refreshed = incidents_repo.get_incident(db, incident.incident_id)
    assert refreshed.summary_lines == ["a", "b", "c"]
    assert refreshed.reviewed_risk_level == RiskLevel.LOW


def test_reset_agent_invocation_recovers_claim(db):
    incident = _secops_incident(db)
    incidents_repo.claim_agent_invocation(db, incident.incident_id, started_at=NOW)
    assert incidents_repo.reset_agent_invocation(db, incident.incident_id)
    assert incidents_repo.claim_agent_invocation(db, incident.incident_id, started_at=NOW)


def test_set_agent_wait_once_and_deadline_constraint(db):
    incident = _secops_incident(db)
    schedule = AgentWaitSchedule(
        incident_id=incident.incident_id,
        started_at=NOW,
        response_deadline_at=NOW + timedelta(seconds=60),
    )
    assert incidents_repo.set_agent_wait(db, schedule)
    assert not incidents_repo.set_agent_wait(db, schedule)


# --- 후보 -----------------------------------------------------------------------


def _candidate(incident, runbook=RunbookId.RUNBOOK_EC2_RIGHTSIZING, cid=None):
    # parameters는 Runbook별 typed 계약이다(#154) — 기본 Runbook의 값을 싣는다.
    # display_parameters는 계약이 여기서 만들어 준다.
    return RunbookCandidateData(
        candidate_id=cid or str(uuid.uuid4()),
        incident_id=incident.incident_id,
        runbook_id=runbook,
        target_arn=incident.subject_arn,
        parameters={"target_instance_type": "t3.medium"},
        evidence_ids=["ev-1"],
        status=CandidateStatus.PENDING_VALIDATION,
    )


def test_active_candidate_unique_per_incident_runbook(db):
    incident = _finops_incident(db)
    cand_a = str(uuid.uuid4())
    incidents_repo.add_candidate(db, _candidate(incident, cid=cand_a))
    with pytest.raises(IntegrityError, match="uq_runbook_candidates_active"):
        with db.begin_nested():
            incidents_repo.add_candidate(db, _candidate(incident))
    # 비활성(REJECTED) 전이 후에는 같은 runbook 후보를 다시 만들 수 있다
    assert incidents_repo.update_candidate_status(
        db, cand_a,
        expected=CandidateStatus.PENDING_VALIDATION, next_status=CandidateStatus.REJECTED,
    )
    incidents_repo.add_candidate(db, _candidate(incident))


def test_update_candidate_status_conditional(db):
    incident = _finops_incident(db)
    cand_x = str(uuid.uuid4())
    incidents_repo.add_candidate(db, _candidate(incident, cid=cand_x))
    assert not incidents_repo.update_candidate_status(
        db, cand_x, expected=CandidateStatus.EXECUTABLE,
        next_status=CandidateStatus.CLAIMED,
    )
    assert incidents_repo.update_candidate_status(
        db, cand_x, expected=CandidateStatus.PENDING_VALIDATION,
        next_status=CandidateStatus.EXECUTABLE,
    )


# --- 실행·단계·백업 -------------------------------------------------------------


def test_idempotency_key_duplicate_rejected(db):
    incident = _finops_incident(db)
    _execution(db, incident, idempotency_key="idem-1")
    with pytest.raises(IntegrityError, match="uq_action_executions_idempotency_key"):
        with db.begin_nested():
            _execution(db, incident, idempotency_key="idem-1")
    assert exec_repo.get_by_idempotency_key(db, "idem-1") is not None


def test_execution_status_conditional_update(db):
    incident = _finops_incident(db)
    execution = _execution(db, incident)
    assert not exec_repo.update_execution_status(
        db, execution.execution_id,
        expected=ExecutionStatus.SUCCESS, next_status=ExecutionStatus.ROLLBACK_INITIATED,
    )
    assert exec_repo.update_execution_status(
        db, execution.execution_id,
        expected=ExecutionStatus.IN_PROGRESS, next_status=ExecutionStatus.SUCCESS,
        finished_at=NOW,
    )


def test_rollback_child_cannot_take_original_only_status(db):
    incident = _finops_incident(db)
    original = _execution(db, incident)
    child = _execution(
        db, incident,
        runbook_id=RunbookId.RUNBOOK_EC2_REVERT_SIZE,
        trigger_source=TriggerSource.AUTO_ON_FAILURE,
        parent_execution_id=original.execution_id,
    )
    with pytest.raises(IntegrityError, match="ck_action_executions_rollback_child_status"):
        with db.begin_nested():
            db.execute(
                models.ActionExecution.__table__.update()
                .where(models.ActionExecution.execution_id == child.execution_id)
                .values(status=ExecutionStatus.ROLLED_BACK)
            )


def test_step_lifecycle_and_effect_constraint(db):
    incident = _finops_incident(db)
    execution = _execution(db, incident)
    exec_repo.add_step(
        db,
        ExecutionStepResult(
            sequence=1, affected_arn="arn:x", step_type="modify",
            aws_operation="ModifyInstanceAttribute",
            status=ExecutionStepStatus.IN_PROGRESS, occurred_at=NOW,
        ),
        execution_id=execution.execution_id,
    )
    assert exec_repo.update_step_result(
        db,
        ExecutionStepResult(
            sequence=1, affected_arn="arn:x", step_type="modify",
            aws_operation="ModifyInstanceAttribute",
            status=ExecutionStepStatus.SUCCESS, effect=ExecutionEffect.APPLIED,
            aws_request_id="req-1", occurred_at=NOW,
        ),
        execution_id=execution.execution_id,
    )
    # SUCCESS + effect NULL은 DB가 거절한다(계약 status↔effect 짝)
    with pytest.raises(IntegrityError, match="ck_execution_steps_effect_matches_status"):
        with db.begin_nested():
            db.add(
                models.ExecutionStep(
                    execution_id=execution.execution_id, sequence=2,
                    affected_arn="arn:x", step_type="modify",
                    aws_operation="op", status=ExecutionStepStatus.SUCCESS,
                    effect=None, occurred_at=NOW,
                )
            )
            db.flush()


def test_backup_record_binds_once(db):
    incident = _finops_incident(db)
    execution = _execution(db, incident)
    backup = exec_repo.create_backup_record(
        db, execution_id=execution.execution_id, target_arn=incident.subject_arn,
        backup_type="SAVE_INSTANCE_SPEC_JSON", payload={"instance_type": "t3.large"},
    )
    assert exec_repo.bind_backup_record(db, execution.execution_id, backup.backup_record_id)
    assert not exec_repo.bind_backup_record(
        db, execution.execution_id, backup.backup_record_id
    )


# --- Guardrail 저장 -------------------------------------------------------------


def test_guardrail_requires_exactly_one_reference(db):
    incident = _finops_incident(db)
    cand_g = str(uuid.uuid4())
    incidents_repo.add_candidate(db, _candidate(incident, cid=cand_g))
    result = GuardrailValidationResult.model_validate(
        {
            "result": GuardrailDecision.PASS,
            "steps": [
                {"step": "SCHEMA_CHECK", "result": "PASS"},
                {"step": "ACTION_WHITELIST", "result": "PASS"},
                {"step": "ARN_MATCH", "result": "PASS"},
                {"step": "AWS_DRY_RUN", "result": "PASS",
                 "verification_summary": "DryRun=True 검증"},
            ],
            "validated_at": NOW,
        }
    )
    saved = guard_repo.add_evaluation(
        db, validation_context=GuardrailValidationContext.AI_CANDIDATE,
        result=result, candidate_id=cand_g,
    )
    assert guard_repo.latest_for_candidate(db, cand_g).guardrail_evaluation_id == (
        saved.guardrail_evaluation_id
    )
    with pytest.raises(IntegrityError, match="ck_guardrail_evaluations_exactly_one"):
        with db.begin_nested():
            guard_repo.add_evaluation(
                db, validation_context=GuardrailValidationContext.AI_CANDIDATE,
                result=result, candidate_id=None, execution_id=None,
            )
