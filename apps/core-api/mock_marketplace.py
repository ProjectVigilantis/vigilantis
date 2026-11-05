# ==============================================================================
# [파일 설명]  담당: 안성일 (BE · T-16)
# 모의 Marketplace 공급자(Post-MVP T-16 · #528) — 유료 권리의 권위 있는 원천을 서버 안에서 흉내 낸다.
#
#   - AWS Marketplace SaaS 계약(월 단위 계약형)의 **원천 경계**를 흉내 낸 것이다: 구매 요청 → 공급자의 활성화 →
#     계약 변경 이벤트(LicenseUpdated 류) → 소비자가 현재 권리를 다시 읽음(GetEntitlements 류). 실제 AWS 를
#     부르지 않고, 메시지 형식 · 날짜 규칙이 실제 Marketplace 와 같다고 주장하지 않는다.
#   - 계약 상태는 여기만 바꾼다. 고객이 고를 수 있는 것은 요금제(구매)와 자동 갱신 끄기뿐이고, 활성화 결과 ·
#     날짜 · 금액 · 구매자 식별자는 서버가 정한다. 임의 이벤트를 주입하는 공개 경로는 없다.
#   - 활성화 · 갱신 · 만료(advance)는 계약 처리 주기(subscription_workflow.process_subscription_cycle)만 부른다.
#     테넌트의 서비스 상태(정지 · 연결 전)와 새 구매 스위치는 보지 않는다 — 계약은 서비스 운영과 따로 흐른다.
#   - 트랜잭션: 고객 요청 경로는 호출부가 소유하고, advance 는 계약 1건마다 commit 한다(한 건의 장애가 다른
#     계약을 막지 않는다). 전이는 revision 조건부 UPDATE 라 겹친 처리기 · 재실행이 같은 전이를 두 번 하지 않는다.
# ==============================================================================

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Literal

from db import models
from db.repositories import mock_marketplace as repo
from schemas.api.subscriptions import MOCK_ACTIVATION_FAILED
from schemas.subscriptions import (
    MockAgreementStatus,
    MockMarketplaceEventKind,
    service_date,
    term_end_after,
)
from sqlalchemy.orm import Session

logger = logging.getLogger("vigilantis.subscriptions")

ActivationOutcome = Literal["SUCCEED", "FAIL", "HOLD"]
_A = models.MockMarketplaceAgreement


@dataclass(frozen=True)
class Entitlement:
    """지금 유효한 권리 1건(GetEntitlements 류의 읽기 결과) — 활성 계약의 현재 기간 안일 때만 있다."""

    agreement_id: str
    plan: str
    term_start: datetime
    term_end: datetime
    auto_renew: bool
    anniversary_day: int


def entitlement_of(
    agreement: models.MockMarketplaceAgreement | None, *, now: datetime
) -> Entitlement | None:
    """진행 중 계약(repo.open_agreement)의 지금 권리. 대기 · 실패 · 만료 계약과 기간 끝 시각이 지난(아직 갱신 전)
    계약은 권리가 없다 — 갱신을 기다리는 중인지는 renewal_pending 이 따로 판정한다."""
    if (
        agreement is None
        or agreement.status is not MockAgreementStatus.ACTIVE
        or not agreement.term_start <= now < agreement.term_end
    ):
        return None
    return Entitlement(
        agreement_id=agreement.agreement_id,
        plan=agreement.plan,
        term_start=agreement.term_start,
        term_end=agreement.term_end,
        auto_renew=agreement.auto_renew,
        anniversary_day=agreement.anniversary_day,
    )


def renewal_pending(agreement: models.MockMarketplaceAgreement | None, *, now: datetime) -> bool:
    """자동 갱신 중인 활성 계약의 기간 끝 시각이 지났고 아직 다음 기간이 이어지지 않았다 — 처리 주기의 갱신 대상이다.

    권리가 끝난 것이 아니라 공급자의 갱신을 기다리는 중이다(_roll_term 이 이 판정으로 갱신한다). 갱신을 끈 계약은
    들지 않는다 — 그 계약의 권리는 기간 끝 시각에 끝난다."""
    return (
        agreement is not None
        and agreement.status is MockAgreementStatus.ACTIVE
        and agreement.auto_renew
        and agreement.term_end <= now
    )


