# ==============================================================================
# [파일 설명]  담당: 안성일 (BE · T-16)
# 고객 구독(Post-MVP T-16 · #528)의 판정 · 트랜잭션 · 투영 · 응답 조립 (계약: schemas.api.subscriptions)
#
#   - 결제는 없다. 유료 권리의 권위 있는 원천은 모의 Marketplace 계약(mock_marketplace)이고, 테넌트의 구독 칸은
#     그 **현재** 계약을 다시 읽어 맞춘 투영이다(reconcile_tenant_subscription). 이벤트 내용을 옮겨 적지 않는다.
#   - 호출부는 인증 · 인가를 마친 뒤 고객 요청 함수를 불러야 한다. `tenant_id` 는 세션의 소속이고,
#     `can_manage`(고객사 중앙계정)는 인가의 결과다. 이 워크플로는 인증 · 인가를 대신하지 않는다.
#   - 서비스 상태와 계약은 따로다. 이용 중단(stop_service)은 테넌트 정지와 같은 전이(tenants_repo.suspend_tenant)이고
#     계약을 해지하지 않는다. 정지 중에도 계약 처리 · 투영 · 갱신 끄기는 계속되고, 재개(tenants_repo.resume_tenant)는
#     구독을 바꾸지 않는다.
#   - 계약 처리 주기(process_subscription_cycle)는 subscription_scheduler 가 부른다: 모의 공급자의 활성화 · 갱신 ·
#     만료 → 이벤트 소비(투영). 예정일은 처리 완료가 아니다 — 그 시각 뒤 첫 주기에 저장된다.
#   - 잠금 순서는 테넌트 행 → 계약 행이다(db.repositories.tenants 머리말). 구매만 그 앞에 사용자 표를 잠근다
#     (purchase_subscription). 이벤트 소비는 이벤트 행 → 테넌트 행이고 계약 행은 잠그지 않는다.
#   - 거절은 기존 코드다: 고객사가 없으면 404 TENANT_NOT_FOUND, 지금 상태에서 할 수 없으면 409 TENANT_STATE_CONFLICT.
# ==============================================================================

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime

