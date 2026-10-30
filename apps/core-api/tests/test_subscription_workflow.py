"""고객 구독 워크플로 · 모의 Marketplace 생애주기 (Post-MVP T-16 · #528) — 실제 PostgreSQL.

워크플로 · 공급자 함수를 직접 호출해 응답 · 저장값 · 다시 읽은 응답을 확인한다. HTTP 인증 · 인가 관문은
test_subscription_api 가 본다. 기대 날짜는 달력 사실(KST · 월말 일수)에서 손으로 적었다.
  ① 구매 요청은 PENDING 이고 권리가 없다. 활성화 이벤트 + 현재 계약 읽기가 권리를 준다. 실패는 권리 없이 재구매.
  ② 자동 갱신은 원래 기념일(1/31 → 2/28·29 → 3/31)로 잇고, 놓친 기간은 지금을 포함하는 기간까지 빈틈없이 잇는다.
  ③ 갱신 끄기는 기간 끝 시각까지 권리를 두고, 그 시각에 만료 → 앱 정책 Free 다.
  ④ 서비스 중단 · 재개는 계약을 해지 · 동결 · 초기화하지 않는다. 새 구매 스위치를 꺼도 저장된 계약은 처리된다.
  ⑤ 구매 거절 — 쓰기가 남지 않는다. 운영자의 유료 수동 기록을 덮지 않는다.
  ⑥ 이벤트 — 중복 · 순서 뒤바뀜에 같은 결과, 한 건 장애 격리.
  ⑦ 경쟁 — 동시 구매 · 구매와 관리자 PATCH 가 테넌트 행 잠금으로 줄을 서고 하나만 이긴다(실제 commit · 스레드).
"""

from __future__ import annotations

import itertools
import logging
import threading
from datetime import UTC, date, datetime, timedelta

import mock_marketplace
import pytest
import subscription_workflow as workflow
import tenant_admin
import workflows
from auth import admin_service
from config import SubscriptionSettings, get_auth_settings, get_tenant_settings
from db import models
from db.repositories import auth as auth_repo
from db.repositories import mock_marketplace as mock_repo
from db.repositories import tenants as tenants_repo
from exceptions import ApiError
from schemas.api.auth import UserRole
from schemas.api.errors import ErrorCode
from schemas.api.subscriptions import (
    SubscriptionAction,
    SubscriptionPurchaseRequest,
    SubscriptionStopRequest,
)
from schemas.api.tenants import (
    TenantStatus,
    TenantSubscription,
    TenantUpdateRequest,
    TenantVerificationResult,
)
from schemas.subscriptions import (
    MockAgreementStatus,
    MockMarketplaceEventKind,
    SubscriptionSource,
)
from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session
from tests.test_auto_isolation_workflow import _approve_request, _committed, _seed


def kst(year, month, day, hour=0, minute=0) -> datetime:
    """KST 시각 → UTC aware 시각(주입하는 now)."""
    return datetime(year, month, day, hour, minute, tzinfo=UTC) - timedelta(hours=9)


NOW = kst(2026, 10, 31, 12)  # 구매 요청
ACTIVATED = kst(2026, 10, 31, 12, 1)  # 다음 처리 주기 — 활성화
TERM_END = kst(2026, 11, 30)  # 기념일 31 · 11월은 30일까지 → 11-30 00시(KST)
FREE = {"plan": "Free", "period_start": None, "period_end": None, "cancel_at_period_end": False,
        "billing_anchor_day": None, "amount_minor": None, "currency": None}
BUSINESS = FREE | {"plan": "Business", "period_start": "2026-10-31", "period_end": "2026-11-29",
                   "billing_anchor_day": 31}
A = SubscriptionAction


def _settings(enabled=True, outcome="SUCCEED") -> SubscriptionSettings:
    return SubscriptionSettings(
        _env_file=None,
        SUBSCRIPTION_MOCK_MARKETPLACE_ENABLED=enabled,
        SUBSCRIPTION_MOCK_ACTIVATION_OUTCOME=outcome,
    )


def _allow() -> None:
    """요청자 재판정 통과(HTTP 관문은 test_subscription_api)."""


@pytest.fixture()
def buyer(make_tenant, make_auth_session):
    """관제 중 Free 테넌트 + 그 고객사 ADMIN → (tenant_id, user_id)."""
    def _make(status=None):
        tenant_id = make_tenant(status)
        return tenant_id, make_auth_session(UserRole.ADMIN, tenant_id=tenant_id).user_id

    return _make


def _purchase(db, who, *, plan="Business", settings=None, now=NOW, authorize=_allow):
    tenant_id, user_id = who
    return workflow.purchase_subscription(
        db, tenant_id, SubscriptionPurchaseRequest(plan=plan), requested_by_user_id=user_id,
        authorize=authorize, settings=settings or _settings(), now=now)


