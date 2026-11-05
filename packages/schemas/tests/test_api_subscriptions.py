"""고객 구독(Post-MVP T-16 · #528) 계약 · 달력 테스트 — DB 없이 돈다.

기대값은 달력 사실(윤년 · 월말 일수)에서 손으로 적었다. 구현 함수로 기대값을 만들지 않는다.
  ① 이용 기간 달력 — 기준일 31 의 비윤년 · 윤년 2월과 3월 31일 복귀, 연말 넘김, 놓친 달의 기준일 유지.
  ② KST 날짜 경계 — UTC 15:00 이 KST 다음 날 00:00 이다. 시간대 없는 시각은 받지 않는다.
  ③ TenantSubscription 의 저장 불변식(DB CHECK 거울)과 관리자 PATCH 의 쓰기 규칙.
  ④ 계약 기간 시각 — 다음 기념일 00시(KST · 미포함)에 끝나고, 날짜 칸은 그 KST 파생값이다.
  ⑤ 구매 요청 · 모의 계약 · CustomerSubscriptionResponse — 예정일은 모의 계약 투영에서만, 권리 끝 날짜로만.
"""

from datetime import UTC, date, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from schemas.api import (
    CustomerSubscriptionResponse,
    MockAgreement,
    SubscriptionPurchaseRequest,
    SubscriptionStopRequest,
    TenantDetail,
    TenantListItem,
    TenantSubscription,
    TenantUpdateRequest,
)
from schemas.subscriptions import (
    FREE_PLAN,
    PAID_PLANS,
    billing_period,
    service_date,
    term_dates,
    term_end_after,
)


# ----- ① 이용 기간 달력 -----

@pytest.mark.parametrize("day,expected", [
    # 비윤년 2027: 1/31 → 2/28 → 3/31
    (date(2027, 1, 31), (date(2027, 1, 31), date(2027, 2, 27))),
    (date(2027, 2, 27), (date(2027, 1, 31), date(2027, 2, 27))),
    (date(2027, 2, 28), (date(2027, 2, 28), date(2027, 3, 30))),
    (date(2027, 3, 31), (date(2027, 3, 31), date(2027, 4, 29))),
    (date(2027, 4, 30), (date(2027, 4, 30), date(2027, 5, 30))),
    # 윤년 2028: 1/31 → 2/29 → 3/31
    (date(2028, 1, 31), (date(2028, 1, 31), date(2028, 2, 28))),
    (date(2028, 2, 29), (date(2028, 2, 29), date(2028, 3, 30))),
    (date(2028, 3, 30), (date(2028, 2, 29), date(2028, 3, 30))),
    (date(2028, 3, 31), (date(2028, 3, 31), date(2028, 4, 29))),
], ids=str)
def test_anchor_31_follows_month_ends_and_returns_to_31(day, expected):
    assert billing_period(31, day) == expected


@pytest.mark.parametrize("anchor,day,expected", [
    (1, date(2026, 12, 15), (date(2026, 12, 1), date(2026, 12, 31))),
    (15, date(2026, 12, 20), (date(2026, 12, 15), date(2027, 1, 14))),  # 연말을 넘긴다
    (15, date(2027, 1, 3), (date(2026, 12, 15), date(2027, 1, 14))),
    (30, date(2027, 2, 28), (date(2027, 2, 28), date(2027, 3, 29))),
    (29, date(2027, 3, 1), (date(2027, 2, 28), date(2027, 3, 28))),
    # 처리를 여러 달 놓쳐도 그날을 포함하는 기준일의 한 달이다 — 2월에 당겨진 날짜(28)로 굳지 않는다
    (31, date(2027, 7, 31), (date(2027, 7, 31), date(2027, 8, 30))),
    (31, date(2027, 9, 15), (date(2027, 8, 31), date(2027, 9, 29))),
])
def test_period_containing_a_day(anchor, day, expected):
    assert billing_period(anchor, day) == expected


