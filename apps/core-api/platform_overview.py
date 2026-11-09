# ==============================================================================
# [파일 설명]  담당: 안성일 (BE · 접근 보호)
# 플랫폼 상태 · 고객사별 API 호출 통계 조회 서비스 (#572 6번) — GET /api/v1/platform/overview 의 본문.
#
#   - 읽기만 한다. 트랜잭션은 요청 세션의 것이며 commit 할 것이 없다.
#   - 값은 공개 계약 schemas.api.platform 으로만 나간다(ORM 비노출). 계약의 검증기가 합계 · 순서를 다시 확인한다.
#   - 기간은 UTC 날짜다 — window_end 는 조회 시각(now)의 UTC 날짜, window_start 는 그 (days - 1)일 전.
#   - 호출 0인 고객사도 items 에 싣는다. 정렬은 total_calls 내림차순 → 이름 → tenant_id 다.
# ==============================================================================

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime, timedelta

from schemas.api.platform import (
    API_USAGE_RETENTION_DAYS,
    COLLECTION_WINDOW_HOURS,
    PlatformOverviewResponse,
)
from schemas.collections import CollectionRunStatus
from sqlalchemy.orm import Session

from db.repositories import platform as platform_repo


def build_overview(db: Session, *, now: datetime, days: int) -> PlatformOverviewResponse:
    """플랫폼 상태와 최근 days 일의 고객사별 호출 통계. days 는 라우터가 1..API_USAGE_RETENTION_DAYS 로 확인한다."""
    tenant_counts = platform_repo.tenant_counts_by_status(db)
    run_counts = platform_repo.collection_run_counts_since(
        db, now - timedelta(hours=COLLECTION_WINDOW_HOURS)
    )
    awaiting = platform_repo.incidents_awaiting_approval(db)

    window_end = now.astimezone(UTC).date()
    window_start = window_end - timedelta(days=days - 1)
    daily: dict[str, list[dict]] = defaultdict(list)
    for tenant_id, day, calls in platform_repo.list_calls_between(db, start=window_start, end=window_end):
        if calls > 0:
            daily[tenant_id].append({"day": day, "calls": calls})
    items = [
        {
            "tenant_id": tenant_id,
            "tenant_name": name,
            "tenant_status": status,
            "total_calls": sum(point["calls"] for point in daily.get(tenant_id, ())),
            "daily": daily.get(tenant_id, []),
        }
        for tenant_id, name, status in platform_repo.list_tenants_for_usage(db)
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
            "incidents": {"awaiting_approval": awaiting},
            "api_usage": {
                "window_start": window_start,
                "window_end": window_end,
                "window_days": days,
                "retention_days": API_USAGE_RETENTION_DAYS,
                "items": items,
            },
        }
    )
