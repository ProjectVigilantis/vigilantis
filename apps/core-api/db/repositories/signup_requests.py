# ==============================================================================
# [파일 설명]
# 도입 신청 저장소 — SignupRequest. (#552)
#
#   - 재시도 키 유일 제약(uq_signup_requests_idempotency_key)이 동시 접수의 관문이다. insert 는 savepoint 안에서
#     flush 해 그 제약 위반만 "이미 있음"으로 읽고, 다른 DB 오류는 그대로 올린다(분류 · 노출하지 않는다).
#   - 같은 이메일의 처리 중 · 승인 신청에 묶여 새 신청을 만들지 않은 접수는 SignupRequestDuplicate 에 키와
#     검증 결과 값을 남긴다. 그 키의 재시도도 신청 행의 키와 같은 규칙(같은 값 202 · 다른 값 409)을 따르게 하기 위해서다.
#   - commit 은 하지 않는다 — 알림 기록과 한 트랜잭션으로 묶는 서비스가 정한다.
# ==============================================================================

from __future__ import annotations

from datetime import datetime

from schemas.api.support import SUPPORT_DEFAULT_PAGE_SIZE, SignupRequestStatus
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from db import models

KEY_CONSTRAINT = "uq_signup_requests_idempotency_key"


def _violates_key_constraint(exc: IntegrityError) -> bool:
    diag = getattr(exc.orig, "diag", None)
    return getattr(diag, "constraint_name", None) == KEY_CONSTRAINT


def get_by_key(db: Session, idempotency_key: str) -> models.SignupRequest | None:
    return db.scalars(
        select(models.SignupRequest)
        .where(models.SignupRequest.idempotency_key == idempotency_key)
        .execution_options(populate_existing=True)
    ).one_or_none()


# 같은 이메일로 새 신청을 받지 않는 상태 — 반려된 신청만 다시 낼 수 있다
_OPEN_STATUSES = (SignupRequestStatus.NEW, SignupRequestStatus.IN_REVIEW, SignupRequestStatus.APPROVED)


# 접수의 트랜잭션 advisory lock 네임스페이스. 두 인자(int4, int4) 꼴은 한 인자(bigint) 꼴과 키 공간이 따로라
# 수집 잡 · 알림 설정의 bigint 키와 겹치지 않고, 실행 접수(repositories/executions.py 의 59510)와도 값이 다르다.
# 값은 이 용도로 고정한 임의 상수다. hashtext 가 겹치면 무관한 두 접수가 잠깐 직렬화될 뿐 판정은 틀리지 않는다.
_KEY_LOCK_NAMESPACE = 55201
_EMAIL_LOCK_NAMESPACE = 55202


def lock_key(db: Session, idempotency_key: str) -> None:
    """같은 재시도 키의 접수를 트랜잭션 끝까지 한 줄로 세운다. 이메일 잠금보다 먼저 잡는다(잡는 순서가 늘 같아 서로 기다리지 않는다).

    키가 신청 행과 중복 접수 행 두 표에 나뉘어 저장되므로, 같은 키 · 다른 이메일의 동시 접수가 두 표에 하나씩 쓰지 않게 막는다.
    """
    db.execute(select(func.pg_advisory_xact_lock(_KEY_LOCK_NAMESPACE, func.hashtext(idempotency_key))))


def lock_email(db: Session, email: str) -> None:
    """같은 이메일(대소문자 무시)의 접수를 트랜잭션 끝까지 한 줄로 세운다. 행이 없어도 잠근다."""
    db.execute(
        select(func.pg_advisory_xact_lock(_EMAIL_LOCK_NAMESPACE, func.hashtext(func.lower(email))))
    )


def get_duplicate_by_key(db: Session, idempotency_key: str) -> models.SignupRequestDuplicate | None:
    return db.scalars(
        select(models.SignupRequestDuplicate)
        .where(models.SignupRequestDuplicate.idempotency_key == idempotency_key)
        .execution_options(populate_existing=True)
    ).one_or_none()


def add_duplicate(
    db: Session,
    *,
    idempotency_key: str,
    request_id: str,
    company_name: str,
    contact_name: str,
    email: str,
    phone: str | None,
    note: str | None,
    now: datetime,
) -> None:
    """새 신청을 만들지 않은 접수의 키와 검증 결과 값을 남긴다. 호출 전에 lock_key 로 그 키를 잠가 둔다."""
    db.add(
        models.SignupRequestDuplicate(
            idempotency_key=idempotency_key,
            request_id=request_id,
            company_name=company_name,
            contact_name=contact_name,
            email=email,
            phone=phone,
            note=note,
            created_at=now,
        )
    )
    db.flush()


def find_open_by_email(db: Session, email: str) -> models.SignupRequest | None:
    """같은 이메일(대소문자 무시)로 처리 중이거나 승인된 신청 중 가장 이른 것."""
    return db.scalars(
        select(models.SignupRequest)
        .where(
            func.lower(models.SignupRequest.email) == email.lower(),
            models.SignupRequest.status.in_(_OPEN_STATUSES),
        )
        .order_by(models.SignupRequest.created_at, models.SignupRequest.request_id)
        .limit(1)
    ).one_or_none()


def insert(
    db: Session,
    *,
    idempotency_key: str,
    company_name: str,
    contact_name: str,
    email: str,
    phone: str | None,
    note: str | None,
    now: datetime,
) -> tuple[models.SignupRequest, bool]:
    """신청을 만든다 → (행, 새로 만들었는가). 같은 키가 이미 있으면 그 행과 False 다."""
    row = models.SignupRequest(
        idempotency_key=idempotency_key,
        company_name=company_name,
        contact_name=contact_name,
        email=email,
        phone=phone,
        note=note,
        status=SignupRequestStatus.NEW,
        created_at=now,
        updated_at=now,
    )
    try:
        with db.begin_nested():
            db.add(row)
            db.flush()
    except IntegrityError as exc:
        if not _violates_key_constraint(exc):
            raise
        existing = get_by_key(db, idempotency_key)
        if existing is None:
            raise
        return existing, False
    return row, True


def list_newest(
    db: Session, *, page: int = 1, page_size: int = SUPPORT_DEFAULT_PAGE_SIZE
) -> tuple[list[tuple[models.SignupRequest, models.SignupRequestDecision | None]], int]:
    total = db.scalar(select(func.count()).select_from(models.SignupRequest))
    offset = (page - 1) * page_size
    if offset >= total:
        return [], total
    rows = db.execute(
        select(models.SignupRequest, models.SignupRequestDecision)
        .outerjoin(
            models.SignupRequestDecision,
            models.SignupRequestDecision.request_id == models.SignupRequest.request_id,
        )
        .order_by(
                models.SignupRequest.created_at.desc(), models.SignupRequest.request_id.desc()
        )
        .offset(offset)
        .limit(page_size)
    )
    return [(request, decision) for request, decision in rows], total


def add_decision(
    db: Session,
    *,
    request_id: str,
    actor_user_id: str,
    actor_email: str,
    decided_at: datetime,
    outcome: SignupRequestStatus,
    rejected_reason: str | None,
) -> models.SignupRequestDecision:
    decision = models.SignupRequestDecision(
        request_id=request_id,
        actor_user_id=actor_user_id,
        actor_email=actor_email,
        decided_at=decided_at,
        outcome=outcome,
        rejected_reason=rejected_reason,
    )
    db.add(decision)
    db.flush()
    return decision


def get_for_update(db: Session, request_id: str) -> models.SignupRequest | None:
    return db.scalars(
        select(models.SignupRequest)
        .where(models.SignupRequest.request_id == request_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).one_or_none()