@pytest.mark.parametrize("anchor", [0, 32])
def test_anchor_out_of_range_is_rejected(anchor):
    with pytest.raises(ValueError):
        billing_period(anchor, date(2026, 10, 1))


def test_plan_vocabulary():
    assert FREE_PLAN == "Free"
    assert PAID_PLANS == {"Business", "Enterprise"}


# ----- ② KST 날짜 경계 -----

def test_service_date_is_the_korean_calendar_day():
    assert service_date(datetime(2026, 10, 31, 14, 59, 59, tzinfo=UTC)) == date(2026, 10, 31)
    assert service_date(datetime(2026, 10, 31, 15, 0, tzinfo=UTC)) == date(2026, 11, 1)
    # 다른 시간대로 들어와도 같은 순간이면 같은 날이다
    assert service_date(datetime(2026, 11, 1, 0, 0, tzinfo=timezone(timedelta(hours=9)))) == date(2026, 11, 1)


def test_naive_time_is_rejected():
    with pytest.raises(ValueError):
        service_date(datetime(2026, 10, 31, 15, 0))


# ----- ③ TenantSubscription · 관리자 PATCH -----

PAID = {"plan": "Business", "period_start": "2026-10-31", "period_end": "2026-11-29", "billing_anchor_day": 31}


def test_subscription_defaults_keep_the_old_three_field_shape_valid():
    sub = TenantSubscription.model_validate({"plan": "basic", "period_start": "2026-10-01"})
    assert (sub.cancel_at_period_end, sub.billing_anchor_day, sub.amount_minor, sub.currency) == (
        False, None, None, None)


@pytest.mark.parametrize("value", [
    {"amount_minor": 0, "currency": "KRW"},  # 0원도 등록된 사실이면 받는다 — 미정(null)과 다르다
    {"amount_minor": 990000, "currency": "KRW"},
    {"amount_minor": 1999, "currency": "USD"},
])
def test_registered_amount_with_currency(value):
    TenantSubscription.model_validate({"plan": "Business", **value})


@pytest.mark.parametrize("value", [
    pytest.param({"amount_minor": 1000}, id="amount-without-currency"),
    pytest.param({"currency": "KRW"}, id="currency-without-amount"),
    pytest.param({"amount_minor": -1, "currency": "KRW"}, id="negative"),
    pytest.param({"amount_minor": 2 ** 53, "currency": "KRW"}, id="beyond-js-safe-integer"),
    pytest.param({"amount_minor": 1000, "currency": "krw"}, id="lowercase-currency"),
    pytest.param({"amount_minor": 1000, "currency": "KRWX"}, id="four-letters"),
    pytest.param({"billing_anchor_day": 31}, id="anchor-without-period"),
    pytest.param({"billing_anchor_day": 31, "period_start": "2026-10-31"}, id="anchor-without-end"),
    pytest.param({"billing_anchor_day": 0, "period_start": "2026-10-01", "period_end": "2026-10-31"}, id="anchor-0"),
    pytest.param({"billing_anchor_day": 32, "period_start": "2026-10-01", "period_end": "2026-10-31"}, id="anchor-32"),
    pytest.param({"cancel_at_period_end": True}, id="cancel-without-end"),
    pytest.param({"unknown": 1}, id="extra"),
])
def test_subscription_shape_violations(value):
    with pytest.raises(ValidationError):
        TenantSubscription.model_validate({"plan": "Business", **value})


def test_list_item_carries_the_subscription_like_detail():
    assert TenantListItem.model_fields["subscription"].default is None
    assert "subscription" in TenantDetail.model_fields


def test_patch_accepts_a_complete_anchored_paid_subscription():
    for over in ({}, {"cancel_at_period_end": True}, {"amount_minor": 990000, "currency": "KRW"}):
        TenantUpdateRequest.model_validate({"subscription": PAID | over})
    # 옛 요금제 이름 · Free · 미등록은 갱신 칸 없이 받는다
    TenantUpdateRequest.model_validate({"subscription": {"plan": "pilot", "period_start": "2026-10-01"}})
    TenantUpdateRequest.model_validate({"subscription": {"plan": FREE_PLAN}})
    TenantUpdateRequest.model_validate({"subscription": None})


