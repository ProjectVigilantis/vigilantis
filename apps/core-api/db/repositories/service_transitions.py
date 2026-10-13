# ==============================================================================
# [파일 설명]
# 서비스 전환 저장소 — 불변 계획·작업 일지·관측과 격리 사실(읽기 전용) 조회.
#
#   - commit하지 않는다. 트랜잭션은 facade(service_transitions.py)가 소유한다.
#   - 계획·관측은 넣기만 한다(UPDATE·DELETE 경로 없음). 시도는 실행마다 한 줄로 잇고, 현재 시도는
#     아무도 대체하지 않은 시도로 유도한다(leaf). 일지·관측은 #446 실행 단위이며 시도는 선택 연결이다.
#   - 일지는 PENDING → 결과로 한 번 닫는다. PENDING·UNCERTAIN에는 현물 대조(reconciled_state)를
#     덧붙인다 — EFFECT_NOT_OBSERVED는 뒤의 읽기가 갱신·대체할 수 있고, EFFECT_OBSERVED는 최종이다.
#   - 변경 배제(mutation_claim)는 PostgreSQL 세션 advisory lock을 **별도 autocommit 연결**에
#     쥔다 — 명령 내내 단계별 commit을 넘어 유지되고, DB 트랜잭션을 열어 두지 않으며,
#     프로세스가 죽으면 연결과 함께 풀린다. 죽은 뒤의 불확실은 일지의 PENDING이 막는다.
# ==============================================================================

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

from schemas.api.actions import ExecutionStatus
from schemas.backups import BackupType
from schemas.executions import ASSET_MAY_HAVE_CHANGED_EFFECTS
from schemas.runbooks import RunbookId
from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session

from .. import models

_UNRESOLVED_STATES = ("PENDING", "UNCERTAIN")


def _now() -> datetime:
    return datetime.now(UTC)


# --- 계획 -----------------------------------------------------------------------


def create_transition(
    db: Session,
    *,
    transition_id: str,
    execution_id: str,
    supersedes_transition_id: str | None,
    plan: dict,
) -> models.ServiceTransition:
    """새 시도 1건. 같은 실행의 root·대체 중복은 DB 제약이 flush에서 거절한다."""
    row = models.ServiceTransition(
        transition_id=transition_id,
        execution_id=execution_id,
        supersedes_transition_id=supersedes_transition_id,
        plan=plan,
    )
    db.add(row)
    db.flush()
    return row


def leaf_transition_id(rows: list[models.ServiceTransition]) -> str | None:
    """아무도 대체하지 않은 시도 — 체인이 한 줄이라 하나뿐이다(제약). 시도가 없으면 None."""
    superseded = {row.supersedes_transition_id for row in rows}
    leaves = [row.transition_id for row in rows if row.transition_id not in superseded]
    return leaves[0] if leaves else None


def get_transition(db: Session, transition_id: str) -> models.ServiceTransition | None:
    return db.get(models.ServiceTransition, transition_id)


def list_transitions_for_execution(
    db: Session, execution_id: str
) -> list[models.ServiceTransition]:
    return list(
        db.execute(
            select(models.ServiceTransition)
            .where(models.ServiceTransition.execution_id == execution_id)
            .order_by(models.ServiceTransition.created_at)
        ).scalars()
    )


# --- 작업 일지 -------------------------------------------------------------------


def add_operation(
    db: Session,
    *,
    execution_id: str,
    transition_id: str | None,
    command: str,
    aws_operation: str,
    resource_ref: str,
    request: dict,
) -> models.ServiceTransitionOperation:
    """AWS 호출 **전에** PENDING 1건. 순번은 실행 단위이고 배제(mutation_claim) 아래에서만 매긴다."""
    last = db.execute(
        select(func.max(models.ServiceTransitionOperation.sequence)).where(
            models.ServiceTransitionOperation.execution_id == execution_id
        )
    ).scalar_one()
    row = models.ServiceTransitionOperation(
        execution_id=execution_id,
        transition_id=transition_id,
        sequence=(last or 0) + 1,
        command=command,
        aws_operation=aws_operation,
        resource_ref=resource_ref[:512],
        request=request,
        state="PENDING",
        started_at=_now(),
    )
    db.add(row)
    db.flush()
    return row


def finish_operation(
    db: Session,
    operation_id: str,
    *,
    state: str,
    response: dict | None = None,
    error_code: str | None = None,
    error_summary: str | None = None,
) -> bool:
    """PENDING을 결과로 한 번만 닫는다."""
    result = db.execute(
        update(models.ServiceTransitionOperation)
        .where(
            models.ServiceTransitionOperation.operation_id == operation_id,
            models.ServiceTransitionOperation.state == "PENDING",
        )
        .values(
            state=state,
            finished_at=_now(),
            response=response,
            error_code=error_code,
            error_summary=(error_summary or None) and error_summary[:1024],
        )
    )
    return result.rowcount == 1


