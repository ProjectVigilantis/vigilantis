"""High 선차단·Medium DB 기한 감시 (#447).

예약·경합은 workflows, AWS 변경·확인은 기존 dispatcher와 #443/#444가 소유한다.
선행 실행 쌍이 없으면 승인 대기를 소비하거나 격리 성공으로 기록하지 않는다.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

import dispatcher
import workflows
from ai import guardrails
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from config import get_settings
from db import models
from db.repositories import assets as assets_repo
from db.repositories import executions as executions_repo
from db.repositories import incidents as incidents_repo
from db.session import get_session_factory
from isolation_timing import begin_trace, current_trace
from realtime import incident_event
from schemas.api.actions import ExecutionStatus
from schemas.api.incidents import (
    IncidentCategory,
    IncidentStatus,
    ResponseMode,
    RiskLevel,
)
from schemas.api.ws import WsEvent, WsEventType
from schemas.guardrails import (
    GuardrailDecision,
    GuardrailValidationContext,
    GuardrailValidationRequest,
)
from schemas.runbooks import RunbookId, TriggerSource
from services.aws import executor
from sqlalchemy.orm import Session, sessionmaker

logger = logging.getLogger("vigilantis.soar")
JOB_ID = "agent_wait_timeout"
Publish = Callable[[WsEvent], None]


def prepare_isolation(db: Session, incident_id: str, trigger_source: TriggerSource):
    """#452 공급 계약을 사용해 4단계를 검증한다. AWS 호출 전에 읽기 트랜잭션을 닫는다.

    공급자·runner는 #452가 소유한다. 아직 미등록이면 호출부의 준비 관문에서 멈추며
    임시 공급 값·가짜 백업·대체 executor를 만들지 않는다.
    """
    incident = incidents_repo.get_incident(db, incident_id)
    if incident is None or incident.category is not IncidentCategory.SECOPS:
        db.rollback()
        return None
    if trigger_source is TriggerSource.PRE_MITIGATION_0_5S:
        eligible = (
            incident.initial_risk_level is RiskLevel.HIGH
            and incident.response_mode is ResponseMode.PRE_MITIGATION_0_5S
            and incident.status is IncidentStatus.ANALYZING
        )
    elif trigger_source is TriggerSource.TIMEOUT_ISOLATION_1M:
        eligible = (
            incident.initial_risk_level is RiskLevel.MEDIUM
            and incident.response_mode is ResponseMode.AGENT_WAIT
            and incident.status is IncidentStatus.AWAITING_APPROVAL
            and incident.response_deadline_at is not None
            and incident.response_deadline_at <= datetime.now(UTC)
        )
    else:
        raise ValueError("자동 격리 출처가 아닙니다")
    if not eligible:
        db.rollback()
        return None
    # 테넌트가 자동 조치를 껐으면 가드레일(④ = AWS Dry-Run)을 부르기 전에 멈춘다(T-6 · #514).
    # 사이에 꺼지는 경우는 접수 관문(reserve_auto_isolation)이 잠근 뒤 같은 함수로 다시 본다
    tenant = None if incident.tenant_id is None else db.get(models.Tenant, incident.tenant_id)
    blocked = workflows.automatic_trigger_block(
        tenant, trigger_source=trigger_source, response_deadline_at=incident.response_deadline_at,
    )
    if blocked is not None:
        logger.info("automatic_isolation_disabled_for_tenant", extra={
            "incident_id": incident_id, "tenant_id": incident.tenant_id,
            "trigger_source": trigger_source.value, "reason": blocked.value,
        })
        db.rollback()
        return None
    target_arn = incident.subject_arn
    target = executor.parse_arn(target_arn)
    if target is None or target.resource_type != "instance":
        logger.warning("automatic_isolation_target_unsupported", extra={
            "incident_id": incident_id, "reason": "EC2 instance required",
        })
        db.rollback()
        return None
    evidence_ids = [row.evidence_id for row in incidents_repo.list_evidence(db, incident_id)]
    managed = assets_repo.get_asset_by_arn(db, target_arn) is not None
    supplied = workflows.resolve_isolation_parameters(db, target_arn)
    db.rollback()
    if not supplied.resolved:
        logger.warning("automatic_isolation_parameters_unresolved", extra={
            "incident_id": incident_id, "reason_code": supplied.reason_code.value,
        })
        return None
    execution_id = str(uuid.uuid4())
    payload = {
        "runbook_id": RunbookId.RUNBOOK_EC2_ISOLATE.value,
        "target_arn": target_arn,
        "parameters": {
            "instance_id": target.resource_id,
            **supplied.as_lookups(),
            "evidence_id": evidence_ids[0] if evidence_ids else "",
        },
        "evidence_ids": evidence_ids,
    }
    guarded = guardrails.run_guardrail_validation(
        GuardrailValidationRequest(
            validation_context=GuardrailValidationContext.AUTO_ISOLATION,
            execution_id=execution_id,
            command_payload=payload,
        ),
        is_managed_arn=lambda arn: managed and arn == target_arn,
        precheck=lambda command: executor.precheck(
            command.runbook_id, command.target_arn, command.parameters,
        ),
    )
    if guarded.result.result is not GuardrailDecision.PASS:
        logger.warning("automatic_isolation_guardrail_rejected", extra={
            "incident_id": incident_id, "failed_step": guarded.result.failed_step.value,
        })
        return execution_id, None, guarded.result
    trace = current_trace()
    if trace is not None:
        trace.mark("guardrail_passed")
    return execution_id, guarded.command.model_dump(mode="json"), guarded.result


def trigger_isolation(
    db: Session,
    incident_id: str,
    *,
    trigger_source: TriggerSource,
    publish: Publish | None = None,
) -> str:
    with dispatcher.automatic_isolation_scope():
        return _trigger_isolation(
            db, incident_id, trigger_source=trigger_source, publish=publish,
        )


def _trigger_isolation(
    db: Session,
    incident_id: str,
    *,
    trigger_source: TriggerSource,
    publish: Publish | None = None,
) -> str:
    """자동 발동 1건. 반환값은 접수/확인/차단을 구분하며 격리 성공을 추정하지 않는다."""
    trace = current_trace()
    if trace is not None:
        trace.trigger_source = trigger_source.value
        trace.bind(incident_id=incident_id)
    if not dispatcher.supports_isolation():
        logger.warning("automatic_isolation_prerequisite_blocked", extra={
            "incident_id": incident_id,
            "trigger_source": trigger_source.value,
            "reason": "EC2_ISOLATE runner/judge and SG/TG supply require #443/#444",
        })
        if trace is not None:
            trace.finish("prerequisite_blocked")
        db.rollback()
        return "prerequisite_blocked"

    # 재전달·분석 스캔은 같은 실행을 쓴다. 적용 후 Dry-Run을 다시 수행해
    # TG 미등록으로 거절하고 진행 중 실행의 회수를 막지 않는다.
    existing = next((
        row for row in executions_repo.list_by_incident(db, incident_id)
        if row.trigger_source is trigger_source
        and row.runbook_id is RunbookId.RUNBOOK_EC2_ISOLATE
    ), None)
    existing_id = existing.execution_id if existing is not None else None
    db.rollback()
    if existing_id is not None:
        dispatcher.dispatch_now(db, existing_id, publish)
        # 기존 실행의 재관측을 새 접수부터의 응답 시간으로 세지 않는다.
        return "skipped"
    prepared = prepare_isolation(db, incident_id, trigger_source)
    if prepared is None:
        if trace is not None:
            trace.finish("failed")
        return "skipped"
    execution_id, command, result = prepared
    reservation = workflows.reserve_auto_isolation(
        db, incident_id, trigger_source=trigger_source, now=datetime.now(UTC),
        execution_id=execution_id, validated_command=command, guardrail_result=result,
    )
    if reservation.execution_id is None:
        if reservation.skipped_reason is workflows.AutoIsolationSkipReason.ALREADY_RESERVED:
            # 분석 쪽이 준비 중인 사이 접수 쪽이 이겼을 수도 있다. 같은 실행 구간을
            # 통과한 뒤 분석 입력을 조립해야 선차단의 중간 상태를 최종 결과로 읽지 않는다.
            key = workflows.auto_isolation_deduplication_key(trigger_source, incident_id)
            winner = executions_repo.get_by_deduplication_key(db, key)
            winner_id = winner.execution_id
            db.rollback()
            dispatcher.dispatch_now(db, winner_id, publish)
        return "skipped"
    if trace is not None:
        trace.bind(
            execution_id=reservation.execution_id,
            response_deadline_at=reservation.response_deadline_at,
        )
        if reservation.created:
            trace.mark("execution_reserved")
    if reservation.created and publish is not None:
        try:
            publish(incident_event(
                WsEventType.INCIDENT_UPDATED, incident_id=incident_id,
                occurred_at=reservation.occurred_at,
            ))
        except Exception:  # 알림 실패로 확정된 예약을 잃지 않는다
            logger.exception("automatic_isolation_publish_failed", extra={
                "incident_id": incident_id,
            })
    if reservation.execution_status is ExecutionStatus.IN_PROGRESS:
        dispatcher.dispatch_now(db, reservation.execution_id, publish)
    execution = executions_repo.get_execution(db, reservation.execution_id)
    status = execution.status
    db.rollback()
    if status is ExecutionStatus.SUCCESS:
        if trace is not None:
            trace.finish("success", confirmed=True)
        return "success"
    result = "failed" if status is ExecutionStatus.FAILED else "unconfirmed"
    if trace is not None:
        trace.finish(result)
    return result


@dataclass
class TimeoutReport:
    scanned: int = 0
    success: int = 0
    failed: int = 0
    unconfirmed: int = 0
    skipped: int = 0
    prerequisite_blocked: int = 0
    errored: int = 0


def run_timeout_cycle(
    session_factory: sessionmaker[Session], publish: Publish | None = None,
) -> TimeoutReport:
    """재시작에도 같은 DB 기한을 읽는다. 조회와 발동 사이의 변경은 예약 관문이 재확인한다."""
    report = TimeoutReport()
    with session_factory() as db:
        pending = incidents_repo.list_expired_agent_waits(
            db, now=datetime.now(UTC),
        )
        db.rollback()
        report.scanned = len(pending)
        for incident_id in pending:
            try:
                with begin_trace(TriggerSource.TIMEOUT_ISOLATION_1M.value):
                    result = trigger_isolation(
                        db, incident_id,
                        trigger_source=TriggerSource.TIMEOUT_ISOLATION_1M,
                        publish=publish,
                    )
                setattr(report, result, getattr(report, result) + 1)
            except Exception:  # 한 건의 장애가 다른 기한을 누락시키지 않는다
                db.rollback()
                report.errored += 1
                logger.exception("automatic_isolation_timeout_failed", extra={
                    "incident_id": incident_id,
                })
    logger.info("automatic_isolation_timeout_cycle", extra=vars(report))
    return report


def start_timeout_watcher(publish: Publish | None = None) -> AsyncIOScheduler | None:
    settings = get_settings()
    if not settings.DISPATCH_ENABLED:
        return None
    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(
        lambda: run_timeout_cycle(get_session_factory(), publish),
        trigger=IntervalTrigger(seconds=settings.AGENT_WAIT_POLL_SECONDS),
        id=JOB_ID,
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )
    scheduler.start()
    logger.info("automatic_isolation_watcher_started", extra={
        "poll_seconds": settings.AGENT_WAIT_POLL_SECONDS,
    })
    return scheduler
