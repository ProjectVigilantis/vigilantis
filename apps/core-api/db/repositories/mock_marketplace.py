# ==============================================================================
# [파일 설명]  담당: 안성일 (BE · T-16)
# 모의 Marketplace 저장소 — mock_marketplace_agreements · mock_marketplace_events (Post-MVP T-16 · #528)
#
#   - commit하지 않는다. 트랜잭션은 호출부(mock_marketplace · subscription_workflow)가 소유한다.
#   - 시각은 호출부가 now로 넘긴다(시계를 읽지 않는다).
#   - 계약 전이는 「읽은 revision · 기대 상태일 때만」 한 행을 바꾸는 조건부 UPDATE 다. 그 사이 다른 처리기 ·
#     고객 요청이 바꿨으면 0행(False)이고 다음 주기가 현재 값으로 다시 고른다.
#   - 이벤트는 전이와 같은 트랜잭션에 쓴다. 같은 전이의 두 번째 기록은 dedupe_key 유일성이 버린다.
#   - 식별자(buyer_ref · license_ref)는 서버가 만든 불투명한 모의 값이다 — 실제 AWS 계정 · ARN 이 아니다.
# ==============================================================================

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime

from schemas.subscriptions import (
    MOCK_AGREEMENT_OPEN_STATUSES,
    MockAgreementStatus,
    MockMarketplaceEventKind,
    SubscriptionSource,
)
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from .. import models

_A = models.MockMarketplaceAgreement
_E = models.MockMarketplaceEvent

_REF_BYTES = 16  # 32자 hex — DB CHECK buyer_ref_format · license_ref_format

#: 진행 중 계약의 유일성(테넌트당 하나) — IntegrityError 를 409 로 해석할 때 쓰는 이름(db.models)
OPEN_AGREEMENT_UNIQUE = "uq_mock_marketplace_agreements_open_tenant"


# --- 계약 ------------------------------------------------------------------------


def _agreements():
    # 다른 세션이 commit 한 전이를 identity map 의 낡은 값이 가리지 않게 DB 값으로 덮어 읽는다
    return select(_A).execution_options(populate_existing=True)


def get_agreement(db: Session, agreement_id: str) -> models.MockMarketplaceAgreement | None:
    return db.execute(_agreements().where(_A.agreement_id == agreement_id)).scalar_one_or_none()


def open_agreement(
    db: Session, tenant_id: str, *, for_update: bool = False
) -> models.MockMarketplaceAgreement | None:
    """진행 중(PENDING · ACTIVE)인 계약 — 테넌트당 하나뿐이다(부분 유일 인덱스)."""
    statement = _agreements().where(
        _A.tenant_id == tenant_id, _A.status.in_(MOCK_AGREEMENT_OPEN_STATUSES)
    )
    if for_update:
        statement = statement.with_for_update()
    return db.execute(statement).scalar_one_or_none()


def latest_agreement(db: Session, tenant_id: str) -> models.MockMarketplaceAgreement | None:
    """가장 최근에 요청한 계약. 진행 중 계약이 있으면 그것이다 — 새 구매는 앞 계약이 끝난 뒤에만 생긴다."""
    return db.execute(
        _agreements()
        .where(_A.tenant_id == tenant_id)
        .order_by(_A.requested_at.desc(), _A.agreement_id.desc())
        .limit(1)
    ).scalar_one_or_none()


def _buyer_ref(db: Session, tenant_id: str) -> str:
    """그 테넌트의 모의 구매자 참조 — 앞선 계약이 있으면 같은 값, 없으면 새로 만든다."""
    existing = db.scalar(
        select(_A.buyer_ref).where(_A.tenant_id == tenant_id).order_by(_A.requested_at).limit(1)
    )
    return existing or f"mock-buyer-{secrets.token_hex(_REF_BYTES)}"


