# ==============================================================================
# [파일 설명]
# 실행·단계·백업 저장소 — ActionExecution·ExecutionStep·BackupRecord. (Issue #60)
#
#   - idempotency_key·deduplication_key 중복은 유니크 제약이 flush 시점에 거절한다
#     — 호출부(Workflow)가 IntegrityError를 중복 요청 응답으로 해석한다.
#   - 실행 상태 6종·Non-terminal 집합은 공개 계약(api/actions.py)과 #55 파생
#     집합(schemas.executions)이 원천이다.
# ==============================================================================

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from schemas.api.actions import ExecutionStatus
from schemas.executions import EXECUTION_NON_TERMINAL_STATUSES, ExecutionStepResult
from schemas.runbooks import RunbookId, TriggerSource

from .. import mappers, models

# --- ActionExecution -----------------------------------------------------------


def create_execution(
    db: Session,
    *,
    incident_id: str,
    runbook_id: RunbookId,
    target_arn: str,
    trigger_source: TriggerSource,
    candidate_id: Optional[str] = None,
    idempotency_key: Optional[str] = None,
    deduplication_key: Optional[str] = None,
    parent_execution_id: Optional[str] = None,
    backup_record_id: Optional[str] = None,
    validated_command: Optional[dict] = None,
) -> models.ActionExecution:
    row = models.ActionExecution(
        incident_id=incident_id,
        runbook_id=runbook_id,
        target_arn=target_arn,
        trigger_source=trigger_source,
        candidate_id=candidate_id,
        idempotency_key=idempotency_key,
        deduplication_key=deduplication_key,
        parent_execution_id=parent_execution_id,
        backup_record_id=backup_record_id,
        validated_command=validated_command,
    )
    db.add(row)
    db.flush()
    return row


def get_execution(db: Session, execution_id: str) -> Optional[models.ActionExecution]:
    return db.execute(
        select(models.ActionExecution).where(
            models.ActionExecution.execution_id == execution_id
        )
    ).scalar_one_or_none()


def get_by_idempotency_key(
    db: Session, idempotency_key: str
) -> Optional[models.ActionExecution]:
    return db.execute(
        select(models.ActionExecution).where(
            models.ActionExecution.idempotency_key == idempotency_key
        )
    ).scalar_one_or_none()


def list_by_incident(db: Session, incident_id: str) -> list[models.ActionExecution]:
    return list(
        db.execute(
            select(models.ActionExecution)
            .where(models.ActionExecution.incident_id == incident_id)
            .order_by(models.ActionExecution.created_at)
        ).scalars()
    )


def lock_execution(db: Session, execution_id: str) -> Optional[models.ActionExecution]:
    """행 잠금 후 최신 값으로 다시 읽는다 — 잠그기 전에 읽은 값은 이미 낡았을 수 있다.
    관제자 복구 접수가 원본 실행을 선점하는 관문이다 (Issue #126)."""
    return db.execute(
        select(models.ActionExecution)
        .where(models.ActionExecution.execution_id == execution_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()


def list_rollback_children(
    db: Session, parent_execution_id: str
) -> list[models.ActionExecution]:
    """원본을 되돌리려 만든 자식 실행. 1건이라도 있으면 복구는 이미 접수된 것이다."""
    return list(
        db.execute(
            select(models.ActionExecution).where(
                models.ActionExecution.parent_execution_id == parent_execution_id
            )
        ).scalars()
    )


def list_non_terminal(db: Session) -> list[models.ActionExecution]:
    """Dispatcher 회수 스캔 — 부분 인덱스(ix_action_executions_non_terminal) 대상."""
    return list(
        db.execute(
            select(models.ActionExecution).where(
                models.ActionExecution.status.in_(EXECUTION_NON_TERMINAL_STATUSES)
            )
        ).scalars()
    )


def update_execution_status(
    db: Session,
    execution_id: str,
    *,
    expected: ExecutionStatus,
    next_status: ExecutionStatus,
    error_summary: Optional[str] = None,
    finished_at: Optional[datetime] = None,
) -> bool:
    """롤백 자식의 상태 제한(SUCCESS|FAILED)은 DB CheckConstraint가 함께 강제한다."""
    values: dict = {"status": next_status}
    if error_summary is not None:
        values["error_summary"] = error_summary
    if finished_at is not None:
        values["finished_at"] = finished_at
    result = db.execute(
        update(models.ActionExecution)
        .where(
            models.ActionExecution.execution_id == execution_id,
            models.ActionExecution.status == expected,
        )
        .values(**values)
    )
    return result.rowcount == 1


def bind_backup_record(db: Session, execution_id: str, backup_record_id: str) -> bool:
    """백업 결속은 1회만 — 이미 결속된 실행은 갱신하지 않는다(ADR-0004 정책 ③)."""
    result = db.execute(
        update(models.ActionExecution)
        .where(
            models.ActionExecution.execution_id == execution_id,
            models.ActionExecution.backup_record_id.is_(None),
        )
        .values(backup_record_id=backup_record_id)
    )
    return result.rowcount == 1


# --- ExecutionStep -------------------------------------------------------------


def add_step(
    db: Session, contract: ExecutionStepResult, *, execution_id: str
) -> models.ExecutionStep:
    """AWS 호출 전 IN_PROGRESS 단계를 먼저 저장하는 흐름의 저장 지점.
    status↔effect 짝은 DB CheckConstraint가 flush 시점에 강제한다."""
    row = mappers.new_execution_step(contract, execution_id)
    db.add(row)
    db.flush()
    return row


def update_step_result(
    db: Session, contract: ExecutionStepResult, *, execution_id: str
) -> bool:
    """AWS 호출 후 같은 (execution, sequence) 단계에 결과를 갱신한다."""
    result = db.execute(
        update(models.ExecutionStep)
        .where(
            models.ExecutionStep.execution_id == execution_id,
            models.ExecutionStep.sequence == contract.sequence,
        )
        .values(
            status=contract.status,
            effect=contract.effect,
            aws_request_id=contract.aws_request_id,
            result_summary=contract.result_summary,
            error_summary=contract.error_summary,
            occurred_at=contract.occurred_at,
        )
    )
    return result.rowcount == 1


def list_steps(db: Session, execution_id: str) -> list[models.ExecutionStep]:
    return list(
        db.execute(
            select(models.ExecutionStep)
            .where(models.ExecutionStep.execution_id == execution_id)
            .order_by(models.ExecutionStep.sequence)
        ).scalars()
    )


# --- BackupRecord --------------------------------------------------------------


def create_backup_record(
    db: Session,
    *,
    execution_id: str,
    target_arn: str,
    backup_type: str,
    payload: dict,
) -> models.BackupRecord:
    row = models.BackupRecord(
        execution_id=execution_id,
        target_arn=target_arn,
        backup_type=backup_type,
        payload=payload,
    )
    db.add(row)
    db.flush()
    return row


def get_backup_record(
    db: Session, backup_record_id: str
) -> Optional[models.BackupRecord]:
    return db.execute(
        select(models.BackupRecord).where(
            models.BackupRecord.backup_record_id == backup_record_id
        )
    ).scalar_one_or_none()
