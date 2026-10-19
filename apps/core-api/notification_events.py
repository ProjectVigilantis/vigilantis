"""#493 PR1: 업무 사건을 채널 중립 outbox로 기록한다. 전송·commit은 하지 않는다.

PR2는 event_id를 참조하는 (event_id, channel)별 발송 상태를 별도로 소유한다.
사건 사실을 전송 payload/status로 덮어쓰거나, account_id를 고객 ID로 해석하지 않는다.
"""

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from db import models
from db.repositories import assets as assets_repo
from db.repositories import notification_events as events_repo
from schemas.api.actions import ExecutionStatus
from schemas.runbooks import RunbookId, TriggerSource
from sqlalchemy.orm import Session


class EventKind(str, Enum):
    HIGH_INCIDENT_CREATED = "HIGH_INCIDENT_CREATED"
    AUTO_ISOLATION = "AUTO_ISOLATION"
    AUTO_ROLLBACK = "AUTO_ROLLBACK"


class EventPhase(str, Enum):
    CREATED = "CREATED"
    REQUESTED = "REQUESTED"
    RESULT = "RESULT"


class EventOutcome(str, Enum):
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    REJECTED = "REJECTED"
    UNVERIFIED = "UNVERIFIED"


@dataclass(frozen=True)
class ExecutionEvent:
    kind: EventKind
    phase: EventPhase
    outcome: EventOutcome | None
    trigger_source: TriggerSource
    # 자동 원복의 요청·결과는 원본 실행을 같은 업무 대상으로 가리킨다.
    use_origin: bool = False


def classify_execution_transition(
    *,
    runbook_id: RunbookId,
    trigger_source: TriggerSource,
    parent_execution_id: str | None,
    previous_status: ExecutionStatus,
    next_status: ExecutionStatus,
    verification_held: bool,
    has_execution_steps: bool,
    origin_settled: bool,
) -> ExecutionEvent | None:
    """상태 문자열만으로 자동 출처나 확인 성공을 추론하지 않는 기록 규칙."""
    if previous_status is next_status:
        return None
    # 실행 전 대조 실패는 시작하지 못한 실패다. 실행 단계가 남은 뒤 결과를
    # 확인하지 못한 경우에만 UNVERIFIED로 기록한다(_held_outcome과 같은 경계).
    result_unverified = verification_held and has_execution_steps
    if parent_execution_id is not None:
        if trigger_source is not TriggerSource.AUTO_ON_FAILURE or not origin_settled:
            return None
        outcome = (
            EventOutcome.UNVERIFIED if result_unverified
            else EventOutcome.SUCCESS if next_status is ExecutionStatus.SUCCESS
            else EventOutcome.FAILED
        )
        return ExecutionEvent(
            EventKind.AUTO_ROLLBACK, EventPhase.RESULT, outcome,
            TriggerSource.AUTO_ON_FAILURE, use_origin=True,
        )
    if next_status is ExecutionStatus.ROLLBACK_INITIATED:
        return ExecutionEvent(
            EventKind.AUTO_ROLLBACK, EventPhase.REQUESTED, None,
            TriggerSource.AUTO_ON_FAILURE,
        )
    if (
        previous_status is ExecutionStatus.ROLLBACK_INITIATED
        and next_status is ExecutionStatus.ROLLBACK_FAILED
    ):
        return ExecutionEvent(
            EventKind.AUTO_ROLLBACK, EventPhase.RESULT, EventOutcome.FAILED,
            TriggerSource.AUTO_ON_FAILURE,
        )
    if (
        runbook_id is RunbookId.RUNBOOK_EC2_ISOLATE
        and trigger_source in {
            TriggerSource.PRE_MITIGATION_0_5S, TriggerSource.TIMEOUT_ISOLATION_1M,
        }
        and next_status in {
            ExecutionStatus.SUCCESS, ExecutionStatus.FAILED, ExecutionStatus.UNVERIFIED,
        }
    ):
        return ExecutionEvent(
            EventKind.AUTO_ISOLATION, EventPhase.RESULT,
            EventOutcome.UNVERIFIED if result_unverified else EventOutcome(next_status.value),
            trigger_source,
        )
    return None


def record(
    db: Session,
    *,
    incident: models.Incident,
    kind: EventKind,
    phase: EventPhase,
    occurred_at: datetime,
    outcome: EventOutcome | None = None,
    execution: models.ActionExecution | None = None,
    related_execution_id: str | None = None,
    trigger_source: TriggerSource | None = None,
    execution_status: ExecutionStatus | None = None,
    reason_code: str | None = None,
) -> models.NotificationEvent:
    """잠금·생성 관문을 지난 호출부가 기존 commit 전에 호출한다. 실패는 전파한다.

    계정은 수집 자산의 저장값만 사용한다. 미수집/미식별은 NULL이며 고객 귀속이나
    발송 가능을 뜻하지 않는다. 원본 로그·AI 요약·상세 예외·수신처는 복사하지 않는다.
    execution_status는 execution_id가 가리키는 실행의 상태다. 자동 원복 결과는
    원본 상태를 저장하고, 결과를 낸 자식은 related_execution_id로 구분한다.
    """
    target_arn = execution.target_arn if execution is not None else incident.subject_arn
    asset = assets_repo.get_asset_by_arn(db, target_arn)
    account_id = asset.account_id if asset is not None else None
    # AWS 계정 계약은 12자리다. 실패 수집의 "unknown" 등은 라우팅 근거가 아니다.
    if account_id is not None and (
        len(account_id) != 12 or not account_id.isascii() or not account_id.isdecimal()
    ):
        account_id = None
    return events_repo.add(
        db,
        incident_id=incident.incident_id,
        execution_id=execution.execution_id if execution is not None else None,
        related_execution_id=related_execution_id,
        kind=kind.value,
        phase=phase.value,
        outcome=outcome.value if outcome is not None else None,
        target_arn=target_arn,
        aws_account_id=account_id,
        region=asset.region if asset is not None else None,
        risk_level=incident.initial_risk_level,
        trigger_source=trigger_source,
        runbook_id=execution.runbook_id if execution is not None else None,
        execution_status=execution_status,
        reason_code=reason_code,
        occurred_at=occurred_at,
    )