@pytest.mark.parametrize("plan", ["Business", "Enterprise"])
@pytest.mark.parametrize("subscription", [
    pytest.param({"period_start": "2026-10-01", "period_end": "2026-10-31"}, id="old-three-fields"),
    pytest.param({"period_start": "2026-10-05", "period_end": "2026-12-20"}, id="any-manual-period"),
    pytest.param({}, id="plan-only"),
    pytest.param({"period_start": "2026-10-01", "period_end": "2026-10-31", "billing_anchor_day": None},
                 id="explicit-null-anchor"),
    # 예약이 없으면 옛 기록의 「끝없음」도 그대로 받는다 — 전환일을 만들 일이 없다
    pytest.param({"period_start": "2026-01-01", "period_end": "9999-12-31"}, id="last-calendar-day-unscheduled"),
])
def test_patch_keeps_accepting_a_manual_paid_record_without_an_anchor(plan, subscription):
    # 기준일 없는 유료 요금제 = 운영자의 수동 기록(이 변경 전의 입력). 기간을 기준일의 한 달로 맞출 필요가
    # 없고, 기준일을 지어내지도 않는다 — 갱신 대상이 아니다
    body = TenantUpdateRequest.model_validate({"subscription": {"plan": plan, **subscription}})
    assert (body.subscription.plan, body.subscription.billing_anchor_day, body.subscription.cancel_at_period_end) == (
        plan, None, False)


@pytest.mark.parametrize("plan", ["Business", "Enterprise"])
def test_patch_accepts_free_conversion_on_a_manual_paid_record_without_an_anchor(plan):
    # Free 전환 예약은 갱신 여부와 따로다 — 기준일 없이 예약해도 기준일이 생기지 않는다
    body = TenantUpdateRequest.model_validate({"subscription": {
        "plan": plan, "period_start": "2026-10-01", "period_end": "2026-11-15", "cancel_at_period_end": True}})
    assert (body.subscription.cancel_at_period_end, body.subscription.billing_anchor_day) == (True, None)


@pytest.mark.parametrize("subscription", [
    pytest.param(PAID | {"period_end": "2026-11-30"}, id="period-not-one-anchored-month"),
    pytest.param(PAID | {"period_start": "2026-10-30"}, id="start-not-on-anchor"),
    pytest.param(PAID | {"plan": "pilot"}, id="legacy-plan-with-anchor"),
    pytest.param({"plan": "pilot", "period_end": "2026-12-31", "cancel_at_period_end": True}, id="legacy-cancel"),
    pytest.param({"plan": FREE_PLAN, "period_end": "2026-12-31", "cancel_at_period_end": True}, id="free-cancel"),
    pytest.param(PAID | {"plan": "business"}, id="case-differs"),
    # 예약의 전환일(period_end 다음 날)이 달력 밖 — 넘침(500)이 아니라 검증 오류다
    pytest.param({"plan": "Enterprise", "period_start": "2026-01-01", "period_end": "9999-12-31",
                  "cancel_at_period_end": True}, id="paid-cancel-on-last-calendar-day"),
    pytest.param({"plan": "Business", "cancel_at_period_end": True}, id="paid-cancel-without-end"),
    # 다음 기간의 시작일(10000-01-31)이 달력 밖 — 예정일을 만들 수 없는 갱신 구독은 받지 않는다
    pytest.param(PAID | {"period_start": "9999-12-31", "period_end": "9999-12-31"}, id="next-period-beyond-calendar"),
])
def test_patch_rejects_subscriptions_that_renewal_could_not_follow(subscription):
    with pytest.raises(ValidationError):
        TenantUpdateRequest.model_validate({"subscription": subscription})


