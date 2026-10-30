# ==============================================================================
# /api/v1/inquiries — 고객 문의 · 처리 이력 라우터. (#552, 계약: schemas.api.inquiries)
#
#   - 얇게 둔다: 판정 · 범위 · 트랜잭션은 inquiries_service 가 맡는다.
#   - 경로별 게이트가 본문 검증보다 먼저 돈다: 접수는 고객사(TENANT) 계정(역할 무관 · 정지 테넌트 포함),
#     목록은 고객사 계정 또는 플랫폼 관리 권한자, 전이 · 메모 · 이력은 플랫폼 관리 권한자뿐이다
#     (고객사 계정은 자기 문의도 403). 서비스가 쓰기 직전에 행위자를 다시 판정한다.
#   - Origin · 인증 전역 의존성은 앱이 라우터를 붙일 때 건다.
# ==============================================================================

from __future__ import annotations

from auth import service
from auth.access import require_tenant_management, require_user
from db.session import get_db
from fastapi import APIRouter, Depends
from inquiries_service import (
    add_note,
    change_status,
    create_inquiry,
    list_events,
    list_inquiries,
)
from schemas.api.inquiries import (
    InquiriesResponse,
    InquiryCreateRequest,
    InquiryEventItem,
    InquiryEventsResponse,
    InquiryItem,
    InquiryNoteRequest,
    InquiryStatusRequest,
)
from sqlalchemy.orm import Session
from support_access import Actor, get_actor, require_customer, require_support_user

_DB = Depends(get_db)
_ACTOR = Depends(get_actor)
_USER = Depends(require_user)


# support_access 의 게이트는 CurrentUser 를 인자로 받는 함수라 의존성(require_user)에 묶어야 경로 게이트가 된다
def customer_gate(user: service.CurrentUser = _USER) -> service.CurrentUser:
    return require_customer(user)


def support_gate(user: service.CurrentUser = _USER) -> service.CurrentUser:
    return require_support_user(user)


_CUSTOMER = [Depends(customer_gate)]
_SUPPORT = [Depends(support_gate)]
_MANAGER = [Depends(require_tenant_management)]

router = APIRouter(prefix="/api/v1/inquiries", tags=["inquiries"])


@router.post("", status_code=201, response_model=InquiryItem, dependencies=_CUSTOMER)
def post_inquiry(
    body: InquiryCreateRequest, db: Session = _DB, actor: Actor = _ACTOR
) -> InquiryItem:
    return create_inquiry(db, actor, body)


@router.get("", response_model=InquiriesResponse, dependencies=_SUPPORT)
def get_inquiries(db: Session = _DB, actor: Actor = _ACTOR) -> InquiriesResponse:
    return list_inquiries(db, actor)


@router.put("/{inquiry_id}/status", response_model=InquiryItem, dependencies=_MANAGER)
def put_status(
    inquiry_id: str, body: InquiryStatusRequest, db: Session = _DB, actor: Actor = _ACTOR
) -> InquiryItem:
    return change_status(db, actor, inquiry_id, body)


@router.post(
    "/{inquiry_id}/notes", status_code=201, response_model=InquiryEventItem, dependencies=_MANAGER
)
def post_note(
    inquiry_id: str, body: InquiryNoteRequest, db: Session = _DB, actor: Actor = _ACTOR
) -> InquiryEventItem:
    return add_note(db, actor, inquiry_id, body)


@router.get("/{inquiry_id}/events", response_model=InquiryEventsResponse, dependencies=_MANAGER)
def get_events(inquiry_id: str, db: Session = _DB, actor: Actor = _ACTOR) -> InquiryEventsResponse:
    return list_events(db, actor, inquiry_id)
