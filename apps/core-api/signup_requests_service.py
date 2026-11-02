# ==============================================================================
# [파일 설명]
# 도입 신청 서비스 — 익명 접수(멱등)와 중앙 관리자 처리. (#552, 계약: schemas.api.signup_requests)
#
#   - 접수: 신청 INSERT 와 운영자 알림이 한 트랜잭션이다. 재시도(같은 키)는 알림을 만들지 않는다.
#     같은 키의 판정은 처음 저장된 검증 결과 값 다섯 칸(회사명 · 담당자명 · 이메일 · 전화 · 메모)과 이번 검증 결과 값의 비교다 — 원문 JSON 이나
#     이메일 소문자화 같은 정규화를 새로 얹지 않는다(값 규약은 스키마가 정한다).
#   - 재시도 키 ↔ 신청 매핑은 신청과 함께 남는다. 보존 · 삭제 기준(TTL)은 정하지 않았고, 공개 전
#     T15 에서 정해야 한다. 이 키는 스팸 방지도 이메일 소유 확인도 아니다.
#   - 처리: 중앙 관리자만(support_access 가 쓰기 직전 행위자를 다시 판정). 승인은 판정 기록일 뿐
#     테넌트 · 사용자 · Cognito · 이메일 발송을 만들지 않는다.
#   - 접수 경로의 DB 오류는 분류하지 않고 내용도 싣지 않는다(개인정보 포함 가능) → 500 INTERNAL_ERROR.
# ==============================================================================

from __future__ import annotations

import logging
from datetime import UTC, datetime
from uuid import UUID

from auth.access import require_central_admin
from db import models
from db.repositories import operator_notifications
from db.repositories import signup_requests as repo
from exceptions import ApiError
from schemas.api.errors import ErrorCode
from schemas.api.signup_requests import (
    SignupRequestAccepted,
    SignupRequestCreate,
    SignupRequestItem,
    SignupRequestsResponse,
    SignupRequestStatusRequest,
)
from schemas.api.support import NotificationKind, SignupRequestStatus
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from support_access import Actor, authorize, write_transaction

logger = logging.getLogger(__name__)

_TRANSITIONS = {
    SignupRequestStatus.NEW: frozenset(
        {SignupRequestStatus.IN_REVIEW, SignupRequestStatus.APPROVED, SignupRequestStatus.REJECTED}
    ),
    SignupRequestStatus.IN_REVIEW: frozenset(
        {SignupRequestStatus.APPROVED, SignupRequestStatus.REJECTED}
    ),
}


def _same_submission(row: models.SignupRequest, body: SignupRequestCreate) -> bool:
    return (
        row.company_name == body.company_name
        and row.contact_name == body.contact_name
        and row.email == body.email
        and row.phone == body.phone
        and row.note == body.note
    )


def create(db: Session, idempotency_key: UUID, body: SignupRequestCreate) -> SignupRequestAccepted:
    """익명 접수. 키는 UUIDv4 이며 정본 문자열로 저장한다. 신규와 재시도 모두 같은 응답이다."""
    now = datetime.now(UTC)
    try:
        row, created = repo.insert(
            db,
            idempotency_key=str(idempotency_key),
            company_name=body.company_name,
            contact_name=body.contact_name,
            email=body.email,
            phone=body.phone,
            note=body.note,
            now=now,
        )
        if not created and not _same_submission(row, body):
            db.rollback()
            raise ApiError(ErrorCode.IDEMPOTENCY_KEY_CONFLICT)
        request_id = row.request_id
        if created:
            operator_notifications.add_notification(
                db,
                kind=NotificationKind.SIGNUP_REQUEST,
                target_id=request_id,
                tenant_id=None,
                subject=row.company_name,
                summary=row.contact_name,
                inquiry_category=None,
                created_at=now,
            )
        db.commit()
    except ApiError:
        raise
    except SQLAlchemyError as exc:
        db.rollback()
        logger.error("signup_request_storage_failed error_type=%s", type(exc).__name__)
        raise ApiError(ErrorCode.INTERNAL_ERROR) from None
    except BaseException:
        db.rollback()
        raise
    return SignupRequestAccepted(request_id=request_id)


def _item(row: models.SignupRequest) -> SignupRequestItem:
    return SignupRequestItem(
        request_id=row.request_id,
        company_name=row.company_name,
        contact_name=row.contact_name,
        email=row.email,
        phone=row.phone,
        note=row.note,
        status=row.status,
        rejected_reason=row.rejected_reason,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def list_requests(db: Session, actor: Actor) -> SignupRequestsResponse:
    authorize(db, actor, require_central_admin)
    return SignupRequestsResponse(items=[_item(r) for r in repo.list_newest(db)])


def change_status(
    db: Session, actor: Actor, request_id: str, body: SignupRequestStatusRequest
) -> SignupRequestItem:
    try:
        canonical_id = str(UUID(request_id))
    except ValueError:
        # 형식이 틀린 id 도 없는 id 와 같다 — 권한 판정은 먼저 한다.
        authorize(db, actor, require_central_admin)
        raise ApiError(ErrorCode.SIGNUP_REQUEST_NOT_FOUND) from None
    with write_transaction(db, actor, require_central_admin):
        row = repo.get_for_update(db, canonical_id)
        if row is None:
            raise ApiError(ErrorCode.SIGNUP_REQUEST_NOT_FOUND)
        if body.status not in _TRANSITIONS.get(row.status, frozenset()):
            raise ApiError(ErrorCode.SIGNUP_REQUEST_STATE_CONFLICT)
        row.status = body.status
        row.rejected_reason = body.reason
        row.updated_at = datetime.now(UTC)
        db.flush()
        return _item(row)
