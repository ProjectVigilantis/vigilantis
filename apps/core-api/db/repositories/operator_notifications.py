# ==============================================================================
# [파일 설명]
# 운영자 알림 수신함 저장소. (#552)
#
#   - add_notification 은 가입 신청 · 문의 생성 서비스가 부모 레코드와 같은 트랜잭션에서 부른다.
#     add + flush 만 하고 commit 은 호출한 서비스가 한다.
#   - 가시 범위(scope)는 auth.access.tenant_scope_ids 결과다: None = 전체(CENTRAL_ADMIN, 가입 신청 포함),
#     튜플 = 그 고객사의 문의만(빈 튜플 = 아무것도 안 보임). 가입 신청 알림은 중앙 관리자만 본다.
#   - 읽음 표시는 (notification_id, user_id) 단위로 남고, 가시 범위를 잃어도 지우지 않는다.
#   - 기존 사건 발송 outbox(NotificationEvent / NotificationDelivery)와 무관하다.
# ==============================================================================

from __future__ import annotations

from collections.abc import Collection, Sequence
from datetime import datetime

from schemas.api.support import InquiryCategory, NotificationKind
from sqlalchemy import and_, false, func, select, true
from sqlalchemy.orm import Session

from db import models

Scope = Sequence[str] | None


def add_notification(
    db: Session,
    *,
    kind: NotificationKind,
    target_id: str,
    tenant_id: str | None,
    subject: str,
    summary: str,
    inquiry_category: InquiryCategory | None,
    created_at: datetime,
) -> models.OperatorNotification:
    row = models.OperatorNotification(
        kind=kind,
        target_id=target_id,
        tenant_id=tenant_id,
        subject=subject,
        summary=summary,
        inquiry_category=inquiry_category,
        created_at=created_at,
    )
    db.add(row)
    db.flush()
    return row


def _visible(scope: Scope):
    N = models.OperatorNotification
    if scope is None:
        return true()
    if not scope:
        return false()
    return and_(N.kind == NotificationKind.INQUIRY, N.tenant_id.in_(list(scope)))


def _read_join(user_id: str):
    R = models.OperatorNotificationRead
    return and_(
        R.notification_id == models.OperatorNotification.notification_id, R.user_id == user_id
    )


def list_latest(
    db: Session, user_id: str, scope: Scope, limit: int
) -> list[tuple[models.OperatorNotification, datetime | None]]:
    N, R = models.OperatorNotification, models.OperatorNotificationRead
    rows = db.execute(
        select(N, R.read_at)
        .outerjoin(R, _read_join(user_id))
        .where(_visible(scope))
        .order_by(N.created_at.desc(), N.notification_id.desc())
        .limit(limit)
        .execution_options(populate_existing=True)
    ).all()
    return [(n, read_at) for n, read_at in rows]


def count_unread(db: Session, user_id: str, scope: Scope) -> int:
    N, R = models.OperatorNotification, models.OperatorNotificationRead
    return db.scalar(
        select(func.count())
        .select_from(N)
        .outerjoin(R, _read_join(user_id))
        .where(_visible(scope), R.notification_id.is_(None))
    ) or 0


def visible_ids(db: Session, scope: Scope, ids: Collection[str]) -> set[str]:
    N = models.OperatorNotification
    if not ids:
        return set()
    return set(
        db.scalars(select(N.notification_id).where(_visible(scope), N.notification_id.in_(list(ids))))
    )


def unread_visible_ids(db: Session, user_id: str, scope: Scope) -> list[str]:
    N, R = models.OperatorNotification, models.OperatorNotificationRead
    return list(
        db.scalars(
            select(N.notification_id)
            .outerjoin(R, _read_join(user_id))
            .where(_visible(scope), R.notification_id.is_(None))
        )
    )


def mark_read(db: Session, user_id: str, ids: Collection[str], read_at: datetime) -> None:
    """이미 읽은 알림은 건드리지 않아 처음 read_at 이 유지된다. 호출자가 users 잠금 안에서 부른다."""
    R = models.OperatorNotificationRead
    if not ids:
        return
    done = set(
        db.scalars(select(R.notification_id).where(R.user_id == user_id, R.notification_id.in_(list(ids))))
    )
    db.add_all(
        R(notification_id=i, user_id=user_id, read_at=read_at) for i in sorted(set(ids) - done)
    )
    db.flush()
