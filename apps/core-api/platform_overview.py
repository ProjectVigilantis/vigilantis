# ==============================================================================
# [파일 설명]  담당: 안성일 (BE · 접근 보호)
# 플랫폼 상태 · 고객사별 API 호출 통계 조회 서비스 (#572 6번) — GET /api/v1/platform/overview 의 본문.
#
#   - 읽기만 한다. 트랜잭션은 요청 세션의 것이며 commit 할 것이 없다.
#   - 값은 공개 계약 schemas.api.platform 으로만 나간다(ORM 비노출). 계약의 검증기가 합계 · 순서 · 칸 수를 다시 확인한다.
#   - 시간 칸은 UTC 정시다 — 마지막 칸은 조회 시각(now)을 정시로 내린 것(진행 중인 칸), 첫 칸은 (hours - 1)시간 전.
#   - 과다 호출 기준(threshold_per_hour)은 계산식이 정해지기 전이라 None 이다. 계산은 서버가 하도록 자리
#     (threshold_per_hour)만 두고 계수는 넣지 않는다(PR #599 리뷰).
#   - 호출 0인 고객사도 items 에 싣는다. 정렬은 total_calls 내림차순 → 이름 → tenant_id 다.
# ==============================================================================

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from schemas.api.platform import (
    API_USAGE_RETENTION_DAYS,
    COLLECTION_WINDOW_HOURS,
    PlatformOverviewResponse,
)
from schemas.collections import CollectionRunStatus
from sqlalchemy.orm import Session

from db.repositories import platform as platform_repo


def floor_to_hour(at: datetime) -> datetime:
    """UTC 정시로 내린다 — 호출 기록과 조회가 같은 칸을 쓰게 하는 유일한 규칙."""
    return at.astimezone(UTC).replace(minute=0, second=0, microsecond=0)


def threshold_per_hour(*, asset_count: int, staff_count: int) -> int | None:
    """고객사의 시간당 과다 호출 기준. 계산식(입력 · 계수)은 PM 이 정하기 전이라 아직 None 이다."""
    return None


def build_overview(db: Session, *, now: datetime, hours: int) -> PlatformOverviewResponse:
    """플랫폼 상태와 최근 hours 시간의 고객사별 호출 통계. hours 는 라우터가 1..API_USAGE_MAX_WINDOW_HOURS 로 확인한다."""
    tenant_counts = platform_repo.tenant_counts_by_status(db)
    run_counts = platform_repo.collection_run_counts_since(
        db, now - timedelta(hours=COLLECTION_WINDOW_HOURS)
    )
    incident_counts = platform_repo.incident_counts_by_status(db)

    last_hour = floor_to_hour(now)
    first_hour = last_hour - timedelta(hours=hours - 1)
    slots = [first_hour + timedelta(hours=offset) for offset in range(hours)]
    index_of = {slot: offset for offset, slot in enumerate(slots)}
    hourly: dict[str, list[int]] = {}
    for tenant_id, hour, calls in platform_repo.list_calls_between(db, start=first_hour, end=last_hour):
        row = hourly.setdefault(tenant_id, [0] * hours)
        row[index_of[hour.astimezone(UTC)]] = calls
    items = [
        {
            "tenant_id": tenant_id,
            "tenant_name": name,
            "tenant_status": status,
            "asset_count": asset_count,
            "staff_count": staff_count,
            "threshold_per_hour": threshold_per_hour(asset_count=asset_count, staff_count=staff_count),
            "total_calls": sum(hourly.get(tenant_id, ())),
            "hourly": hourly.get(tenant_id, [0] * hours),
        }
        for tenant_id, name, status, asset_count, staff_count in platform_repo.list_tenants_for_usage(db)
    ]
    items.sort(key=lambda item: (-item["total_calls"], item["tenant_name"], item["tenant_id"]))

    return PlatformOverviewResponse.model_validate(
        {
            "generated_at": now,
            "tenants": {"total": sum(tenant_counts.values()), "by_status": tenant_counts},
            "collection": {
                "window_hours": COLLECTION_WINDOW_HOURS,
                "success": run_counts[CollectionRunStatus.SUCCESS],
                "partial": run_counts[CollectionRunStatus.PARTIAL],
                "failed": run_counts[CollectionRunStatus.FAILED],
                "in_progress": run_counts[CollectionRunStatus.IN_PROGRESS],
            },
            "incidents": {"total": sum(incident_counts.values()), "by_status": incident_counts},
            "api_usage": {
                "window_hours": hours,
                "hours": slots,
                "retention_days": API_USAGE_RETENTION_DAYS,
                "items": items,
            },
        }
    )