def test_patch_accepts_feb_end_anchored_period():
    # 기준일 31 의 2월 기간(윤년) — 말일로 당겨진 시작일도 기준일의 한 달이다
    TenantUpdateRequest.model_validate({"subscription": {
        "plan": "Enterprise", "period_start": "2028-02-29", "period_end": "2028-03-30", "billing_anchor_day": 31}})


# ----- ④ 계약 기간 시각 -----

KST = timezone(timedelta(hours=9))


@pytest.mark.parametrize("term_start,anniversary,expected_end", [
    # KST 10-31 23:30 활성화 → 기념일 31 → 11월에는 31일이 없어 11-30 00시(KST)에 끝난다
    (datetime(2026, 10, 31, 23, 30, tzinfo=KST), 31, datetime(2026, 11, 30, tzinfo=KST)),
    # 갱신: 앞 기간의 끝 시각에 시작 — 2월(비윤년)은 28일, 다음은 다시 31일
    (datetime(2027, 1, 31, tzinfo=KST), 31, datetime(2027, 2, 28, tzinfo=KST)),
    (datetime(2027, 2, 28, tzinfo=KST), 31, datetime(2027, 3, 31, tzinfo=KST)),
    # 윤년 2월은 29일
    (datetime(2028, 1, 31, tzinfo=KST), 31, datetime(2028, 2, 29, tzinfo=KST)),
    # UTC 10-31 15:30 = KST 11-01 00:30 — 기념일은 KST 날짜의 일(1)이다
    (datetime(2026, 10, 31, 15, 30, tzinfo=UTC), 1, datetime(2026, 12, 1, tzinfo=KST)),
    # 연말을 넘긴다
    (datetime(2026, 12, 15, 9, 0, tzinfo=KST), 15, datetime(2027, 1, 15, tzinfo=KST)),
], ids=str)
def test_term_ends_at_the_next_anniversary_kst_midnight(term_start, anniversary, expected_end):
    assert term_end_after(anniversary, term_start) == expected_end


def test_term_dates_are_the_kst_days_with_an_inclusive_last_day():
    # 끝 시각(11-30 00시 KST)은 포함하지 않는다 — 마지막 이용일은 11-29 다
    assert term_dates(datetime(2026, 10, 31, 14, 30, tzinfo=UTC), datetime(2026, 11, 29, 15, 0, tzinfo=UTC)) == (
        date(2026, 10, 31), date(2026, 11, 29))


# ----- ⑤ 고객 요청 · 계약 · 응답 -----

@pytest.mark.parametrize("plan", ["Business", "Enterprise"])
def test_purchase_accepts_only_paid_plans(plan):
    assert SubscriptionPurchaseRequest.model_validate({"plan": plan}).plan == plan


@pytest.mark.parametrize("body", [
    {"plan": "Free"}, {"plan": "business"}, {"plan": "pilot"}, {},
    # 고객은 날짜 · 금액 · 구매자 · 활성화 결과 · 테넌트를 고르지 않는다
    {"plan": "Business", "term_start": "2026-10-01T00:00:00Z"},
    {"plan": "Business", "amount_minor": 0},
    {"plan": "Business", "buyer_ref": "mock-buyer-x"},
    {"plan": "Business", "outcome": "SUCCEED"},
    {"plan": "Business", "tenant_id": "00000000-0000-0000-0000-000000000000"},
])
def test_purchase_rejects_anything_else(body):
    with pytest.raises(ValidationError):
        SubscriptionPurchaseRequest.model_validate(body)


def test_stop_has_the_suspend_reason_rules():
    assert SubscriptionStopRequest.model_validate({"reason": "계약 종료"}).reason == "계약 종료"
    for body in ({}, {"reason": ""}, {"reason": "x" * 501}, {"reason": "x", "extra": 1}):
        with pytest.raises(ValidationError):
            SubscriptionStopRequest.model_validate(body)


ACTIVE_AGREEMENT = {
    "agreement_id": "a1",
    "plan": "Business",
    "status": "ACTIVE",
    "auto_renew": True,
    "requested_at": "2026-10-31T14:00:00Z",
    "activated_at": "2026-10-31T14:30:00Z",
    "term_start": "2026-10-31T14:30:00Z",
    "term_end": "2026-11-29T15:00:00Z",
}
DISABLED = {"auto_renew": False, "renewal_disabled_at": "2026-11-05T00:00:00Z"}


