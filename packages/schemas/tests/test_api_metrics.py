"""GET /api/v1/metrics/timeseries 외부 DTO 계약 테스트.

축마다 따로 매기는 상태(AxisStatus)와 데이터·사유의 교차 불변식, 시각 오름차순,
"Z" 직렬화를 검증한다.
"""

import pytest
from pydantic import ValidationError

from schemas.api import (
    AssetInventoryAxis,
    AssetInventoryPoint,
    AssetStatusAxis,
    AssetStatusPoint,
    AxisStatus,
    CpuAxis,
    CpuSeries,
    MetricsTimeseriesResponse,
    NetworkAxis,
    NetworkSeries,
    SgExposureAxis,
)

ARN = "arn:aws:ec2:ap-northeast-2:123456789012:instance/i-0123"


def make_cpu_axis(**over):
    base = {
        "status": "READY",
        "period_seconds": 3600,
        "window_start": "2026-09-15T00:00:00Z",
        "window_end": "2026-09-18T00:00:00Z",
        "idle_cpu_avg_threshold": 5.0,
        "series": [
            {
                "arn": ARN,
                "resource_id": "i-0123",
                "name": "vigilantis-seed-idle",
                "points": [
                    {"at": "2026-09-17T23:00:00Z", "value": 1.5},
                    {"at": "2026-09-18T00:00:00Z", "value": 2.0},
                ],
            }
        ],
    }
    base.update(over)
    return base


def make_network_axis(**over):
    base = {
        "status": "READY",
        "period_seconds": 3600,
        "window_start": "2026-09-15T00:00:00Z",
        "window_end": "2026-09-18T00:00:00Z",
        "series": [
            {
                "arn": ARN,
                "resource_id": "i-0123",
                "name": "vigilantis-seed-idle",
                "in_points": [{"at": "2026-09-17T23:00:00Z", "value": 1024.0}],
                "out_points": [{"at": "2026-09-17T23:00:00Z", "value": 2048.0}],
            }
        ],
    }
    base.update(over)
    return base


def make_sg_axis(**over):
    base = {
        "status": "READY",
        "points": [
            {"at": "2026-09-17T23:00:00Z", "value": 1},
            {"at": "2026-09-18T00:00:00Z", "value": 2},
        ],
    }
    base.update(over)
    return base


def make_asset_status_point(**over):
    base = {
        "at": "2026-09-18T00:00:00Z",
        "judged": 13,
        "threat": 2,
        "cost_candidate": 1,
        "unused": 2,
        "skip": 7,
        "undecided": 1,
    }
    base.update(over)
    return base


def make_asset_status_axis(**over):
    base = {
        "status": "READY",
        "points": [
            make_asset_status_point(at="2026-09-17T23:00:00Z"),
            make_asset_status_point(),
        ],
    }
    base.update(over)
    return base


def make_inventory_point(**over):
    base = {
        "at": "2026-09-18T00:00:00Z",
        "total": 16,
        "counts": {"EC2": 4, "SG": 4, "EBS": 5, "NACL": 2, "LAUNCH_TEMPLATE": 1},
    }
    base.update(over)
    return base


def make_inventory_axis(**over):
    base = {
        "status": "READY",
        "points": [make_inventory_point(at="2026-09-17T23:00:00Z"), make_inventory_point()],
    }
    base.update(over)
    return base


def test_정상_응답이_Z_시각으로_직렬화된다():
    res = MetricsTimeseriesResponse(
        generated_at="2026-09-18T01:00:00Z",
        cpu=make_cpu_axis(),
        network=make_network_axis(),
        sg_exposure=make_sg_axis(),
        asset_status=make_asset_status_axis(),
        asset_inventory=make_inventory_axis(),
    )
    dumped = res.model_dump(mode="json")

    assert dumped["generated_at"] == "2026-09-18T01:00:00Z"
    assert dumped["cpu"]["window_end"] == "2026-09-18T00:00:00Z"
    assert dumped["cpu"]["series"][0]["points"][0]["at"] == "2026-09-17T23:00:00Z"
    assert dumped["sg_exposure"]["points"][-1]["value"] == 2
    assert dumped["asset_status"]["points"][-1] == make_asset_status_point()
    assert dumped["asset_inventory"]["points"][-1] == make_inventory_point()


def test_모르는_필드는_거부한다():
    with pytest.raises(ValidationError):
        MetricsTimeseriesResponse(
            generated_at="2026-09-18T01:00:00Z",
            cpu=make_cpu_axis(),
            network=make_network_axis(),
            sg_exposure=make_sg_axis(),
            asset_status=make_asset_status_axis(),
            asset_inventory=make_inventory_axis(),
            extra_axis={},
        )


