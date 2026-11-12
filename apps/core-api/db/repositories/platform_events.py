# ==============================================================================
# [파일 설명]  담당: 김승철 (BE)
# 플랫폼 활동 로그 저장소 — PlatformEvent(#589). 기록 계약: schemas.platform_events
#
#   - 쓰기는 record_platform_event 하나로 한다. 기능 코드는 행동 · 행위자 · 대상 · details 만 넘기고,
#     요청 문맥(요청 식별자 · 접속지 · 브라우저 정보)은 여기서 채운다(request_origin). 세션은 행위자가
#     싣는다 — person(user) 가 요청자의 세션 ID 를 함께 옮긴다.
#   - 행을 만드는 일(build_platform_event · 계약 검증)과 쓰는 일을 나눠 둔다 — 따로 남기기가 계약 위반은
#     그대로 올리고 DB 실패만 삼키게 하기 위해서다.
#   - 상태 변경과 **같은 트랜잭션**에서 부른다. flush 까지만 하고 commit 은 호출부가 한다 —
#     로그를 못 쓰면 변경도 되돌아간다.
#   - 거절 · 실패를 요청 트랜잭션과 따로 남기는 길은 platform_event_log.record_detached 다(자기 세션을 연다).
#   - 재요청(멱등 200 재생)은 부르지 않는다 — 호출부가 새 행이 생긴 경로에서만 부른다(audit 와 같다).
#   - 읽기는 list_events 하나다(GET /platform-events · #589 C). 범위는 호출부가 고객사 범위
#     (auth.access.tenant_scope_ids)와 보는 사람을 넘기고, 여기는 그대로 거른다.
# ==============================================================================

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

from request_origin import current_origin
from schemas.api.auth import AccountType, PlatformRole
from schemas.platform_events import (
    PlatformActorKind,
    PlatformEventAction,
    PlatformEventCategory,
    PlatformEventOutcome,
    PlatformEventRecord,
    PlatformEventTargetType,
)
from sqlalchemy import and_, literal, or_, select, tuple_
from sqlalchemy.orm import Session

from .. import models


@dataclass(frozen=True)
class EventActor:
    """누가 · 무엇이 했나. 사람은 쓰는 시점의 계정 종류 · 플랫폼 등급을 함께 싣는다."""

    kind: PlatformActorKind
    user_id: Optional[str] = None
    account_type: Optional[AccountType] = None
    platform_role: Optional[PlatformRole] = None
    #: 요청자의 서버 세션 ID. 사람이 아니면 없다(로그인처럼 그 요청에서 세션이 생기면 기록 함수에 넘긴다).
    session_id: Optional[str] = None


SYSTEM = EventActor(PlatformActorKind.SYSTEM)
ANONYMOUS = EventActor(PlatformActorKind.ANONYMOUS)


def person(user: Any) -> EventActor:
    """요청자(auth.service.CurrentUser)의 신원을 행위자로 — 판정 시점의 계정 종류 · 등급 · 세션을 옮겨 둔다."""
    return EventActor(
        PlatformActorKind.USER,
        user_id=user.user_id,
        account_type=user.account_type,
        platform_role=user.platform_role,
        session_id=user.session_id,
    )


def build_platform_event(
    *,
    action: PlatformEventAction,
    actor: EventActor,
    outcome: PlatformEventOutcome = PlatformEventOutcome.SUCCEEDED,
    tenant_id: Optional[str] = None,
    target_id: Optional[str] = None,
    details: Optional[Mapping[str, Any]] = None,
    session_id: Optional[str] = None,
    occurred_at: Optional[datetime] = None,
) -> models.PlatformEvent:
    """계약을 거친 한 행(아직 세션에 넣지 않음). 계약 위반은 ValueError(pydantic)로 바로 올린다.

    session_id 를 넘기면 행위자의 세션보다 앞선다 — 로그인 성공처럼 그 요청에서 세션이 생기는 자리다.
    """
    record = PlatformEventRecord(
        action=action,
        outcome=outcome,
        actor_kind=actor.kind,
        actor_user_id=actor.user_id,
        actor_account_type=actor.account_type,
        actor_platform_role=actor.platform_role,
        tenant_id=tenant_id,
        target_id=target_id,
        details=dict(details or {}),
    )
    origin = current_origin()
    values: dict[str, Any] = {}
    if occurred_at is not None:
        values["occurred_at"] = occurred_at
    return models.PlatformEvent(
        **values,
        category=record.category,
        action=record.action.value,
        outcome=record.outcome,
        actor_kind=record.actor_kind,
        actor_user_id=str(record.actor_user_id) if record.actor_user_id else None,
        actor_account_type=record.actor_account_type,
        actor_platform_role=record.actor_platform_role,
        session_id=session_id if session_id is not None else actor.session_id,
        request_id=origin.request_id if origin else None,
        source_ip=origin.source_ip if origin else None,
        user_agent=origin.user_agent if origin else None,
        tenant_id=str(record.tenant_id) if record.tenant_id else None,
        target_type=record.target_type,
        target_id=record.target_id,
        details=record.details,
    )


