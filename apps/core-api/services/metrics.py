# ==============================================================================
# [파일 설명]  담당: 김세혁 (PM · Infra & DevSecOps)
# 대시보드 시계열 차트(GET /api/v1/metrics/timeseries)의 CPU 축 데이터 원천입니다.
#
# **CloudWatch 를 그 자리에서 부른다 — 적재된 metric_summaries 를 읽지 않는다.**
#   적재분(cpu_avg)은 METRIC_LOOKBACK_DAYS(기본 14일) 창의 평균이고, 같은 메트릭 입자
#   안에서는 회차 간 그대로 복사된다(#255). 그것으로 곡선을 그리면 값이 변하지 않는
#   수평 직선이 나온다. 차트가 필요한 것은 요약 이전의 원계열이다.
#
# 조회 비용: 인스턴스 1대당 쿼리 1개(CPU 단일 메트릭), (소유 고객사 × 리전)당 get_metric_data 1회.
#
# 고객사 자격(#533): 고객사가 소유한 인스턴스는 그 고객사 자격(tenant_scope)으로만 부른다. 한 호출에
# 다른 고객사의 인스턴스 ID 를 섞지 않고, 소유 없는 MVP 기록(access=None)은 프로세스 자격으로 따로 부른다.
# ==============================================================================

from __future__ import annotations

import logging
from contextlib import nullcontext
from datetime import datetime
from typing import NamedTuple, Sequence

from schemas.api.metrics import CpuSeries, NetworkSeries, TimeseriesPoint
from schemas.assets import MetricName, MetricSeries

from services.aws.client import TenantAccess, aws_client, tenant_scope
from services.collector import _fetch_metrics

_log = logging.getLogger(__name__)


class Ec2Ref(NamedTuple):
    """CPU 곡선을 그릴 EC2 1대의 식별 정보. DB 자산 행에서 뽑는다.

    ``access`` 는 자산 행의 소유(tenant_id)가 가리키는 고객사의 접근 재료다 — ARN · 리소스 ID 에서
    추론하지 않는다. None 은 소유 고객사가 없는 MVP 단일 계정 기록이며 프로세스 자격으로 부른다.
    """

    arn: str
    resource_id: str  # = InstanceId (CloudWatch Dimension 값)
    name: str | None
    region: str
    access: TenantAccess | None = None


class MetricDataError(RuntimeError):
    """get_metric_data 가 **지표 단위 오류**를 돌려준 경우(`StatusCode` != `Complete`).

    호출 자체는 200 이라 botocore 는 예외를 내지 않는다. 그대로 두면 실패한 지표가 빈
    시리즈로 남아 축이 `READY` + 빈 곡선이 되고, 화면은 "조회 실패"를 "관측 없음"으로
    그린다 — 둘은 관제자가 해야 할 일이 다르다(권한·한도를 고치는 일 ↔ 기다리는 일).
    그래서 **예외로 세워 축 상태로 올린다.**

    ``reason_code`` 는 라우터가 축의 사유로 그대로 싣는다(collector._failure_reason 이
    이 속성을 먼저 본다) — `Forbidden` 처럼 AWS 가 준 코드가 클래스명으로 환원되지 않게 한다.
    """

    def __init__(self, errors: dict[tuple[str, MetricName], str]) -> None:
        codes = sorted({code for code in errors.values()})
        self.reason_code = "|".join(codes) or "MetricDataError"
        super().__init__(f"CloudWatch 지표 오류 {len(errors)}건 — {self.reason_code}")


