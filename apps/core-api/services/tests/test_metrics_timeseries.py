"""시계열 차트 축(services/metrics.py) 단위 테스트 — DB·LocalStack 불필요.

가짜 CloudWatch 로 **SDK 응답 대역**을 직접 만든다. LocalStack 시드로는 재현되지 않는 두 가지가
여기 있다(PR #387 리뷰):

  1. **한 period 안의 표본이 여럿인 경우** — 표본이 하나면 Sum 과 Average 가 같은 값이라
     차트가 어느 통계로 읽는지가 드러나지 않는다. 계약은 period 총 바이트이므로 Sum 이어야 한다.
  2. **지표 단위 오류(`MetricDataResult.StatusCode`)** — get_metric_data 는 쿼리 하나가
     실패해도 호출이 200 으로 돌아온다. 그 상태를 버리면 실패가 정상 무관측이 된다.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

CORE_API = Path(__file__).resolve().parents[2]
REPO_ROOT = Path(__file__).resolve().parents[4]
for p in (str(CORE_API), str(REPO_ROOT / "packages")):
    if p not in sys.path:
        sys.path.insert(0, p)

from schemas.assets import MetricName  # noqa: E402
from services.collector import _failure_reason, _fetch_metrics  # noqa: E402
from services.metrics import (  # noqa: E402
    Ec2Ref,
    MetricDataError,
    cpu_timeseries,
    network_timeseries,
)

REGION = "ap-northeast-2"
PERIOD = 3600
WINDOW_END = datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc)
WINDOW_START = WINDOW_END - timedelta(hours=3)


def _ref(suffix: str = "0001") -> Ec2Ref:
    return Ec2Ref(
        arn=f"arn:aws:ec2:{REGION}:123456789012:instance/i-{suffix}",
        resource_id=f"i-{suffix}",
        name=f"vig-{suffix}",
        region=REGION,
    )


class _FakeCloudWatch:
    """get_metric_data 만 흉내내는 가짜 클라이언트.

    `results` 는 (쿼리 Id 접미사 → 결과 조각) 지도다 — 접미사는 `_fetch_metrics` 가 만드는
    `q{idx}_{metric}` 의 뒷부분(`cpu_utilization`·`network_in`·`network_out`)이다. 지도에
    없는 쿼리는 `Complete` + 빈 관측으로 답한다.
    """

    def __init__(self, results: dict[str, dict] | None = None, pages: list[list[dict]] | None = None):
        self._results = results or {}
        self._pages = pages
        self.calls: list[dict] = []

    def get_metric_data(self, **kwargs):
        self.calls.append(kwargs)
        if self._pages is not None:
            index = len(self.calls) - 1
            page = self._pages[index]
            token = "next" if index + 1 < len(self._pages) else None
            return {"MetricDataResults": page, **({"NextToken": token} if token else {})}

        out = []
        for q in kwargs["MetricDataQueries"]:
            suffix = q["Id"].split("_", 1)[1]
            chunk = self._results.get(suffix, {"StatusCode": "Complete"})
            out.append({"Id": q["Id"], "Timestamps": [], "Values": [], **chunk})
        return {"MetricDataResults": out}


@pytest.fixture
def fake_cw(monkeypatch):
    """`services.metrics` 가 부르는 클라이언트 팩토리를 가짜로 바꾼다."""

    def _install(client) -> None:
        monkeypatch.setattr("services.metrics.aws_client", lambda service, region: client)

    return _install


def _stat_of(call: dict, suffix: str) -> str:
    query = next(q for q in call["MetricDataQueries"] if q["Id"].endswith(suffix))
    return query["MetricStat"]["Stat"]


# ---------------------------------------------------------------- 통계(Stat)


def test_네트워크_차트는_Sum_으로_조회한다(fake_cw):
    """계약은 period **총** 바이트다 — 화면이 period_seconds 로 나눠 초당으로 읽는다."""
    cw = _FakeCloudWatch()
    fake_cw(cw)

    network_timeseries(
        [_ref()], window_start=WINDOW_START, window_end=WINDOW_END, period_seconds=PERIOD
    )

    assert _stat_of(cw.calls[0], "network_in") == "Sum"
    assert _stat_of(cw.calls[0], "network_out") == "Sum"


def test_CPU_축은_Average_를_유지한다(fake_cw):
    """사용률은 비율이라 합계에 뜻이 없다. 수집 경로의 요약과도 같은 통계여야 한다."""
    cw = _FakeCloudWatch()
    fake_cw(cw)

    cpu_timeseries(
        [_ref()], window_start=WINDOW_START, window_end=WINDOW_END, period_seconds=PERIOD
    )

    assert _stat_of(cw.calls[0], "cpu_utilization") == "Average"


def test_수집_경로의_기본_통계는_Average_그대로다():
    """차트가 Sum 을 쓰게 되어도 `_summarize` 의 평균(net_in_avg) 뜻이 바뀌지 않아야 한다."""
    cw = _FakeCloudWatch()

    _fetch_metrics(cw, ["i-0001"], WINDOW_START, WINDOW_END, PERIOD)

    stats = {q["MetricStat"]["Stat"] for q in cw.calls[0]["MetricDataQueries"]}
    assert stats == {"Average"}


def test_한_구간에_표본이_여럿이면_Sum_이_총량을_싣는다(fake_cw):
    """리뷰 지적의 실제 숫자 — 매분 60,000B 표본 60개짜리 1시간 구간.

    Sum 이면 3,600,000B/시간 → 화면의 초당 환산(÷3600)이 1,000 B/s 로 맞는다. Average 였다면
    60,000 이 실려 16.67 B/s 가 된다. 응답 자체가 통계의 차이를 담으므로, 여기서는 **그 값이
    가공 없이 계약에 실리는지**를 본다(초당 환산은 화면 몫이다).
    """
    per_minute, minutes = 60_000.0, 60
    cw = _FakeCloudWatch(
        {
            "network_in": {
                "Timestamps": [WINDOW_END - timedelta(hours=1)],
                "Values": [per_minute * minutes],
                "StatusCode": "Complete",
            },
            "network_out": {
                "Timestamps": [WINDOW_END - timedelta(hours=1)],
                "Values": [per_minute * minutes],
                "StatusCode": "Complete",
            },
        }
    )
    fake_cw(cw)

    series = network_timeseries(
        [_ref()], window_start=WINDOW_START, window_end=WINDOW_END, period_seconds=PERIOD
    )

    assert [p.value for p in series[0].in_points] == [3_600_000.0]
    assert series[0].in_points[0].value / PERIOD == pytest.approx(1_000.0)


# ---------------------------------------------------------------- 지표 단위 오류


def test_지표_오류는_축을_실패로_올린다(fake_cw):
    """StatusCode 가 Complete 가 아니면 값이 비어도 '관측 없음'이 아니다."""
    cw = _FakeCloudWatch({"cpu_utilization": {"StatusCode": "InternalError"}})
    fake_cw(cw)

    with pytest.raises(MetricDataError) as exc:
        cpu_timeseries(
            [_ref()], window_start=WINDOW_START, window_end=WINDOW_END, period_seconds=PERIOD
        )

    # 축의 reason_code 로 그대로 나가는 값 — 클래스명으로 환원되면 원인이 사라진다.
    assert _failure_reason(exc.value) == "InternalError"


def test_한_방향만_실패해도_네트워크_축이_실패한다(fake_cw):
    """In 은 정상이고 Out 만 Forbidden 인 경우. 반쪽 곡선을 성공으로 그리면 화면에는
    '한 방향이 빠졌다'고 적을 자리가 없다."""
    cw = _FakeCloudWatch(
        {
            "network_in": {
                "Timestamps": [WINDOW_END - timedelta(hours=1)],
                "Values": [1_000.0],
                "StatusCode": "Complete",
            },
            "network_out": {"StatusCode": "Forbidden"},
        }
    )
    fake_cw(cw)

    with pytest.raises(MetricDataError) as exc:
        network_timeseries(
            [_ref()], window_start=WINDOW_START, window_end=WINDOW_END, period_seconds=PERIOD
        )

    assert _failure_reason(exc.value) == "Forbidden"


def test_오류_코드가_여러_종류면_모두_사유에_남는다(fake_cw):
    cw = _FakeCloudWatch(
        {"network_in": {"StatusCode": "Forbidden"}, "network_out": {"StatusCode": "InternalError"}}
    )
    fake_cw(cw)

    with pytest.raises(MetricDataError) as exc:
        network_timeseries(
            [_ref()], window_start=WINDOW_START, window_end=WINDOW_END, period_seconds=PERIOD
        )

    assert _failure_reason(exc.value) == "Forbidden|InternalError"


def test_전량_실패도_같은_경로로_올라간다(fake_cw):
    cw = _FakeCloudWatch(
        {"network_in": {"StatusCode": "Forbidden"}, "network_out": {"StatusCode": "Forbidden"}}
    )
    fake_cw(cw)

    with pytest.raises(MetricDataError):
        network_timeseries(
            [_ref("0001"), _ref("0002")],
            window_start=WINDOW_START,
            window_end=WINDOW_END,
            period_seconds=PERIOD,
        )


def test_정상_응답에는_오류가_없다():
    cw = _FakeCloudWatch(
        {
            "cpu_utilization": {
                "Timestamps": [WINDOW_END - timedelta(hours=1)],
                "Values": [12.5],
                "StatusCode": "Complete",
            }
        }
    )

    fetched = _fetch_metrics(
        cw, ["i-0001"], WINDOW_START, WINDOW_END, PERIOD, metrics=(MetricName.CPU_UTILIZATION,)
    )

    assert fetched.errors == {}
    assert fetched.series["i-0001"][MetricName.CPU_UTILIZATION].values == [12.5]


def test_상태를_싣지_않는_응답은_실패로_치지_않는다():
    """StatusCode 를 돌려주지 않는 구현(일부 스텁)에서 축이 통째로 죽으면 안 된다."""
    cw = _FakeCloudWatch({"cpu_utilization": {"Timestamps": [WINDOW_END], "Values": [3.0]}})

    fetched = _fetch_metrics(
        cw, ["i-0001"], WINDOW_START, WINDOW_END, PERIOD, metrics=(MetricName.CPU_UTILIZATION,)
    )

    assert fetched.errors == {}


def test_페이지_중간의_PartialData_는_오류가_아니다():
    """CloudWatch 는 이어질 페이지가 있으면 PartialData 로 답한다 — 마지막 페이지가 Complete 면
    그 쿼리는 성공이다. 중간 상태를 오류로 세면 큰 창의 조회가 전부 실패로 읽힌다."""
    cw = _FakeCloudWatch(
        pages=[
            [{"Id": "q0_cpu_utilization", "Timestamps": [WINDOW_END - timedelta(hours=2)],
              "Values": [1.0], "StatusCode": "PartialData"}],
            [{"Id": "q0_cpu_utilization", "Timestamps": [WINDOW_END - timedelta(hours=1)],
              "Values": [2.0], "StatusCode": "Complete"}],
        ]
    )

    fetched = _fetch_metrics(
        cw, ["i-0001"], WINDOW_START, WINDOW_END, PERIOD, metrics=(MetricName.CPU_UTILIZATION,)
    )

    assert fetched.errors == {}
    assert fetched.series["i-0001"][MetricName.CPU_UTILIZATION].values == [1.0, 2.0]


def test_마지막_페이지까지_PartialData_면_오류다():
    """끝까지 돌았는데도 부분 데이터면 곡선이 잘린 것이다 — 완전한 곡선으로 그리지 않는다."""
    cw = _FakeCloudWatch(
        pages=[
            [{"Id": "q0_cpu_utilization", "Timestamps": [WINDOW_END], "Values": [1.0],
              "StatusCode": "PartialData"}],
        ]
    )

    fetched = _fetch_metrics(
        cw, ["i-0001"], WINDOW_START, WINDOW_END, PERIOD, metrics=(MetricName.CPU_UTILIZATION,)
    )

    assert fetched.errors == {("i-0001", MetricName.CPU_UTILIZATION): "PartialData"}
