# ==============================================================================
# [파일 설명]
# /api/v1/notifications — 운영자 알림 수신함 라우터. (#552 · 계약: schemas.api.notifications)
#
#   - 플랫폼 관리 역할 게이트가 라우터 의존성이라 본문 검증보다 먼저 돈다(고객사 계정은 403).
#   - 판정 · 트랜잭션 · 응답 조립은 operator_notifications_service 가 맡는다.
# ==============================================================================

from __future__ import annotations

import operator_notifications_service as service
from auth import admin_service
from auth.access import require_tenant_management
from db.session import get_db
from fastapi import APIRouter, Depends, Response
from schemas.api.notifications import NotificationsReadRequest, NotificationsResponse
from sqlalchemy.orm import Session
from support_access import get_actor

_DB = Depends(get_db)
_ACTOR = Depends(get_actor)

router = APIRouter(
    prefix="/api/v1/notifications",
    tags=["notifications"],
    dependencies=[Depends(require_tenant_management)],
)


@router.get("", response_model=NotificationsResponse)
def list_notifications(
    db: Session = _DB, actor: admin_service.Actor = _ACTOR
) -> NotificationsResponse:
    return service.list_notifications(db, actor)


@router.post("/read", status_code=204)
def mark_read(
    body: NotificationsReadRequest, db: Session = _DB, actor: admin_service.Actor = _ACTOR
) -> Response:
    service.mark_read(db, actor, body)
    return Response(status_code=204)
