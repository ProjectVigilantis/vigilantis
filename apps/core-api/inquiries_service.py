# ==============================================================================
# /api/v1/inquiries 의 판정 · 트랜잭션 · 응답 조립 (#552, 계약: schemas.api.inquiries)
#
#   - 쓰기는 support_access.write_transaction 안에서 한다(users 잠금 → 행위자 재판정 → 본문 → commit).
#     본문의 ApiError 는 전부 rollback 이라 실패한 전이가 이력 · 알림을 남기지 않는다. 응답은 commit 전에
#     같은 트랜잭션 안에서 만든다.
#   - 접수(POST)는 require_customer 만 본다 — 정지 테넌트의 활성 사용자도 자기 테넌트에 접수할 수 있다
#     (사용자 결정). admin_service._transaction 은 정지를 거절하므로 쓰지 않는다.
#   - 접수 = 문의 + INQUIRY_CREATED 이력 + 운영자 알림을 한 트랜잭션에 쓴다.
#   - 관리 경로(전이 · 메모 · 이력)는 대상 문의 행을 FOR UPDATE 로 잡은 뒤 조건부 UPDATE 로 전이한다.
#     범위는 auth.access.tenant_scope_ids 하나다. 형식이 어긋난 · 없는 · 범위 밖 ID 는 같은 404 다.
#   - 이력 시각은 잠금 안에서 읽고, 직전 이력보다 앞서지 않게 맞춰 오래된 순 정렬을 안정시킨다.
# ==============================================================================

from __future__ import annotations

from datetime import datetime, timedelta

from auth import service
from auth.access import require_tenant_management, tenant_scope_ids
from db import models
from db.repositories import inquiries as inquiries_repo
from db.repositories import operator_notifications
from exceptions import ApiError
from identifiers import canonical_id
from schemas.api.errors import ErrorCode
from schemas.api.inquiries import (
    INQUIRY_TRANSITIONS,
    InquiriesResponse,
    InquiryCreateRequest,
    InquiryEventItem,
    InquiryEventsResponse,
    InquiryItem,
    InquiryNoteRequest,
    InquiryStatusRequest,
)
from schemas.api.support import InquiryEventAction, InquiryStatus, NotificationKind
from sqlalchemy.orm import Session
from support_access import (
    Actor,
    authorize,
    require_customer,
    require_support_user,
    write_transaction,
)

_ONE_MICROSECOND = timedelta(microseconds=1)


def _item(row: inquiries_repo.InquiryRow) -> InquiryItem:
    inquiry = row.inquiry
    return InquiryItem(
        inquiry_id=inquiry.inquiry_id,
        tenant_id=inquiry.tenant_id,
        tenant_name=row.tenant_name,
        author_email=row.author_email,
        author_role=row.author_role,
        category=inquiry.category,
        title=inquiry.title,
        body=inquiry.body,
        occurred_at=inquiry.occurred_at,
        page_path=inquiry.page_path,
        status=inquiry.status,
        created_at=inquiry.created_at,
        updated_at=inquiry.updated_at,
    )


def _event_item(event: models.InquiryEvent, actor_email: str) -> InquiryEventItem:
    """actor_email 은 조회 시점의 현재 사용자 이메일이다(당시 스냅샷이 아니다)."""
    return InquiryEventItem(
        event_id=event.event_id,
        inquiry_id=event.inquiry_id,
        actor_user_id=event.actor_user_id,
        actor_email=actor_email,
        action=event.action,
        before_status=event.before_status,
        after_status=event.after_status,
        note=event.note,
        occurred_at=event.occurred_at,
    )


def _reload(db: Session, inquiry_id: str) -> InquiryItem:
    row = inquiries_repo.get_inquiry(db, inquiry_id, scope_ids=None)
    assert row is not None  # 같은 트랜잭션에서 방금 잡은 행이다
    return _item(row)


def _locked_target(
    db: Session, user: service.CurrentUser, inquiry_id: str
) -> models.Inquiry:
    """범위 안의 문의를 잠가 돌려준다. 형식 오류 · 없음 · 범위 밖은 모두 404."""
    stored_id = canonical_id(inquiry_id)
    inquiry = (
        None
        if stored_id is None
        else inquiries_repo.lock_inquiry(db, stored_id, scope_ids=tenant_scope_ids(user))
    )
    if inquiry is None:
        raise ApiError(ErrorCode.INQUIRY_NOT_FOUND)
    return inquiry