def test_agreement_shapes_follow_the_provider_lifecycle():
    pending = {"agreement_id": "a1", "plan": "Business", "status": "PENDING", "auto_renew": True,
               "requested_at": "2026-10-31T14:00:00Z"}
    assert MockAgreement.model_validate(pending).source == "MOCK_MARKETPLACE"
    MockAgreement.model_validate(pending | {"status": "FAILED", "ended_at": "2026-10-31T14:01:00Z",
                                            "failure_code": "MOCK_ACTIVATION_FAILED"})
    MockAgreement.model_validate(ACTIVE_AGREEMENT)
    MockAgreement.model_validate(ACTIVE_AGREEMENT | DISABLED)
    MockAgreement.model_validate(ACTIVE_AGREEMENT | DISABLED | {"status": "EXPIRED", "ended_at": "2026-11-29T15:00:00Z"})


@pytest.mark.parametrize("over", [
    pytest.param({"source": "AWS_MARKETPLACE"}, id="not-mock"),
    pytest.param({"status": "PENDING"}, id="pending-with-term"),
    pytest.param({"term_end": None}, id="half-term"),
    pytest.param({"term_end": "2026-10-31T14:30:00Z"}, id="empty-term"),
    pytest.param({"auto_renew": False}, id="disabled-without-instant"),
    pytest.param({"renewal_disabled_at": "2026-11-05T00:00:00Z"}, id="instant-without-disabled"),
    pytest.param({"ended_at": "2026-11-29T15:00:00Z"}, id="active-with-end"),
    pytest.param({"failure_code": "MOCK_ACTIVATION_FAILED"}, id="active-with-failure"),
    pytest.param({"status": "EXPIRED", "ended_at": "2026-11-29T15:00:00Z"}, id="expired-while-renewing"),
    pytest.param(DISABLED | {"status": "EXPIRED", "ended_at": "2026-11-20T00:00:00Z"}, id="expired-before-term-end"),
    pytest.param({"status": "FAILED", "ended_at": "2026-11-01T00:00:00Z"}, id="failed-without-code"),
    pytest.param({"buyer_ref": "mock-buyer-x"}, id="extra"),
])
def test_agreement_shape_violations(over):
    with pytest.raises(ValidationError):
        MockAgreement.model_validate(ACTIVE_AGREEMENT | over)


def test_list_item_names_the_subscription_source():
    common = {"tenant_id": "t1", "name": "학원", "status": "ACTIVE", "aws_account_id": "123456789012",
              "asset_count": 0, "open_incident_count": 0}
    TenantListItem.model_validate(common)
    TenantListItem.model_validate(common | {"subscription": PAID, "subscription_source": "MOCK_MARKETPLACE",
                                            "entitlement_ends_at": "2026-11-29T15:00:00Z"})
    for over in (
        {"subscription": PAID},  # 원천 없는 구독
        {"subscription_source": "MANUAL"},  # 구독 없는 원천
        {"subscription": PAID, "subscription_source": "MOCK_MARKETPLACE"},  # 권리 끝 시각 없음
        {"subscription": PAID, "subscription_source": "MANUAL", "entitlement_ends_at": "2026-11-29T15:00:00Z"},
    ):
        with pytest.raises(ValidationError):
            TenantListItem.model_validate(common | over)


MOCK_PROJECTION = PAID  # 활성화 KST 10-31 → 기념일 31 → 마지막 이용일 11-29 · 권리 끝 11-30 00시(KST)


def _response(**over):
    base = {
        "tenant_name": "학원",
        "service_status": "ACTIVE",
        "subscription": MOCK_PROJECTION,
        "source": "MOCK_MARKETPLACE",
        "entitlement_ends_at": "2026-11-29T15:00:00Z",
        "agreement": ACTIVE_AGREEMENT,
        "next_renewal_date": "2026-11-30",
        "expiry_date": None,
        "mock_marketplace_enabled": True,
        "can_manage": True,
        "available_actions": ["CANCEL_RENEWAL", "STOP"],
    }
    base.update(over)
    return base