def _cycle(db, now, **settings):
    return workflow.process_subscription_cycle(db, settings=_settings(**settings), now=now)


def _get(db, tenant_id, now, *, can_manage=True, settings=None):
    return workflow.get_subscription(
        db, tenant_id, can_manage=can_manage, settings=settings or _settings(), now=now)


def _cancel(db, tenant_id, now, authorize=_allow):
    return workflow.cancel_renewal(db, tenant_id, authorize=authorize, settings=_settings(), now=now)


def _stop(db, tenant_id, now=NOW):
    return workflow.stop_service(
        db, tenant_id, SubscriptionStopRequest(reason="운영 중단"), authorize=_allow, settings=_settings(), now=now)


def _agreements(db, tenant_id) -> list[models.MockMarketplaceAgreement]:
    return list(db.scalars(
        select(models.MockMarketplaceAgreement)
        .where(models.MockMarketplaceAgreement.tenant_id == tenant_id)
        .order_by(models.MockMarketplaceAgreement.requested_at)
        .execution_options(populate_existing=True)))


def _events(db, agreement_id) -> list[models.MockMarketplaceEvent]:
    return list(db.scalars(
        select(models.MockMarketplaceEvent)
        .where(models.MockMarketplaceEvent.agreement_id == agreement_id)
        .order_by(models.MockMarketplaceEvent.created_at, models.MockMarketplaceEvent.kind)
        .execution_options(populate_existing=True)))


def _tenant(db, tenant_id) -> models.Tenant:
    return tenants_repo.get_tenant(db, tenant_id).tenant


def _shown(response) -> tuple:
    """응답의 권리 요약 — (요금제 투영, 원천, 다음 갱신일, 만료일)."""
    sub = None if response.subscription is None else response.subscription.model_dump(mode="json")
    return sub, response.source, response.next_renewal_date, response.expiry_date


def _refused(call, code=ErrorCode.TENANT_STATE_CONFLICT):
    with pytest.raises(ApiError) as caught:
        call()
    assert caught.value.code is code


def _active(db, who, *, plan="Business"):
    """구매 → 활성화까지 마친 테넌트."""
    _purchase(db, who, plan=plan)
    _cycle(db, ACTIVATED)
    return who[0]


# ----- ① 구매 · 활성화 · 실패 -------------------------------------------------------

def test_purchase_is_pending_and_grants_no_rights(db, buyer):
    who = buyer()
    tenant_id = who[0]
    response = _purchase(db, who)

    assert _shown(response) == (FREE, SubscriptionSource.APP_FREE, None, None)
    assert (response.agreement.status, response.agreement.plan, response.agreement.source) == (
        MockAgreementStatus.PENDING, "Business", "MOCK_MARKETPLACE")
    assert response.available_actions == [A.STOP]  # 진행 중 계약이 있어 재구매 · 갱신 끄기 없음
    [agreement] = _agreements(db, tenant_id)
    # 식별자는 서버가 만든 불투명한 모의 값 — 관제 계정 ID 가 아니다
    assert agreement.buyer_ref.startswith("mock-buyer-") and agreement.license_ref.startswith("mock-license-")
    assert _tenant(db, tenant_id).aws_account_id not in agreement.buyer_ref
    assert (agreement.requested_by_user_id, agreement.requested_at) == (who[1], NOW)
    assert _shown(_get(db, tenant_id, NOW)) == (FREE, SubscriptionSource.APP_FREE, None, None)


def test_activation_event_and_current_authority_grant_the_term(db, buyer):
    who = buyer()
    tenant_id = who[0]
    _purchase(db, who)

    report = _cycle(db, ACTIVATED)

    assert report.provider.activated >= 1 and report.events.errored == 0
    shown = _get(db, tenant_id, ACTIVATED)
    assert _shown(shown) == (BUSINESS, SubscriptionSource.MOCK_MARKETPLACE, date(2026, 11, 30), None)
    assert shown.entitlement_ends_at == TERM_END
    assert (shown.agreement.status, shown.agreement.term_start, shown.agreement.term_end) == (
        MockAgreementStatus.ACTIVE, ACTIVATED, TERM_END)
    assert shown.available_actions == [A.CANCEL_RENEWAL, A.STOP]
    [event] = _events(db, shown.agreement.agreement_id)
    assert (event.kind, event.processed_at) == (MockMarketplaceEventKind.ACTIVATED, ACTIVATED)

    # 다시 돌아도 같은 결과 — 활성화 · 투영 쓰기가 다시 일어나지 않는다
    updated_at = _tenant(db, tenant_id).updated_at
    again = _cycle(db, ACTIVATED + timedelta(minutes=1))
    assert (again.provider.activated, again.events.processed) == (0, 0)
    assert _tenant(db, tenant_id).updated_at == updated_at


