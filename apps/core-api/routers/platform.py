# ==============================================================================
# [파일 설명]  담당: 안성일 (BE · 접근 보호)
# GET /api/v1/platform/overview — 플랫폼 상태 · 고객사별 API 호출 통계 (#572 6번). 중앙 관리계정 전용.
#
#   - 라우터 의존성 require_central_admin — PLATFORM · CENTRAL_ADMIN 만 통과하고 그 밖(관리자 직원계정 ·
#     고객사 계정)은 403 AUTH_FORBIDDEN 이다. main.py 가 Origin → 세션 배선으로 포함한다.
#   - 응답은 공개 계약 schemas.api.platform.PlatformOverviewResponse 로만 직렬화한다. 집계는 platform_overview 가
#     맡고 라우터는 SQL 을 만지지 않는다.
#   - hours 는 1..API_USAGE_MAX_WINDOW_HOURS(기본 API_USAGE_DEFAULT_WINDOW_HOURS), 그 밖은 422. 시간 칸을 내므로
#     조회 범위도 시간으로 받는다 — FE 의 window_hours 와 같은 단위라 환산이 없다.
#   - DB 만 읽는다 — AWS · Cognito 를 부르지 않는다.
# ==============================================================================

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from schemas.api.platform import (
    API_USAGE_DEFAULT_WINDOW_HOURS,
    API_USAGE_MAX_WINDOW_HOURS,
    PlatformOverviewResponse,
)

import platform_overview
from auth.access import require_central_admin
from auth.http import get_clock
from db.session import get_db

router = APIRouter(
    prefix="/api/v1/platform",
    tags=["platform"],
    dependencies=[Depends(require_central_admin)],
)


@router.get("/overview", response_model=PlatformOverviewResponse)
def get_platform_overview(
    hours: int = Query(
        API_USAGE_DEFAULT_WINDOW_HOURS,
        ge=1,
        le=API_USAGE_MAX_WINDOW_HOURS,
        description="호출 통계 조회 기간(시간). 마지막 칸은 지금이 속한 UTC 정시 칸.",
    ),
    db: Session = Depends(get_db),
    clock: Callable[[], datetime] = Depends(get_clock),
) -> PlatformOverviewResponse:
    return platform_overview.build_overview(db, now=clock(), hours=hours)
