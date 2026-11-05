# ==============================================================================
# [파일 설명]  담당: 안성일 (BE · T-16)
# 고객 구독(Post-MVP T-16 · #528)의 어휘와 계약 기간 달력입니다. 공개 계약(api.tenants ·
# api.subscriptions) · DB 모델 · Core API 의 모의 Marketplace 처리가 같은 규칙을 이 파일에서 읽습니다.
#
#   - 결제는 없다. 유료 권리의 원천은 **모의 Marketplace 계약**(MOCK_MARKETPLACE)이다 — AWS Marketplace 의
#     SaaS 계약(월 단위 · 계약형)을 흉내 낸 서버 안의 대역이고, 실제 AWS 호출 · 등록 · 청구가 없다. 실제
#     Marketplace 와 메시지 형식 · 날짜 규칙이 같다고 주장하지 않는다.
#   - 원천이 셋이다(SubscriptionSource). 수동 입력(MANUAL) · 모의 계약의 투영(MOCK_MARKETPLACE) ·
#     앱 정책의 Free(APP_FREE). 테넌트의 구독 칸은 **투영**이고 계약 자체가 아니다 — 모의 계약의 현재 상태가
#     권위이며, 투영은 그 현재 상태를 다시 읽어 맞춘다(이벤트 내용을 옮겨 적지 않는다).
#   - 요금제(`plan`)는 문자열 칸 그대로다(DB enum 이 아니다). 수동으로 넣은 옛 값(basic · pilot 등)은
#     보존한다. plan 이 null 이면 「등록 안 됨」이지 Free 가 아니다. 요금 · 기능 · SLA 는 정하지 않았다.
#   - 계약 시각은 시간대가 있는 정확한 시각(UTC 저장)이다. 첫 기간은 활성화 시각에 시작하고, 끝은 다음
#     기념일(활성화한 KST 날짜의 일 · 日) 00시(KST)다(끝 시각은 포함하지 않는다). 그 날이 없는 달은
#     말일이고 다음 달은 다시 기념일이다(1/31 → 2/28·29 → 3/31). 날짜 칸(period_start · period_end)은 이
#     시각에서 KST 로 파생한 값이고, period_end 는 마지막 이용일(포함)이다.
# ==============================================================================

from __future__ import annotations

import calendar
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from typing import Literal, get_args

#: 앱 정책의 기본 요금제. 새 테넌트의 시작값이자 모의 계약이 끝난 뒤의 도착점이다.
FREE_PLAN = "Free"

#: 고객이 모의 Marketplace 로 구매를 요청할 수 있는 요금제. 월 단위 계약형이다. 요금제 사이 전환은 없다.
PaidPlan = Literal["Business", "Enterprise"]
PAID_PLANS: frozenset[str] = frozenset(get_args(PaidPlan))


class SubscriptionSource(str, Enum):
    """테넌트 구독 투영의 원천. null 은 「등록 안 됨」이다(구독 칸이 전부 비어 있다)."""

    #: 운영자가 PATCH /tenants/{id} 로 넣은 값(이 변경 전의 기록 포함). 자동으로 갱신 · 전환하지 않는다.
    MANUAL = "MANUAL"
    #: 모의 Marketplace 계약의 현재 권리를 투영한 값. 운영자 PATCH 로 바꿀 수 없다.
    MOCK_MARKETPLACE = "MOCK_MARKETPLACE"
    #: 앱 정책의 Free — 새 테넌트의 시작값 · 모의 계약이 끝난 뒤의 값.
    APP_FREE = "APP_FREE"


class MockAgreementStatus(str, Enum):
    """모의 Marketplace 계약 1건의 상태 — 권위 있는 원천(서버의 모의 공급자)이 정한다."""

    #: 구매 요청을 받았고 활성화 전이다. 아무 권리도 주지 않는다.
    PENDING = "PENDING"
    #: 활성화됐다. 권리는 현재 기간의 끝 시각(term_end · 미포함)까지다.
    ACTIVE = "ACTIVE"
    #: 활성화에 실패했다(종료 상태). 권리가 없고 새 구매 요청을 받는다.
    FAILED = "FAILED"
    #: 자동 갱신을 끈 계약이 기간 끝 시각에 끝났다(종료 상태).
    EXPIRED = "EXPIRED"


