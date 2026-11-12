"""LocalStack 시드 데이터셋의 판정 분포 고정 테스트. AWS/DB 불필요 — 순수 함수 호출만 한다.

seed_localstack 의 INSTANCES·CPU 프로필을 그대로 evaluate_ec2 에 태워, 시드가 의도한
Verdict 분포를 유지하는지 검증한다. test_rule_engine 은 합성 케이스로 판정 규칙 자체를
검증하고, 이쪽은 "실제 시드가 그 규칙 아래에서 어떤 분포가 되는가"를 고정한다.

배경(#81/#88 회귀): Environment 태그 도입으로 idle 인스턴스가 prod 보호에 흡수되면서
COST_CANDIDATE 가 0대가 됐지만, 기존 테스트는 자산 개수만 >= 1 로 확인해 이를 놓쳤다.
"""

import importlib.util
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

CORE_API = Path(__file__).resolve().parents[2]
REPO_ROOT = Path(__file__).resolve().parents[4]
if str(CORE_API) not in sys.path:
    sys.path.insert(0, str(CORE_API))

from services.rule_engine import MIN_DATAPOINTS, SkipReason, Verdict, evaluate_ec2  # noqa: E402


def _load_seed_module():
    """scripts/seed_localstack.py 를 경로로 로드(패키지가 아니라 스크립트라 import 불가)."""
    path = REPO_ROOT / "scripts" / "seed_localstack.py"
    spec = importlib.util.spec_from_file_location("seed_localstack", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


SEED = _load_seed_module()

# 시드 인스턴스별 기대 판정 — 시드를 늘리면 여기도 함께 갱신해야 한다(아래 커버리지 테스트가 강제).
EXPECTED = {
    "vigilantis-seed-idle": (Verdict.SKIP, SkipReason.SKIP_PROD_PROTECTED),
    "vigilantis-seed-normal": (Verdict.SKIP, SkipReason.SKIP_ACTIVE),
    "vigilantis-seed-spike": (Verdict.SKIP, SkipReason.SKIP_LOW_UTIL),
    "vigilantis-seed-idle-dev": (Verdict.COST_CANDIDATE, None),
}


def _evaluate(name, profile, environment):
    series = SEED._cpu_series(profile)
    tags = {"Name": name}
    if environment:
        tags["Environment"] = environment
    # 시드는 run_instances 로 띄우기만 하고 멈추지 않는다 — 전부 running 이다
    return evaluate_ec2(sum(series) / len(series), max(series), len(series), tags, state="running")


def test_expected_covers_every_seed_instance():
    """시드에 인스턴스를 추가/삭제하면 기대 판정 표도 함께 고쳐야 한다."""
    assert {i[0] for i in SEED.INSTANCES} == set(EXPECTED)


@pytest.mark.parametrize("entry", SEED.INSTANCES, ids=lambda e: e[0])
def test_seed_instance_verdict(entry):
    name, _itype, _sg, profile, environment = entry
    verdict, skip, _health = _evaluate(name, profile, environment)
    assert (verdict, skip) == EXPECTED[name]


def test_cost_candidate_exists():
    """FinOps 경로(RIGHTSIZING) 시연에는 절감 후보가 최소 1대 있어야 한다 — #81/#88 회귀 가드."""
    verdicts = [_evaluate(n, p, e)[0] for n, _t, _s, p, e in SEED.INSTANCES]
    assert verdicts.count(Verdict.COST_CANDIDATE) >= 1


def test_prod_idle_is_protected_and_large():
    """'대형이어도 prod면 끄지 않는다' 확인용 자산 — prod 태그 + 저활성 + 대형 타입 조합을 고정."""
    entry = next(i for i in SEED.INSTANCES if i[0] == "vigilantis-seed-idle")
    _name, itype, _sg, profile, environment = entry
    assert environment == "production"
    assert profile == "idle"
    assert not itype.endswith((".micro", ".small"))  # 절감 여지가 커 보이는 타입이어야 대조가 성립


# --- 하루 창(#625) ---------------------------------------------------------------
# 유휴 판정은 최근 하루를 본다. 시드가 「언제 수집해도 창에 하루치 무늬가 들어온다」를 지키지 못하면
# 위의 기대 판정은 순수 함수에서만 맞고 실제 LocalStack 수집에서는 어긋난다.

SEEDED_AT = datetime(2026, 11, 12, 7, 0, tzinfo=timezone.utc)


def _window(points, collected_at):
    """수집 시각의 하루 창 [collected_at − 24h, collected_at) 에 들어오는 값들(수집기의 조회 창과 같다)."""
    start = collected_at - timedelta(days=1)
    return [value for ts, value in points if start <= ts < collected_at]


def _judge(name, profile, environment, values):
    tags = {"Name": name, **({"Environment": environment} if environment else {})}
    return evaluate_ec2(sum(values) / len(values), max(values), len(values), tags, state="running")[:2]


@pytest.mark.parametrize("delay", [
    timedelta(minutes=7),            # 시드 직후
    timedelta(hours=5, minutes=30),  # 반나절 뒤 — 지난 표본만 넣던 시드는 여기서 데이터 부족이 된다
    timedelta(days=3, hours=11),
    timedelta(days=13, hours=23, minutes=59),  # 앞쪽 표본의 끝
], ids=["just-seeded", "5h-later", "3d-later", "end-of-future"])
@pytest.mark.parametrize("entry", SEED.INSTANCES, ids=lambda e: e[0])
def test_a_one_day_window_sees_the_expected_verdict_whenever_it_collects(entry, delay):
    name, _itype, _sg, profile, environment = entry
    values = _window(SEED._cpu_points(SEEDED_AT, profile), SEEDED_AT + delay)

    assert len(values) >= MIN_DATAPOINTS, "창이 비면 전부 SKIP_INSUFFICIENT_DATA 다"
    assert _judge(name, profile, environment, values) == EXPECTED[name]


def test_spike_peaks_exactly_once_in_every_one_day_window():
    """두 번이면 평균이 임계치를 넘어 「정상 가동」, 0번이면 「다운사이징 후보」가 된다."""
    points = SEED._cpu_points(SEEDED_AT, "spike")
    first, last = points[0][0], points[-1][0]
    collected_at = first + timedelta(days=1)
    while collected_at <= last:
        assert _window(points, collected_at).count(SEED.SPIKE_PEAK) == 1, collected_at
        collected_at += timedelta(hours=1)


def test_reinjecting_at_another_hour_writes_the_same_value_at_the_same_time():
    """다시 주입(--metrics-only)해도 같은 시각의 값이 같다 — CloudWatch 는 같은 구간의 표본을 평균한다.

    무늬를 시드한 시각에 묶으면 다시 주입할 때 스파이크가 다른 시각에 한 번 더 생기고, 그 구간 평균이
    (최대치 + 바닥) / 2 로 깎여 스파이크 판정이 사라진다."""
    for profile in ("idle", "normal", "spike"):
        first = dict(SEED._cpu_points(SEEDED_AT, profile))
        again = dict(SEED._cpu_points(SEEDED_AT + timedelta(hours=7), profile))
        shared = first.keys() & again.keys()
        assert len(shared) > SEED.DAY_HOURS
        assert all(first[ts] == again[ts] for ts in shared), profile


def test_seed_spans_the_chart_window_behind_and_two_weeks_ahead():
    hours = SEED._hours(SEEDED_AT)
    assert hours[0] == SEEDED_AT - timedelta(days=SEED.METRIC_PAST_DAYS)
    assert hours[-1] == SEEDED_AT + timedelta(days=SEED.METRIC_FUTURE_DAYS) - timedelta(hours=1)
    assert all(b - a == timedelta(hours=1) for a, b in zip(hours, hours[1:]))
    assert len(SEED._cpu_series("idle")) == SEED.DAY_HOURS >= MIN_DATAPOINTS