def test_activation_failure_leaves_no_paid_rights_and_allows_a_retry(db, buyer):
    who = buyer()
    tenant_id = who[0]
    _purchase(db, who)

    _cycle(db, ACTIVATED, outcome="FAIL")

    shown = _get(db, tenant_id, ACTIVATED)
    assert _shown(shown) == (FREE, SubscriptionSource.APP_FREE, None, None)
    assert (shown.agreement.status, shown.agreement.failure_code, shown.agreement.ended_at) == (
        MockAgreementStatus.FAILED, "MOCK_ACTIVATION_FAILED", ACTIVATED)
    assert shown.available_actions == [A.PURCHASE, A.STOP]

    retry = _purchase(db, who, plan="Enterprise", now=ACTIVATED + timedelta(minutes=1))
    assert retry.agreement.status is MockAgreementStatus.PENDING
    _cycle(db, ACTIVATED + timedelta(minutes=2))
    failed, retried = _agreements(db, tenant_id)
    assert (failed.status, retried.status, retried.plan) == (
        MockAgreementStatus.FAILED, MockAgreementStatus.ACTIVE, "Enterprise")
    assert retried.buyer_ref == failed.buyer_ref and retried.license_ref != failed.license_ref
    assert _get(db, tenant_id, ACTIVATED + timedelta(minutes=2)).subscription.plan == "Enterprise"


def test_hold_keeps_the_purchase_pending_without_rights(db, buyer):
    who = buyer()
    _purchase(db, who)
    report = _cycle(db, ACTIVATED, outcome="HOLD")
    assert report.provider.held >= 1
    shown = _get(db, who[0], ACTIVATED)
    assert (shown.agreement.status, shown.subscription.plan) == (MockAgreementStatus.PENDING, "Free")


# ----- ② 자동 갱신 ------------------------------------------------------------------

@pytest.mark.parametrize("year,feb_end", [(2027, 28), (2028, 29)], ids=["common-year", "leap-year"])
def test_auto_renewal_keeps_the_original_anniversary_through_february(db, buyer, year, feb_end):
    who = buyer()
    tenant_id = who[0]
    _purchase(db, who, now=kst(year, 1, 31, 9))
    _cycle(db, kst(year, 1, 31, 9, 1))
    expected = [
        (kst(year, 2, feb_end), (f"{year}-01-31", f"{year}-02-{feb_end - 1}")),
        (kst(year, 3, 31), (f"{year}-02-{feb_end}", f"{year}-03-30")),
        (kst(year, 4, 30), (f"{year}-03-31", f"{year}-04-29")),
        (kst(year, 5, 31), (f"{year}-04-30", f"{year}-05-30")),
    ]
    for (term_end, (start, end)), (next_end, _) in itertools.pairwise(expected):
        shown = _get(db, tenant_id, term_end - timedelta(seconds=1))
        assert (shown.subscription.period_start.isoformat(), shown.subscription.period_end.isoformat()) == (
            start, end)
        assert shown.entitlement_ends_at == term_end
        _cycle(db, term_end)  # 정확히 끝 시각 — 다음 기간으로 잇는다
        [agreement] = _agreements(db, tenant_id)
        assert (agreement.term_start, agreement.term_end) == (term_end, next_end)


def test_missed_terms_catch_up_to_the_term_containing_now_without_gaps(db, buyer):
    who = buyer()
    tenant_id = who[0]
    _active(db, who)

    _cycle(db, kst(2027, 1, 15))

    [agreement] = _agreements(db, tenant_id)
    assert (agreement.term_start, agreement.term_end, agreement.term_number) == (
        kst(2026, 12, 31), kst(2027, 1, 31), 3)
    renewed = [e for e in _events(db, agreement.agreement_id) if e.kind is MockMarketplaceEventKind.RENEWED]
    assert [(e.dedupe_key.rsplit(":", 1)[1], e.occurred_at) for e in renewed] == [("3", kst(2026, 12, 31))]
    shown = _get(db, tenant_id, kst(2027, 1, 15))
    assert (shown.subscription.period_start, shown.subscription.period_end, shown.next_renewal_date) == (
        date(2026, 12, 31), date(2027, 1, 30), date(2027, 1, 31))


# ----- ③ 갱신 끄기 · 만료 ------------------------------------------------------------