# --- 고객 요청 (호출부가 인가 · 테넌트 잠금을 마친 뒤 부른다) ---------------------------


def request_purchase(
    db: Session, tenant_id: str, *, plan: str, requested_by_user_id: str, now: datetime
) -> models.MockMarketplaceAgreement:
    """구매 요청 = PENDING 계약. 권리는 없다. 진행 중 계약이 있으면 flush 에서 IntegrityError(호출부가 409)."""
    return repo.create_agreement(
        db, tenant_id=tenant_id, plan=plan, requested_by_user_id=requested_by_user_id, now=now
    )


def disable_renewal(db: Session, tenant_id: str, *, now: datetime) -> str | None:
    """활성 계약의 자동 갱신을 끈다 → 이벤트 ID. 권리는 지금 기간의 끝 시각까지 그대로다(해지가 아니다).

    자동 갱신 중이고 기간 끝 시각 전인 활성 계약이 없으면 None(쓴 것이 없다). 계약 행을 잠가 같은 계약의
    갱신 처리와 줄을 세운다 — 그 사이 갱신됐으면 새 기간의 갱신을 끈다.
    """
    agreement = repo.open_agreement(db, tenant_id, for_update=True)
    if (
        agreement is None
        or agreement.status is not MockAgreementStatus.ACTIVE
        or not agreement.auto_renew
        or agreement.term_end <= now
    ):
        return None
    if not repo.transition(
        db,
        agreement,
        {"auto_renew": False, "renewal_disabled_at": now},
        expected_status=MockAgreementStatus.ACTIVE,
        now=now,
        conditions=(_A.auto_renew.is_(True), _A.term_end > now),
    ):
        return None
    event_id = repo.record_event(
        db, agreement, MockMarketplaceEventKind.RENEWAL_DISABLED, occurred_at=now, now=now
    )
    if event_id is None:  # 자동 갱신은 한 번만 꺼진다 — 같은 키가 이미 있으면 저장값이 어긋난 것이다
        raise RuntimeError("renewal_disabled event already recorded")
    return event_id


# --- 공급자 처리 (계약 처리 주기) ------------------------------------------------------


@dataclass
class ProviderReport:
    activated: int = 0
    activation_failed: int = 0
    #: HOLD 설정으로 대기에 둔 구매 수.
    held: int = 0
    renewed: int = 0
    expired: int = 0
    #: 조회와 쓰기 사이에 바뀌어(고객 요청 · 다른 처리기) 0행이었던 건 — 다음 주기가 현재 값으로 다시 고른다.
    skipped: int = 0
    errored: int = 0


def _activate(db: Session, agreement: models.MockMarketplaceAgreement, now: datetime) -> bool:
    anniversary_day = service_date(now).day
    if not repo.transition(
        db,
        agreement,
        {
            "status": MockAgreementStatus.ACTIVE,
            "activated_at": now,
            "term_start": now,
            "term_end": term_end_after(anniversary_day, now),
            "anniversary_day": anniversary_day,
            "term_number": 1,
        },
        expected_status=MockAgreementStatus.PENDING,
        now=now,
    ):
        return False
    repo.record_event(db, agreement, MockMarketplaceEventKind.ACTIVATED, occurred_at=now, now=now)
    return True


def _fail(db: Session, agreement: models.MockMarketplaceAgreement, now: datetime) -> bool:
    if not repo.transition(
        db,
        agreement,
        {"status": MockAgreementStatus.FAILED, "ended_at": now, "failure_code": MOCK_ACTIVATION_FAILED},
        expected_status=MockAgreementStatus.PENDING,
        now=now,
    ):
        return False
    repo.record_event(
        db, agreement, MockMarketplaceEventKind.ACTIVATION_FAILED, occurred_at=now, now=now
    )
    return True