#: 테넌트마다 하나만 있을 수 있는 계약 상태(DB 부분 유일 인덱스). 운영자 PATCH 와 새 구매를 막는다.
MOCK_AGREEMENT_OPEN_STATUSES: frozenset[MockAgreementStatus] = frozenset(
    {MockAgreementStatus.PENDING, MockAgreementStatus.ACTIVE}
)


class MockMarketplaceEventKind(str, Enum):
    """모의 공급자가 계약을 바꿀 때 남기는 이벤트의 종류. 소비자는 종류와 무관하게 계약의 **현재** 상태를
    다시 읽어 투영을 맞춘다(AWS 의 LicenseUpdated → GetEntitlements 경계를 흉내 낸다)."""

    ACTIVATED = "ACTIVATED"
    ACTIVATION_FAILED = "ACTIVATION_FAILED"
    RENEWAL_DISABLED = "RENEWAL_DISABLED"
    RENEWED = "RENEWED"
    EXPIRED = "EXPIRED"

#: Asia/Seoul — 1988년 이후 일광 절약 시간이 없어 고정 +09:00 과 같다. IANA 시간대 DB(tzdata)
#: 설치 여부에 따라 기동이 갈리지 않게 고정 오프셋으로 둔다.
SERVICE_TIMEZONE = timezone(timedelta(hours=9), "Asia/Seoul")


def service_date(now: datetime) -> date:
    """시각 → 한국 시간의 달력 날짜. 시간대 없는 시각은 받지 않는다(어느 시간대인지 추정하지 않는다)."""
    if now.utcoffset() is None:
        raise ValueError("now 는 시간대가 있는 시각이어야 합니다")
    return now.astimezone(SERVICE_TIMEZONE).date()


def anchored_date(year: int, month: int, anchor_day: int) -> date:
    """그 달의 기준일 — 그 날이 없는 달은 말일이다."""
    return date(year, month, min(anchor_day, calendar.monthrange(year, month)[1]))


def _shift_month(year: int, month: int, delta: int) -> tuple[int, int]:
    shifted_year, month_index = divmod(year * 12 + month - 1 + delta, 12)
    return shifted_year, month_index + 1


def billing_period(anchor_day: int, day: date) -> tuple[date, date]:
    """`day` 를 포함하는 한 달 이용 기간 (시작일, 마지막 이용일).

    시작일은 그 달의 기준일, 마지막 이용일은 다음 달 기준일의 전날이다. 기준일은 기간마다 원래 값에서
    다시 계산한다 — 2월에 말일로 당겨져도 3월은 다시 31일이다.
    """
    if not 1 <= anchor_day <= 31:
        raise ValueError("billing_anchor_day 는 1~31 입니다")
    start = anchored_date(day.year, day.month, anchor_day)
    if start > day:
        start = anchored_date(*_shift_month(day.year, day.month, -1), anchor_day)
    next_start = anchored_date(*_shift_month(start.year, start.month, 1), anchor_day)
    return start, next_start - timedelta(days=1)


def service_midnight(day: date) -> datetime:
    """그 KST 날짜의 00시(시간대 있는 시각)."""
    return datetime(day.year, day.month, day.day, tzinfo=SERVICE_TIMEZONE)


def term_end_after(anniversary_day: int, term_start: datetime) -> datetime:
    """`term_start` 가 든 한 달 기간의 끝 시각 = 다음 기념일 00시(KST · 미포함).

    첫 기간(활성화 시각 시작)과 갱신 기간(앞 기간의 끝 시각 시작)이 같은 규칙이다. 기념일은 기간마다 원래
    값에서 다시 계산한다(billing_period).
    """
    _, last_day = billing_period(anniversary_day, service_date(term_start))
    return service_midnight(last_day + timedelta(days=1))


def term_dates(term_start: datetime, term_end: datetime) -> tuple[date, date]:
    """계약 기간의 정확한 시각 → 기존 날짜 칸 (period_start, 마지막 이용일 period_end · KST)."""
    return service_date(term_start), service_date(term_end - timedelta(microseconds=1))
