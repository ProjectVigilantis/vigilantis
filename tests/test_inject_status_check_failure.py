# scripts/inject_status_check_failure.py 검증 — 시연 T1 7번 주입이 코드로 지키는 약속.
#
#   ① 정지는 **유형이 바뀐 채 기동된 뒤에만** 요청한다. 실행 전(원래 유형 running)이나
#      정지·유형 변경 사이(바뀐 유형 stopped)에 멈추면 실행기의 다음 단계와 겹친다.
#   ② 시간 안에 그 순간이 없으면 아무것도 멈추지 않는다.
#   ③ 실 AWS 엔드포인트에서는 AWS를 부르기 전에 멈춘다(ADR-0006 §2).
#   ④ 기본 대상은 시드의 유일한 다운사이징 후보다 — 시드 구성이 바뀌면 여기서 깨진다.
#
# scripts/ 는 CI pytest 경로에 없어 여기(루트 tests/)에 둔다. AWS를 부르지 않는다(대역).
# 실제 앱 타이머 위의 동작은 2026-09-17 LocalStack 실측(#349 코멘트)이 근거다.

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, REPO_ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    # dataclass가 자기 모듈을 sys.modules에서 찾는다 — 등록하지 않으면 정의가 깨진다
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


inject = _load("inject_status_check_failure")

INSTANCE = "i-0123456789abcdef0"
START = "m5.2xlarge"
APPLIED = "m5.large"


class FakeEc2:
    """describe_instances 가 부를 때마다 다음 상태를 돌려준다. 마지막 상태는 계속 유지한다."""

    def __init__(self, states):
        self._states = list(states)
        self.stop_calls = []
        self.seen = 0

    def describe_instances(self, **_):
        state = self._states[min(self.seen, len(self._states) - 1)]
        self.seen += 1
        return {"Reservations": [{"Instances": [
            {"InstanceId": INSTANCE, "InstanceType": state[0], "State": {"Name": state[1]}}
        ]}]}

    def stop_instances(self, **kwargs):
        # 정지 요청 시점에 몇 번째 상태를 보고 있었는지 남긴다
        self.stop_calls.append((self.seen, kwargs))
        return {}


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def _run(ec2, **kwargs):
    clock = Clock()
    return inject.wait_and_stop(
        ec2, INSTANCE, START, clock=clock, sleep=clock.sleep, **kwargs
    )


def test_stops_only_after_the_downsized_instance_starts():
    """실행 주기의 정지 → 유형 변경 → 기동을 그대로 따라가고, 기동이 보인 그때 한 번 멈춘다."""
    ec2 = FakeEc2([
        (START, "running"),     # 승인 전
        (START, "running"),
        (START, "stopped"),     # ① 정지
        (APPLIED, "stopped"),   # ② 유형 변경 — 아직 멈추면 안 된다
        (APPLIED, "running"),   # ③ 기동 요청 뒤
    ])
    changes = []

    injection = _run(ec2, on_change=lambda *c: changes.append(c[1:]))

    assert ec2.stop_calls == [(5, {"InstanceIds": [INSTANCE]})]
    assert injection is not None
    assert (injection.start_type, injection.applied_type) == (START, APPLIED)
    assert changes == [(START, "running"), (START, "stopped"),
                       (APPLIED, "stopped"), (APPLIED, "running")]


def test_pending_counts_as_started():
    """LocalStack 은 곧바로 running 이지만 실 AWS 처럼 pending 을 거쳐도 같은 순간으로 본다."""
    ec2 = FakeEc2([(START, "running"), (APPLIED, "stopped"), (APPLIED, "pending")])

    assert _run(ec2) is not None
    assert len(ec2.stop_calls) == 1


def test_times_out_without_stopping():
    """[조치 실행]이 오지 않으면 아무것도 멈추지 않고 None 을 돌려준다."""
    ec2 = FakeEc2([(START, "running")])

    assert _run(ec2, timeout_seconds=1.0, poll_seconds=0.1) is None
    assert ec2.stop_calls == []


@pytest.mark.parametrize("endpoint", [None, "https://ec2.ap-northeast-2.amazonaws.com"])
def test_refuses_without_localstack_endpoint(monkeypatch, endpoint):
    """엔드포인트가 없거나 실 AWS 면 AWS 클라이언트를 만들기 전에 끝난다."""
    monkeypatch.setattr(inject, "endpoint_url", lambda: endpoint)

    def must_not_call(*_a, **_k):
        raise AssertionError("실 AWS 가드보다 먼저 클라이언트를 만들었다")

    monkeypatch.setattr(inject, "aws_client", must_not_call)

    with pytest.raises(SystemExit):
        inject.main([])


@pytest.mark.parametrize(
    ("state", "reason"),
    [((APPLIED, "running"), "시드와 다르다"), ((START, "stopped"), "running이 아니다")],
    ids=["already-downsized", "not-running"],
)
def test_refuses_without_waiting_and_guides_both_causes(monkeypatch, state, reason):
    """출발 상태가 틀리면 기다리지 않고, 늦게 띄운 경우와 세션 사이를 함께 안내한다.

    시작 유형이 틀리면 변화를 못 알아보고, 멈춰 있으면 기동이 오지 않는다. 같은 상태가 이전 시연의 흔적에서도, [조치 실행]보다 늦게 띄운 데서도 나온다. 늦게 띄운 경우
    LocalStack 재기동은 준비한 Incident의 대상을 없애므로 안내는 두 갈래를 함께 보인다(PR #373 리뷰).
    """
    ec2 = FakeEc2([state])
    monkeypatch.setattr(inject, "endpoint_url", lambda: "http://localhost:4566")
    monkeypatch.setattr(inject, "aws_client", lambda *_a, **_k: ec2)

    with pytest.raises(SystemExit) as exc:
        inject.main([])

    message = str(exc.value)
    assert reason in message
    assert "LocalStack을 재기동하지 않는다" in message
    assert "[이전 스펙 복원]" in message
    assert "사전 준비를 처음부터" in message
    assert ec2.stop_calls == []


def test_default_target_is_the_only_seed_rightsizing_candidate():
    """기본 대상은 시드에서 유일하게 운영 보호에 걸리지 않는 유휴 인스턴스여야 한다."""
    seed = _load("seed_localstack")
    candidates = [
        name for name, _type, _sg, profile, environment in seed.INSTANCES
        if profile == "idle" and environment != "production"
    ]

    assert candidates == [inject.DEFAULT_NAME]
    assert inject._seed_instance_types()[inject.DEFAULT_NAME] == START
