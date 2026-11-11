# ==============================================================================
# [파일 설명]
# 도입 신청 서비스 — 익명 접수(멱등)와 중앙 관리자 처리. (#552, 계약: schemas.api.signup_requests)
#
#   - 접수: 신청 INSERT 와 운영자 알림이 한 트랜잭션이다. 재시도(같은 키)는 알림을 만들지 않는다.
#     같은 키의 판정은 처음 저장된 검증 결과 값 다섯 칸(회사명 · 담당자명 · 이메일 · 전화 · 메모)과 이번 검증 결과 값의 비교다 — 원문 JSON 이나
#     이메일 소문자화 같은 정규화를 새로 얹지 않는다(값 규약은 스키마가 정한다).
#   - 재시도 키 ↔ 신청 매핑은 신청과 함께 남는다. 보존 · 삭제 기준(TTL)은 정하지 않았고, 공개 전
#     T15 에서 정해야 한다. 이 키는 스팸 방지도 이메일 소유 확인도 아니다.
#   - 처리: 중앙 관리자만(support_access 가 쓰기 직전 행위자를 다시 판정). 승인은 POST approve 하나다(#572 묶음 B) —
#     한 트랜잭션에서 고객사를 만들고(tenant_admin.create_tenant_uncommitted · PENDING_CONNECTION · 그룹 없음) 신청을
#     APPROVED 로 옮겨 그 고객사에 잇고 결정을 남긴 뒤, 응답까지 만들고서 commit 한다. 사용자 · Cognito · 이메일 발송은
#     만들지 않는다. PUT status 는 승인하지 않는다(409 SIGNUP_REQUEST_APPROVAL_REQUIRED).
#   - 접수 경로의 DB 오류는 분류하지 않고 내용도 싣지 않는다(개인정보 포함 가능) → 500 INTERNAL_ERROR.
# ==============================================================================

from __future__ import annotations

import logging
from datetime import UTC, datetime
from uuid import UUID

import tenant_admin
from auth.access import require_central_admin
from config import TenantSettings
from db import models
from db.repositories import operator_notifications
from db.repositories import signup_requests as repo
from db.repositories.platform_events import ANONYMOUS, person, record_platform_event
from exceptions import ApiError
from schemas.api.errors import ErrorCode
from schemas.api.signup_requests import (
    SignupRequestAccepted,
    SignupRequestApproveRequest,
    SignupRequestApproveResponse,
    SignupRequestCreate,
    SignupRequestDecisionItem,
    SignupRequestItem,
    SignupRequestsResponse,
    SignupRequestStatusRequest,
)
from schemas.api.support import (
    SUPPORT_DEFAULT_PAGE_SIZE,
    NotificationKind,
    SignupRequestStatus,
)
from schemas.api.tenants import TenantCreateRequest
from schemas.platform_events import PlatformEventAction
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from support_access import Actor, authorize, write_transaction

logger = logging.getLogger(__name__)