import mock_marketplace
from config import SubscriptionSettings
from db import models
from db.repositories import auth as auth_repo
from db.repositories import mock_marketplace as mock_repo
from db.repositories import tenants as tenants_repo
from exceptions import ApiError
from schemas.api.errors import ErrorCode
from schemas.api.subscriptions import (
    CustomerSubscriptionResponse,
    MockAgreement,
    SubscriptionAction,
    SubscriptionPurchaseRequest,
    SubscriptionStopRequest,
)
from schemas.api.tenants import SUSPENDABLE_STATUSES, TenantStatus, TenantSubscription
from schemas.subscriptions import (
    FREE_PLAN,
    MOCK_AGREEMENT_OPEN_STATUSES,
    MockAgreementStatus,
    SubscriptionSource,
    service_date,
    term_dates,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from tenant_admin import _commit_after as commit_after
from tenant_admin import subscription_of

logger = logging.getLogger("vigilantis.subscriptions")

_MOCK = SubscriptionSource.MOCK_MARKETPLACE


# --- 응답 조립 ------------------------------------------------------------------


def _agreement(row: models.MockMarketplaceAgreement | None) -> MockAgreement | None:
    if row is None:
        return None
    return MockAgreement(
        agreement_id=row.agreement_id,
        source=row.source.value,
        plan=row.plan,
        status=row.status,
        auto_renew=row.auto_renew,
        requested_at=row.requested_at,
        activated_at=row.activated_at,
        term_start=row.term_start,
        term_end=row.term_end,
        renewal_disabled_at=row.renewal_disabled_at,
        ended_at=row.ended_at,
        failure_code=row.failure_code,
    )


def _available_actions(
    tenant: models.Tenant,
    agreement: models.MockMarketplaceAgreement | None,
    *,
    settings: SubscriptionSettings,
    now: datetime,
) -> list[SubscriptionAction]:
    """쓰기 경로의 받는 조건과 같은 판정(화면 편의). 실제 차단은 쓰기 경로가 한다."""
    actions = []
    in_progress = agreement is not None and agreement.status in MOCK_AGREEMENT_OPEN_STATUSES
    if (
        settings.SUBSCRIPTION_MOCK_MARKETPLACE_ENABLED
        and tenant.status is TenantStatus.ACTIVE
        and tenant.plan == FREE_PLAN
        and tenant.subscription_source is not _MOCK
        and not in_progress
    ):
        actions.append(SubscriptionAction.PURCHASE)
    if (
        agreement is not None
        and agreement.status is MockAgreementStatus.ACTIVE
        and agreement.auto_renew
        and agreement.term_end > now
    ):
        actions.append(SubscriptionAction.CANCEL_RENEWAL)
    if tenant.status in SUSPENDABLE_STATUSES:
        actions.append(SubscriptionAction.STOP)
    return actions


def customer_subscription(
    tenant: models.Tenant,
    agreement: models.MockMarketplaceAgreement | None,
    *,
    can_manage: bool,
    settings: SubscriptionSettings,
    now: datetime,
) -> CustomerSubscriptionResponse:
    """저장된 투영 + 가장 최근 계약 + 예정일. 예정일은 모의 계약 투영에만 있고 서비스 상태와 무관하다."""
    next_renewal = expiry = None
    if tenant.subscription_source is _MOCK:
        # 권리 끝 시각은 다음 기념일 00시(KST)라 그 날짜가 갱신 · 만료일이다(DB CHECK entitlement_end_with_mock)
        scheduled = service_date(tenant.entitlement_ends_at)
        if tenant.cancel_at_period_end:
            expiry = scheduled
        else:
            next_renewal = scheduled
    return CustomerSubscriptionResponse(
        tenant_name=tenant.name,
        service_status=tenant.status,
        subscription=subscription_of(tenant),
        source=tenant.subscription_source,
        entitlement_ends_at=tenant.entitlement_ends_at,
        agreement=_agreement(agreement),
        next_renewal_date=next_renewal,
        expiry_date=expiry,
        mock_marketplace_enabled=settings.SUBSCRIPTION_MOCK_MARKETPLACE_ENABLED,
        can_manage=can_manage,
        available_actions=(
            _available_actions(tenant, agreement, settings=settings, now=now) if can_manage else []
        ),
    )


def _load(db: Session, tenant_id: str) -> models.Tenant:
    """세션 소속의 테넌트(저장된 ID). DB 값으로 덮어 읽는다(tenants_repo.get_tenant)."""
    row = tenants_repo.get_tenant(db, tenant_id)
    if row is None:
        raise ApiError(ErrorCode.TENANT_NOT_FOUND)
    return row.tenant


def _lock(db: Session, tenant_id: str, authorize: Callable[[], None]) -> models.Tenant:
    """쓰기의 첫 단계 — 테넌트 행을 잠그고, 잠근 뒤 요청자를 다시 판정하고(무효면 401 · 403 · rollback),
    잠긴 행을 DB 값으로 다시 읽는다. 잠금을 기다리는 사이 회수 · 강등된 요청자가 쓰지 못한다."""
    if tenants_repo.lock_tenant(db, tenant_id) is None:
        raise ApiError(ErrorCode.TENANT_NOT_FOUND)
    authorize()
    return tenants_repo.lock_tenant(db, tenant_id)


def _conflict(db: Session) -> ApiError:
    """받지 않는 요청 — 쓴 것이 없다. 잡은 잠금을 바로 놓는다."""
    db.rollback()
    return ApiError(ErrorCode.TENANT_STATE_CONFLICT)


def _refused(db: Session, tenant_id: str) -> ApiError:
    """쓰기가 0행이었다 — 없어서(404)인지 지금 상태가 받지 않아서(409)인지 다시 읽어 가른다. 쓴 것이 없다."""
    _load(db, tenant_id)
    return _conflict(db)


def _view(
    db: Session, tenant_id: str, *, can_manage: bool, settings: SubscriptionSettings, now: datetime
) -> CustomerSubscriptionResponse:
    return customer_subscription(
        _load(db, tenant_id),
        mock_repo.latest_agreement(db, tenant_id),
        can_manage=can_manage,
        settings=settings,
        now=now,
    )


# --- 투영 (모의 계약의 현재 상태 → 테넌트 구독 칸) ------------------------------------


def _mock_projection(entitlement: mock_marketplace.Entitlement) -> dict:
    period_start, period_end = term_dates(entitlement.term_start, entitlement.term_end)
    return tenants_repo.projection_values(
        TenantSubscription(
            plan=entitlement.plan,
            period_start=period_start,
            period_end=period_end,
            cancel_at_period_end=not entitlement.auto_renew,
            billing_anchor_day=entitlement.anniversary_day,
        ),
        _MOCK,
        entitlement.term_end,
    )


def reconcile_tenant_subscription(db: Session, tenant_id: str, *, now: datetime) -> bool:
    """테넌트 행을 잠그고 모의 계약의 **현재** 권리를 다시 읽어 투영을 맞춘다 → 바꿨는가. commit 하지 않는다.

    - 지금 권리(활성 계약의 현재 기간 안)가 있으면 그 계약을 투영한다(MOCK_MARKETPLACE).
    - 권리가 없는데 투영이 모의 계약이면(만료 · 기간 끝 · 그 밖) 앱 정책 Free(APP_FREE)로 돌린다.
    - 그 밖(대기 · 실패 · 계약 없음)에는 지금 투영(수동 입력 · Free)을 그대로 둔다 — 대기는 권리가 아니다.
    - 유료 수동 기록(MANUAL · Free 아님)은 계약으로 덮어쓰지 않는다(구매 · PATCH 관문이 이 조합을
      막으므로 정상 경로에서는 일어나지 않는다 — 일어나면 경고만 남긴다).
    서비스 상태는 보지도 바꾸지도 않는다 — 정지 중에도 같은 결과이고, 재개 뒤 다시 불러도 같은 결과다(멱등).
    """
    tenant = tenants_repo.lock_tenant(db, tenant_id)
    if tenant is None:
        return False
    entitlement = mock_marketplace.current_entitlement(db, tenant_id, now=now)
    if entitlement is not None:
        if tenant.subscription_source is not _MOCK and tenant.plan != FREE_PLAN:
            logger.warning(
                "mock_entitlement_not_projected_over_manual_record",
                extra={"tenant_id": tenant_id, "agreement_id": entitlement.agreement_id},
            )
            return False
        desired = _mock_projection(entitlement)
    elif tenant.subscription_source is _MOCK:
        desired = tenants_repo.projection_values(tenants_repo.APP_FREE, SubscriptionSource.APP_FREE)
    else:
        return False
    if {column: getattr(tenant, column) for column in desired} == desired:
        return False
    return tenants_repo.project_subscription(db, tenant_id, desired, now=now)


# --- 고객 요청 (호출부가 인가를 마친 뒤 부른다) ----------------------------------------


def get_subscription(
    db: Session, tenant_id: str, *, can_manage: bool, settings: SubscriptionSettings, now: datetime
) -> CustomerSubscriptionResponse:
    return _view(db, tenant_id, can_manage=can_manage, settings=settings, now=now)


def purchase_subscription(
    db: Session,
    tenant_id: str,
    body: SubscriptionPurchaseRequest,
    *,
    requested_by_user_id: str,
    authorize: Callable[[], None],
    settings: SubscriptionSettings,
    now: datetime,
) -> CustomerSubscriptionResponse:
    """모의 Marketplace 구매 요청 → PENDING 계약. 권리는 주지 않는다 — 투영은 그대로 Free 다.

    받는 조건: 새 구매 스위치가 켜져 있고, 관제 중(ACTIVE)이고, 지금 요금제가 Free(수동 입력 · 앱 정책)이고,
    진행 중 계약이 없다. 유료 수동 기록 · 옛 요금제 · 미등록은 받지 않는다(덮어쓰지 않는다).
    테넌트 행을 잠그고 판정하며, 동시 요청은 부분 유일 인덱스가 하나만 받는다 — 두 번째는 409 다.
    """
    if not settings.SUBSCRIPTION_MOCK_MARKETPLACE_ENABLED:
        raise _refused(db, tenant_id)
    # 계약의 구매자 FK 가 사용자 표를 참조한다 — 관리 쓰기(tenant_admin._management: 사용자 표 → 테넌트 행)와 같은
    # 순서로 먼저 잡는다. 테넌트 행을 먼저 쥐면 겹친 정지 · PATCH 와 교착한다. EXCLUSIVE 가 아니라 참조용이어야 테넌트 행을
    # 쥔 채 사용자 FK 를 참조하는 신뢰 경로 쓰기(actor 없는 workflows.reserve_execution)와도 교착하지 않는다.
    auth_repo.lock_users_for_reference(db)
    tenant = _lock(db, tenant_id, authorize)
    if (
        tenant.status is not TenantStatus.ACTIVE
        or tenant.plan != FREE_PLAN
        or tenant.subscription_source is _MOCK
        or tenants_repo.has_open_agreement(db, tenant_id)
    ):
        raise _conflict(db)
    try:
        with db.begin_nested():  # 유일성 거절이 이 쓰기만 되돌린다
            mock_marketplace.request_purchase(
                db, tenant_id, plan=body.plan, requested_by_user_id=requested_by_user_id, now=now
            )
    except IntegrityError as exc:
        if getattr(getattr(exc.orig, "diag", None), "constraint_name", None) == mock_repo.OPEN_AGREEMENT_UNIQUE:
            raise _conflict(db) from exc
        raise
    return commit_after(db, lambda: _view(db, tenant_id, can_manage=True, settings=settings, now=now))


def cancel_renewal(
    db: Session,
    tenant_id: str,
    *,
    authorize: Callable[[], None],
    settings: SubscriptionSettings,
    now: datetime,
) -> CustomerSubscriptionResponse:
    """활성 계약의 자동 갱신 끄기 — 권리는 지금 기간의 끝 시각까지 그대로이고 그 뒤 앱 정책 Free 다.

    서비스 상태와 무관하다(정지 중에도 받는다). 자동 갱신 중인 활성 계약이 없으면 409(이미 끔 · 대기 · 계약 없음 ·
    수동 기록). 공급자 쪽 변경 이벤트를 같은 트랜잭션에서 소비해 투영을 바로 맞춘다.
    """
    _lock(db, tenant_id, authorize)
    event_id = mock_marketplace.disable_renewal(db, tenant_id, now=now)
    if event_id is None:
        raise _conflict(db)
    reconcile_tenant_subscription(db, tenant_id, now=now)
    mock_repo.mark_processed(db, event_id, now=now)
    return commit_after(db, lambda: _view(db, tenant_id, can_manage=True, settings=settings, now=now))


def stop_service(
    db: Session,
    tenant_id: str,
    body: SubscriptionStopRequest,
    *,
    authorize: Callable[[], None],
    settings: SubscriptionSettings,
    now: datetime,
) -> CustomerSubscriptionResponse:
    """서비스 이용 중단 = 테넌트 정지와 같은 SUSPENDED 전이. 계약 · 투영은 그대로이고 계속 처리된다."""
    _lock(db, tenant_id, authorize)
    if not tenants_repo.suspend_tenant(db, tenant_id, reason=body.reason, now=now):
        raise _conflict(db)
    return commit_after(db, lambda: _view(db, tenant_id, can_manage=True, settings=settings, now=now))


# --- 계약 처리 주기 (subscription_scheduler) -----------------------------------------


@dataclass
class EventReport:
    processed: int = 0
    #: 다른 처리기가 잡고 있거나 이미 처리된 이벤트.
    skipped: int = 0
    errored: int = 0


@dataclass
class CycleReport:
    provider: mock_marketplace.ProviderReport = field(default_factory=mock_marketplace.ProviderReport)
    events: EventReport = field(default_factory=EventReport)


#: 한 주기에 소비하는 이벤트 수 — 남은 것은 다음 주기가 이어 받는다.
EVENT_BATCH = 200


def process_subscription_events(db: Session, *, now: datetime, limit: int = EVENT_BATCH) -> EventReport:
    """처리되지 않은 모의 Marketplace 이벤트를 소비한다 — 이벤트마다 그 테넌트의 투영을 현재 계약으로 맞추고
    processed_at 을 남긴 뒤 commit 한다. 실패한 이벤트는 처리되지 않은 채 남아 다음 주기에 다시 시도된다.

    중복 · 순서 뒤바뀜에 같은 결과다 — 이벤트 내용이 아니라 계약의 현재 상태를 읽는다.
    """
    report = EventReport()
    event_ids = mock_repo.unprocessed_event_ids(db, limit=limit)
    db.rollback()
    for event_id in event_ids:
        try:
            claimed = mock_repo.claim_event(db, event_id)
            if claimed is None:
                db.rollback()
                report.skipped += 1
                continue
            reconcile_tenant_subscription(db, claimed.tenant_id, now=now)
            mock_repo.mark_processed(db, event_id, now=now)
            db.commit()
        except Exception:
            db.rollback()
            report.errored += 1
            logger.exception("mock_marketplace_event_failed", extra={"event_id": event_id})
            continue
        report.processed += 1
    return report


def process_subscription_cycle(
    db: Session, *, settings: SubscriptionSettings, now: datetime
) -> CycleReport:
    """한 주기: 모의 공급자의 활성화 · 갱신 · 만료 → 이벤트 소비. 새 구매 스위치 · 서비스 상태와 무관하다."""
    report = CycleReport(
        provider=mock_marketplace.advance(
            db, outcome=settings.SUBSCRIPTION_MOCK_ACTIVATION_OUTCOME, now=now
        ),
        events=process_subscription_events(db, now=now),
    )
    logger.info("subscription_cycle", extra={"events": asdict(report.events)})
    return report
