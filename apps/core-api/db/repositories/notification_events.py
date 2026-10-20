"""채널 중립 사건/outbox 저장. commit·예외 억제·사건 수정·발송은 하지 않는다.

PR2 조회 계약: get(event_id), list_by_incident(incident_id), NotificationEvent.
PR2의 채널 상태는 별도 (event_id, channel) 행이다. 이 목록을 읽었다는 사실은
발송 claim/완료가 아니며, 조회 cursor를 발송 복구 근거로 사용하지 않는다.
"""

from datetime import datetime

from schemas.api.actions import ExecutionStatus
from schemas.api.incidents import RiskLevel
from schemas.runbooks import RunbookId, TriggerSource
from sqlalchemy import select
from sqlalchemy.orm import Session

from db import models


def add(
    db: Session,
    *,
    incident_id: str,
    execution_id: str | None,
    related_execution_id: str | None,
    kind: str,
    phase: str,
    outcome: str | None,
    target_arn: str,
    aws_account_id: str | None,
    region: str | None,
    risk_level: RiskLevel | None,
    trigger_source: TriggerSource | None,
    runbook_id: RunbookId | None,
    execution_status: ExecutionStatus | None,
    reason_code: str | None,
    occurred_at: datetime,
) -> models.NotificationEvent:
    row = models.NotificationEvent(
        incident_id=incident_id,
        execution_id=execution_id,
        related_execution_id=related_execution_id,
        kind=kind,
        phase=phase,
        outcome=outcome,
        target_arn=target_arn,
        aws_account_id=aws_account_id,
        region=region,
        risk_level=risk_level,
        trigger_source=trigger_source,
        runbook_id=runbook_id,
        execution_status=execution_status,
        reason_code=reason_code,
        occurred_at=occurred_at,
    )
    db.add(row)
    db.flush()
    return row


def get(db: Session, event_id: str) -> models.NotificationEvent | None:
    return db.get(models.NotificationEvent, event_id)


def list_by_incident(db: Session, incident_id: str) -> list[models.NotificationEvent]:
    return list(db.scalars(
        select(models.NotificationEvent)
        .where(models.NotificationEvent.incident_id == incident_id)
        .order_by(models.NotificationEvent.occurred_at, models.NotificationEvent.event_id)
    ))