def test_disabling_renewal_keeps_rights_until_the_exact_term_end_then_projects_app_free(db, buyer):
    who = buyer()
    tenant_id = _active(db, who)
    disabled_at = kst(2026, 11, 5)

    response = _cancel(db, tenant_id, disabled_at)

    assert _shown(response) == (
        BUSINESS | {"cancel_at_period_end": True}, SubscriptionSource.MOCK_MARKETPLACE, None, date(2026, 11, 30))
    assert (response.agreement.auto_renew, response.agreement.renewal_disabled_at) == (False, disabled_at)
    assert response.available_actions == [A.STOP]
    _refused(lambda: _cancel(db, tenant_id, disabled_at))  # 이미 끔

    # 끝 시각 직전에는 그대로다
    _cycle(db, TERM_END - timedelta(microseconds=1))
    assert _get(db, tenant_id, TERM_END).subscription.plan == "Business"

    _cycle(db, TERM_END)
    shown = _get(db, tenant_id, TERM_END)
    assert _shown(shown) == (FREE, SubscriptionSource.APP_FREE, None, None)
    assert (shown.agreement.status, shown.agreement.ended_at) == (MockAgreementStatus.EXPIRED, TERM_END)
    assert shown.available_actions == [A.PURCHASE, A.STOP]
    # 늦게 처리돼도 만료 시각은 기간 끝 시각이다
    assert _tenant(db, tenant_id).entitlement_ends_at is None


def test_cancel_is_refused_without_an_auto_renewing_active_agreement(db, buyer):
    who = buyer()
    tenant_id = who[0]
    _refused(lambda: _cancel(db, tenant_id, NOW))  # 계약 없음
    _purchase(db, who)
    _refused(lambda: _cancel(db, tenant_id, NOW))  # 대기 중
    [agreement] = _agreements(db, tenant_id)
    assert agreement.auto_renew is True


# ----- ④ 서비스 상태와 계약은 따로다 ---------------------------------------------------

def test_service_suspension_neither_cancels_nor_freezes_the_contract(db, buyer):
    who = buyer()
    tenant_id = _active(db, who)

    stopped = _stop(db, tenant_id, kst(2026, 11, 10))
    assert stopped.service_status is TenantStatus.SUSPENDED
    assert _shown(stopped) == (BUSINESS, SubscriptionSource.MOCK_MARKETPLACE, date(2026, 11, 30), None)
    assert stopped.available_actions == [A.CANCEL_RENEWAL]
    _refused(lambda: _stop(db, tenant_id))  # 이미 중단

    _cycle(db, TERM_END)  # 정지 중에도 갱신한다
    renewed = _get(db, tenant_id, TERM_END)
    assert (renewed.service_status, renewed.subscription.period_end, renewed.next_renewal_date) == (
        TenantStatus.SUSPENDED, date(2026, 12, 30), date(2026, 12, 31))

    _cancel(db, tenant_id, kst(2026, 12, 1))  # 정지 중에도 갱신 끄기를 받는다
    _cycle(db, kst(2026, 12, 31))  # 정지 중에도 만료한다
    expired = _get(db, tenant_id, kst(2026, 12, 31))
    assert (expired.service_status, _shown(expired)) == (
        TenantStatus.SUSPENDED, (FREE, SubscriptionSource.APP_FREE, None, None))
    _refused(lambda: _purchase(db, who, now=kst(2026, 12, 31, 1)))  # 정지 중 새 구매는 받지 않는다


def test_resume_preserves_current_rights_and_a_failed_resume_keeps_service_stopped(db, buyer):
    who = buyer()
    tenant_id = _active(db, who)
    _stop(db, tenant_id, kst(2026, 11, 10))
    before = _shown(_get(db, tenant_id, kst(2026, 11, 11)))
    history = db.scalar(select(func.count()).select_from(models.TenantSubscriptionHistory))

    assert tenants_repo.resume_tenant(
        db, tenant_id, result=TenantVerificationResult.ASSUME_DENIED, reason_code="AccessDenied",
        now=kst(2026, 11, 11))
    db.commit()
    failed = _get(db, tenant_id, kst(2026, 11, 11))
    assert (failed.service_status, _shown(failed)) == (TenantStatus.SUSPENDED, before)

    assert tenants_repo.resume_tenant(
        db, tenant_id, result=TenantVerificationResult.OK, reason_code=None, now=kst(2026, 11, 12))
    db.commit()
    resumed = _get(db, tenant_id, kst(2026, 11, 12))
    assert (resumed.service_status, _shown(resumed)) == (TenantStatus.ACTIVE, before)
    # 옛 재개 규칙의 이력을 새로 쓰지 않는다 · 재개 뒤의 투영 새로 고침(B 의 DB 전용 자리)은 바꿀 것이 없다
    assert db.scalar(select(func.count()).select_from(models.TenantSubscriptionHistory)) == history
    assert workflow.reconcile_tenant_subscription(db, tenant_id, now=kst(2026, 11, 12)) is False


