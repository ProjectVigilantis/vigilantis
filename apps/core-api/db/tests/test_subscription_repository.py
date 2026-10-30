# ==============================================================================
# [파일 설명]  담당: 안성일 (BE · T-16)
# 구독 투영 · 모의 Marketplace 저장소 회귀 (Post-MVP T-16 · #528) — 실제 PostgreSQL · Alembic head.
#
# 지키는 것.
#   ① 새 테넌트는 앱 정책 Free(APP_FREE)로 시작한다.
#   ② 관리자 PATCH 의 구독은 운영자 입력(MANUAL)이고, 진행 중(PENDING · ACTIVE) 계약 · 계약 투영은 덮지 않는다
#      (0행 · 아무것도 바뀌지 않음). 구독 밖의 칸만 보낸 PATCH 는 받는다.
#   ③ 재개(확인 OK)는 구독을 바꾸지 않고 옛 이력에 새 행을 쓰지 않는다. 실패도 그대로다.
#   ④ 계약 전이는 읽은 revision · 기대 상태일 때만 — 낡은 쓰기는 0행이다.
#   ⑤ DB 가 계약의 모양 · 테넌트당 진행 중 계약 하나 · 투영 원천의 모양을 강제한다.
#   ⑥ 옛 재개 이력(tenant_subscription_history)은 읽기 전용으로 남고 테넌트 FK 를 지킨다.
# ==============================================================================

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pytest
from db import models
from db.repositories import auth as auth_repo
from db.repositories import mock_marketplace as mock_repo
from db.repositories import tenants as tenants_repo
from schemas.api.auth import AccountType, UserRole
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
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

NOW = datetime(2026, 10, 31, 3, 0, tzinfo=UTC)  # KST 2026-10-31 12:00
LATER = NOW + timedelta(minutes=5)
TERM_END = datetime(2026, 11, 29, 15, 0, tzinfo=UTC)  # KST 2026-11-30 00:00
OK = TenantVerificationResult.OK
DENIED = TenantVerificationResult.ASSUME_DENIED
UNREGISTERED = {"plan": None, "period_start": None, "period_end": None, "cancel_at_period_end": False,
                    "billing_anchor_day": None, "amount_minor": None, "currency": None}
MANUAL_PAID = TenantSubscription(plan="Business", period_start=date(2026, 10, 1), period_end=date(2026, 10, 31),
                                 billing_anchor_day=1, amount_minor=990000, currency="KRW")
ACTIVE_VALUES = {"status": MockAgreementStatus.ACTIVE, "activated_at": NOW, "term_start": NOW, "term_end": TERM_END,
                     "anniversary_day": 31, "term_number": 1}

_accounts = iter(f"{n:012d}" for n in range(400000000001, 400000009999))


def _tenant(db, status=TenantStatus.ACTIVE) -> models.Tenant:
    tenant = tenants_repo.create_tenant(
        db, name="학원", aws_account_id=next(_accounts), regions=["ap-northeast-2"], now=NOW)
    if status is not TenantStatus.PENDING_CONNECTION:
        assert tenants_repo.record_verification(db, tenant.tenant_id, result=OK, reason_code=None, now=NOW)
    if status is TenantStatus.SUSPENDED:
        assert tenants_repo.suspend_tenant(db, tenant.tenant_id, reason="계약 만료", now=NOW)
    return tenant


def _user(db, tenant_id: str) -> str:
    suffix = uuid.uuid4().hex
    return auth_repo.create_user(
        db, issuer="https://issuer.example", cognito_sub=f"sub-{suffix}", email=f"{suffix[:8]}@example.com",
        role=UserRole.ADMIN, account_type=AccountType.TENANT, tenant_id=tenant_id, now=NOW,
        email_verified_at=NOW,
    ).user_id


