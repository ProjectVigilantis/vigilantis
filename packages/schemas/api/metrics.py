# ==============================================================================
# [파일 설명]  담당: 김세혁 (PM · Infra & DevSecOps)
# GET /api/v1/metrics/timeseries 외부 응답 DTO입니다. 대시보드 시계열 차트 2종의
# 공개 계약이며, GET /api/v1/assets(스냅샷)와는 별개의 엔드포인트입니다.
#
# 왜 /assets 에 얹지 않았나
#   - /assets 응답은 `extra="forbid"` 인 자산 1건의 **현재 상태**다. 시계열을 얹으면
#     자산 1건의 표현에 시간축이 섞이고, 스냅샷만 필요한 화면도 CloudWatch 조회를
#     기다리게 된다. 계약을 나눠 실패도 따로 받는다.
#
# 계약 원칙 (assets.py 와 동일)
#   - snake_case, UTC ISO 8601("Z") 문자열, nullable 은 null 반환.
#   - AWS 원본 응답·ORM 객체를 그대로 노출하지 않는다.
#   - 축마다 상태(status)를 따로 둔다 — 두 축의 원천이 다르기 때문이다(아래).
# ==============================================================================

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Annotated, Optional

from pydantic import BaseModel, ConfigDict, Field, PlainSerializer, model_validator


def _to_utc_z(v: datetime) -> str:
    if v.tzinfo is None:
        v = v.replace(tzinfo=timezone.utc)
    return v.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


UtcDateTime = Annotated[datetime, PlainSerializer(_to_utc_z, return_type=str, when_used="json")]


class AxisStatus(str, Enum):
    """축 하나의 조회 결과. **축마다 따로 매긴다.**

    두 축의 원천이 다르기 때문이다 — CPU 는 CloudWatch 를 그 자리에서 부르고,
    SG 개방 건수는 이미 적재된 판정 이력을 읽는다. 한 축이 실패했다고 다른 축을
    비우면 화면은 "둘 다 없음"으로 그려져, 실제로 살아 있는 축까지 못 보게 된다.
    """

    READY = "READY"
    #: 조회 실패. `reason_code` 가 원인을 싣고 데이터는 비어 있다.
    UNAVAILABLE = "UNAVAILABLE"


class TimeseriesPoint(BaseModel):
    """시각 하나의 값. 두 축이 같은 모양을 쓴다 — 화면이 x축을 한 번만 다루면 된다."""

    model_config = ConfigDict(extra="forbid")

    at: UtcDateTime
    value: float


class CpuSeries(BaseModel):
    """EC2 한 대의 CPU 곡선."""

    model_config = ConfigDict(extra="forbid")

    arn: str = Field(min_length=1)
    resource_id: str = Field(min_length=1)
    #: Name 태그. 없는 인스턴스가 있으므로 화면은 null 이면 `resource_id` 로 대신한다.
    name: Optional[str] = None
    points: list[TimeseriesPoint] = Field(default_factory=list)

    @model_validator(mode="after")
    def _enforce_contract(self):
        ats = [p.at for p in self.points]
        if ats != sorted(ats):
            raise ValueError("points 는 시각 오름차순이어야 합니다")
        if len(ats) != len(set(ats)):
            raise ValueError("points 의 at 은 중복될 수 없습니다")
        return self


