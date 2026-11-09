"""/api/v1/platform/overview 외부 DTO 계약 테스트 (#572 6번).

상태별 고객사 수의 완전성 · 합계, 호출 통계의 기간 · 정렬 · 일별 합계 · 중복 금지, 날짜 · 시각 직렬화를 검증한다.
"""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from schemas.api.platform import (
    API_USAGE_RETENTION_DAYS,
    ApiUsageSummary,
    PlatformOverviewResponse,
    TenantApiUsage,
    TenantCounts,
)

BY_STATUS = {"PENDING_CONNECTION": 1, "ACTIVE": 2, "SUSPENDED": 0}


def usage(tenant_id="t-1", name="학원", total=3, daily=None):
    if daily is None:
        daily = [{"day": "2026-11-08", "calls": 1}, {"day": "2026-11-09", "calls": 2}]
    return {"tenant_id": tenant_id, "tenant_name": name, "tenant_status": "ACTIVE", "total_calls": total, "daily": daily}


def summary(**over):
    base = {
        "window_start": "2026-11-03",
        "window_end": "2026-11-09",
        "window_days": 7,
        "retention_days": API_USAGE_RETENTION_DAYS,
        "items": [usage(), usage("t-2", "병원", 0, [])],
    }
    base.update(over)
    return base


def overview(**over):
    base = {
        "generated_at": datetime(2026, 11, 9, 1, 2, 3, tzinfo=UTC),
        "tenants": {"total": 3, "by_status": BY_STATUS},
        "collection": {"window_hours": 24, "success": 4, "partial": 1, "failed": 0, "in_progress": 1},
        "incidents": {"awaiting_approval": 2},
        "api_usage": summary(),
    }
    base.update(over)
    return base


def test_overview_serializes_dates_and_utc_z():
    body = PlatformOverviewResponse.model_validate(overview()).model_dump(mode="json")
    assert body["generated_at"] == "2026-11-09T01:02:03Z"
    assert body["tenants"]["by_status"] == BY_STATUS
    assert body["api_usage"]["window_start"] == "2026-11-03"
    assert body["api_usage"]["items"][0]["daily"][0] == {"day": "2026-11-08", "calls": 1}


@pytest.mark.parametrize(
    "counts",
    [
        {"total": 3, "by_status": {"PENDING_CONNECTION": 1, "ACTIVE": 2}},  # 상태 누락
        {"total": 2, "by_status": BY_STATUS},  # 합계 불일치
        {"total": 3, "by_status": {**BY_STATUS, "UNPAID": 0}},  # 서버에 없는 상태
    ],
)
def test_tenant_counts_must_cover_every_status_and_sum(counts):
    with pytest.raises(ValidationError):
        TenantCounts.model_validate(counts)


@pytest.mark.parametrize(
    "item",
    [
        usage(total=2),  # 일별 합과 다름
        usage(daily=[{"day": "2026-11-09", "calls": 2}, {"day": "2026-11-08", "calls": 1}]),  # 내림차순
        usage(daily=[{"day": "2026-11-09", "calls": 1}, {"day": "2026-11-09", "calls": 2}]),  # 같은 날 중복
        usage(total=0, daily=[{"day": "2026-11-09", "calls": 0}]),  # 호출 없는 날은 싣지 않는다
    ],
)
def test_tenant_usage_daily_is_ascending_and_sums_to_total(item):
    with pytest.raises(ValidationError):
        TenantApiUsage.model_validate(item)


@pytest.mark.parametrize(
    "over",
    [
        {"window_days": 6},  # 날 수 불일치
        {"window_days": 31, "window_start": "2026-10-10"},  # 보관 기간 초과
        {"items": [usage("t-2", "병원", 0, []), usage()]},  # 호출 수 오름차순
        {"items": [usage("t-1", "학원", 0, []), usage("t-1", "학원", 0, [])]},  # 같은 고객사 두 번
        {"items": [usage(daily=[{"day": "2026-11-02", "calls": 3}])]},  # 기간 밖 날짜
    ],
)
def test_summary_window_and_ordering_rules(over):
    with pytest.raises(ValidationError):
        ApiUsageSummary.model_validate(summary(**over))


def test_summary_orders_equal_totals_by_name_then_id():
    ApiUsageSummary.model_validate(
        summary(items=[usage("t-9", "가", 0, []), usage("t-1", "나", 0, []), usage("t-2", "나", 0, [])])
    )
    with pytest.raises(ValidationError):
        ApiUsageSummary.model_validate(summary(items=[usage("t-2", "나", 0, []), usage("t-1", "나", 0, [])]))


def test_overview_rejects_extra_keys():
    with pytest.raises(ValidationError):
        PlatformOverviewResponse.model_validate(overview(extra=1))