def _agreement(db, tenant: models.Tenant, **values) -> models.MockMarketplaceAgreement:
    """PENDING 계약을 만들고(저장소) 필요하면 상태 값을 DB 에 직접 쓴다(공급자 전이의 결과를 재현한다)."""
    agreement = mock_repo.create_agreement(
        db, tenant_id=tenant.tenant_id, plan="Business", requested_by_user_id=_user(db, tenant.tenant_id), now=NOW)
    if values:
        db.execute(update(models.MockMarketplaceAgreement)
                   .where(models.MockMarketplaceAgreement.agreement_id == agreement.agreement_id).values(**values))
        db.refresh(agreement)
    return agreement


def _stored(db, tenant_id: str) -> dict:
    row = db.execute(select(models.Tenant).where(models.Tenant.tenant_id == tenant_id)
                     .execution_options(populate_existing=True)).scalar_one()
    return {column: getattr(row, column) for column in (*UNREGISTERED, "subscription_source",
                                                        "entitlement_ends_at", "status")}


def _patch(**fields) -> TenantUpdateRequest:
    return TenantUpdateRequest(**fields)


# ----- ① 생성 -----------------------------------------------------------------------

def test_new_tenant_starts_with_app_policy_free(db):
    tenant = _tenant(db, TenantStatus.PENDING_CONNECTION)
    assert _stored(db, tenant.tenant_id) == UNREGISTERED | {
        "plan": "Free", "subscription_source": SubscriptionSource.APP_FREE, "entitlement_ends_at": None,
        "status": TenantStatus.PENDING_CONNECTION}


# ----- ② 관리자 PATCH ---------------------------------------------------------------

def test_patch_records_an_operator_subscription_and_null_unregisters(db):
    tenant = _tenant(db)
    assert tenants_repo.update_tenant(db, tenant.tenant_id, _patch(subscription=MANUAL_PAID), now=LATER)
    assert _stored(db, tenant.tenant_id) == MANUAL_PAID.model_dump() | {
        "subscription_source": SubscriptionSource.MANUAL, "entitlement_ends_at": None, "status": TenantStatus.ACTIVE}

    assert tenants_repo.update_tenant(db, tenant.tenant_id, _patch(name="학원 본점"), now=LATER)
    assert _stored(db, tenant.tenant_id)["subscription_source"] is SubscriptionSource.MANUAL

    assert tenants_repo.update_tenant(db, tenant.tenant_id, _patch(subscription=None), now=LATER)
    assert _stored(db, tenant.tenant_id) == UNREGISTERED | {
        "subscription_source": None, "entitlement_ends_at": None, "status": TenantStatus.ACTIVE}


@pytest.mark.parametrize("agreement_values", [
    pytest.param({}, id="pending"),
    pytest.param(ACTIVE_VALUES, id="active"),
])
def test_patch_never_erases_an_open_agreement(db, agreement_values):
    tenant = _tenant(db)
    _agreement(db, tenant, **agreement_values)
    before = _stored(db, tenant.tenant_id)

    for subscription in (MANUAL_PAID, TenantSubscription(plan="Free"), None):
        assert not tenants_repo.update_tenant(db, tenant.tenant_id, _patch(subscription=subscription), now=LATER)
    assert _stored(db, tenant.tenant_id) == before
    # 구독 밖의 칸은 받는다
    assert tenants_repo.update_tenant(db, tenant.tenant_id, _patch(name="학원 본점"), now=LATER)


def test_patch_never_erases_a_mock_projection(db):
    tenant = _tenant(db)
    agreement = _agreement(db, tenant, **ACTIVE_VALUES)
    db.execute(update(models.Tenant).where(models.Tenant.tenant_id == tenant.tenant_id).values(
        **tenants_repo.projection_values(
            TenantSubscription(plan="Business", period_start=date(2026, 10, 31), period_end=date(2026, 11, 29)),
            SubscriptionSource.MOCK_MARKETPLACE, TERM_END)))
    # 계약이 끝났지만 투영이 아직 바뀌기 전(다음 처리 주기 전)이어도 덮지 않는다
    db.execute(update(models.MockMarketplaceAgreement)
               .where(models.MockMarketplaceAgreement.agreement_id == agreement.agreement_id)
               .values(status=MockAgreementStatus.EXPIRED, auto_renew=False, renewal_disabled_at=NOW,
                       ended_at=TERM_END))
    assert not tenants_repo.update_tenant(db, tenant.tenant_id, _patch(subscription=MANUAL_PAID), now=LATER)
    assert _stored(db, tenant.tenant_id)["subscription_source"] is SubscriptionSource.MOCK_MARKETPLACE