def reconcile_operation(
    db: Session, operation_id: str, *, reconciled_state: str
) -> bool:
    """현물 대조를 덧붙인다. 원래 결과(state)는 바꾸지 않는다.

    EFFECT_OBSERVED는 최종이라 다시 바꾸지 않는다. EFFECT_NOT_OBSERVED는 뒤의 읽기가 갱신하거나
    EFFECT_OBSERVED로 대체할 수 있다 — 지금 안 보인다는 것은 요청이 거절됐다는 증거가 아니다.
    """
    result = db.execute(
        update(models.ServiceTransitionOperation)
        .where(
            models.ServiceTransitionOperation.operation_id == operation_id,
            models.ServiceTransitionOperation.state.in_(_UNRESOLVED_STATES),
            or_(
                models.ServiceTransitionOperation.reconciled_state.is_(None),
                models.ServiceTransitionOperation.reconciled_state
                == "EFFECT_NOT_OBSERVED",
            ),
        )
        .values(reconciled_state=reconciled_state, reconciled_at=_now())
    )
    return result.rowcount == 1


def list_operations(
    db: Session, *, execution_ids: Iterable[str]
) -> list[models.ServiceTransitionOperation]:
    return list(
        db.execute(
            select(models.ServiceTransitionOperation)
            .where(
                models.ServiceTransitionOperation.execution_id.in_(list(execution_ids))
            )
            .order_by(
                models.ServiceTransitionOperation.execution_id,
                models.ServiceTransitionOperation.sequence,
            )
        ).scalars()
    )


def is_unresolved(row: models.ServiceTransitionOperation) -> bool:
    """결과를 모르는 변경 — 의도한 효과가 확인되기(EFFECT_OBSERVED) 전까지 관문을 막는다."""
    return row.state in _UNRESOLVED_STATES and row.reconciled_state != "EFFECT_OBSERVED"


def is_effective(row: models.ServiceTransitionOperation) -> bool:
    """의도한 효과가 받아들여졌거나(APPLIED) 현물에서 확인됐다(EFFECT_OBSERVED)."""
    return row.state == "APPLIED" or row.reconciled_state == "EFFECT_OBSERVED"


# --- 관측 -----------------------------------------------------------------------


def add_observation(
    db: Session,
    *,
    execution_id: str,
    transition_id: str | None,
    kind: str,
    verdict: str,
    observed_at: datetime,
    evidence: dict,
) -> models.ServiceTransitionObservation:
    row = models.ServiceTransitionObservation(
        execution_id=execution_id,
        transition_id=transition_id,
        kind=kind,
        verdict=verdict,
        observed_at=observed_at,
        evidence=evidence,
    )
    db.add(row)
    db.flush()
    return row


def latest_observations(
    db: Session, execution_id: str, *, limit: int = 10
) -> list[models.ServiceTransitionObservation]:
    return list(
        db.execute(
            select(models.ServiceTransitionObservation)
            .where(models.ServiceTransitionObservation.execution_id == execution_id)
            .order_by(models.ServiceTransitionObservation.observed_at.desc())
            .limit(limit)
        ).scalars()
    )


# --- 격리 사실(읽기 전용) ----------------------------------------------------------


