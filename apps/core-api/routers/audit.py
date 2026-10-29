# ==============================================================================
# [파일 설명]
# GET /api/v1/audit-events — 조치 승인 · 종료 판단 감사 기록 조회 (T-2 B · #544, 계약: schemas.api.audit)
#
#   - 범위: 고객사 계정은 자기 테넌트의 기록만, 플랫폼 계정은 전부. 모든 역할이 본다(VIEWER 포함).
#     다른 업무 라우터처럼 임시 출시 관문(require_business_access) 뒤에 둔다 — 지금 고객사 계정은 403 이다.
#     범위는 그래도 이 라우터가 직접 건다: 관문이 걷히면(테넌트 격리 뒤) 고칠 것 없이 고객사 계정에 열린다.
#   - 행위자 이메일은 보는 사람이 정한다: PLATFORM_USER 의 이메일은 플랫폼 계정에게만 싣는다.
#   - 범위 밖 · 형식이 어긋난 incident_id 는 404 가 아니라 빈 목록이다(존재를 알리지 않는다).
#   - 커서는 직전 쪽 마지막 기록의 (occurred_at, event_id) 를 감싼 불투명 문자열이다. 깨지면 422.
#     시각 질의는 시간대가 있어야 한다(없으면 422) — 기록은 UTC 이고 해석을 서버가 추측하지 않는다.
# ==============================================================================

from __future__ import annotations

import base64
import binascii
import json
from datetime import datetime
from typing import Optional

from auth.access import is_platform_account, require_user
from auth.service import CurrentUser
from db.repositories import audit as audit_repo
from db.session import get_db
from exceptions import ApiError
from fastapi import APIRouter, Depends, Query
from identifiers import canonical_id
from schemas.api.audit import (
    AUDIT_EVENTS_DEFAULT_LIMIT,
    AUDIT_EVENTS_MAX_LIMIT,
    AuditActorKind,
    AuditEventAction,
    AuditEventsResponse,
)
from schemas.api.errors import ErrorCode
from sqlalchemy.orm import Session

router = APIRouter(prefix="/api/v1", tags=["audit"])


def _encode_cursor(row: audit_repo.EventRow) -> str:
    raw = json.dumps([row.event.occurred_at.isoformat(), row.event.event_id])
    return base64.urlsafe_b64encode(raw.encode()).rstrip(b"=").decode()


def _decode_cursor(cursor: str) -> tuple[datetime, str]:
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        occurred_at, event_id = json.loads(raw)
        occurred_at = datetime.fromisoformat(occurred_at)
        event_id = canonical_id(event_id)
    except (binascii.Error, UnicodeDecodeError, ValueError, TypeError, AttributeError):
        raise ApiError(ErrorCode.REQUEST_VALIDATION_FAILED) from None
    if occurred_at.tzinfo is None or event_id is None:
        raise ApiError(ErrorCode.REQUEST_VALIDATION_FAILED)
    return occurred_at, event_id


def _aware(value: Optional[datetime]) -> Optional[datetime]:
    if value is not None and value.tzinfo is None:
        raise ApiError(ErrorCode.REQUEST_VALIDATION_FAILED)
    return value


def _actor_email(row: audit_repo.EventRow, *, platform_viewer: bool) -> Optional[str]:
    """SYSTEM 은 사용자가 없어 조인한 이메일이 언제나 null 이다(DB CHECK)."""
    if row.event.actor_kind is AuditActorKind.PLATFORM_USER and not platform_viewer:
        return None
    return row.actor_email


def _to_item(row: audit_repo.EventRow, *, platform_viewer: bool) -> dict:
    event = row.event
    item = {
        "action": event.action,
        "event_id": event.event_id,
        "occurred_at": event.occurred_at,
        "actor": {
            "kind": event.actor_kind,
            "email": _actor_email(row, platform_viewer=platform_viewer),
        },
        "incident_id": event.incident_id,
        "tenant_id": event.tenant_id,
    }
    if event.action is AuditEventAction.ACTION_EXECUTION_STARTED:
        item |= {
            "trigger_source": event.trigger_source,
            "execution_id": event.execution_id,
            "runbook_id": row.runbook_id,
            "target_arn": row.target_arn,
        }
    else:
        item["resolution"] = event.resolution
    return item


@router.get("/audit-events", response_model=AuditEventsResponse)
def list_audit_events(
    incident_id: Optional[str] = None,
    since: Optional[datetime] = None,
    until: Optional[datetime] = None,
    cursor: Optional[str] = Query(default=None, min_length=1),
    limit: int = Query(default=AUDIT_EVENTS_DEFAULT_LIMIT, ge=1, le=AUDIT_EVENTS_MAX_LIMIT),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_user),
) -> AuditEventsResponse:
    since, until = _aware(since), _aware(until)
    before = None if cursor is None else _decode_cursor(cursor)
    if incident_id is not None:
        incident_id = canonical_id(incident_id)
        if incident_id is None:
            return AuditEventsResponse(items=[], next_cursor=None)
    platform_viewer = is_platform_account(user)
    scope = None if platform_viewer else user.tenant_id
    if not platform_viewer and scope is None:
        # 소속 없는 고객사 계정은 세션 판정을 통과하지 않는다 — 통과했다면 「전부」로 열지 않는다
        raise ApiError(ErrorCode.AUTH_FORBIDDEN)
    # 한 건 더 읽어 다음 쪽이 있는지 본다
    rows = audit_repo.list_events(
        db,
        tenant_id=scope,
        incident_id=incident_id,
        since=since,
        until=until,
        before=before,
        limit=limit + 1,
    )
    page = rows[:limit]
    return AuditEventsResponse.model_validate(
        {
            "items": [_to_item(row, platform_viewer=platform_viewer) for row in page],
            "next_cursor": _encode_cursor(page[-1]) if len(rows) > limit else None,
        }
    )