def test_자산_현황_축이_빠지면_거부한다():
    """축 4는 선택이 아니다 — 빠진 응답을 화면이 '이력 없음'으로 읽지 않게 한다."""
    with pytest.raises(ValidationError):
        MetricsTimeseriesResponse(
            generated_at="2026-09-18T01:00:00Z",
            cpu=make_cpu_axis(),
            network=make_network_axis(),
            sg_exposure=make_sg_axis(),
            asset_inventory=make_inventory_axis(),
        )


def test_자산_수_축이_빠지면_거부한다():
    with pytest.raises(ValidationError):
        MetricsTimeseriesResponse(
            generated_at="2026-09-18T01:00:00Z",
            cpu=make_cpu_axis(),
            network=make_network_axis(),
            sg_exposure=make_sg_axis(),
            asset_status=make_asset_status_axis(),
        )


# ----- 축 5 자산 수 -----


def test_자산_수의_total은_관측한_유형의_합이다():
    AssetInventoryPoint(**make_inventory_point())
    with pytest.raises(ValidationError):
        AssetInventoryPoint(**make_inventory_point(total=17))
    with pytest.raises(ValidationError):
        AssetInventoryPoint(**make_inventory_point(total=-1, counts={"EC2": -1}))


def test_자산_수는_모르는_유형을_거부한다():
    with pytest.raises(ValidationError):
        AssetInventoryPoint(**make_inventory_point(total=1, counts={"RDS": 1}))


def test_자산_수_점은_시각_오름차순이고_중복되지_않는다():
    with pytest.raises(ValidationError):
        AssetInventoryAxis(**make_inventory_axis(points=[make_inventory_point(), make_inventory_point(at="2026-09-17T23:00:00Z")]))
    with pytest.raises(ValidationError):
        AssetInventoryAxis(**make_inventory_axis(points=[make_inventory_point(), make_inventory_point()]))


def test_자산_수_축도_상태와_데이터가_어긋나면_거부한다():
    AssetInventoryAxis(status="UNAVAILABLE", reason_code="OperationalError")
    AssetInventoryAxis(status="READY", points=[])
    with pytest.raises(ValidationError):
        AssetInventoryAxis(**make_inventory_axis(status="UNAVAILABLE", reason_code="OperationalError"))
    with pytest.raises(ValidationError):
        AssetInventoryAxis(**make_inventory_axis(reason_code="OperationalError"))


# ----- 축 4 자산 현황 -----


def test_자산_현황의_합은_판정별_건수와_같아야_한다():
    AssetStatusPoint(**make_asset_status_point())
    with pytest.raises(ValidationError):
        AssetStatusPoint(**make_asset_status_point(judged=12))
    # 판정 대기·실패도 합에 든다 — 빼면 judged 가 '판정이 끝난 자산 수'로 뜻이 바뀐다
    with pytest.raises(ValidationError):
        AssetStatusPoint(**make_asset_status_point(undecided=0))


def test_자산_현황_건수는_음수일_수_없다():
    with pytest.raises(ValidationError):
        AssetStatusPoint(**make_asset_status_point(threat=-1, judged=10))


def test_자산_현황_점은_시각_오름차순이고_중복되지_않는다():
    late, early = "2026-09-18T00:00:00Z", "2026-09-17T23:00:00Z"
    with pytest.raises(ValidationError):
        AssetStatusAxis(**make_asset_status_axis(points=[
            make_asset_status_point(at=late), make_asset_status_point(at=early),
        ]))
    with pytest.raises(ValidationError):
        AssetStatusAxis(**make_asset_status_axis(points=[
            make_asset_status_point(at=late), make_asset_status_point(at=late),
        ]))


def test_자산_현황_축도_상태와_데이터가_어긋나면_거부한다():
    AssetStatusAxis(status="UNAVAILABLE", reason_code="OperationalError")
    AssetStatusAxis(status="READY", points=[])  # 회차가 아직 없는 계정
    with pytest.raises(ValidationError):
        AssetStatusAxis(**make_asset_status_axis(status="UNAVAILABLE", reason_code="OperationalError"))
    with pytest.raises(ValidationError):
        AssetStatusAxis(**make_asset_status_axis(reason_code="OperationalError"))


# ----- 축 상태 ↔ 데이터·사유 교차 불변식 -----


def test_UNAVAILABLE이면_사유가_필수다():
    with pytest.raises(ValidationError, match="reason_code 가 필수"):
        CpuAxis(
            status=AxisStatus.UNAVAILABLE,
            period_seconds=None,
            window_start=None,
            window_end=None,
            idle_cpu_avg_threshold=None,
        )


def test_UNAVAILABLE인데_데이터가_실리면_거부한다():
    """실패를 성공처럼 그리게 두지 않는다."""
    with pytest.raises(ValidationError, match="series 는 비어 있어야"):
        CpuAxis(**make_cpu_axis(
            status="UNAVAILABLE",
            reason_code="InternalFailure",
            period_seconds=None,
            window_start=None,
            window_end=None,
            idle_cpu_avg_threshold=None,
        ))

    with pytest.raises(ValidationError, match="points 는 비어 있어야"):
        SgExposureAxis(**make_sg_axis(status="UNAVAILABLE", reason_code="OperationalError"))


