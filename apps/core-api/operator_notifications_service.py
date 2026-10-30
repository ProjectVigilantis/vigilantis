# ==============================================================================
# [파일 설명]
# 운영자 알림 수신함 서비스. (#552 · 라우터: routers/notifications.py)
#
#   - 가시 범위는 요청마다 auth.access.tenant_scope_ids 로 다시 계산한다(회수 · 재배정 즉시 반영).
#   - 읽기는 authorize 로 새로 판정한 사용자로, 쓰기는 write_transaction(users 잠금 안 재판정 · commit)으로 한다.
#   - 개별 읽음은 하나라도 없거나 범위 밖이면 404 NOTIFICATION_NOT_FOUND 이고 아무것도 쓰지 않는다.
#   - 읽음 표시만 쓴다. 문의 · 가입 신청 상태는 건드리지 않는다. 이 안에는 외부 호출이 없다.
# ==============================================================================

from __future__ import annotations

from auth import admin_service
from auth.access import require_tenant_management, tenant_scope_ids
from db import models
from db.repositories import operator_notifications as repo
from exceptions import ApiError
from schemas.api.errors import ErrorCode
from schemas.api.notifications import (
    INBOX_PAGE_SIZE,
    NotificationItem,
    NotificationsReadRequest,
    NotificationsResponse,
)
from sqlalchemy.orm import Session
from support_access import authorize, write_transaction


def _item(row: models.OperatorNotification, read_at) -> NotificationItem:
    return NotificationItem(
        notification_id=row.notification_id,
        kind=row.kind,
        target_id=row.target_id,
        tenant_id=row.tenant_id,
        subject=row.subject,
        summary=row.summary,
        inquiry_category=row.inquiry_category,
        created_at=row.created_at,
        read_at=read_at,
    )


def list_notifications(db: Session, actor: admin_service.Actor) -> NotificationsResponse:
    user = authorize(db, actor, require_tenant_management)
    scope = tenant_scope_ids(user)
    rows = repo.list_latest(db, user.user_id, scope, INBOX_PAGE_SIZE)
    return NotificationsResponse(
        items=[_item(n, read_at) for n, read_at in rows],
        unread_count=repo.count_unread(db, user.user_id, scope),
    )


def mark_read(db: Session, actor: admin_service.Actor, body: NotificationsReadRequest) -> None:
    with write_transaction(db, actor, require_tenant_management) as user:
        scope = tenant_scope_ids(user)
        if body.notification_ids is None:
            ids = repo.unread_visible_ids(db, user.user_id, scope)
        else:
            ids = {str(i) for i in body.notification_ids}
            if ids - repo.visible_ids(db, scope, ids):
                raise ApiError(ErrorCode.NOTIFICATION_NOT_FOUND)
        repo.mark_read(db, user.user_id, ids, actor.clock())