class CpuAxis(BaseModel):
    """축 1 — EC2별 CPU 추이 + IDLE_CPU_AVG 임계선.

    **이 축의 값은 CloudWatch 원계열이지 `metric_summaries` 의 요약이 아니다.**
    적재된 요약(`cpu_avg`)은 `METRIC_LOOKBACK_DAYS`(기본 14일) 창의 평균이고 같은
    입자 안에서는 회차 간 복사되므로, 그것으로 곡선을 그리면 수평 직선이 된다.
    """

    model_config = ConfigDict(extra="forbid")

    status: AxisStatus
    #: 데이터 포인트 간격(초). Rule 판정이 쓰는 METRIC_PERIOD_SECONDS 와 같은 값이다.
    period_seconds: Optional[int] = Field(None, gt=0)
    window_start: Optional[UtcDateTime] = None
    window_end: Optional[UtcDateTime] = None
    #: Rule Evaluator 의 저활성 판정 임계치(services/rule_engine.IDLE_CPU_AVG).
    #: 화면이 상수를 따로 갖지 않게 서버가 싣는다 — 두 곳에 두면 임계치를 고칠 때 갈린다.
    idle_cpu_avg_threshold: Optional[float] = Field(None, gt=0)
    series: list[CpuSeries] = Field(default_factory=list)
    #: UNAVAILABLE 일 때의 원인. AWS 오류 코드 또는 예외 클래스명 — 자유 문자열이다.
    reason_code: Optional[str] = Field(None, min_length=1)

    @model_validator(mode="after")
    def _enforce_contract(self):
        _enforce_axis_status(self, data_fields=("series",))

        required = (
            self.period_seconds,
            self.window_start,
            self.window_end,
            self.idle_cpu_avg_threshold,
        )
        if self.status == AxisStatus.READY:
            if any(v is None for v in required):
                raise ValueError(
                    "READY 이면 period_seconds·window_start·window_end·"
                    "idle_cpu_avg_threshold 가 모두 필요합니다"
                )
            if self.window_end < self.window_start:
                raise ValueError("window_end 는 window_start 이상이어야 합니다")
        else:
            if any(v is not None for v in required):
                raise ValueError("UNAVAILABLE 이면 창·임계치 필드는 null 이어야 합니다")

        arns = [s.arn for s in self.series]
        if len(arns) != len(set(arns)):
            raise ValueError("series 의 arn 은 중복될 수 없습니다")
        return self


class NetworkSeries(BaseModel):
    """EC2 한 대의 네트워크 처리량 곡선. 들어온 것과 나간 것을 **한 자산 안에서 짝으로** 싣는다.

    두 방향을 각각의 series 로 쪼개지 않는 이유: 화면이 한 인스턴스의 In/Out 을 겹쳐 그려야
    "받기만 하는가, 내보내기만 하는가"가 읽힌다. 따로 실으면 화면이 arn 으로 다시 짝지어야 한다.

    **값의 단위는 period 당 바이트다**(CloudWatch ``NetworkIn``/``NetworkOut`` 의 Average).
    초당 처리량으로 읽으려면 축의 ``period_seconds`` 로 나눈다 — 나누는 쪽을 화면에 두는 것은
    CPU 축이 원계열을 그대로 싣는 것과 같은 원칙이다(서버는 관측값을 가공하지 않는다).
    """

    model_config = ConfigDict(extra="forbid")

    arn: str = Field(min_length=1)
    resource_id: str = Field(min_length=1)
    name: Optional[str] = None
    in_points: list[TimeseriesPoint] = Field(default_factory=list)
    out_points: list[TimeseriesPoint] = Field(default_factory=list)

    @model_validator(mode="after")
    def _enforce_contract(self):
        for field in ("in_points", "out_points"):
            points = getattr(self, field)
            ats = [p.at for p in points]
            if ats != sorted(ats):
                raise ValueError(f"{field} 는 시각 오름차순이어야 합니다")
            if len(ats) != len(set(ats)):
                raise ValueError(f"{field} 의 at 은 중복될 수 없습니다")
            if any(p.value < 0 for p in points):
                raise ValueError(f"{field} 의 값은 0 이상이어야 합니다")
        return self