def test_patch_is_accepted_after_the_agreement_ended_and_was_projected_free(db):
    tenant = _tenant(db)
    _agreement(db, tenant, status=MockAgreementStatus.FAILED, ended_at=NOW, failure_code="MOCK_ACTIVATION_FAILED")
    assert tenants_repo.update_tenant(db, tenant.tenant_id, _patch(subscription=MANUAL_PAID), now=LATER)


# ----- ③ 재개 ------------------------------------------------------------------------

@pytest.mark.parametrize("result,reason,status", [
    (OK, None, TenantStatus.ACTIVE),
    (DENIED, "AccessDenied", TenantStatus.SUSPENDED),
])
def test_resume_never_touches_the_subscription_or_writes_history(db, result, reason, status):
    tenant = _tenant(db, TenantStatus.SUSPENDED)
    assert tenants_repo.update_tenant(db, tenant.tenant_id, _patch(subscription=MANUAL_PAID), now=NOW)
    before = _stored(db, tenant.tenant_id)
    history = db.scalar(select(func.count()).select_from(models.TenantSubscriptionHistory))

    assert tenants_repo.resume_tenant(db, tenant.tenant_id, result=result, reason_code=reason, now=LATER)

    assert _stored(db, tenant.tenant_id) == before | {"status": status}
    assert db.scalar(select(func.count()).select_from(models.TenantSubscriptionHistory)) == history


# ----- ④ 계약 전이 --------------------------------------------------------------------

def test_transition_needs_the_revision_it_read(db):
    tenant = _tenant(db)
    agreement = _agreement(db, tenant)
    # 다른 처리기가 같은 계약을 먼저 읽어 둔 값
    stale = SimpleNamespace(agreement_id=agreement.agreement_id, revision=agreement.revision)

    assert mock_repo.transition(db, agreement, ACTIVE_VALUES, expected_status=MockAgreementStatus.PENDING, now=NOW)
    assert (agreement.status, agreement.revision) == (MockAgreementStatus.ACTIVE, stale.revision + 1)

    failed = {"status": MockAgreementStatus.FAILED, "ended_at": NOW, "failure_code": "MOCK_ACTIVATION_FAILED"}
    assert not mock_repo.transition(db, stale, failed, expected_status=MockAgreementStatus.PENDING, now=LATER)
    assert mock_repo.get_agreement(db, agreement.agreement_id).status is MockAgreementStatus.ACTIVE


# ----- ⑤ DB 제약 ------------------------------------------------------------------------

def test_one_open_agreement_per_tenant_but_ended_ones_may_accumulate(db):
    tenant = _tenant(db)
    _agreement(db, tenant, status=MockAgreementStatus.FAILED, ended_at=NOW, failure_code="MOCK_ACTIVATION_FAILED")
    _agreement(db, tenant)
    with pytest.raises(IntegrityError) as rejected, db.begin_nested():
        _agreement(db, tenant)
    assert rejected.value.orig.diag.constraint_name == mock_repo.OPEN_AGREEMENT_UNIQUE


