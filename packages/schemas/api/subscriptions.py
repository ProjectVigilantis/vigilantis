# ==============================================================================
# [파일 설명]  담당: 안성일 (BE · T-16)
# /api/v1/me/subscription — 고객사 계정의 자기 구독(Post-MVP T-16 · #528) 외부 DTO입니다.
# 어휘와 계약 기간 달력은 schemas.subscriptions, 구독 투영 칸은 api.tenants.TenantSubscription 이다.
#
# 경로 (전부 /api/v1 · 응답은 전부 CustomerSubscriptionResponse)
#   GET    /me/subscription           — 고객사 중앙계정 · 고객사 직원계정(TENANT)
#   POST   /me/subscription/purchase  SubscriptionPurchaseRequest → 202 — 모의 Marketplace 구매 요청(PENDING)
#   POST   /me/subscription/cancel    본문 없음 — 활성 계약의 자동 갱신 끄기(즉시 해지가 아니다)
#   POST   /me/subscription/stop      SubscriptionStopRequest — 서비스 이용 중단(테넌트 정지와 같은 SUSPENDED)
#   쓰기 셋은 고객사 중앙계정(ADMIN)만이다. 플랫폼(PLATFORM) 계정은 역할과 관계없이 403 이다. 대상 고객사는 세션의
#   소속이 정한다 — 요청에 tenant_id · 날짜 · 금액 · 구매자 식별자 · 활성화 결과를 싣지 않는다.
#   거절은 기존 코드를 쓴다: 지금 상태에서 할 수 없는 요청은 409 TENANT_STATE_CONFLICT 다 — 모의
#   Marketplace 꺼짐 · 관제 중(ACTIVE)이 아님 · 지금 요금제가 Free 가 아님 · 진행 중(PENDING · ACTIVE) 계약이
#   있음(구매), 자동 갱신 중인 활성 계약이 없음(갱신 끄기), 관제 중이 아님(중단).
#
# 계약 원칙
#   - 결제는 없다. 유료 권리의 원천은 모의 Marketplace 계약(`agreement`)이고, `subscription` 은 그 현재 권리를
#     맞춘 **투영**이다. 구매 요청(PENDING)은 권리를 주지 않는다 — 서버의 모의 공급자가 활성화하고 투영이
#     현재 계약을 다시 읽은 뒤에야 `subscription` 이 유료 요금제가 된다. 실패(FAILED)는 권리가 없고 다시 구매할 수 있다.
#   - 서비스 상태(`service_status`)와 계약은 따로다. 이용 중단(SUSPENDED) 중에도 계약의 갱신 · 만료와 투영은
#     계속되고, 중단이 계약을 해지하지 않는다. 중단 중에도 갱신 끄기는 받는다.
#   - 시각은 정확한 시각(UTC "Z")이고 날짜 칸은 KST 파생값이다. `next_renewal_date` · `expiry_date` 는
#     **예정**이다 — 그 시각 이후 첫 처리 주기에 투영이 바뀐다. 지금 요금제는 언제나 `subscription.plan` 이다.
#   - `can_manage` · `available_actions` 는 요청한 사용자 기준의 서버 판정이다(DB 칸이 아니다). 화면 편의이며
#     실제 차단은 쓰기 경로가 한다.
# ==============================================================================

from __future__ import annotations

from datetime import date
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..subscriptions import (
    CONTRACT_SOURCES,
    FREE_PLAN,
    MOCK_AGREEMENT_OPEN_STATUSES,
    PAID_PLANS,
    MockAgreementStatus,
    PaidPlan,
    SubscriptionSource,
    service_date,
)
from .assets import UtcDateTime
from .tenants import TenantName, TenantStatus, TenantSubscription, TenantSuspendRequest

#: 활성화 실패 사유 코드(모의 공급자). 실제 결제 실패를 뜻하지 않는다.
MOCK_ACTIVATION_FAILED = "MOCK_ACTIVATION_FAILED"