def test_closing_new_purchases_still_processes_stored_contracts(db, buyer):
    who = buyer()
    tenant_id = who[0]
    _purchase(db, who)

    _refused(lambda: _purchase(db, buyer(), settings=_settings(enabled=False)))
    _cycle(db, ACTIVATED, enabled=False)
    assert _get(db, tenant_id, ACTIVATED, settings=_settings(enabled=False)).subscription.plan == "Business"
    _cancel(db, tenant_id, kst(2026, 11, 5))
    _cycle(db, TERM_END, enabled=False)
    shown = _get(db, tenant_id, TERM_END, settings=_settings(enabled=False))
    assert (_shown(shown), shown.mock_marketplace_enabled) == ((FREE, SubscriptionSource.APP_FREE, None, None), False)
    assert shown.available_actions == [A.STOP]  # 새 구매가 닫혀 있다


# ----- ⑤ 구매 거절 ------------------------------------------------------------------

def _manual(subscription):
    def prepare(db, who):
        assert tenants_repo.update_tenant(db, who[0], TenantUpdateRequest(subscription=subscription), now=NOW)
        db.commit()
    return prepare


def _prior(activate):
    def prepare(db, who):
        _purchase(db, who, now=NOW - timedelta(minutes=5))
        if activate:
            _cycle(db, NOW - timedelta(minutes=4))
    return prepare


@pytest.mark.parametrize("status,prepare,enabled", [
    pytest.param(None, None, False, id="new-purchases-closed"),
    pytest.param(TenantStatus.SUSPENDED, None, True, id="service-stopped"),
    pytest.param(TenantStatus.PENDING_CONNECTION, None, True, id="not-connected"),
    pytest.param(None, _manual(TenantSubscription(plan="Business", period_start=date(2026, 10, 1),
                                                  period_end=date(2026, 12, 31))), True, id="manual-paid-record"),
    pytest.param(None, _manual(TenantSubscription(plan="pilot")), True, id="legacy-plan"),
    pytest.param(None, _manual(None), True, id="unregistered"),
    pytest.param(None, _prior(activate=False), True, id="pending-agreement"),
    pytest.param(None, _prior(activate=True), True, id="active-agreement"),
])
def test_purchase_is_refused_without_writing_anything(db, buyer, status, prepare, enabled):
    who = buyer(status)
    tenant_id = who[0]
    if prepare is not None:
        prepare(db, who)
    before = (_shown(_get(db, tenant_id, NOW)), len(_agreements(db, tenant_id)))

    _refused(lambda: _purchase(db, who, settings=_settings(enabled=enabled)))

    assert (_shown(_get(db, tenant_id, NOW)), len(_agreements(db, tenant_id))) == before


def test_manual_free_record_may_enter_checkout(db, buyer):
    who = buyer()
    _manual(TenantSubscription(plan="Free"))(db, who)
    assert _tenant(db, who[0]).subscription_source is SubscriptionSource.MANUAL
    assert _purchase(db, who).agreement.status is MockAgreementStatus.PENDING
    _cycle(db, ACTIVATED)
    assert _get(db, who[0], ACTIVATED).source is SubscriptionSource.MOCK_MARKETPLACE


def test_reconcile_never_overwrites_a_manual_paid_record(db, buyer, caplog):
    who = buyer()
    tenant_id = who[0]
    _purchase(db, who)
    # 관문을 우회한 저장값(정상 경로에서는 PATCH · 구매 관문이 막는다) — 계약이 수동 기록을 덮지 않는다
    db.execute(update(models.Tenant).where(models.Tenant.tenant_id == tenant_id).values(
        plan="Business", period_start=date(2026, 10, 1), period_end=date(2026, 12, 31),
        subscription_source=SubscriptionSource.MANUAL))
    db.commit()

    with caplog.at_level(logging.WARNING, logger="vigilantis.subscriptions"):
        _cycle(db, ACTIVATED)

    stored = _tenant(db, tenant_id)
    assert (stored.subscription_source, stored.period_end, stored.entitlement_ends_at) == (
        SubscriptionSource.MANUAL, date(2026, 12, 31), None)
    assert "mock_entitlement_not_projected_over_manual_record" in caplog.text


def test_reauthorization_failure_writes_nothing(db, buyer):
    who = buyer()
    tenant_id = who[0]

    def revoked():
        raise ApiError(ErrorCode.AUTH_FORBIDDEN)

    _refused(lambda: _purchase(db, who, authorize=revoked), ErrorCode.AUTH_FORBIDDEN)
    assert _agreements(db, tenant_id) == []

    _active(db, who)
    _refused(lambda: _cancel(db, tenant_id, kst(2026, 11, 5), authorize=revoked), ErrorCode.AUTH_FORBIDDEN)
    [agreement] = _agreements(db, tenant_id)
    assert agreement.auto_renew is True


# ----- ⑥ 이벤트 ---------------------------------------------------------------------