def isolation_facts(
    db: Session,
    *,
    target_arns: Iterable[str],
    autoscaling_execution_id: str | None = None,
    restore_target_group_arns: Iterable[str] = (),
) -> list[dict[str, Any]]:
    """EC2_ISOLATE 원본 실행과 그 해제(롤백 자식)의 DB 사실. 자산을 바꾸지 않는다.

    대상은 지정 ARN의 격리 + (주면) 격리 백업의 scale-in 보호 관측이 그 #446 실행을 가리킨 격리
    + (주면) 불변 SG/TG 백업의 복원 TG(`target_group_arn`)가 지정 TG인 격리다 — 이미 종료된 ASG
    멤버의 격리도 빠지지 않는다. active는 "격리가 걸려 있거나 걸렸을 수 있다"다:
    ROLLED_BACK만 해제 완료, FAILED는 자산이 만져졌을 수 있는 단계가 있을 때만 active다.
    """
    arns = sorted(set(target_arns))
    restore_tgs = sorted(set(restore_target_group_arns))
    conditions = [models.ActionExecution.target_arn.in_(arns)] if arns else []
    if restore_tgs:
        conditions.append(
            models.ActionExecution.backup_record_id.in_(
                select(models.BackupRecord.backup_record_id).where(
                    models.BackupRecord.backup_type
                    == BackupType.SAVE_CURRENT_SG_AND_TG_MAPPING.value,
                    models.BackupRecord.payload["target_group_arn"].astext.in_(
                        restore_tgs
                    ),
                )
            )
        )
    if autoscaling_execution_id is not None:
        conditions.append(
            models.ActionExecution.backup_record_id.in_(
                select(models.BackupRecord.backup_record_id).where(
                    models.BackupRecord.backup_type
                    == BackupType.SAVE_CURRENT_SG_AND_TG_MAPPING.value,
                    models.BackupRecord.payload["scale_in_protection"][
                        "autoscaling_execution_id"
                    ].astext
                    == autoscaling_execution_id,
                )
            )
        )
    if not conditions:
        return []
    isolations = list(
        db.execute(
            select(models.ActionExecution)
            .where(
                models.ActionExecution.runbook_id == RunbookId.RUNBOOK_EC2_ISOLATE,
                models.ActionExecution.parent_execution_id.is_(None),
                or_(*conditions),
            )
            .order_by(models.ActionExecution.created_at)
        ).scalars()
    )
    if not isolations:
        return []
    ids = [row.execution_id for row in isolations]
    effects: dict[str, set] = {}
    for execution_id, effect in db.execute(
        select(models.ExecutionStep.execution_id, models.ExecutionStep.effect).where(
            models.ExecutionStep.execution_id.in_(ids)
        )
    ):
        effects.setdefault(execution_id, set()).add(effect)
    children: dict[str, list[models.ActionExecution]] = {}
    for child in db.execute(
        select(models.ActionExecution)
        .where(models.ActionExecution.parent_execution_id.in_(ids))
        .order_by(models.ActionExecution.created_at)
    ).scalars():
        children.setdefault(child.parent_execution_id, []).append(child)
    record_ids = [row.backup_record_id for row in isolations if row.backup_record_id]
    payloads = (
        dict(
            db.execute(
                select(
                    models.BackupRecord.backup_record_id, models.BackupRecord.payload
                ).where(models.BackupRecord.backup_record_id.in_(record_ids))
            ).all()
        )
        if record_ids
        else {}
    )

    facts = []
    for row in isolations:
        touched = bool(
            effects.get(row.execution_id, set()) & ASSET_MAY_HAVE_CHANGED_EFFECTS
        )
        if row.status is ExecutionStatus.ROLLED_BACK:
            active = False
        elif row.status is ExecutionStatus.FAILED:
            active = touched
        else:
            active = True
        facts.append(
            {
                "execution_id": row.execution_id,
                "target_arn": row.target_arn,
                "status": row.status.value,
                "active": active,
                "backup_record_id": row.backup_record_id,
                # 불변 백업의 복원 TG — 읽지 못하면 None(정리 관문은 이 값이 정리 대상 TG일 때만 막는다)
                "restore_target_group_arn": (
                    payloads[row.backup_record_id].get("target_group_arn")
                    if isinstance(payloads.get(row.backup_record_id), dict)
                    else None
                ),
                "scale_in_protection_result": row.scale_in_protection_result,
                "unisolations": [
                    {
                        "execution_id": child.execution_id,
                        "runbook_id": child.runbook_id.value,
                        "status": child.status.value,
                        "scale_in_protection_result": child.scale_in_protection_result,
                    }
                    for child in children.get(row.execution_id, [])
                ],
            }
        )
    return facts


# --- 변경 배제 -------------------------------------------------------------------


def transitions_on_listener(
    db: Session, listener_arn: str
) -> list[models.ServiceTransition]:
    """같은 운영 리스너에 묶인 전환 — 계획 JSON의 원래 경로 리스너로 찾는다."""
    return list(
        db.execute(
            select(models.ServiceTransition).where(
                models.ServiceTransition.plan["original_route"]["listener_arn"].astext
                == listener_arn
            )
        ).scalars()
    )


def _lock_key(identity: str) -> int:
    return int.from_bytes(
        hashlib.sha256(identity.encode()).digest()[:8], "big", signed=True
    )


@contextmanager
def mutation_claim(db: Session, identities: Iterable[str]) -> Iterator[bool]:
    """공유 AWS 좌표(#446 실행 = ASG, 운영 리스너) 단위로 변경을 한 프로세스에 묶는다. 다 잡았으면 True.

    기다리지 않는다(try) — 하나라도 못 잡으면 잡은 것을 풀고 False. 이 도구 밖의 AWS 조작과는
    원자적이지 않다 — 그 창은 변경 전후 대조가 드러낼 뿐이다.
    """
    bind = db.get_bind()
    engine = getattr(bind, "engine", bind)
    connection = engine.connect().execution_options(isolation_level="AUTOCOMMIT")
    held: list[int] = []
    try:
        for key in sorted({_lock_key(identity) for identity in identities}):
            if not connection.execute(
                select(func.pg_try_advisory_lock(key))
            ).scalar_one():
                break
            held.append(key)
        else:
            yield True
            return
        yield False
    finally:
        for key in held:
            connection.execute(select(func.pg_advisory_unlock(key)))
        connection.close()