def record_platform_event(
    db: Session,
    action: PlatformEventAction,
    actor: EventActor,
    *,
    outcome: PlatformEventOutcome = PlatformEventOutcome.SUCCEEDED,
    tenant_id: Optional[str] = None,
    target_id: Optional[str] = None,
    details: Optional[Mapping[str, Any]] = None,
    session_id: Optional[str] = None,
    occurred_at: Optional[datetime] = None,
) -> models.PlatformEvent:
    """상태 변경과 같은 트랜잭션에서 한 행을 남긴다. 계약 위반은 ValueError(pydantic)로 바로 올린다."""
    row = build_platform_event(
        action=action,
        actor=actor,
        outcome=outcome,
        tenant_id=tenant_id,
        target_id=target_id,
        details=details,
        session_id=session_id,
        occurred_at=occurred_at,
    )
    db.add(row)
    db.flush()
    return row


@dataclass(frozen=True)
class EventFilters:
    """GET /platform-events 의 질의. 비어 있는 칸은 거르지 않는다."""

    since: Optional[datetime] = None
    until: Optional[datetime] = None
    category: Optional[PlatformEventCategory] = None
    action: Optional[PlatformEventAction] = None
    outcome: Optional[PlatformEventOutcome] = None
    actor_kind: Optional[PlatformActorKind] = None
    actor_user_id: Optional[str] = None
    tenant_id: Optional[str] = None
    target_type: Optional[PlatformEventTargetType] = None
    target_id: Optional[str] = None


@dataclass(frozen=True)
class EventRow:
    event: models.PlatformEvent
    #: 행위자의 지금 이메일(사람만). 기록에는 이메일을 복사하지 않는다
    actor_email: Optional[str]


def list_events(
    db: Session,
    *,
    tenant_ids: Optional[tuple[str, ...]],
    viewer_user_id: str,
    filters: EventFilters,
    before: Optional[tuple[datetime, str]],
    limit: int,
) -> list[EventRow]:
    """최신순(occurred_at · event_id 내림차순) 최대 limit 건.

    tenant_ids 가 None 이면 전부(고객사 없는 행 포함)다. 아니면 그 고객사들의 행과, 고객사 없는 행 가운데
    본인 행만이다(#589 Q4) — 빈 튜플은 본인 행만 남긴다(전체로 넓히지 않는다).
    본인 행은 viewer_user_id 가 한 행 + 그 계정이 대상인 인증(AUTH) 행이다. 로그인 · MFA · 비밀번호 재설정
    시도는 로그인 전이라 행위자가 ANONYMOUS 이고 계정은 대상으로만 이어지기 때문이다. 인증 행은 행위자가
    누구든 들어간다 — 다른 직원이 내 인증 앱을 초기화한 행(AUTH_TOTP_RECOVERY)도 내 로그인 수단이 바뀐
    일이라 본인이 본다. 인증 밖에서 나를 대상으로 한 일(내 등급 변경 등)은 넣지 않는다 — 다른 직원의 행이다.
    before 는 직전 쪽의 마지막 (occurred_at, event_id) — 그보다 앞선 것만.
    """
    event = models.PlatformEvent
    query = select(event, models.AppUser.email).outerjoin(
        models.AppUser, models.AppUser.user_id == event.actor_user_id
    )
    if tenant_ids is not None:
        about_me = and_(
            event.category == PlatformEventCategory.AUTH,
            event.target_type == PlatformEventTargetType.USER,
            event.target_id == viewer_user_id,
        )
        own = and_(event.tenant_id.is_(None), or_(event.actor_user_id == viewer_user_id, about_me))
        query = query.where(or_(event.tenant_id.in_(tenant_ids), own) if tenant_ids else own)
    f = filters
    if f.since is not None:
        query = query.where(event.occurred_at >= f.since)
    if f.until is not None:
        query = query.where(event.occurred_at < f.until)
    for column, value in (
        (event.category, f.category),
        (event.action, None if f.action is None else f.action.value),
        (event.outcome, f.outcome),
        (event.actor_kind, f.actor_kind),
        (event.actor_user_id, f.actor_user_id),
        (event.tenant_id, f.tenant_id),
        (event.target_type, f.target_type),
        (event.target_id, f.target_id),
    ):
        if value is not None:
            query = query.where(column == value)
    if before is not None:
        at, event_id = before
        # 묶음 비교의 오른쪽도 칸 타입으로 묶는다 — 그냥 넘기면 문자열(varchar)이라 uuid 와 비교되지 않는다
        query = query.where(
            tuple_(event.occurred_at, event.event_id)
            < tuple_(literal(at, event.occurred_at.type), literal(event_id, event.event_id.type))
        )
    rows = db.execute(query.order_by(event.occurred_at.desc(), event.event_id.desc()).limit(limit))
    return [EventRow(*row) for row in rows]