def cpu_timeseries(
    instances: Sequence[Ec2Ref],
    *,
    window_start: datetime,
    window_end: datetime,
    period_seconds: int,
) -> list[CpuSeries]:
    """EC2별 CPU 곡선. (소유 고객사 × 리전)별로 배치 조회한 뒤 자산 순서대로 되돌린다.

    **관측치가 0개인 인스턴스도 빈 줄로 남긴다** — 차트 범례에서 사라지면 "CPU 가 0" 인지
    "메트릭이 없는 인스턴스" 인지 화면에서 구분되지 않는다. LocalStack 은 비영속이라
    재시작 후 시드를 다시 넣기 전까지 실제로 이 상태가 된다.

    조회 실패는 삼키지 않는다 — 호출자(라우터)가 축 전체를 UNAVAILABLE 로 내린다.
    일부 리전만 성공한 반쪽 곡선을 성공처럼 보여 주지 않기 위해서다. **지표 단위 실패도
    같다**(MetricDataError) — 한 대만 Forbidden 이어도 나머지 곡선을 성공으로 그리면
    그 화면에는 "한 대가 빠졌다"고 적을 자리가 없다. 한 고객사만 실패해도 같다.
    """
    fetched = _fetch_by_owner(
        instances,
        window_start=window_start,
        window_end=window_end,
        period_seconds=period_seconds,
        metrics=(MetricName.CPU_UTILIZATION,),
        stat="Average",
    )
    return [
        CpuSeries(
            arn=ref.arn,
            resource_id=ref.resource_id,
            name=ref.name,
            points=_points_of(fetched, ref, MetricName.CPU_UTILIZATION),
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

    조회 비용: 인스턴스 1대당 쿼리 2개(In·Out), (소유 고객사 × 리전)당 get_metric_data 1회.

    **통계는 ``Sum`` 이다 — 수집 경로의 ``Average`` 와 다르다.** NetworkIn/Out 의 표본값은
    그 표본 구간(실 AWS 는 5분, 상세 모니터링이면 1분)에 오간 **바이트 수**이므로, period
    안에 표본이 여럿이면 Average 는 표본 하나치가 된다. 계약은 이 값을 period 전체의
    바이트로 적고 화면은 `period_seconds` 로 나눠 초당으로 읽으므로, Average 를 실으면
    초당 값이 표본 수만큼 작아진다(1시간 period·1분 표본이면 60분의 1). Sum 만이
    "그 구간에 오간 총 바이트"라는 계약의 뜻과 맞는다.
    """
    fetched = _fetch_by_owner(
        instances,
        window_start=window_start,
        window_end=window_end,
        period_seconds=period_seconds,
        metrics=(MetricName.NETWORK_IN, MetricName.NETWORK_OUT),
        stat="Sum",
    )
    return [
        NetworkSeries(
            arn=ref.arn,
            resource_id=ref.resource_id,
            name=ref.name,
            in_points=_points_of(fetched, ref, MetricName.NETWORK_IN),
            out_points=_points_of(fetched, ref, MetricName.NETWORK_OUT),
        )
        for ref in instances
    ]


#: 조회 결과의 자리 — (소유 고객사 자격, 리전, 인스턴스 ID).
_OwnedInstance = tuple[TenantAccess | None, str, str]


def _fetch_by_owner(
    instances: Sequence[Ec2Ref],
    *,
    window_start: datetime,
    window_end: datetime,
    period_seconds: int,
    metrics: tuple[MetricName, ...],
    stat: str,
) -> dict[_OwnedInstance, dict[MetricName, MetricSeries]]:
    """(소유 고객사 × 리전)마다 get_metric_data 를 부른다 — 고객사 인스턴스는 그 고객사 자격으로만.

    클라이언트는 tenant_scope 안에서 만든다(자격은 만들 때 정해진다). 소유 없는 기록(access=None)은
    문맥 없이 프로세스 자격으로 부른다. 결과를 인스턴스 ID 만으로 묶지 않는다 — 다른 고객 계정
    (LocalStack 포함)에서 같은 ID 가 나올 수 있어, ID 로만 묶으면 한 고객사의 곡선이 다른 고객사의
    ARN 에 실린다. 한 묶음이라도 지표 단위 오류가 있으면 MetricDataError 로 올린다.
    """
    groups: dict[tuple[TenantAccess | None, str], list[str]] = {}
    for ref in instances:
        groups.setdefault((ref.access, ref.region), []).append(ref.resource_id)

    out: dict[_OwnedInstance, dict[MetricName, MetricSeries]] = {}
    for (access, region), instance_ids in groups.items():
        with tenant_scope(access) if access is not None else nullcontext():
            fetched = _fetch_metrics(
                aws_client("cloudwatch", region),
                instance_ids,
                window_start,
                window_end,
                period_seconds,
                metrics=metrics,
                stat=stat,
            )
        if fetched.errors:
            raise MetricDataError(fetched.errors)
        for instance_id, by_metric in fetched.series.items():
            out[(access, region, instance_id)] = by_metric
    return out


def _points_of(
    fetched: dict[_OwnedInstance, dict[MetricName, MetricSeries]],
    ref: Ec2Ref,
    metric: MetricName,
) -> list[TimeseriesPoint]:
    """그 인스턴스(소유 · 리전 · ID)의 한 지표. 관측이 없으면 빈 줄이다."""
    series = fetched.get((ref.access, ref.region, ref.resource_id), {}).get(metric)
    return _points(series) if series is not None else []


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