def _renew(db: Session, agreement: models.MockMarketplaceAgreement, now: datetime) -> bool:
    """원래 기념일로 다음 기간을 잇는다. 처리를 놓친 기간이 있으면 지금을 포함하는 기간까지 잇는다(기간이
    겹치거나 비지 않는다 — 각 기간은 앞 기간의 끝 시각에 시작한다)."""
    start, number = agreement.term_end, agreement.term_number
    while True:
        end = term_end_after(agreement.anniversary_day, start)
        number += 1
        if end > now:
            break
        start = end
    if not repo.transition(
        db,
        agreement,
        {"term_start": start, "term_end": end, "term_number": number},
        expected_status=MockAgreementStatus.ACTIVE,
        now=now,
        conditions=(_A.auto_renew.is_(True),),
    ):
        return False
    repo.record_event(db, agreement, MockMarketplaceEventKind.RENEWED, occurred_at=start, now=now)
    return True


def _expire(db: Session, agreement: models.MockMarketplaceAgreement, now: datetime) -> bool:
    """자동 갱신을 끈 계약은 기간 끝 시각에 끝난다 — 만료 시각은 처리 시각이 아니라 그 끝 시각이다."""
    ended_at = agreement.term_end
    if not repo.transition(
        db,
        agreement,
        {"status": MockAgreementStatus.EXPIRED, "ended_at": ended_at},
        expected_status=MockAgreementStatus.ACTIVE,
        now=now,
        conditions=(_A.auto_renew.is_(False),),
    ):
        return False
    repo.record_event(db, agreement, MockMarketplaceEventKind.EXPIRED, occurred_at=ended_at, now=now)
    return True


def _settle_pending(
    db: Session, agreement: models.MockMarketplaceAgreement, outcome: ActivationOutcome, now: datetime
) -> str | None:
    """대기 구매 1건 → 보고 칸 이름(바뀐 것이 없으면 None)."""
    if agreement.status is not MockAgreementStatus.PENDING:
        return None
    if outcome == "SUCCEED":
        return "activated" if _activate(db, agreement, now) else None
    return "activation_failed" if _fail(db, agreement, now) else None


def _roll_term(db: Session, agreement: models.MockMarketplaceAgreement, now: datetime) -> str | None:
    """기간 끝 시각이 지난 활성 계약 1건 → 보고 칸 이름(바뀐 것이 없으면 None)."""
    if renewal_pending(agreement, now=now):
        return "renewed" if _renew(db, agreement, now) else None
    if agreement.status is not MockAgreementStatus.ACTIVE or agreement.term_end > now:
        return None
    return "expired" if _expire(db, agreement, now) else None


def _run(db: Session, agreement_ids: list[str], step, report: ProviderReport) -> None:
    for agreement_id in agreement_ids:
        try:
            agreement = repo.get_agreement(db, agreement_id)
            field = None if agreement is None else step(agreement)
            db.commit()
        except Exception:
            db.rollback()
            report.errored += 1
            logger.exception("mock_marketplace_step_failed", extra={"agreement_id": agreement_id})
            continue
        if field is None:
            report.skipped += 1
        else:
            setattr(report, field, getattr(report, field) + 1)


def advance(db: Session, *, outcome: ActivationOutcome, now: datetime) -> ProviderReport:
    """대기 구매를 설정된 결과로 처리하고, 기간 끝 시각이 지난 활성 계약을 갱신 · 만료한다.

    대상은 매번 DB 에서 고른다 — 재실행 · 재기동에 같은 결과이고, 이미 처리한 계약은 상태 · 기간이 바뀌어 빠진다.
    """
    report = ProviderReport()
    pending = repo.pending_agreement_ids(db)
    ended = repo.term_ended_agreement_ids(db, now=now)
    db.rollback()  # 조회 트랜잭션을 닫는다 — 건별 쓰기만 짧게 연다
    if outcome == "HOLD":
        report.held = len(pending)
    else:
        _run(db, pending, lambda agreement: _settle_pending(db, agreement, outcome, now), report)
    _run(db, ended, lambda agreement: _roll_term(db, agreement, now), report)
    logger.info("mock_marketplace_cycle", extra=asdict(report))
    return report