def test_events_are_idempotent_and_order_independent(db, buyer):
    who = buyer()
    tenant_id = who[0]
    _purchase(db, who)
    # 공급자만 활성화하고 소비는 아직 — 이벤트가 소비되기 전에는 권리가 없다
    mock_marketplace.advance(db, outcome="SUCCEED", now=ACTIVATED)
    assert _get(db, tenant_id, ACTIVATED).subscription.plan == "Free"

    # 갱신 끄기가 먼저 소비된다(자기 이벤트) — 현재 계약(활성 + 갱신 끔)을 읽어 맞춘다
    _cancel(db, tenant_id, kst(2026, 11, 5))
    expected = (BUSINESS | {"cancel_at_period_end": True}, SubscriptionSource.MOCK_MARKETPLACE, None,
                date(2026, 11, 30))
    assert _shown(_get(db, tenant_id, kst(2026, 11, 5))) == expected
    [agreement] = _agreements(db, tenant_id)
    activated, disabled = _events(db, agreement.agreement_id)
    assert (activated.kind, activated.processed_at, disabled.kind) == (
        MockMarketplaceEventKind.ACTIVATED, None, MockMarketplaceEventKind.RENEWAL_DISABLED)

    # 더 오래된 활성화 이벤트가 나중에 소비돼도 결과가 같다(내용이 아니라 현재 상태를 읽는다)
    updated_at = _tenant(db, tenant_id).updated_at
    assert workflow.process_subscription_events(db, now=kst(2026, 11, 6)).processed == 1
    assert _shown(_get(db, tenant_id, kst(2026, 11, 6))) == expected
    assert _tenant(db, tenant_id).updated_at == updated_at

    # 같은 이벤트를 다시 받아도 같다
    db.execute(update(models.MockMarketplaceEvent)
               .where(models.MockMarketplaceEvent.event_id == activated.event_id).values(processed_at=None))
    db.commit()
    assert workflow.process_subscription_events(db, now=kst(2026, 11, 7)).processed == 1
    assert _shown(_get(db, tenant_id, kst(2026, 11, 7))) == expected
    # 같은 전이(계약 · 종류 · 기간 번호 1)의 두 번째 기록은 버린다
    assert mock_repo.record_event(
        db, agreement, MockMarketplaceEventKind.ACTIVATED, occurred_at=ACTIVATED, now=ACTIVATED) is None


def test_one_failing_event_does_not_block_the_others(db, buyer, monkeypatch):
    broken, healthy = buyer(), buyer()
    _purchase(db, broken)
    _purchase(db, healthy)
    real = workflow.reconcile_tenant_subscription

    def flaky(session, tenant_id, *, now):
        if tenant_id == broken[0]:
            raise RuntimeError("projection store unavailable")
        return real(session, tenant_id, now=now)

    monkeypatch.setattr(workflow, "reconcile_tenant_subscription", flaky)
    report = _cycle(db, ACTIVATED)
    assert (report.events.processed, report.events.errored) == (1, 1)
    assert (_tenant(db, broken[0]).plan, _tenant(db, healthy[0]).plan) == ("Free", "Business")

    # 실패한 이벤트는 남아 다음 주기에 다시 처리된다
    monkeypatch.setattr(workflow, "reconcile_tenant_subscription", real)
    assert workflow.process_subscription_events(db, now=ACTIVATED + timedelta(minutes=1)).processed == 1
    assert _tenant(db, broken[0]).plan == "Business"


# ----- ⑦ 경쟁 (실제 commit · 독립 세션) -----------------------------------------------

@pytest.fixture()
def committed_buyer(pg_engine, committed_auth):
    """commit 된 관제 중 Free 테넌트 + 고객사 ADMIN. 끝나면 계약 · 이벤트를 먼저 지운다(사용자 · 테넌트는 committed_auth)."""
    tenant_id = committed_auth.make_tenant()
    who = (tenant_id, committed_auth.make(UserRole.ADMIN, tenant_id=tenant_id).user_id)
    yield who
    with Session(pg_engine) as cleanup:
        ids = select(models.MockMarketplaceAgreement.agreement_id).where(
            models.MockMarketplaceAgreement.tenant_id == tenant_id)
        cleanup.execute(delete(models.MockMarketplaceEvent).where(models.MockMarketplaceEvent.agreement_id.in_(ids)))
        cleanup.execute(delete(models.MockMarketplaceAgreement).where(
            models.MockMarketplaceAgreement.tenant_id == tenant_id))
        cleanup.commit()


