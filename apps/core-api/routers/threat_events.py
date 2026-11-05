# ==============================================================================
# [파일 설명]  담당: 김승철 (API 계약 · BE)
# GET /api/v1/threat-events — 기간 안 위협 이벤트를 한 건씩, 최근 것부터(#572 10번)입니다.
#
#   - 응답은 공개 계약 schemas.api.threat_events.ThreatEventsResponse 로만 직렬화한다.
#   - 범위는 인시던트 목록 · 감사 기록과 같은 require_tenant_scope 다(고객사 계정 = 자기 고객사,
#     담당 직원 = 배정 고객사, 중앙 관리계정 = 전체). 관제 리전은 자산 목록 · 메트릭과 같은
#     routers.assets._monitored_regions 의 (고객사, 리전) 쌍이다 — 메트릭 축 6 과 같은 조건이라
#     대시보드의 위협 이벤트 그래프와 이 목록의 건수가 맞는다.
#   - DB 만 읽는다. 고객사 계정을 부르지 않으므로 고객사 상태로 막지 않는다(정지 고객사도 조회된다).
#   - 상한은 THREAT_EVENTS_LIMIT 건이고 커서는 없다. `total` 이 실제 건수를 알린다.
# ==============================================================================

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from schemas.api.errors import ErrorCode
from schemas.api.threat_events import (
    THREAT_EVENT_RANGE_DAYS,
    THREAT_EVENTS_LIMIT,
    ThreatEventsResponse,
)

from auth.access import require_tenant_scope
from db.repositories import assets as assets_repo
from db.session import get_db
from exceptions import ApiError
from routers import assets as assets_router
from tenancy import TenantScope

router = APIRouter(prefix="/api/v1", tags=["threat-events"])


def _now() -> datetime:
    """조회 시각. 테스트가 기간 경계를 재현하려고 바꿔 끼운다."""
    return datetime.now(timezone.utc)


@router.get("/threat-events", response_model=ThreatEventsResponse)
def list_threat_events(
    days: int = Query(..., description="조회 기간(일). 1 · 7 · 30 중 하나."),
    db: Session = Depends(get_db),
    scope: TenantScope = Depends(require_tenant_scope),
) -> ThreatEventsResponse:
    if days not in THREAT_EVENT_RANGE_DAYS:
        raise ApiError(ErrorCode.REQUEST_VALIDATION_FAILED)

    now = _now()
    window_start = now - timedelta(days=days)
    # 모듈 경유로 부른다 — 테스트가 routers.assets._configured_regions 를 바꿔 끼우면 이쪽도 따른다(메트릭과 같다)
    regions = assets_router._monitored_regions(db, scope)
    rows, total = assets_repo.list_threat_events(
        db, regions=regions, since=window_start, until=now, limit=THREAT_EVENTS_LIMIT, scope=scope,
    )
    return ThreatEventsResponse.model_validate(
        {
            "window_start": window_start,
            "window_end": now,
            "total": total,
            "items": [
                {
                    "threat_event_id": row.threat_event_id,
                    "event_type": row.event_type,
                    "target_arn": row.target_arn,
                    "occurred_at": row.occurred_at,
                    "payload": row.payload,
                }
                for row in rows
            ],
        }
    )
