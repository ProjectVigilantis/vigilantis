# ==============================================================================
# [파일 설명]
# 감사 기록 저장소 — AuditEvent(조치 승인 · 종료 판단 · T-2 · #544). 계약: schemas.api.audit
#
#   - 쓰는 자리는 Workflow 의 실행 행 생성(관제자 승인 · 자동 격리 · 자동 원복)과 종료 판단 저장뿐이다.
#     그 쓰기와 **같은 트랜잭션**에서 부른다 — commit 은 호출부가 한다. 기록 없이 조치만 남지 않는다.
#   - 테넌트 칸은 인시던트에서 옮긴다. 시각은 원래 행의 시각을 그대로 쓴다(실행 created_at ·
#     인시던트 resolved_at) — 상세 화면의 시각과 기록의 시각이 갈리지 않게.
#   - 행위자 · 발동 원천 · 종료 판단의 짝은 DB CHECK(models.AuditEvent)가 최후 관문이다.
#   - 조회(list_events)는 범위 · 질의 · 쪽만 건다. 이메일을 보일지는 보는 사람이 정하므로 라우터 몫이다.
# ==============================================================================

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from schemas.api.audit import AuditActorKind, AuditEventAction
from schemas.api.auth import AccountType
from schemas.api.incidents import ResolutionJudgement
from schemas.runbooks import RunbookId
from sqlalchemy import literal, select, tuple_
from sqlalchemy.orm import Session

from .. import models

_PERSON_KIND = {
    AccountType.PLATFORM: AuditActorKind.PLATFORM_USER,
    AccountType.TENANT: AuditActorKind.TENANT_USER,
}


@dataclass(frozen=True)
class Actor:
    """누가 · 무엇이 시작했나. 사람은 user_id 가 있고 SYSTEM 은 없다."""

    kind: AuditActorKind
    user_id: str | None

    def __post_init__(self) -> None:
        if (self.kind is AuditActorKind.SYSTEM) != (self.user_id is None):
            raise ValueError("SYSTEM 만 사용자가 없고, 사람은 사용자가 있어야 합니다")


SYSTEM = Actor(AuditActorKind.SYSTEM, None)


def person(user_id: str, account_type: AccountType) -> Actor:
    """요청자(auth.CurrentUser)의 신원을 행위자로. 소속 미지정(UNASSIGNED)은 세션 판정을 통과하지 않는다."""
    kind = _PERSON_KIND.get(account_type)
    if kind is None:
        raise ValueError(f"감사 기록의 행위자가 될 수 없는 계정 종류입니다: {account_type}")
    return Actor(kind, user_id)


def _incident_tenant(db: Session, incident_id: str) -> str | None:
    return db.scalar(
        select(models.Incident.tenant_id).where(models.Incident.incident_id == incident_id)
    )


def _add(db: Session, row: models.AuditEvent) -> models.AuditEvent:
    db.add(row)
    db.flush()
    return row


def record_execution_started(
    db: Session, execution: models.ActionExecution, actor: Actor
) -> models.AuditEvent:
    """실행 행이 새로 생겼다 — 그 행을 만든 트랜잭션 안에서 부른다."""
    return _add(
        db,
        models.AuditEvent(
            action=AuditEventAction.ACTION_EXECUTION_STARTED,
            occurred_at=execution.created_at,
            actor_kind=actor.kind,
            actor_user_id=actor.user_id,
            incident_id=execution.incident_id,
            tenant_id=_incident_tenant(db, execution.incident_id),
            execution_id=execution.execution_id,
            trigger_source=execution.trigger_source,
        ),
    )


def record_incident_resolved(
    db: Session, incident_id: str, resolution: ResolutionJudgement, actor: Actor
) -> models.AuditEvent:
    """종료 판단이 새로 저장됐다 — 그 UPDATE 와 같은 트랜잭션 안, UPDATE 뒤에 부른다."""
    tenant_id, resolved_at = db.execute(
        select(models.Incident.tenant_id, models.Incident.resolved_at).where(
            models.Incident.incident_id == incident_id
        )
    ).one()
    return _add(
        db,
        models.AuditEvent(
            action=AuditEventAction.INCIDENT_RESOLVED,
            occurred_at=resolved_at,
            actor_kind=actor.kind,
            actor_user_id=actor.user_id,
            incident_id=incident_id,
            tenant_id=tenant_id,
            resolution=resolution,
        ),
    )


def list_by_incident(db: Session, incident_id: str) -> list[models.AuditEvent]:
    """한 인시던트의 기록 — 시각 · event_id 오름차순. 쓰기 검증용(조회 API 는 list_events)."""
    return list(
        db.scalars(
            select(models.AuditEvent)
            .where(models.AuditEvent.incident_id == incident_id)
            .order_by(models.AuditEvent.occurred_at, models.AuditEvent.event_id)
        )
    )


@dataclass(frozen=True)
class EventRow:
    """조회 한 줄 — 기록 + 실행의 대상(시작 기록만) + 행위자 이메일(사람만)."""

    event: models.AuditEvent
    runbook_id: RunbookId | None
    target_arn: str | None
    actor_email: str | None


def list_events(
    db: Session,
    *,
    tenant_id: str | None,
    incident_id: str | None,
    since: datetime | None,
    until: datetime | None,
    before: tuple[datetime, str] | None,
    limit: int,
) -> list[EventRow]:
    """최신순(occurred_at · event_id 내림차순) 최대 limit 건.

    tenant_id 가 있으면 그 테넌트의 기록만 — 기록에 옮겨 둔 인시던트의 테넌트로 거른다. None 은
    범위 제한 없음(플랫폼 계정)이다. before 는 직전 쪽의 마지막 (occurred_at, event_id) — 그보다 앞선 것만.
    """
    event = models.AuditEvent
    query = (
        select(
            event,
            models.ActionExecution.runbook_id,
            models.ActionExecution.target_arn,
            models.AppUser.email,
        )
        .outerjoin(
            models.ActionExecution,
            models.ActionExecution.execution_id == event.execution_id,
        )
        .outerjoin(models.AppUser, models.AppUser.user_id == event.actor_user_id)
    )
    if tenant_id is not None:
        query = query.where(event.tenant_id == tenant_id)
    if incident_id is not None:
        query = query.where(event.incident_id == incident_id)
    if since is not None:
        query = query.where(event.occurred_at >= since)
    if until is not None:
        query = query.where(event.occurred_at < until)
    if before is not None:
        at, event_id = before
        # 묶음 비교의 오른쪽도 칸 타입으로 묶는다 — 그냥 넘기면 문자열(varchar)이라 uuid 와 비교되지 않는다
        query = query.where(
            tuple_(event.occurred_at, event.event_id)
            < tuple_(literal(at, event.occurred_at.type), literal(event_id, event.event_id.type))
        )
    rows = db.execute(
        query.order_by(event.occurred_at.desc(), event.event_id.desc()).limit(limit)
    )
    return [EventRow(*row) for row in rows]