@pytest.mark.parametrize("values", [
    pytest.param({"source": SubscriptionSource.MANUAL}, id="not-mock"),
    pytest.param({"buyer_ref": "123456789012"}, id="buyer-is-an-aws-account"),
    pytest.param({"license_ref": "arn:aws:license-manager::1:license:l-1"}, id="license-is-an-arn"),
    pytest.param({"plan": "Free"}, id="free-plan"),
    pytest.param({"status": MockAgreementStatus.ACTIVE}, id="active-without-term"),
    pytest.param({"term_start": NOW, "term_end": TERM_END}, id="pending-with-term"),
    pytest.param(ACTIVE_VALUES | {"term_end": NOW}, id="empty-term"),
    pytest.param({"auto_renew": False}, id="disabled-without-instant"),
    pytest.param({"status": MockAgreementStatus.FAILED, "ended_at": NOW}, id="failed-without-code"),
    pytest.param(ACTIVE_VALUES | {"status": MockAgreementStatus.EXPIRED, "ended_at": TERM_END},
                 id="expired-while-renewing"),
    pytest.param(ACTIVE_VALUES | {"status": MockAgreementStatus.EXPIRED, "ended_at": NOW, "auto_renew": False,
                                  "renewal_disabled_at": NOW}, id="expired-before-term-end"),
    pytest.param(ACTIVE_VALUES | {"ended_at": TERM_END}, id="active-with-end"),
])
def test_agreement_shape_is_enforced_by_the_database(db, values):
    tenant = _tenant(db)
    agreement = _agreement(db, tenant)
    with pytest.raises(IntegrityError), db.begin_nested():
        db.execute(update(models.MockMarketplaceAgreement)
                   .where(models.MockMarketplaceAgreement.agreement_id == agreement.agreement_id).values(**values))


@pytest.mark.parametrize("values", [
    pytest.param({"subscription_source": None}, id="facts-without-source"),
    pytest.param({**UNREGISTERED, "subscription_source": SubscriptionSource.MANUAL}, id="source-without-facts"),
    pytest.param({"subscription_source": SubscriptionSource.MOCK_MARKETPLACE}, id="mock-without-entitlement-end"),
    pytest.param({"entitlement_ends_at": TERM_END}, id="entitlement-end-on-app-free"),
    pytest.param({"plan": "Business"}, id="app-free-on-paid"),
    pytest.param({"subscription_source": SubscriptionSource.MOCK_MARKETPLACE, "entitlement_ends_at": TERM_END},
                 id="mock-projection-on-free"),
    pytest.param({"plan": "Business", "period_start": date(2026, 10, 31), "period_end": date(2026, 11, 29),
                  "amount_minor": 1, "currency": "KRW", "subscription_source": SubscriptionSource.MOCK_MARKETPLACE,
                  "entitlement_ends_at": TERM_END}, id="mock-projection-with-invented-amount"),
])
def test_projection_source_shape_is_enforced_by_the_database(db, values):
    tenant = _tenant(db)
    with pytest.raises(IntegrityError), db.begin_nested():
        db.execute(update(models.Tenant).where(models.Tenant.tenant_id == tenant.tenant_id).values(**values))


def test_event_dedupe_key_keeps_one_record_per_transition(db):
    tenant = _tenant(db)
    agreement = _agreement(db, tenant, **ACTIVE_VALUES)

    first =mock_repo.record_event(db, agreement, MockMarketplaceEventKind.ACTIVATED, occurred_at=NOW, now=NOW)
    assert first is not None
    assert mock_repo.record_event(db, agreement, MockMarketplaceEventKind.ACTIVATED, occurred_at=NOW, now=LATER) is None
    event = db.get(models.MockMarketplaceEvent, first)
    assert (event.source, event.dedupe_key) == (
        SubscriptionSource.MOCK_MARKETPLACE, f"{agreement.agreement_id}:ACTIVATED:1")


# ----- ⑥ 옛 재개 이력 -------------------------------------------------------------------

def test_legacy_history_requires_an_existing_tenant(db):
    db.add(models.TenantSubscriptionHistory(
        tenant_id="00000000-0000-0000-0000-000000000000", plan="Business", cancel_at_period_end=False,
        archived_at=NOW))
    with pytest.raises(IntegrityError):
        db.flush()


def test_legacy_history_keeps_its_tenant_from_being_deleted(db):
    tenant = _tenant(db, TenantStatus.PENDING_CONNECTION)
    db.add(models.TenantSubscriptionHistory(
        tenant_id=tenant.tenant_id, plan="pilot", cancel_at_period_end=False, archived_at=NOW))
    db.flush()
    with pytest.raises(IntegrityError), db.begin_nested():
        tenants_repo.delete_tenant(db, tenant.tenant_id)
    assert db.scalar(select(func.count()).select_from(models.TenantSubscriptionHistory)
                     .where(models.TenantSubscriptionHistory.tenant_id == tenant.tenant_id)) == 1