def _race(pg_engine, first_write, second_write):
    """첫 세션이 테넌트 행을 잠근 채 커밋 전이다 — 둘째 쓰기는 그 커밋을 기다린 뒤 판정한다 → 둘째의 결과 · 예외."""
    first, second = Session(pg_engine), Session(pg_engine)
    outcome: dict = {}

    def run():
        try:
            outcome["result"] = second_write(second)
            second.commit()
        except Exception as exc:  # noqa: BLE001 — 스레드 밖에서 단언한다
            second.rollback()
            outcome["error"] = exc

    try:
        first_write(first)
        racer = threading.Thread(target=run)
        racer.start()
        racer.join(timeout=0.5)
        assert racer.is_alive(), "둘째 쓰기가 테넌트 행 잠금을 기다려야 한다"
        first.commit()
        racer.join(timeout=10)
        assert not racer.is_alive()
        return outcome
    finally:
        first.close()
        second.close()


def _in_flight_purchase(who):
    def write(session):
        assert tenants_repo.lock_tenant(session, who[0]) is not None
        mock_marketplace.request_purchase(session, who[0], plan="Business", requested_by_user_id=who[1], now=NOW)
    return write


def test_concurrent_purchases_create_exactly_one_agreement(pg_engine, committed_buyer):
    outcome = _race(pg_engine, _in_flight_purchase(committed_buyer), lambda session: _purchase(session, committed_buyer))
    assert outcome["error"].code is ErrorCode.TENANT_STATE_CONFLICT
    with Session(pg_engine) as check:
        assert [a.status for a in _agreements(check, committed_buyer[0])] == [MockAgreementStatus.PENDING]


def test_admin_patch_waits_for_an_in_flight_purchase_and_is_refused(pg_engine, committed_buyer):
    patch = TenantUpdateRequest(subscription=TenantSubscription(plan="Business", period_end=date(2026, 12, 31)))
    outcome = _race(
        pg_engine,
        _in_flight_purchase(committed_buyer),
        lambda session: tenants_repo.update_tenant(session, committed_buyer[0], patch, now=NOW),
    )
    assert outcome["result"] is False
    with Session(pg_engine) as check:
        stored = _tenant(check, committed_buyer[0])
        assert (stored.plan, stored.subscription_source) == ("Free", SubscriptionSource.APP_FREE)


def test_purchase_waits_for_an_in_flight_admin_patch_and_does_not_overwrite_it(pg_engine, committed_buyer):
    patch = TenantUpdateRequest(subscription=TenantSubscription(plan="Business", period_end=date(2026, 12, 31)))
    outcome = _race(
        pg_engine,
        lambda session: tenants_repo.update_tenant(session, committed_buyer[0], patch, now=NOW),
        lambda session: _purchase(session, committed_buyer),
    )
    assert outcome["error"].code is ErrorCode.TENANT_STATE_CONFLICT
    with Session(pg_engine) as check:
        assert _agreements(check, committed_buyer[0]) == []
        assert _tenant(check, committed_buyer[0]).subscription_source is SubscriptionSource.MANUAL


def test_purchase_overlapping_an_admin_suspension_waits_instead_of_deadlocking(
    pg_engine, committed_auth, committed_buyer, monkeypatch
):
    """관리자 정지(사용자 표 잠금 → 테넌트 행)와 겹친 구매 — 교착 없이 정지 뒤에 줄을 서고, 정지를 보고 409 다.

    정지가 사용자 표 잠금을 쥔 채 멈춘 동안 구매를 출발시킨다. 구매가 사용자 표 잠금을 청하거나 테넌트 행을 잠근
    뒤 재판정에 닿으면 정지를 놓는다 — 그 순간 각자 쥔 잠금이 순서를 정하고, 시각 · 폴링은 순서를 정하지 않는다.
    """
    tenant_id = committed_buyer[0]
    actor = admin_service.Actor(
        token=committed_auth.make().token,  # PLATFORM CENTRAL_ADMIN
        idle_seconds=get_auth_settings().AUTH_SESSION_IDLE_SECONDS,
        clock=lambda: datetime.now(UTC),
    )
    suspender, buyer = Session(pg_engine), Session(pg_engine)
    suspender_holds_users, purchase_reached_locks, release = (threading.Event() for _ in range(3))
    real_lock_users_table, real_lock_users_for_reference = (
        auth_repo.lock_users_table, auth_repo.lock_users_for_reference)

    def lock_users_table(session):
        real_lock_users_table(session)
        if session is suspender:
            suspender_holds_users.set()
            assert release.wait(timeout=10), "정지 해제 신호가 오지 않음"

    def lock_users_for_reference(session):
        if session is buyer:
            purchase_reached_locks.set()  # 정지가 쥔 잠금을 청한다 — 테넌트 행은 아직 잠그지 않았다
        real_lock_users_for_reference(session)

    monkeypatch.setattr(auth_repo, "lock_users_table", lock_users_table)
    monkeypatch.setattr(auth_repo, "lock_users_for_reference", lock_users_for_reference)
    suspended, refused = _overlap(
        (suspender, lambda: tenant_admin.suspend_tenant(
            suspender, actor, tenant_id, reason="원격 회수", settings=get_tenant_settings(), now=datetime.now(UTC))),
        suspender_holds_users,
        (buyer, lambda: _purchase(buyer, committed_buyer, authorize=purchase_reached_locks.set)),
        purchase_reached_locks,
        release,
    )

    assert not isinstance(suspended, Exception), repr(suspended)
    assert suspended.status is TenantStatus.SUSPENDED
    assert isinstance(refused, ApiError), repr(refused)
    assert refused.code is ErrorCode.TENANT_STATE_CONFLICT
    with Session(pg_engine) as check:
        assert _agreements(check, tenant_id) == []
        assert _tenant(check, tenant_id).status is TenantStatus.SUSPENDED


