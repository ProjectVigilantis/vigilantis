# ==============================================================================
# [파일 설명]
# 도입 신청 저장소 — SignupRequest. (#552)
#
#   - 재시도 키 유일 제약(uq_signup_requests_idempotency_key)이 동시 접수의 관문이다. insert 는 savepoint 안에서
#     flush 해 그 제약 위반만 "이미 있음"으로 읽고, 다른 DB 오류는 그대로 올린다(분류 · 노출하지 않는다).
#   - commit 은 하지 않는다 — 알림 기록과 한 트랜잭션으로 묶는 서비스가 정한다.
# ==============================================================================

from __future__ import annotations

from datetime import datetime

from schemas.api.support import SignupRequestStatus
from sqlalchemy import select
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


def list_newest(db: Session) -> list[models.SignupRequest]:
    return list(
        db.scalars(
            select(models.SignupRequest).order_by(
                models.SignupRequest.created_at.desc(), models.SignupRequest.request_id.desc()
            )
        )
    )


def get_for_update(db: Session, request_id: str) -> models.SignupRequest | None:
    return db.scalars(
        select(models.SignupRequest)
        .where(models.SignupRequest.request_id == request_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).one_or_none()