class SubscriptionPurchaseRequest(BaseModel):
    """POST /me/subscription/purchase — 고르는 것은 요금제뿐이다. 날짜 · 금액 · 결과는 서버가 정한다."""

    model_config = ConfigDict(extra="forbid")

    plan: PaidPlan


class SubscriptionStopRequest(TenantSuspendRequest):
    """POST /me/subscription/stop — 테넌트 정지(TenantSuspendRequest)와 같은 사유 규칙이다."""


class SubscriptionAction(str, Enum):
    """이 사용자가 지금 요청할 수 있는 쓰기(서버 판정)."""

    PURCHASE = "PURCHASE"
    CANCEL_RENEWAL = "CANCEL_RENEWAL"
    STOP = "STOP"


class MockAgreement(BaseModel):
    """모의 Marketplace 계약 1건 — 서버의 모의 공급자가 가진 현재 상태(권위 있는 원천). 아래 불변식은 DB CHECK 와 같다."""

    model_config = ConfigDict(extra="forbid")

    agreement_id: str = Field(min_length=1)
    #: 언제나 모의다. 실제 AWS 계약 · 라이선스 ARN 이 아니다.
    source: Literal["MOCK_MARKETPLACE"] = "MOCK_MARKETPLACE"
    plan: str = Field(min_length=1, max_length=50)
    status: MockAgreementStatus
    #: false = 자동 갱신 끔. 기간 끝 시각까지 권리는 그대로다.
    auto_renew: bool
    requested_at: UtcDateTime
    activated_at: UtcDateTime | None = None
    #: 현재(또는 마지막) 기간. 끝 시각은 포함하지 않는다.
    term_start: UtcDateTime | None = None
    term_end: UtcDateTime | None = None
    renewal_disabled_at: UtcDateTime | None = None
    #: 실패 시각(FAILED) 또는 만료 시각 = 마지막 기간의 끝(EXPIRED).
    ended_at: UtcDateTime | None = None
    failure_code: str | None = Field(None, min_length=1, max_length=64)

    @model_validator(mode="after")
    def _enforce_contract(self):
        term = self.term_start is not None and self.term_end is not None
        if (self.term_start is None) != (self.term_end is None):
            raise ValueError("term_start 와 term_end 는 함께 있거나 함께 없습니다")
        if term and self.term_end <= self.term_start:
            raise ValueError("term_end 는 term_start 보다 뒤입니다")
        if self.auto_renew != (self.renewal_disabled_at is None):
            raise ValueError("renewal_disabled_at 은 자동 갱신을 끈 계약에만, 그리고 반드시 있습니다")
        if (self.status is MockAgreementStatus.FAILED) != (self.failure_code is not None):
            raise ValueError("failure_code 는 FAILED 에만, 그리고 반드시 있습니다")
        activated = self.status in (MockAgreementStatus.ACTIVE, MockAgreementStatus.EXPIRED)
        if activated != (self.activated_at is not None) or activated != term:
            raise ValueError("활성화 시각 · 기간은 활성화된 계약(ACTIVE · EXPIRED)에만, 그리고 반드시 있습니다")
        if (self.status in MOCK_AGREEMENT_OPEN_STATUSES) == (self.ended_at is not None):
            raise ValueError("ended_at 은 끝난 계약(FAILED · EXPIRED)에만, 그리고 반드시 있습니다")
        if self.status is MockAgreementStatus.EXPIRED and (self.auto_renew or self.ended_at != self.term_end):
            raise ValueError("EXPIRED 는 자동 갱신을 끈 계약이 기간 끝 시각에 끝난 것입니다")
        return self


