"""/api/v1/platform/overview 외부 DTO 계약 테스트 (#572 6번).

상태별 수(고객사 · 인시던트)의 완전성 · 합계, 호출 통계의 시간 칸(정시 · 1시간 간격 · 개수) · 정렬 · 시간별 합계 ·
중복 금지 · 과다 호출 기준 null 허용, "Z" 직렬화를 검증한다.
"""

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from schemas.api.platform import (
    API_USAGE_MAX_WINDOW_HOURS,
    API_USAGE_RETENTION_DAYS,
    ApiUsageSummary,
    IncidentCounts,
    PlatformOverviewResponse,
    TenantApiUsage,
    TenantCounts,
)

TENANTS_BY_STATUS = {"PENDING_CONNECTION": 1, "ACTIVE": 2, "SUSPENDED": 0}
INCIDENTS_BY_STATUS = {
    "ANALYZING": 1, "AWAITING_APPROVAL": 2, "ACTION_IN_PROGRESS": 0, "AWAITING_CLOSURE": 0, "RESOLVED": 3, "FAILED": 0,
}
T0 = datetime(2026, 11, 9, 1, 0, tzinfo=UTC)
HOURS = [T0 + timedelta(hours=i) for i in range(3)]


def usage(tenant_id="t-1", name="학원", hourly=(1, 0, 2), **over):
    base = {
        "tenant_id": tenant_id, "tenant_name": name, "tenant_status": "ACTIVE",
        "asset_count": 4, "staff_count": 2, "threshold_per_hour": None,
        "total_calls": sum(hourly), "hourly": list(hourly),
    }
    base.update(over)
    return base


def summary(**over):
    base = {
        "window_hours": 3, "hours": HOURS, "retention_days": API_USAGE_RETENTION_DAYS,
        "items": [usage(), usage("t-2", "병원", (0, 0, 0))],
    }
    base.update(over)
    return base


def overview(**over):
    base = {
        "generated_at": datetime(2026, 11, 9, 3, 2, 3, tzinfo=UTC),
        "tenants": {"total": 3, "by_status": TENANTS_BY_STATUS},
        "collection": {"window_hours": 24, "success": 4, "partial": 1, "failed": 0, "in_progress": 1},
        "incidents": {"total": 6, "by_status": INCIDENTS_BY_STATUS},
        "api_usage": summary(),
    }
    base.update(over)
    return base


def test_overview_serializes_utc_z_and_enum_keys():
    body = PlatformOverviewResponse.model_validate(overview()).model_dump(mode="json")
    assert body["generated_at"] == "2026-11-09T03:02:03Z"
    assert body["tenants"]["by_status"] == TENANTS_BY_STATUS
    assert body["incidents"]["by_status"] == INCIDENTS_BY_STATUS
    assert body["api_usage"]["hours"] == ["2026-11-09T01:00:00Z", "2026-11-09T02:00:00Z", "2026-11-09T03:00:00Z"]
    assert body["api_usage"]["items"][0]["threshold_per_hour"] is None


@pytest.mark.parametrize(
    "counts",
    [
        {"total": 3, "by_status": {"PENDING_CONNECTION": 1, "ACTIVE": 2}},  # 상태 누락
        {"total": 2, "by_status": TENANTS_BY_STATUS},  # 합계 불일치
        {"total": 3, "by_status": {**TENANTS_BY_STATUS, "UNPAID": 0}},  # 서버에 없는 상태
    ],
)
def test_tenant_counts_must_cover_every_status_and_sum(counts):
    with pytest.raises(ValidationError):
        TenantCounts.model_validate(counts)


@pytest.mark.parametrize(
    "counts",
    [
        {"total": 6, "by_status": {k: v for k, v in INCIDENTS_BY_STATUS.items() if k != "FAILED"}},  # 상태 누락
        {"total": 5, "by_status": INCIDENTS_BY_STATUS},  # 합계 불일치
        {"total": 6, "by_status": {**INCIDENTS_BY_STATUS, "RESOLVED": -3, "FAILED": 6}},  # 음수
    ],
)
def test_incident_counts_must_cover_every_status_and_sum(counts):
    with pytest.raises(ValidationError):
        IncidentCounts.model_validate(counts)


@pytest.mark.parametrize(
    "item",
    [
        usage(total_calls=2),  # 시간별 합과 다름
        usage(hourly=(1, -1, 3)),  # 음수 칸
        usage(threshold_per_hour=0),  # 기준은 1 이상이거나 null
        usage(asset_count=-1),
    ],
)
def test_tenant_usage_hourly_sums_to_total_and_inputs_are_nonnegative(item):
    with pytest.raises(ValidationError):
        TenantApiUsage.model_validate(item)


def test_tenant_usage_accepts_integer_threshold():
    assert TenantApiUsage.model_validate(usage(threshold_per_hour=120)).threshold_per_hour == 120


@pytest.mark.parametrize(
    "over",
    [
        {"window_hours": 2},  # 칸 수 불일치
        {"hours": [HOURS[0], HOURS[2], HOURS[2] + timedelta(hours=1)]},  # 빠진 칸
        {"hours": [HOURS[2], HOURS[1], HOURS[0]]},  # 내림차순
        {"hours": [T0 + timedelta(minutes=30) + timedelta(hours=i) for i in range(3)]},  # 정시 아님
        {"items": [usage("t-2", "병원", (0, 0, 0)), usage()]},  # 호출 수 오름차순
        {"items": [usage("t-1", "학원", (0, 0, 0)), usage("t-1", "학원", (0, 0, 0))]},  # 같은 고객사 두 번
        {"items": [usage(hourly=(1, 2))]},  # hourly 길이 불일치
        {"window_hours": API_USAGE_MAX_WINDOW_HOURS + 1, "hours": [T0 + timedelta(hours=i) for i in range(API_USAGE_MAX_WINDOW_HOURS + 1)]},
    ],
)
def test_summary_window_and_ordering_rules(over):
    with pytest.raises(ValidationError):
        ApiUsageSummary.model_validate(summary(**over))


def test_summary_judges_on_the_hour_in_utc_not_in_the_input_offset():
    """+05:30 의 01:00 은 UTC 19:30 이라 거절하고, +05:30 의 01:30 은 UTC 20:00 이라 받는다."""
    from datetime import timezone

    ist = timezone(timedelta(hours=5, minutes=30))
    with pytest.raises(ValidationError):
        ApiUsageSummary.model_validate(
            summary(hours=[datetime(2026, 11, 9, 1, 0, tzinfo=ist) + timedelta(hours=i) for i in range(3)])
        )
    body = ApiUsageSummary.model_validate(
        summary(hours=[datetime(2026, 11, 9, 1, 30, tzinfo=ist) + timedelta(hours=i) for i in range(3)])
    ).model_dump(mode="json")
    assert body["hours"][0] == "2026-11-08T20:00:00Z"


def test_summary_orders_equal_totals_by_name_then_id():
    zero = (0, 0, 0)
    ApiUsageSummary.model_validate(
        summary(items=[usage("t-9", "가", zero), usage("t-1", "나", zero), usage("t-2", "나", zero)])
    )
    with pytest.raises(ValidationError):
        ApiUsageSummary.model_validate(summary(items=[usage("t-2", "나", zero), usage("t-1", "나", zero)]))


def test_overview_rejects_extra_keys():
    with pytest.raises(ValidationError):
        PlatformOverviewResponse.model_validate(overview(extra=1))