def _event_time(db: Session, inquiry_id: str, now: datetime) -> datetime:
    latest = inquiries_repo.latest_event_at(db, inquiry_id)
    if latest is not None and now <= latest:
        return latest + _ONE_MICROSECOND
    return now


# --- 접수 · 조회 -----------------------------------------------------------------


def create_inquiry(db: Session, actor: Actor, body: InquiryCreateRequest) -> InquiryItem:
    with write_transaction(db, actor, require_customer) as user:
        # 테넌트 · 작성자는 판정된 신원에서만 온다. 알림 제목에 쓰는 이름은 같은 트랜잭션의 현재 값이다
        tenant = db.get(
            models.Tenant, user.tenant_id, populate_existing=True, with_for_update={"read": True}
        )
        if tenant is None:
            raise ApiError(ErrorCode.AUTH_FORBIDDEN)
        now = actor.clock()
        inquiry = inquiries_repo.create_inquiry(
            db,
            tenant_id=tenant.tenant_id,
            author_user_id=user.user_id,
            category=body.category,
            title=body.title,
            body=body.body,
            occurred_at=body.occurred_at,
            page_path=body.page_path,
            now=now,
        )
        inquiries_repo.add_event(
            db,
            inquiry_id=inquiry.inquiry_id,
            actor_user_id=user.user_id,
            action=InquiryEventAction.INQUIRY_CREATED,
            before_status=None,
            after_status=InquiryStatus.OPEN,
            note=None,
            now=now,
        )
        operator_notifications.add_notification(
            db,
            kind=NotificationKind.INQUIRY,
            target_id=inquiry.inquiry_id,
            tenant_id=tenant.tenant_id,
            subject=tenant.name,
            summary=inquiry.title,
            inquiry_category=inquiry.category,
            created_at=now,
        )
        return _reload(db, inquiry.inquiry_id)


def list_inquiries(db: Session, actor: Actor) -> InquiriesResponse:
    user = authorize(db, actor, require_support_user)
    rows = inquiries_repo.list_inquiries(db, scope_ids=tenant_scope_ids(user))
    return InquiriesResponse(items=[_item(row) for row in rows])


# --- 관리 -----------------------------------------------------------------------


def change_status(
    db: Session, actor: Actor, inquiry_id: str, body: InquiryStatusRequest
) -> InquiryItem:
    with write_transaction(db, actor, require_tenant_management) as user:
        inquiry = _locked_target(db, user, inquiry_id)
        before = inquiry.status
        if body.status not in INQUIRY_TRANSITIONS[before]:
            raise ApiError(ErrorCode.INQUIRY_STATE_CONFLICT)
        now = _event_time(db, inquiry.inquiry_id, actor.clock())
        if not inquiries_repo.transition_status(
            db, inquiry.inquiry_id, from_status=before, to_status=body.status, now=now
        ):
            raise ApiError(ErrorCode.INQUIRY_STATE_CONFLICT)
        inquiries_repo.add_event(
            db,
            inquiry_id=inquiry.inquiry_id,
            actor_user_id=user.user_id,
            action=InquiryEventAction.STATUS_CHANGED,
            before_status=before,
            after_status=body.status,
            note=body.note,
            now=now,
        )
        return _reload(db, inquiry.inquiry_id)


def add_note(
    db: Session, actor: Actor, inquiry_id: str, body: InquiryNoteRequest
) -> InquiryEventItem:
    with write_transaction(db, actor, require_tenant_management) as user:
        inquiry = _locked_target(db, user, inquiry_id)
        event = inquiries_repo.add_event(
            db,
            inquiry_id=inquiry.inquiry_id,
            actor_user_id=user.user_id,
            action=InquiryEventAction.NOTE_ADDED,
            before_status=None,
            after_status=None,
            note=body.note,
            now=_event_time(db, inquiry.inquiry_id, actor.clock()),
        )
        return _event_item(event, user.email)


def list_events(db: Session, actor: Actor, inquiry_id: str) -> InquiryEventsResponse:
    user = authorize(db, actor, require_tenant_management)
    stored_id = canonical_id(inquiry_id)
    row = (
        None
        if stored_id is None
        else inquiries_repo.get_inquiry(db, stored_id, scope_ids=tenant_scope_ids(user))
    )
    if row is None:
        raise ApiError(ErrorCode.INQUIRY_NOT_FOUND)
    events = inquiries_repo.list_events(db, row.inquiry.inquiry_id)
    return InquiryEventsResponse(items=[_event_item(event, email) for event, email in events])