def _overlap(holder, holder_holds, waiter, waiter_reached, release):
    """holder(세션, 호출)가 잠금을 쥔 채 멈추면 waiter 를 출발시키고, waiter 가 잠금 단계에 닿으면 holder 를 놓는다.

    → (holder 결과, waiter 결과) — 예외는 결과로 돌려준다. 그 순간 각자 쥔 잠금이 순서를 정한다.
    """
    outcome: dict = {}

    def run(name, session, call):
        try:
            outcome[name] = call()
        except Exception as exc:  # noqa: BLE001 — 스레드 밖에서 단언한다
            session.rollback()
            outcome[name] = exc

    threads = [threading.Thread(target=run, args=(name, *pair)) for name, pair in (("holder", holder), ("waiter", waiter))]
    try:
        threads[0].start()
        assert holder_holds.wait(timeout=10)
        threads[1].start()
        assert waiter_reached.wait(timeout=10)
    finally:
        release.set()
        for thread in threads:
            if thread.ident is not None:
                thread.join(timeout=30)
    assert not any(thread.is_alive() for thread in threads)
    for session, _ in (holder, waiter):
        session.close()
    return outcome["holder"], outcome["waiter"]


def test_purchase_overlapping_a_trusted_action_reservation_completes_without_deadlock(
    pg_engine, committed_buyer, committed_actor, make_incident, make_candidate, monkeypatch
):
    """신뢰 경로의 조치 접수(actor 없음 · 사람 audit_actor)와 겹친 구매 — 둘 다 끝난다.

    그 접수는 사용자 표를 잠그지 않고 테넌트 행을 FOR SHARE 로 쥔 뒤 감사 기록의 행위자 FK 로 사용자 표를
    참조한다(workflows._write_context · reserve_execution). 접수가 테넌트 행을 쥔 채 멈춘 동안 구매가 테넌트 행
    잠금에 닿으면 접수를 놓는다 — 구매가 그 앞에 잡은 사용자 표 잠금이 FK 참조를 막으면 교착이다.
    """
    tenant_id = committed_buyer[0]
    with _committed(pg_engine) as cleanup_ids:
        with Session(pg_engine) as setup:
            seeded = _seed(setup, make_incident, make_candidate, "medium")
            cleanup_ids.append(seeded.incident_id)
            setup.execute(update(models.Incident).where(
                models.Incident.incident_id == seeded.incident_id).values(tenant_id=tenant_id))
            setup.commit()
        request = _approve_request(seeded.incident_id)
        action, buyer = Session(pg_engine), Session(pg_engine)
        action_holds_tenant, purchase_reached_tenant, release = (threading.Event() for _ in range(3))
        real_read_tenant_for_share, real_lock_tenant = tenants_repo.read_tenant_for_share, tenants_repo.lock_tenant

        def read_tenant_for_share(session, locked_id):
            row = real_read_tenant_for_share(session, locked_id)
            if session is action:
                action_holds_tenant.set()
                assert release.wait(timeout=10), "접수 해제 신호가 오지 않음"
            return row

        def lock_tenant(session, locked_id):
            if session is buyer:
                purchase_reached_tenant.set()  # 사용자 표 잠금은 이미 잡았다
            return real_lock_tenant(session, locked_id)

        monkeypatch.setattr(tenants_repo, "read_tenant_for_share", read_tenant_for_share)
        monkeypatch.setattr(tenants_repo, "lock_tenant", lock_tenant)
        reserved, purchased = _overlap(
            (action, lambda: workflows.reserve_execution(action, request, audit_actor=committed_actor)),
            action_holds_tenant,
            (buyer, lambda: _purchase(buyer, committed_buyer)),
            purchase_reached_tenant,
            release,
        )

        assert not isinstance(reserved, Exception), repr(reserved)
        assert reserved.created is True
        assert not isinstance(purchased, Exception), repr(purchased)
        assert purchased.agreement.status is MockAgreementStatus.PENDING
        with Session(pg_engine) as check:
            assert [a.status for a in _agreements(check, tenant_id)] == [MockAgreementStatus.PENDING]
