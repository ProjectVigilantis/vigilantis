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
    PlatformEventOutcome,
    PlatformEventRecord,
)
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
