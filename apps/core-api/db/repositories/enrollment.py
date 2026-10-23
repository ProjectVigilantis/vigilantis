# ==============================================================================
# [파일 설명]
# 플랫폼 계정 등록 링크 저장소 — platform_enrollments. (#492)
#
#   - commit하지 않는다. 트랜잭션은 호출부(auth.enrollment · auth.bootstrap_enrollment)가 소유한다.
#   - 링크는 token_hash(SHA-256 hex)로만 찾는다. 원본 토큰은 이 계층에 들어오지 않는다.
#   - 상태 전이는 받는 상태일 때만 한 행을 바꾸는 조건부 UPDATE다(ISSUED → RESERVED,
#     ISSUED · RESERVED → REVOKED). 0행이면 그 사이 다른 요청 · 운영자가 바꿨거나 만료됐다.
#     RESERVED → REDEEMED 는 호출부가 lock 으로 잡은 행에서 바꾼다.
#   - 시각은 호출부가 now로 넘긴다(시계를 읽지 않는다).
# ==============================================================================

from __future__ import annotations

from datetime import datetime

from sqlalchemy import exists, select, update
from sqlalchemy.orm import Session

from .. import models

_E = models.PlatformEnrollment
_S = models.PlatformEnrollmentStatus


def add(
    db: Session, *, enrollment_id: str, token_hash: str, now: datetime, expires_at: datetime
) -> models.PlatformEnrollment:
    """ISSUED 링크 1건. 해시 중복 · expires_at <= now 는 DB가 flush에서 거절한다."""
    row = _E(
        enrollment_id=enrollment_id,
        token_hash=token_hash,
        status=_S.ISSUED,
        created_at=now,
        expires_at=expires_at,
    )
    db.add(row)
    db.flush()
    return row


def get(db: Session, enrollment_id: str) -> models.PlatformEnrollment | None:
    return db.get(_E, enrollment_id, populate_existing=True)


def list_all(db: Session) -> list[models.PlatformEnrollment]:
    return list(
        db.scalars(
            select(_E)
            .order_by(_E.created_at, _E.enrollment_id)
            .execution_options(populate_existing=True)
        )
    )


def is_redeemable(db: Session, *, token_hash: str, now: datetime) -> bool:
    """ISSUED 이고 만료 전인 링크인지(읽기만)."""
    return bool(
        db.scalar(
            select(
                exists().where(
                    _E.token_hash == token_hash,
                    _E.status == _S.ISSUED,
                    _E.expires_at > now,
                )
            )
        )
    )


def reserve(db: Session, *, token_hash: str, email: str, now: datetime) -> str | None:
    """ISSUED · 만료 전인 링크를 RESERVED 로 잡는다 → enrollment_id. 잡지 못하면 None.

    같은 링크의 동시 요청은 행 잠금 뒤 조건을 다시 평가하므로 하나만 잡는다.
    """
    return db.execute(
        update(_E)
        .where(_E.token_hash == token_hash, _E.status == _S.ISSUED, _E.expires_at > now)
        .values(status=_S.RESERVED, reserved_at=now, email=email)
        .returning(_E.enrollment_id)
        .execution_options(synchronize_session=False)
    ).scalar_one_or_none()


def lock(db: Session, enrollment_id: str) -> models.PlatformEnrollment | None:
    """행을 FOR UPDATE 로 잡고 DB 값으로 읽는다 — 트랜잭션 끝까지 회수와 겹치지 않는다."""
    return db.get(_E, enrollment_id, with_for_update=True, populate_existing=True)


def revoke(db: Session, *, enrollment_id: str, now: datetime) -> bool:
    """ISSUED · RESERVED 링크만 회수한다. 사용된(REDEEMED) · 이미 회수된 링크는 False."""
    result = db.execute(
        update(_E)
        .where(
            _E.enrollment_id == enrollment_id,
            _E.status.in_((_S.ISSUED, _S.RESERVED)),
        )
        .values(status=_S.REVOKED, revoked_at=now)
        .execution_options(synchronize_session=False)
    )
    return result.rowcount == 1
