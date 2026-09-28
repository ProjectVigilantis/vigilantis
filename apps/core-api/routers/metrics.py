# ==============================================================================
# [파일 설명]
# GET /api/v1/metrics/timeseries — 대시보드 시계열 차트 2축 조회 라우터입니다.
#
#   축 1 CPU        : CloudWatch 원계열(services/metrics.cpu_timeseries) + 판정 임계선
#   축 2 SG 개방 건수 : 회차별 Rule 판정 이력(db.repositories.assets.sg_exposure_history)
#
#   - 응답은 공개 계약 schemas.api.metrics.MetricsTimeseriesResponse 로만 직렬화한다.
#   - **두 축의 실패를 따로 받는다.** 원천이 다르므로(AWS 호출 ↔ DB 조회) 한쪽 실패가
#     다른 쪽을 비우면 화면은 살아 있는 축까지 못 보게 된다. 계약의 AxisStatus 가 그 축이다.
#   - GET /api/v1/assets 와 달리 조회 창(hours)을 받는다 — 스냅샷이 아니라 구간 조회다.
# ==============================================================================

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from schemas.api.assets import AssetType
from schemas.api.metrics import (
    AxisStatus,
    CpuAxis,
    MetricsTimeseriesResponse,
    NetworkAxis,
    SgExposureAxis,
    TimeseriesPoint,
)

from config import get_collector_settings
from db.repositories import assets as assets_repo
from db.session import get_db
from routers import assets as assets_router
from services.collector import _failure_reason
from services.metrics import Ec2Ref, cpu_timeseries, network_timeseries
from services.rule_engine import IDLE_CPU_AVG

_log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["metrics"])

#: 조회 창의 상한. 14일을 넘기면 시간 입자(기본 1시간) 기준 점이 인스턴스당 336개를 넘어
#: 화면이 읽을 수 없는 밀도가 되고, CloudWatch 응답도 페이지가 갈린다.
_MAX_HOURS = 336


@router.get("/metrics/timeseries", response_model=MetricsTimeseriesResponse)
def get_metrics_timeseries(
    hours: int = Query(72, ge=1, le=_MAX_HOURS, description="조회 창(시간). 기본 72."),
    db: Session = Depends(get_db),
) -> MetricsTimeseriesResponse:
    # 관제 대상 리전은 /assets 와 같은 근거를 쓴다(#261) — 두 화면이 서로 다른 범위를
    # 보면 같은 순간에 자산 수와 곡선 수가 어긋난다. 모듈 경유로 부르는 것은 의도적이다:
    # 테스트가 routers.assets._configured_regions 를 monkeypatch 하면 이쪽도 함께 따른다.
    regions = assets_router._configured_regions()
    settings = get_collector_settings()
    now = datetime.now(timezone.utc)
    window_start = now - timedelta(hours=hours)

    return MetricsTimeseriesResponse(
        generated_at=now,
        cpu=_cpu_axis(db, regions=regions, window_start=window_start, window_end=now,
                      period_seconds=settings.METRIC_PERIOD_SECONDS),
        network=_network_axis(db, regions=regions, window_start=window_start, window_end=now,
                              period_seconds=settings.METRIC_PERIOD_SECONDS),
        sg_exposure=_sg_axis(db, regions=regions, since=window_start,
                             bucket_seconds=settings.SCAN_INTERVAL_SECONDS),
    )


def _ec2_refs(db: Session, regions: list[str]) -> list[Ec2Ref]:
    """곡선을 그릴 EC2 목록. CloudWatch 를 부르는 두 축이 같은 근거를 쓰게 한 자리에 둔다."""
    return [
        Ec2Ref(arn=a.arn, resource_id=a.resource_id, name=a.name, region=a.region)
        for a in assets_repo.list_assets(db, asset_type=AssetType.EC2, regions=regions)
    ]


def _cpu_axis(
    db: Session,
    *,
    regions: list[str],
    window_start: datetime,
    window_end: datetime,
    period_seconds: int,
) -> CpuAxis:
    """축 1. CloudWatch 호출이므로 여기만 외부 의존이 있다 — 실패를 이 축에 가둔다."""
    try:
        series = cpu_timeseries(
            _ec2_refs(db, regions),
            window_start=window_start,
            window_end=window_end,
            period_seconds=period_seconds,
        )
    except Exception as exc:  # AWS·DB 어느 쪽이든 이 축만 내린다
        reason = _failure_reason(exc)
        _log.warning("CPU 시계열 조회 실패 — 축을 UNAVAILABLE 로 내린다(%s)", reason)
        return CpuAxis(status=AxisStatus.UNAVAILABLE, reason_code=reason)

    return CpuAxis(
        status=AxisStatus.READY,
        period_seconds=period_seconds,
        window_start=window_start,
        window_end=window_end,
        # 임계선 값은 서버가 싣는다 — 화면이 상수를 따로 가지면 임계치를 고칠 때 갈린다.
        idle_cpu_avg_threshold=IDLE_CPU_AVG,
        series=series,
    )


def _network_axis(
    db: Session,
    *,
    regions: list[str],
    window_start: datetime,
    window_end: datetime,
    period_seconds: int,
) -> NetworkAxis:
    """축 3. CPU 와 원천은 같지만 **호출을 나눈다** — 쿼리가 인스턴스당 2개 더 붙는 쪽이라
    한도·권한 문제로 이 축만 떨어지는 경우가 있고, 그때 CPU 곡선까지 비우지 않기 위해서다."""
    try:
        series = network_timeseries(
            _ec2_refs(db, regions),
            window_start=window_start,
            window_end=window_end,
            period_seconds=period_seconds,
        )
    except Exception as exc:
        reason = _failure_reason(exc)
        _log.warning("네트워크 시계열 조회 실패 — 축을 UNAVAILABLE 로 내린다(%s)", reason)
        return NetworkAxis(status=AxisStatus.UNAVAILABLE, reason_code=reason)

    return NetworkAxis(
        status=AxisStatus.READY,
        period_seconds=period_seconds,
        window_start=window_start,
        window_end=window_end,
        series=series,
    )


def _sg_axis(
    db: Session, *, regions: list[str], since: datetime, bucket_seconds: int
) -> SgExposureAxis:
    """축 2. DB 만 본다 — CloudWatch 가 죽어도 이 축은 그려진다."""
    try:
        buckets = assets_repo.sg_exposure_history(
            db, regions=regions, since=since, bucket_seconds=bucket_seconds
        )
    except Exception as exc:
        reason = _failure_reason(exc)
        _log.warning("SG 개방 이력 조회 실패 — 축을 UNAVAILABLE 로 내린다(%s)", reason)
        return SgExposureAxis(status=AxisStatus.UNAVAILABLE, reason_code=reason)

    return SgExposureAxis(
        status=AxisStatus.READY,
        points=[TimeseriesPoint(at=b.observed_at, value=b.open_count) for b in buckets],
    )
