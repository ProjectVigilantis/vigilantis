# ==============================================================================
# [파일 설명]  담당: 김세혁 (PM · Infra & DevSecOps)
# 대시보드 시계열 차트(GET /api/v1/metrics/timeseries)의 CPU 축 데이터 원천입니다.
#
# **CloudWatch 를 그 자리에서 부른다 — 적재된 metric_summaries 를 읽지 않는다.**
#   적재분(cpu_avg)은 METRIC_LOOKBACK_DAYS(기본 14일) 창의 평균이고, 같은 메트릭 입자
#   안에서는 회차 간 그대로 복사된다(#255). 그것으로 곡선을 그리면 값이 변하지 않는
#   수평 직선이 나온다. 차트가 필요한 것은 요약 이전의 원계열이다.
#
# 조회 비용: 인스턴스 1대당 쿼리 1개(CPU 단일 메트릭), 리전당 get_metric_data 1회.
# ==============================================================================

from __future__ import annotations

import logging
from datetime import datetime
from typing import NamedTuple, Sequence

from schemas.api.metrics import CpuSeries, NetworkSeries, TimeseriesPoint
from schemas.assets import MetricName, MetricSeries

from services.aws.client import aws_client
from services.collector import _fetch_metrics

_log = logging.getLogger(__name__)


class Ec2Ref(NamedTuple):
    """CPU 곡선을 그릴 EC2 1대의 식별 정보. DB 자산 행에서 뽑는다."""

    arn: str
    resource_id: str  # = InstanceId (CloudWatch Dimension 값)
    name: str | None
    region: str


def cpu_timeseries(
    instances: Sequence[Ec2Ref],
    *,
    window_start: datetime,
    window_end: datetime,
    period_seconds: int,
) -> list[CpuSeries]:
    """EC2별 CPU 곡선. 리전별로 배치 조회한 뒤 자산 순서대로 되돌린다.

    **관측치가 0개인 인스턴스도 빈 줄로 남긴다** — 차트 범례에서 사라지면 "CPU 가 0" 인지
    "메트릭이 없는 인스턴스" 인지 화면에서 구분되지 않는다. LocalStack 은 비영속이라
    재시작 후 시드를 다시 넣기 전까지 실제로 이 상태가 된다.

    조회 실패는 삼키지 않는다 — 호출자(라우터)가 축 전체를 UNAVAILABLE 로 내린다.
    일부 리전만 성공한 반쪽 곡선을 성공처럼 보여 주지 않기 위해서다.
    """
    by_region: dict[str, list[Ec2Ref]] = {}
    for ref in instances:
        by_region.setdefault(ref.region, []).append(ref)

    values_by_id: dict[str, list[TimeseriesPoint]] = {}
    for region, refs in by_region.items():
        cw = aws_client("cloudwatch", region)
        fetched = _fetch_metrics(
            cw,
            [r.resource_id for r in refs],
            window_start,
            window_end,
            period_seconds,
            metrics=(MetricName.CPU_UTILIZATION,),
        )
        for instance_id, by_metric in fetched.items():
            series = by_metric.get(MetricName.CPU_UTILIZATION)
            if series is None:
                continue
            values_by_id[instance_id] = _points(series)

    return [
        CpuSeries(
            arn=ref.arn,
            resource_id=ref.resource_id,
            name=ref.name,
            points=values_by_id.get(ref.resource_id, []),
        )
        for ref in instances
    ]


def network_timeseries(
    instances: Sequence[Ec2Ref],
    *,
    window_start: datetime,
    window_end: datetime,
    period_seconds: int,
) -> list[NetworkSeries]:
    """EC2별 네트워크 처리량(In·Out) 곡선. CPU 곡선과 같은 규칙을 따른다.

    **CPU 와 한 번에 긁지 않는다.** 한 호출로 합치면 쿼리 수는 아끼지만 실패가 묶여, CPU 곡선만
    살릴 수 있는 경우에도 두 축이 함께 내려간다 — 축마다 상태를 매기는 계약이 그 대가를 치르지
    않으려고 만든 것이다(schemas/api/metrics.NetworkAxis).

    조회 비용: 인스턴스 1대당 쿼리 2개(In·Out), 리전당 get_metric_data 1회.
    값은 **period 당 바이트**를 그대로 싣는다 — 초당으로 환산하는 것은 화면 몫이다.
    """
    by_region: dict[str, list[Ec2Ref]] = {}
    for ref in instances:
        by_region.setdefault(ref.region, []).append(ref)

    points_by_id: dict[str, dict[MetricName, list[TimeseriesPoint]]] = {}
    for region, refs in by_region.items():
        cw = aws_client("cloudwatch", region)
        fetched = _fetch_metrics(
            cw,
            [r.resource_id for r in refs],
            window_start,
            window_end,
            period_seconds,
            metrics=(MetricName.NETWORK_IN, MetricName.NETWORK_OUT),
        )
        for instance_id, by_metric in fetched.items():
            points_by_id[instance_id] = {
                name: _points(series) for name, series in by_metric.items()
            }

    return [
        NetworkSeries(
            arn=ref.arn,
            resource_id=ref.resource_id,
            name=ref.name,
            in_points=points_by_id.get(ref.resource_id, {}).get(MetricName.NETWORK_IN, []),
            out_points=points_by_id.get(ref.resource_id, {}).get(MetricName.NETWORK_OUT, []),
        )
        for ref in instances
    ]


def _points(series: MetricSeries) -> list[TimeseriesPoint]:
    """관측 시계열 1개를 계약 점 목록으로. **정렬은 여기서 한 번 더 세운다** —
    ScanBy=TimestampAscending 으로 받아도 페이지가 갈리면 이어붙인 순서가 보장되지 않는다.

    음수는 버린다. CloudWatch 가 음의 바이트를 주지는 않지만, 계약이 0 이상을 요구하므로
    이상값 하나로 축 전체가 검증에서 떨어지는 것보다 그 점만 빠지는 편이 낫다.
    """
    return sorted(
        (
            TimeseriesPoint(at=ts, value=round(value, 2))
            for ts, value in zip(series.timestamps, series.values)
            if value >= 0
        ),
        key=lambda p: p.at,
    )
