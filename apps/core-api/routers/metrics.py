# ==============================================================================
# [파일 설명]
# GET /api/v1/metrics/timeseries — 시계열 차트 6축 조회 라우터입니다.
#
#   축 1 CPU        : CloudWatch 원계열(services/metrics.cpu_timeseries) + 판정 임계선
#   축 2 SG 개방 건수 : 회차별 Rule 판정 이력(db.repositories.assets.sg_exposure_history)
#   축 3 네트워크     : CloudWatch 원계열(services/metrics.network_timeseries)
#   축 4 자산 현황    : 회차별 Rule 판정 이력(db.repositories.assets.asset_status_history)
#   축 5 자산 수      : 회차별 유형별 자산 수(db.repositories.assets.asset_inventory_history)
#   축 6 위협 이벤트   : 칸별 유형별 위협 이벤트 발생 수(db.repositories.assets.threat_event_history)
#
#   - 응답은 공개 계약 schemas.api.metrics.MetricsTimeseriesResponse 로만 직렬화한다.
#   - **축마다 실패를 따로 받는다.** 원천이 다르므로(AWS 호출 ↔ DB 조회) 한쪽 실패가
#     다른 쪽을 비우면 화면은 살아 있는 축까지 못 보게 된다. 계약의 AxisStatus 가 그 축이다.
#     여섯 축이 요청의 세션 하나를 차례로 쓰므로, 실패한 축은 세션을 되돌리고 내려간다(_axis_failed).
#   - GET /api/v1/assets 와 달리 조회 창(hours)을 받는다 — 스냅샷이 아니라 구간 조회다.
#   - CloudWatch 축(1·3)의 창은 메트릭 입자 경계에 맞추고, 같은 창의 결과는 다시 부르지 않고
#     재사용한다(_cloudwatch_window · _cached_cloudwatch). DB 축은 고정 원점으로 칸을 나누므로
#     지금 시각 기준 창을 그대로 쓴다.
# ==============================================================================

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import NamedTuple, TypeVar

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from schemas.api.assets import AssetType
from schemas.api.metrics import (
    AssetInventoryAxis,
    AssetInventoryPoint,
    AssetStatusAxis,
    AssetStatusPoint,
    AxisStatus,
    CpuAxis,
    MetricsTimeseriesResponse,
    NetworkAxis,
    SgExposureAxis,
    ThreatEventAxis,
    ThreatEventPoint,
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

#: CloudWatch 축의 창 끝을 입자 경계에서 이만큼 늦게 넘긴다 — 직전 칸의 마지막 표본이 CloudWatch 에
#: 게시될 때까지 기다린다. AWS 가 게시 지연 수치를 공개하지 않아 기본 모니터링 표본 간격(5분)에
#: 여유를 더했다. 짧으면 마지막 점이 한 입자 내내 덜 찬 값으로 재사용되고(네트워크 Sum 은 가짜
#: 하락), 길면 최신 점이 그만큼 늦게 나온다 — 앞쪽 비용이 커서 넉넉히 잡는다.
_SETTLE_SECONDS = 600

_T = TypeVar("_T")


class _CloudWatchKey(NamedTuple):
    """CloudWatch 축 결과의 재사용 키 — 요청을 정하는 값이 전부 들어간다.

    곡선 대상(EC2 목록)도 키에 넣는다. 빼면 입자 중간에 새로 수집된 인스턴스가 다음 경계까지
    곡선 목록에서 빠진다 — services.metrics.cpu_timeseries 가 관측이 없는 인스턴스도 빈 줄로
    남겨 범례에서 사라지지 않게 한 것과 어긋난다. 대가는 인스턴스 구성이 바뀐 회차에 한 번 더
    부르는 것이다.
    """

    axis: str
    window_start: datetime
    window_end: datetime
    period_seconds: int
    refs: tuple[Ec2Ref, ...]


class _InFlight:
    """진행 중인 CloudWatch 조회 하나. 같은 키로 동시에 들어온 요청이 그 결과(또는 예외)를 함께 받는다."""

    def __init__(self) -> None:
        self.done = threading.Event()
        self.series: list = []
        self.error: BaseException | None = None


#: 성공한 결과만 담는다 — 실패를 담으면 한 번의 스로틀로 그 축이 한 입자 내내 비어 버린다.
#: 프로세스 메모리라 uvicorn worker 1개를 전제한다(재기동하면 한 번 더 부를 뿐이다).
_cloudwatch_cache: dict[_CloudWatchKey, list] = {}
_cloudwatch_inflight: dict[_CloudWatchKey, _InFlight] = {}
#: 두 사전만 지킨다 — CloudWatch 를 부르는 동안에는 쥐지 않는다.
_cloudwatch_cache_lock = threading.Lock()


def _now() -> datetime:
    """조회 시각. 테스트가 입자 경계 앞뒤를 재현하려고 바꿔 끼운다."""
    return datetime.now(timezone.utc)


@router.get("/metrics/timeseries", response_model=MetricsTimeseriesResponse)
def get_metrics_timeseries(
    hours: int | None = Query(
        None, ge=1, le=_MAX_HOURS, description="조회 창(시간). 생략하면 METRICS_WINDOW_HOURS(기본 72)."
    ),
    db: Session = Depends(get_db),
) -> MetricsTimeseriesResponse:
    # 관제 대상 리전은 /assets 와 같은 근거를 쓴다(#261) — 두 화면이 서로 다른 범위를
    # 보면 같은 순간에 자산 수와 곡선 수가 어긋난다. 모듈 경유로 부르는 것은 의도적이다:
    # 테스트가 routers.assets._configured_regions 를 monkeypatch 하면 이쪽도 함께 따른다.
    regions = assets_router._configured_regions()
    settings = get_collector_settings()
    if hours is None:
        hours = settings.METRICS_WINDOW_HOURS
    now = _now()
    window_start = now - timedelta(hours=hours)
    cw_start, cw_end = _cloudwatch_window(
        now, hours=hours, period_seconds=settings.METRIC_PERIOD_SECONDS
    )

    return MetricsTimeseriesResponse(
        generated_at=now,
        cpu=_cpu_axis(db, regions=regions, window_start=cw_start, window_end=cw_end,
                      period_seconds=settings.METRIC_PERIOD_SECONDS),
        network=_network_axis(db, regions=regions, window_start=cw_start, window_end=cw_end,
                              period_seconds=settings.METRIC_PERIOD_SECONDS),
        sg_exposure=_sg_axis(db, regions=regions, since=window_start,
                             bucket_seconds=settings.SCAN_INTERVAL_SECONDS),
        asset_status=_asset_status_axis(db, regions=regions, since=window_start,
                                        bucket_seconds=settings.SCAN_INTERVAL_SECONDS),
        asset_inventory=_asset_inventory_axis(db, regions=regions, since=window_start,
                                              bucket_seconds=settings.SCAN_INTERVAL_SECONDS),
        threat_events=_threat_event_axis(db, regions=regions, since=window_start, until=now,
                                         bucket_seconds=settings.SCAN_INTERVAL_SECONDS),
    )


def _cloudwatch_window(
    now: datetime, *, hours: int, period_seconds: int
) -> tuple[datetime, datetime]:
    """CloudWatch 축의 조회 창 — 양 끝을 입자 경계(epoch 기준 period 배수)에 맞춘다.

    CloudWatch 는 StartTime 을 분 단위로만 내려 맞추고 그 시각부터 period 씩 칸을 자른다
    (GetMetricData API 참조). 창이 `now − hours ~ now` 이면 칸 경계가 매분 움직여 같은 시각의
    점이 조회마다 달라진다. 경계를 고정하면 점은 입자마다 한 칸씩 밀린다.

    **진행 중인 칸은 넣지 않는다.** EndTime 은 제외 경계라 끝을 경계에 두면 그 칸이 빠진다.
    표본이 덜 찬 칸은 재사용되는 동안 틀린 값으로 남는다(네트워크 Sum 은 낮게 그려진다).
    창 길이는 입자 배수로 올려 잡는다 — 기본 입자(1시간)에서는 hours 와 같다.
    """
    settled = int((now - timedelta(seconds=_SETTLE_SECONDS)).timestamp())
    end = datetime.fromtimestamp(settled // period_seconds * period_seconds, tz=timezone.utc)
    span = -(-hours * 3600 // period_seconds) * period_seconds
    return end - timedelta(seconds=span), end


def _cached_cloudwatch(key: _CloudWatchKey, fetch: Callable[[], list[_T]]) -> list[_T]:
    """같은 창의 CloudWatch 결과는 한 번만 부른다 — 화면은 15초마다 다시 읽는다.

    동기 엔드포인트라 요청이 스레드풀에서 겹친다. 같은 키로 동시에 들어온 요청은 먼저 온 요청의
    조회 하나를 기다려 그 결과를 받는다 — 실패도 같은 예외로 받는다. 그 조회 하나를 나누는 것이지
    실패를 담는 것은 아니어서, 끝난 뒤의 조회는 다시 부른다. 잠금은 CloudWatch 를 부르는 동안
    쥐지 않는다 — 쥐면 느리게 실패하는 조회 뒤로 다른 키의 요청까지 줄을 선다.
    창이 넘어가면 지난 창의 결과는 버린다.
    """
    with _cloudwatch_cache_lock:
        hit = _cloudwatch_cache.get(key)
        if hit is not None:
            return hit
        flight = _cloudwatch_inflight.get(key)
        leader = flight is None
        if flight is None:
            flight = _cloudwatch_inflight[key] = _InFlight()

    if not leader:
        flight.done.wait()
        if flight.error is not None:
            raise flight.error
        return flight.series

    try:
        flight.series = fetch()
    except BaseException as exc:
        flight.error = exc
        raise
    else:
        with _cloudwatch_cache_lock:
            for stale in [k for k in _cloudwatch_cache if k.window_end < key.window_end]:
                del _cloudwatch_cache[stale]
            _cloudwatch_cache[key] = flight.series
        return flight.series
    finally:
        with _cloudwatch_cache_lock:
            del _cloudwatch_inflight[key]
        flight.done.set()


def _axis_failed(db: Session, label: str, exc: Exception) -> str:
    """축 하나의 실패를 그 축에 가둔다 — 세션을 되돌리고 축에 실을 사유 코드를 돌려준다.

    PostgreSQL 은 SQL 오류가 난 트랜잭션을 되돌릴 때까지 다음 문장을 전부 거절한다. 되돌리지
    않으면 한 축의 DB 오류가 같은 세션을 쓰는 뒤 축을 모두 InternalError 로 끌고 간다.
    실패 원인이 AWS 여도 되돌린다 — 읽기만 하는 세션이라 잃을 쓰기가 없다.

    되돌리기 자체의 실패(연결 단절 등)는 올리지 않는다 — 올리면 응답 전체가 500 이 되어
    축마다 상태를 매기는 계약이 깨진다. 그때 뒤 축은 각자 실패해 UNAVAILABLE 로 내려간다.
    """
    reason = _failure_reason(exc)
    _log.warning("%s 조회 실패 — 축을 UNAVAILABLE 로 내린다(%s)", label, reason)
    try:
        db.rollback()
    except Exception as rollback_exc:
        _log.warning("%s 실패 뒤 세션 되돌리기 실패(%s)", label, _failure_reason(rollback_exc))
    return reason


def _ec2_refs(db: Session, regions: list[str]) -> list[Ec2Ref]:
    """곡선을 그릴 EC2 목록. CloudWatch 를 부르는 두 축이 같은 근거를 쓰게 한 자리에 둔다.

    읽은 뒤 트랜잭션을 끝낸다 — 호출자는 이어서 CloudWatch 를 부르거나 기다리는데, 그동안 풀 연결을
    쥐고 있으면 CloudWatch 가 느릴 때 다른 API 까지 연결을 못 얻는다. 목록은 값으로 복사했고
    읽기만 하는 세션이라 잃을 것이 없다.
    """
    refs = [
        Ec2Ref(arn=a.arn, resource_id=a.resource_id, name=a.name, region=a.region)
        for a in assets_repo.list_assets(db, asset_type=AssetType.EC2, regions=regions)
    ]
    db.rollback()
    return refs


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
        refs = _ec2_refs(db, regions)
        series = _cached_cloudwatch(
            _CloudWatchKey("cpu", window_start, window_end, period_seconds, tuple(refs)),
            lambda: cpu_timeseries(
                refs,
                window_start=window_start,
                window_end=window_end,
                period_seconds=period_seconds,
            ),
        )
    except Exception as exc:  # AWS·DB 어느 쪽이든 이 축만 내린다
        return CpuAxis(
            status=AxisStatus.UNAVAILABLE, reason_code=_axis_failed(db, "CPU 시계열", exc)
        )

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
        refs = _ec2_refs(db, regions)
        series = _cached_cloudwatch(
            _CloudWatchKey("network", window_start, window_end, period_seconds, tuple(refs)),
            lambda: network_timeseries(
                refs,
                window_start=window_start,
                window_end=window_end,
                period_seconds=period_seconds,
            ),
        )
    except Exception as exc:
        return NetworkAxis(
            status=AxisStatus.UNAVAILABLE, reason_code=_axis_failed(db, "네트워크 시계열", exc)
        )

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
        return SgExposureAxis(
            status=AxisStatus.UNAVAILABLE, reason_code=_axis_failed(db, "SG 개방 이력", exc)
        )

    return SgExposureAxis(
        status=AxisStatus.READY,
        points=[TimeseriesPoint(at=b.observed_at, value=b.open_count) for b in buckets],
    )


def _asset_status_axis(
    db: Session, *, regions: list[str], since: datetime, bucket_seconds: int
) -> AssetStatusAxis:
    """축 4. 축 2와 같이 DB 만 본다 — 대시보드가 쓰는 축이라 CloudWatch 가 죽어도 그려져야 한다."""
    try:
        buckets = assets_repo.asset_status_history(
            db, regions=regions, since=since, bucket_seconds=bucket_seconds
        )
    except Exception as exc:
        return AssetStatusAxis(
            status=AxisStatus.UNAVAILABLE, reason_code=_axis_failed(db, "자산 현황 이력", exc)
        )

    return AssetStatusAxis(
        status=AxisStatus.READY,
        points=[
            AssetStatusPoint(
                at=b.observed_at,
                # 합은 서버가 싣는다 — 화면이 다시 더하면 계약의 불변식과 따로 논다.
                judged=b.threat + b.cost_candidate + b.unused + b.skip + b.undecided,
                threat=b.threat,
                cost_candidate=b.cost_candidate,
                unused=b.unused,
                skip=b.skip,
                undecided=b.undecided,
            )
            for b in buckets
        ],
    )


def _asset_inventory_axis(
    db: Session, *, regions: list[str], since: datetime, bucket_seconds: int
) -> AssetInventoryAxis:
    """축 5. DB 만 본다 — 축 4와 같은 이유로 CloudWatch 와 따로 살아야 한다."""
    try:
        buckets = assets_repo.asset_inventory_history(
            db, regions=regions, since=since, bucket_seconds=bucket_seconds
        )
    except Exception as exc:
        return AssetInventoryAxis(
            status=AxisStatus.UNAVAILABLE, reason_code=_axis_failed(db, "자산 수 이력", exc)
        )

    # 저장소는 (칸, 유형) 행으로 준다 — 칸 하나를 점 하나로 접는다(행은 칸 순으로 정렬돼 온다).
    by_at: dict[datetime, dict[AssetType, int]] = {}
    for b in buckets:
        by_at.setdefault(b.observed_at, {})[b.asset_type] = b.count
    return AssetInventoryAxis(
        status=AxisStatus.READY,
        points=[
            # 합은 서버가 싣는다 — 화면이 다시 더하면 계약의 불변식과 따로 논다.
            AssetInventoryPoint(at=at, total=sum(counts.values()), counts=counts)
            for at, counts in sorted(by_at.items())
        ],
    )


def _threat_event_axis(
    db: Session, *, regions: list[str], since: datetime, until: datetime, bucket_seconds: int
) -> ThreatEventAxis:
    """축 6. DB 만 본다 — 판정 축(4)과 한 차트에 겹치므로 같은 칸 크기를 쓴다."""
    try:
        buckets = assets_repo.threat_event_history(
            db, regions=regions, since=since, until=until, bucket_seconds=bucket_seconds
        )
    except Exception as exc:
        return ThreatEventAxis(
            status=AxisStatus.UNAVAILABLE, reason_code=_axis_failed(db, "위협 이벤트 이력", exc)
        )

    by_at: dict[datetime, dict[str, int]] = {}
    for b in buckets:
        by_at.setdefault(b.observed_at, {})[b.event_type] = b.count
    return ThreatEventAxis(
        status=AxisStatus.READY,
        points=[
            ThreatEventPoint(at=at, total=sum(counts.values()), counts=counts)
            for at, counts in sorted(by_at.items())
        ],
    )