def test_READY인데_사유가_실리면_거부한다():
    with pytest.raises(ValidationError, match="reason_code 는 null"):
        SgExposureAxis(**make_sg_axis(reason_code="InternalFailure"))


def test_CPU축은_READY면_창과_임계치가_모두_필요하다():
    with pytest.raises(ValidationError, match="idle_cpu_avg_threshold 가 모두 필요"):
        CpuAxis(**make_cpu_axis(idle_cpu_avg_threshold=None))


def test_CPU축은_UNAVAILABLE이면_창과_임계치가_null이어야_한다():
    with pytest.raises(ValidationError, match="창·임계치 필드는 null"):
        CpuAxis(
            status="UNAVAILABLE",
            reason_code="AccessDenied",
            period_seconds=3600,
        )


def test_창이_뒤집히면_거부한다():
    with pytest.raises(ValidationError, match="window_end 는 window_start 이상"):
        CpuAxis(**make_cpu_axis(
            window_start="2026-09-18T00:00:00Z", window_end="2026-09-15T00:00:00Z"
        ))


# ----- 시계열 정렬·중복 -----


def test_점이_시각_역순이면_거부한다():
    """화면이 정렬을 다시 하지 않아도 되게 서버가 보장한다."""
    with pytest.raises(ValidationError, match="오름차순"):
        CpuSeries(
            arn=ARN,
            resource_id="i-0123",
            points=[
                {"at": "2026-09-18T00:00:00Z", "value": 2.0},
                {"at": "2026-09-17T23:00:00Z", "value": 1.5},
            ],
        )

    with pytest.raises(ValidationError, match="오름차순"):
        SgExposureAxis(**make_sg_axis(points=[
            {"at": "2026-09-18T00:00:00Z", "value": 2},
            {"at": "2026-09-17T23:00:00Z", "value": 1},
        ]))


def test_같은_시각이_두_번_오면_거부한다():
    with pytest.raises(ValidationError, match="중복될 수 없습니다"):
        SgExposureAxis(**make_sg_axis(points=[
            {"at": "2026-09-18T00:00:00Z", "value": 1},
            {"at": "2026-09-18T00:00:00Z", "value": 2},
        ]))


def test_같은_자산이_두_줄이면_거부한다():
    series = {
        "arn": ARN,
        "resource_id": "i-0123",
        "points": [{"at": "2026-09-18T00:00:00Z", "value": 1.0}],
    }
    with pytest.raises(ValidationError, match="arn 은 중복될 수 없습니다"):
        CpuAxis(**make_cpu_axis(series=[series, dict(series)]))


def test_SG_건수는_음수나_소수일_수_없다():
    with pytest.raises(ValidationError, match="0 이상의 정수"):
        SgExposureAxis(**make_sg_axis(points=[{"at": "2026-09-18T00:00:00Z", "value": -1}]))

    with pytest.raises(ValidationError, match="0 이상의 정수"):
        SgExposureAxis(**make_sg_axis(points=[{"at": "2026-09-18T00:00:00Z", "value": 1.5}]))


# ----- 네트워크 축 -----


def test_네트워크축은_한_자산에_두_방향을_싣는다():
    axis = NetworkAxis(**make_network_axis())
    dumped = axis.model_dump(mode="json")
    assert dumped["series"][0]["in_points"][0]["value"] == 1024.0
    assert dumped["series"][0]["out_points"][0]["value"] == 2048.0


def test_네트워크_방향별로_정렬과_음수를_따로_본다():
    """In 만 검사하고 Out 을 놓치면 한쪽 곡선이 뒤죽박죽인 채 통과한다."""
    with pytest.raises(ValidationError, match="out_points 는 시각 오름차순"):
        NetworkSeries(
            arn=ARN,
            resource_id="i-0123",
            out_points=[
                {"at": "2026-09-18T00:00:00Z", "value": 2.0},
                {"at": "2026-09-17T23:00:00Z", "value": 1.0},
            ],
        )

    with pytest.raises(ValidationError, match="in_points 의 값은 0 이상"):
        NetworkSeries(
            arn=ARN,
            resource_id="i-0123",
            in_points=[{"at": "2026-09-18T00:00:00Z", "value": -1.0}],
        )


def test_네트워크축은_UNAVAILABLE이면_창이_null이어야_한다():
    with pytest.raises(ValidationError, match="창 필드는 null"):
        NetworkAxis(status="UNAVAILABLE", reason_code="Throttling", period_seconds=3600)


def test_빈_축도_READY일_수_있다():
    """수집이 아직 없어 점이 0개인 것은 실패가 아니다 — 화면은 빈 차트를 그린다."""
    axis = SgExposureAxis(status="READY", points=[])
    assert axis.reason_code is None
