# ==============================================================================
# [파일 설명]  담당: 김승철 (BE)
# GET /api/v1/platform-events — 플랫폼 활동 로그 조회 (#589 C, 계약: schemas.api.platform_events)
#
#   - 관문: 플랫폼 관리 등급(CENTRAL_ADMIN · TENANT_MANAGER)만 — auth.access.require_tenant_management.
#     고객사 계정 · 등급 없는 플랫폼 계정은 403(고객사 계정의 조회는 2차).
#   - 범위(#589 Q4): 중앙 관리자는 전부, 담당자는 담당 그룹 고객사의 행 + 고객사 없는 행 가운데 본인 행
#     (자기가 한 행 · 자기 계정이 대상인 인증 행 — db.repositories.platform_events.list_events).
#     범위는 요청마다 auth.access.tenant_scope_ids 로 다시 정한다.
#   - 범위 밖 · 형식이 어긋난 tenant_id · actor_user_id 는 404 가 아니라 빈 목록이다(존재를 알리지 않는다).
#   - 커서 · 시각 질의 규칙은 감사 기록 조회와 같다(page_cursor).
# ==============================================================================

from __future__ import annotations

from datetime import datetime
from typing import Optional

import page_cursor
from auth import service
from auth.access import require_tenant_management, tenant_scope_ids
from db.repositories import platform_events as platform_events_repo
from db.session import get_db
from exceptions import ApiError
from fastapi import APIRouter, Depends, Query
from identifiers import canonical_id
from schemas.api.errors import ErrorCode
from schemas.api.platform_events import (
    PLATFORM_EVENTS_DEFAULT_LIMIT,
    PLATFORM_EVENTS_MAX_LIMIT,
    PlatformEventsResponse,
)
from schemas.platform_events import (
    TARGET_ID_MAX_LENGTH,
    PlatformActorKind,
    PlatformEventAction,
    PlatformEventCategory,
    PlatformEventOutcome,
    PlatformEventTargetType,
)
from sqlalchemy.orm import Session

router = APIRouter(prefix="/api/v1", tags=["platform-events"])


def _to_item(row: platform_events_repo.EventRow) -> dict:
    event = row.event
    return {
        "event_id": event.event_id,
        "occurred_at": event.occurred_at,
        "category": event.category,
        "action": event.action,
        "outcome": event.outcome,
        "actor": {
            "kind": event.actor_kind,
            "user_id": event.actor_user_id,
            "email": row.actor_email,
            "account_type": event.actor_account_type,
            "platform_role": event.actor_platform_role,
        },
        "tenant_id": event.tenant_id,
        "target": (
            None if event.target_type is None
            else {"type": event.target_type, "id": event.target_id}
        ),
        "details": event.details,
        "session_id": event.session_id,
        "request_id": event.request_id,
        # INET 칸은 주소 객체로 온다
        "source_ip": None if event.source_ip is None else str(event.source_ip),
        "user_agent": event.user_agent,
    }


@router.get("/platform-events", response_model=PlatformEventsResponse)
def list_platform_events(
    since: Optional[datetime] = None,
    until: Optional[datetime] = None,
    category: Optional[PlatformEventCategory] = None,
    action: Optional[PlatformEventAction] = None,
    outcome: Optional[PlatformEventOutcome] = None,
    actor_kind: Optional[PlatformActorKind] = None,
    actor_user_id: Optional[str] = None,
    tenant_id: Optional[str] = None,
    target_type: Optional[PlatformEventTargetType] = None,
    target_id: Optional[str] = Query(default=None, min_length=1, max_length=TARGET_ID_MAX_LENGTH),
    cursor: Optional[str] = Query(default=None, min_length=1),
    limit: int = Query(default=PLATFORM_EVENTS_DEFAULT_LIMIT, ge=1, le=PLATFORM_EVENTS_MAX_LIMIT),
    db: Session = Depends(get_db),
    user: service.CurrentUser = Depends(require_tenant_management),
) -> PlatformEventsResponse:
    since, until = page_cursor.aware(since), page_cursor.aware(until)
    before = None if cursor is None else page_cursor.decode(cursor)
    if target_id is not None and target_type is None:
        raise ApiError(ErrorCode.REQUEST_VALIDATION_FAILED)
    if actor_user_id is not None:
        actor_user_id = canonical_id(actor_user_id)
        if actor_user_id is None:
            return PlatformEventsResponse(items=[], next_cursor=None)
    if tenant_id is not None:
        tenant_id = canonical_id(tenant_id)
        if tenant_id is None:
            return PlatformEventsResponse(items=[], next_cursor=None)
    filters = platform_events_repo.EventFilters(
        since=since,
        until=until,
        category=category,
        action=action,
        outcome=outcome,
        actor_kind=actor_kind,
        actor_user_id=actor_user_id,
        tenant_id=tenant_id,
        target_type=target_type,
        target_id=target_id,
    )
    # 한 건 더 읽어 다음 쪽이 있는지 본다
    rows = platform_events_repo.list_events(
        db,
        tenant_ids=tenant_scope_ids(user),
        viewer_user_id=user.user_id,
        filters=filters,
        before=before,
        limit=limit + 1,
    )
    page = rows[:limit]
    return PlatformEventsResponse.model_validate(
        {
            "items": [_to_item(row) for row in page],
            "next_cursor": (
                page_cursor.encode(page[-1].event.occurred_at, page[-1].event.event_id)
                if len(rows) > limit else None
            ),
        }
    )
