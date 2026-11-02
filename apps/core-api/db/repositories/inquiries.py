# ==============================================================================
# 문의 저장소 — inquiries · inquiry_events. (#552)
#
#   - commit하지 않는다. 트랜잭션은 호출부(inquiries_service)가 소유한다. 쓰기는 flush까지 한다.
#   - 시각은 호출부가 now로 넘긴다.
#   - 범위(scope_ids)는 auth.access.tenant_scope_ids 의 값을 그대로 받는다: None = 전체, 튜플 = 그 테넌트들만
#     (빈 튜플 = 아무것도 없음). 범위 밖은 없는 것과 같다.
#   - 상태 전이는 「받는 상태일 때만」 바꾸는 조건부 UPDATE 다. 0행이면 False.
#   - inquiry_events 는 INSERT 만 한다(DB 트리거가 UPDATE · DELETE 를 거절한다).
# ==============================================================================

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from schemas.api.auth import UserRole
from schemas.api.support import InquiryCategory, InquiryEventAction, InquiryStatus
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from .. import models

_I = models.Inquiry
_E = models.InquiryEvent


@dataclass(frozen=True)
class InquiryRow:
    """응답의 원천 — 문의 행과 현재 조인 값(고객사 이름 · 작성자 이메일 · 역할)."""

    inquiry: models.Inquiry
    tenant_name: str
    author_email: str
    author_role: UserRole


def _select_rows(scope_ids: Sequence[str] | None):
    statement = (
        select(_I, models.Tenant.name, models.AppUser.email, models.AppUser.role)
        .join(models.Tenant, models.Tenant.tenant_id == _I.tenant_id)
        .join(models.AppUser, models.AppUser.user_id == _I.author_user_id)
        # 같은 요청 세션이 앞서 읽은 값이 아니라 DB 의 현재 값을 돌려준다
        .execution_options(populate_existing=True)
    )
    if scope_ids is not None:
        statement = statement.where(_I.tenant_id.in_(list(scope_ids)))
    return statement


def create_inquiry(
    db: Session,
    *,
    tenant_id: str,
    author_user_id: str,
    category: InquiryCategory,
    title: str,
    body: str,
    occurred_at: datetime | None,
    page_path: str | None,
    now: datetime,
) -> models.Inquiry:
    row = _I(
        tenant_id=tenant_id,
        author_user_id=author_user_id,
        category=category,
        title=title,
        body=body,
        occurred_at=occurred_at,
        page_path=page_path,
        status=InquiryStatus.OPEN,
        created_at=now,
        updated_at=now,
    )
    db.add(row)
    db.flush()
    return row


def list_inquiries(db: Session, *, scope_ids: Sequence[str] | None) -> list[InquiryRow]:
    """최신순 · 같은 시각은 inquiry_id 내림차순."""
    if scope_ids is not None and not scope_ids:
        return []
    statement = _select_rows(scope_ids).order_by(_I.created_at.desc(), _I.inquiry_id.desc())
    return [InquiryRow(*row) for row in db.execute(statement).all()]


def get_inquiry(
    db: Session, inquiry_id: str, *, scope_ids: Sequence[str] | None
) -> InquiryRow | None:
    if scope_ids is not None and not scope_ids:
        return None
    row = db.execute(_select_rows(scope_ids).where(_I.inquiry_id == inquiry_id)).first()
    return None if row is None else InquiryRow(*row)


def lock_inquiry(
    db: Session, inquiry_id: str, *, scope_ids: Sequence[str] | None
) -> models.Inquiry | None:
    """범위 안의 문의 행을 FOR UPDATE 로 잡는다 — 같은 문의의 전이 · 이력 추가를 줄 세운다."""
    if scope_ids is not None and not scope_ids:
        return None
    statement = (
        select(_I)
        .where(_I.inquiry_id == inquiry_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if scope_ids is not None:
        statement = statement.where(_I.tenant_id.in_(list(scope_ids)))
    return db.scalars(statement).one_or_none()


def transition_status(
    db: Session,
    inquiry_id: str,
    *,
    from_status: InquiryStatus,
    to_status: InquiryStatus,
    now: datetime,
) -> bool:
    result = db.execute(
        update(_I)
        .where(_I.inquiry_id == inquiry_id, _I.status == from_status)
        .values(status=to_status, updated_at=now)
        .execution_options(synchronize_session=False)
    )
    return result.rowcount == 1


def latest_event_at(db: Session, inquiry_id: str) -> datetime | None:
    return db.scalar(select(func.max(_E.occurred_at)).where(_E.inquiry_id == inquiry_id))


def add_event(
    db: Session,
    *,
    inquiry_id: str,
    actor_user_id: str,
    action: InquiryEventAction,
    before_status: InquiryStatus | None,
    after_status: InquiryStatus | None,
    note: str | None,
    now: datetime,
) -> models.InquiryEvent:
    row = _E(
        inquiry_id=inquiry_id,
        actor_user_id=actor_user_id,
        action=action,
        before_status=before_status,
        after_status=after_status,
        note=note,
        occurred_at=now,
    )
    db.add(row)
    db.flush()
    return row


def list_events(db: Session, inquiry_id: str) -> list[tuple[models.InquiryEvent, str]]:
    """(이력, 행위자의 현재 이메일) — 오래된 순 · 같은 시각은 event_id 순."""
    statement = (
        select(_E, models.AppUser.email)
        .join(models.AppUser, models.AppUser.user_id == _E.actor_user_id)
        .where(_E.inquiry_id == inquiry_id)
        .order_by(_E.occurred_at, _E.event_id)
    )
    return [(event, email) for event, email in db.execute(statement).all()]