def test_response_valid_shapes():
    CustomerSubscriptionResponse.model_validate(_response())
    # 자동 갱신 끔 → 만료일
    CustomerSubscriptionResponse.model_validate(_response(
        subscription=MOCK_PROJECTION | {"cancel_at_period_end": True}, agreement=ACTIVE_AGREEMENT | DISABLED,
        next_renewal_date=None, expiry_date="2026-11-30", available_actions=["STOP"]))
    # 이용 중단 중에도 계약은 그대로 흐른다 — 예정일이 남는다
    CustomerSubscriptionResponse.model_validate(_response(service_status="SUSPENDED", available_actions=["CANCEL_RENEWAL"]))
    # 대기 중 구매는 권리가 없다 — 투영은 Free 그대로이고 예정일이 없다
    pending = {"agreement_id": "a2", "plan": "Enterprise", "status": "PENDING", "auto_renew": True,
               "requested_at": "2026-10-31T14:00:00Z"}
    for over in (
        {"subscription": {"plan": FREE_PLAN}, "source": "APP_FREE", "agreement": pending},
        {"subscription": {"plan": FREE_PLAN}, "source": "MANUAL", "agreement": None,
         "available_actions": ["PURCHASE", "STOP"]},
        {"subscription": {"plan": "pilot", "period_start": "2026-01-01", "period_end": "9999-12-31"},
         "source": "MANUAL", "agreement": None},
        {"subscription": None, "source": None, "agreement": None},
        {"can_manage": False, "available_actions": [], "subscription": {"plan": FREE_PLAN}, "source": "APP_FREE"},
    ):
        CustomerSubscriptionResponse.model_validate(_response(
            entitlement_ends_at=None, next_renewal_date=None, **({"available_actions": []} | over)))


@pytest.mark.parametrize("over", [
    pytest.param({"entitlement_ends_at": None}, id="mock-without-entitlement-end"),
    pytest.param({"source": "MANUAL"}, id="entitlement-end-on-manual"),
    pytest.param({"next_renewal_date": "2026-11-29"}, id="renewal-not-the-entitlement-day"),
    pytest.param({"expiry_date": "2026-11-30"}, id="both-dates"),
    pytest.param({"next_renewal_date": None, "expiry_date": "2026-11-30"}, id="expiry-while-renewing"),
    pytest.param({"subscription": MOCK_PROJECTION | {"cancel_at_period_end": True}}, id="renewal-while-disabled"),
    pytest.param({"entitlement_ends_at": "2026-12-30T15:00:00Z", "next_renewal_date": "2026-12-31"},
                 id="entitlement-end-not-after-period-end"),
    pytest.param({"subscription": {"plan": FREE_PLAN}}, id="mock-projection-on-free"),
    pytest.param({"source": "APP_FREE", "entitlement_ends_at": None, "next_renewal_date": None},
                 id="app-free-on-paid"),
    pytest.param({"source": None, "entitlement_ends_at": None, "next_renewal_date": None}, id="subscription-without-source"),
    pytest.param({"source": "MANUAL", "entitlement_ends_at": None}, id="dates-on-manual"),
    pytest.param({"can_manage": False}, id="actions-without-manage"),
    pytest.param({"available_actions": ["STOP", "STOP"]}, id="duplicate-actions"),
    pytest.param({"can_manage": None}, id="can-manage-required"),
])
def test_response_rejects_shapes_that_imply_something_else(over):
    with pytest.raises(ValidationError):
        CustomerSubscriptionResponse.model_validate(_response(**over))


def test_response_requires_explicit_flags():
    body = _response()
    del body["mock_marketplace_enabled"]
    with pytest.raises(ValidationError):
        CustomerSubscriptionResponse.model_validate(body)