class NetworkAxis(BaseModel):
    """축 3 — EC2별 네트워크 처리량 추이.

    CPU 축과 원천이 같은 CloudWatch 지만 **축을 따로 둔다.** 조회 쿼리가 인스턴스당 2개 더
    붙어(실 계정 과금 단위가 쿼리 수다) 한쪽만 실패하는 경우가 실제로 생기고, 그때 CPU 곡선까지
    비우지 않기 위해서다 — 축마다 상태를 매기는 이 계약의 원칙 그대로다.

    화면에서의 쓸모: CPU 가 낮은데 네트워크가 살아 있으면 저활성 판정의 반례다(프록시·NAT 처럼
    CPU 를 거의 안 쓰는 워크로드). 다운사이징 승인 전에 보는 자리다.
    """

    model_config = ConfigDict(extra="forbid")

    status: AxisStatus
    period_seconds: Optional[int] = Field(None, gt=0)
    window_start: Optional[UtcDateTime] = None
    window_end: Optional[UtcDateTime] = None
    series: list[NetworkSeries] = Field(default_factory=list)
    reason_code: Optional[str] = Field(None, min_length=1)

    @model_validator(mode="after")
    def _enforce_contract(self):
        _enforce_axis_status(self, data_fields=("series",))

        required = (self.period_seconds, self.window_start, self.window_end)
        if self.status == AxisStatus.READY:
            if any(v is None for v in required):
                raise ValueError("READY 이면 period_seconds·window_start·window_end 가 모두 필요합니다")
            if self.window_end < self.window_start:
                raise ValueError("window_end 는 window_start 이상이어야 합니다")
        elif any(v is not None for v in required):
            raise ValueError("UNAVAILABLE 이면 창 필드는 null 이어야 합니다")

        arns = [s.arn for s in self.series]
        if len(arns) != len(set(arns)):
            raise ValueError("series 의 arn 은 중복될 수 없습니다")
        return self


class SgExposureAxis(BaseModel):
    """축 2 — 인터넷 개방 SG 건수 추이.

    수집 회차(`collection_runs`)마다 남은 Rule 판정에서 센다. 자산 행(`assets.spec`)은
    회차마다 덮어쓰지만 판정은 (자산 × 회차)로 보존되므로, 이력은 그쪽에만 있다.

    **`value` 는 그 회차에 `THREAT` 판정을 받은 SG 의 수다.** default SG 는
    전체 개방이라도 화이트리스트로 먼저 빠지므로(`SKIP_WHITELISTED`) 세지 않는다 —
    삭제·변경 대상이 아니라서 "조치할 개방"이 아니기 때문이다.
    """

    model_config = ConfigDict(extra="forbid")

    status: AxisStatus
    #: x축은 회차의 **시작 시각**이다. 스캔이 멈춘 구간은 점이 없다 — 0건과 구분된다.
    points: list[TimeseriesPoint] = Field(default_factory=list)
    reason_code: Optional[str] = Field(None, min_length=1)

    @model_validator(mode="after")
    def _enforce_contract(self):
        _enforce_axis_status(self, data_fields=("points",))

        ats = [p.at for p in self.points]
        if ats != sorted(ats):
            raise ValueError("points 는 시각 오름차순이어야 합니다")
        if len(ats) != len(set(ats)):
            raise ValueError("points 의 at 은 중복될 수 없습니다")
        if any(p.value < 0 or p.value != int(p.value) for p in self.points):
            raise ValueError("건수는 0 이상의 정수여야 합니다")
        return self


def _enforce_axis_status(axis, *, data_fields: tuple[str, ...]) -> None:
    """축 공통 불변식 — 상태와 데이터·사유가 어긋나지 않게 한다.

    UNAVAILABLE 인데 데이터가 실려 있으면 화면이 실패를 성공처럼 그린다. 반대로
    READY 인데 사유가 실려 있으면 어느 쪽이 사실인지 알 수 없다.
    """
    if axis.status == AxisStatus.UNAVAILABLE:
        if axis.reason_code is None:
            raise ValueError("UNAVAILABLE 이면 reason_code 가 필수입니다")
        for field in data_fields:
            if getattr(axis, field):
                raise ValueError(f"UNAVAILABLE 이면 {field} 는 비어 있어야 합니다")
    elif axis.reason_code is not None:
        raise ValueError("READY 이면 reason_code 는 null 이어야 합니다")


class MetricsTimeseriesResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: 응답을 만든 시각. 축들의 x축 오른쪽 끝을 화면이 같은 기준으로 맞출 때 쓴다.
    generated_at: UtcDateTime
    cpu: CpuAxis
    network: NetworkAxis
    sg_exposure: SgExposureAxis