def create_agreement(
    db: Session, *, tenant_id: str, plan: str, requested_by_user_id: str, now: datetime
) -> models.MockMarketplaceAgreement:
    """PENDING 계약을 만든다(flush 까지). 진행 중 계약이 이미 있으면 flush 에서 IntegrityError
    (OPEN_AGREEMENT_UNIQUE) — 호출부가 SAVEPOINT 안에서 부르고 409 로 해석한다."""
    row = _A(
        tenant_id=tenant_id,
        source=SubscriptionSource.MOCK_MARKETPLACE,
        buyer_ref=_buyer_ref(db, tenant_id),
        license_ref=f"mock-license-{secrets.token_hex(_REF_BYTES)}",
        plan=plan,
        status=MockAgreementStatus.PENDING,
        auto_renew=True,
        term_number=0,
        revision=1,
        requested_by_user_id=requested_by_user_id,
        requested_at=now,
        created_at=now,
        updated_at=now,
    )
    db.add(row)
    db.flush()
    return row


def transition(
    db: Session,
    agreement: models.MockMarketplaceAgreement,
    values: dict,
    *,
    expected_status: MockAgreementStatus,
    now: datetime,
    conditions=(),
) -> bool:
    """읽은 revision · 기대 상태(+ 추가 조건)일 때만 바꾸고 revision 을 1 올린다. 바뀌었으면 객체를 다시 읽는다."""
    result = db.execute(
        update(_A)
        .where(
            _A.agreement_id == agreement.agreement_id,
            _A.revision == agreement.revision,
            _A.status == expected_status,
            *conditions,
        )
        .values(**values, revision=agreement.revision + 1, updated_at=now),
        execution_options={"synchronize_session": False},
    )
    if result.rowcount != 1:
        return False
    db.refresh(agreement)
    return True


def pending_agreement_ids(db: Session) -> list[str]:
    return list(db.scalars(
        select(_A.agreement_id)
        .where(_A.status == MockAgreementStatus.PENDING)
        .order_by(_A.requested_at, _A.agreement_id)
    ))


def term_ended_agreement_ids(db: Session, *, now: datetime) -> list[str]:
    """활성 계약 중 현재 기간의 끝 시각이 지난 것 — 갱신 또는 만료 대상."""
    return list(db.scalars(
        select(_A.agreement_id)
        .where(_A.status == MockAgreementStatus.ACTIVE, _A.term_end <= now)
        .order_by(_A.term_end, _A.agreement_id)
    ))


# --- 이벤트 ------------------------------------------------------------------------


def record_event(
    db: Session,
    agreement: models.MockMarketplaceAgreement,
    kind: MockMarketplaceEventKind,
    *,
    occurred_at: datetime,
    now: datetime,
) -> str | None:
    """이벤트 1건 → event_id. 같은 전이(계약 · 종류 · 기간 번호)가 이미 기록됐으면 쓰지 않고 None."""
    return db.scalar(
        insert(_E)
        .values(
            agreement_id=agreement.agreement_id,
            source=SubscriptionSource.MOCK_MARKETPLACE,
            kind=kind,
            dedupe_key=f"{agreement.agreement_id}:{kind.value}:{agreement.term_number}",
            occurred_at=occurred_at,
            created_at=now,
        )
        .on_conflict_do_nothing(index_elements=["dedupe_key"])
        .returning(_E.event_id)
    )


@dataclass(frozen=True)
class ClaimedEvent:
    event_id: str
    tenant_id: str


def unprocessed_event_ids(db: Session, *, limit: int) -> list[str]:
    return list(db.scalars(
        select(_E.event_id)
        .where(_E.processed_at.is_(None))
        .order_by(_E.created_at, _E.event_id)
        .limit(limit)
    ))


def claim_event(db: Session, event_id: str) -> ClaimedEvent | None:
    """아직 처리되지 않은 이벤트를 잠근다(다른 처리기가 잡고 있으면 건너뛴다 · SKIP LOCKED)."""
    row = db.execute(
        select(_E.event_id, _A.tenant_id)
        .join(_A, _A.agreement_id == _E.agreement_id)
        .where(_E.event_id == event_id, _E.processed_at.is_(None))
        .with_for_update(of=_E, skip_locked=True)
    ).one_or_none()
    return None if row is None else ClaimedEvent(*row)


def mark_processed(db: Session, event_id: str, *, now: datetime) -> bool:
    result = db.execute(
        update(_E)
        .where(_E.event_id == event_id, _E.processed_at.is_(None))
        .values(processed_at=now),
        execution_options={"synchronize_session": False},
    )
    return result.rowcount == 1