class CustomerSubscriptionResponse(BaseModel):
    """/me/subscription 경로 4개의 응답."""

    model_config = ConfigDict(extra="forbid")

    tenant_name: TenantName
    #: 서비스 상태 — 계약과 따로다. SUSPENDED 여도 계약 · 투영은 계속 바뀐다.
    service_status: TenantStatus
    #: 지금 적용 중인 구독(투영). null = 구독 정보가 등록되지 않았다(Free 가 아니다).
    subscription: TenantSubscription | None = None
    #: 투영의 원천. null = 등록 안 됨.
    source: SubscriptionSource | None = None
    #: 계약 투영의 권리 끝 시각(미포함). 원천이 MOCK_MARKETPLACE · AWS_MARKETPLACE 일 때만 있다.
    entitlement_ends_at: UtcDateTime | None = None
    #: 가장 최근 모의 계약(진행 중이면 그 계약). 없으면 null. PENDING · FAILED 는 여기서만 보인다.
    agreement: MockAgreement | None = None
    #: 다음 자동 갱신일(KST · 권리 끝 시각의 날짜). 자동 갱신 중인 모의 계약 투영에만 있다.
    next_renewal_date: date | None = None
    #: 만료일(KST · 권리 끝 시각의 날짜) — 그 시각 뒤 앱 정책 Free 로 돌아간다. 자동 갱신을 끈 투영에만 있다.
    expiry_date: date | None = None
    #: 새 모의 구매 요청을 받는지(서버 설정). 실제 청구는 어느 쪽이든 없다.
    mock_marketplace_enabled: bool
    #: 이 사용자가 고객사 중앙계정인지(쓰기 인가).
    can_manage: bool
    available_actions: list[SubscriptionAction] = Field(default_factory=list)

    @model_validator(mode="after")
    def _enforce_contract(self):
        mock = self.source is SubscriptionSource.MOCK_MARKETPLACE
        if (self.source in CONTRACT_SOURCES) != (self.entitlement_ends_at is not None):
            raise ValueError("entitlement_ends_at 은 계약 투영(모의 · 실제 Marketplace)에만, 그리고 반드시 있습니다")
        if self.next_renewal_date is not None and self.expiry_date is not None:
            raise ValueError("다음 갱신일과 만료일은 함께 있을 수 없습니다")
        sub = self.subscription
        # 실제 Marketplace 권리의 끝 시각은 KST 자정이 아닐 수 있고 갱신 끔 여부를 모른다 — 예정일을 내지 않는다
        if self.source is SubscriptionSource.AWS_MARKETPLACE and (
            sub is None or sub.plan not in PAID_PLANS or sub.period_end is None
        ):
            raise ValueError("실제 Marketplace 투영은 기간이 있는 유료 요금제입니다")
        if mock:
            if sub is None or sub.plan not in PAID_PLANS or sub.period_end is None:
                raise ValueError("모의 계약 투영은 기간이 있는 유료 요금제입니다")
            expected = service_date(self.entitlement_ends_at)
            if expected.toordinal() != sub.period_end.toordinal() + 1:
                raise ValueError("entitlement_ends_at 은 마지막 이용일 다음 날 00시(KST)입니다")
            scheduled, other = (
                (self.expiry_date, self.next_renewal_date)
                if sub.cancel_at_period_end
                else (self.next_renewal_date, self.expiry_date)
            )
            if scheduled != expected or other is not None:
                raise ValueError("모의 계약 투영의 예정일은 권리 끝 시각의 KST 날짜 하나입니다")
        elif self.next_renewal_date is not None or self.expiry_date is not None:
            raise ValueError("예정된 갱신 · 만료는 모의 계약 투영에만 있습니다")
        if self.source is SubscriptionSource.APP_FREE and (sub is None or sub.plan != FREE_PLAN):
            raise ValueError("APP_FREE 는 Free 입니다")
        if (self.source is None) != (sub is None):
            raise ValueError("등록 안 됨(source null)은 구독 정보가 없는 것입니다")
        if self.available_actions and not self.can_manage:
            raise ValueError("쓰기 권한이 없는 사용자에게 요청할 수 있는 쓰기가 없습니다")
        if len(set(self.available_actions)) != len(self.available_actions):
            raise ValueError("available_actions 는 중복될 수 없습니다")
        return self