# PUT status 의 전이. 승인(APPROVED)은 여기 없다 — 고객사 생성과 한 트랜잭션인 approve 로만 간다.
_TRANSITIONS = {
    SignupRequestStatus.NEW: frozenset({SignupRequestStatus.IN_REVIEW, SignupRequestStatus.REJECTED}),
    SignupRequestStatus.IN_REVIEW: frozenset({SignupRequestStatus.REJECTED}),
}
# approve 를 받는 상태. 승인 · 반려된 신청은 고객사를 지워 tenant_id 가 비어도 다시 승인하지 않는다.
_APPROVABLE = frozenset({SignupRequestStatus.NEW, SignupRequestStatus.IN_REVIEW})


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
            record_platform_event(
                db, PlatformEventAction.SIGNUP_REQUEST_CREATE, ANONYMOUS, target_id=request_id
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


def _item(
    row: models.SignupRequest, decision: models.SignupRequestDecision | None = None
) -> SignupRequestItem:
    return SignupRequestItem(
        request_id=row.request_id,
        tenant_id=row.tenant_id,
        company_name=row.company_name,
        contact_name=row.contact_name,
        email=row.email,
        phone=row.phone,
        note=row.note,
        status=row.status,
        rejected_reason=row.rejected_reason,
        created_at=row.created_at,
        updated_at=row.updated_at,
        decision=(
            SignupRequestDecisionItem.model_validate(decision) if decision is not None else None
        ),
    )


def list_requests(
    db: Session, actor: Actor, *, page: int = 1, page_size: int = SUPPORT_DEFAULT_PAGE_SIZE
) -> SignupRequestsResponse:
    authorize(db, actor, require_central_admin)
    rows, total = repo.list_newest(db, page=page, page_size=page_size)
    return SignupRequestsResponse(
        items=[_item(row, decision) for row, decision in rows],
        page=page, page_size=page_size, total=total,
    )


def _stored_id(db: Session, actor: Actor, request_id: str) -> str:
    try:
        return str(UUID(request_id))
    except ValueError:
        # 형식이 틀린 id 도 없는 id 와 같다 — 권한 판정은 먼저 한다.
        authorize(db, actor, require_central_admin)
        raise ApiError(ErrorCode.SIGNUP_REQUEST_NOT_FOUND) from None


def change_status(
    db: Session, actor: Actor, request_id: str, body: SignupRequestStatusRequest
) -> SignupRequestItem:
    canonical_id = _stored_id(db, actor, request_id)
    with write_transaction(db, actor, require_central_admin) as user:
        row = repo.get_for_update(db, canonical_id)
        if row is None:
            raise ApiError(ErrorCode.SIGNUP_REQUEST_NOT_FOUND)
        if body.status is SignupRequestStatus.APPROVED:
            raise ApiError(ErrorCode.SIGNUP_REQUEST_APPROVAL_REQUIRED)
        if body.status not in _TRANSITIONS.get(row.status, frozenset()):
            raise ApiError(ErrorCode.SIGNUP_REQUEST_STATE_CONFLICT)
        row.status = body.status
        row.rejected_reason = body.reason
        row.updated_at = datetime.now(UTC)
        decision = None
        if body.status is SignupRequestStatus.REJECTED:
            decision = repo.add_decision(
                db,
                request_id=row.request_id,
                actor_user_id=user.user_id,
                actor_email=user.email,
                decided_at=row.updated_at,
                outcome=body.status,
                rejected_reason=body.reason,
            )
            record_platform_event(
                db,
                PlatformEventAction.SIGNUP_REQUEST_DECIDE,
                person(user),
                tenant_id=row.tenant_id,
                target_id=row.request_id,
                details={"decision": SignupRequestStatus.REJECTED.value},
            )
        db.flush()
        return _item(row, decision)


def approve(
    db: Session,
    actor: Actor,
    request_id: str,
    body: SignupRequestApproveRequest,
    settings: TenantSettings,
) -> SignupRequestApproveResponse:
    """승인 = 고객사 생성. 신청 잠금 → 상태 판정 → 고객사(commit 없는 생성 본체) → APPROVED · tenant_id · 결정 기록 →
    응답(온보딩 링크 포함)을 다 만든 뒤 write_transaction 이 한 번 commit 한다. 어디서 실패해도 전부 되돌린다."""
    canonical_id = _stored_id(db, actor, request_id)
    with write_transaction(db, actor, require_central_admin) as user:
        row = repo.get_for_update(db, canonical_id)
        if row is None:
            raise ApiError(ErrorCode.SIGNUP_REQUEST_NOT_FOUND)
        if row.status not in _APPROVABLE:
            raise ApiError(ErrorCode.SIGNUP_REQUEST_STATE_CONFLICT)
        now = actor.clock()
        created = tenant_admin.create_tenant_uncommitted(
            db,
            user,
            TenantCreateRequest(
                name=row.company_name, aws_account_id=body.aws_account_id, regions=body.regions
            ),
            settings,
            now=now,
        )
        row.status = SignupRequestStatus.APPROVED
        row.rejected_reason = None
        row.tenant_id = created.tenant.tenant_id
        row.updated_at = now
        decision = repo.add_decision(
            db,
            request_id=row.request_id,
            actor_user_id=user.user_id,
            actor_email=user.email,
            decided_at=now,
            outcome=SignupRequestStatus.APPROVED,
            rejected_reason=None,
        )
        record_platform_event(
            db,
            PlatformEventAction.SIGNUP_REQUEST_DECIDE,
            person(user),
            tenant_id=created.tenant.tenant_id,
            target_id=row.request_id,
            details={"decision": SignupRequestStatus.APPROVED.value},
        )
        return SignupRequestApproveResponse(request=_item(row, decision), tenant=created)
