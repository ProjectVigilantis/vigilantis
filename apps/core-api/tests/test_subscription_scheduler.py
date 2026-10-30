"""구독 계약 처리 잡의 설정 · 기동 · lifespan 배선 (Post-MVP T-16 · #528) — DB 없이 돈다.

  ① 설정 기본값 — 새 모의 구매는 꺼짐(명시적으로 켠다), 모의 활성화 결과는 성공, 계약 처리 잡은 켜짐(만료는
     기간 끝에 처리돼야 한다). 활성화 결과는 정해진 셋만 받는다.
  ② 기동 스위치는 SUBSCRIPTION_SCHEDULER_ENABLED 하나다 — SCAN_ENABLED · DISPATCH_ENABLED 를 꺼도 뜬다.
  ③ 잡 본체는 기동 때 고정한 설정과 UTC aware 시각을 워크플로에 넘기고 세션을 닫는다.
  ④ 실제 lifespan 이 스캔 · 디스패치가 꺼진 채로 이 잡을 기동 · 종료한다.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
import subscription_scheduler
from config import SubscriptionSettings, get_collector_settings, get_settings, get_subscription_settings

_KEYS = (
    "SUBSCRIPTION_MOCK_MARKETPLACE_ENABLED",
    "SUBSCRIPTION_MOCK_ACTIVATION_OUTCOME",
    "SUBSCRIPTION_SCHEDULER_ENABLED",
    "SUBSCRIPTION_DUE_INTERVAL_SECONDS",
)


@pytest.fixture()
def subscription_env(monkeypatch):
    """스캔 · 디스패치를 끈 채(conftest 와 같은 값) 구독 설정만 바꾼다."""
    monkeypatch.setenv("SCAN_ENABLED", "false")
    monkeypatch.setenv("DISPATCH_ENABLED", "false")

    def configure(**values: str) -> None:
        for key in _KEYS:
            monkeypatch.delenv(key, raising=False)
        for key, value in values.items():
            monkeypatch.setenv(key, value)
        for cached in (get_subscription_settings, get_settings, get_collector_settings):
            cached.cache_clear()

    yield configure
    for cached in (get_subscription_settings, get_settings, get_collector_settings):
        cached.cache_clear()


def test_defaults_keep_new_mock_purchases_closed_and_the_contract_job_on(monkeypatch):
    for key in _KEYS:
        monkeypatch.delenv(key, raising=False)
    settings = SubscriptionSettings(_env_file=None)
    assert settings.SUBSCRIPTION_MOCK_MARKETPLACE_ENABLED is False
    assert settings.SUBSCRIPTION_MOCK_ACTIVATION_OUTCOME == "SUCCEED"
    assert settings.SUBSCRIPTION_SCHEDULER_ENABLED is True
    assert settings.SUBSCRIPTION_DUE_INTERVAL_SECONDS == 60


@pytest.mark.parametrize("key,value", [
    ("SUBSCRIPTION_DUE_INTERVAL_SECONDS", "0"),
    ("SUBSCRIPTION_DUE_INTERVAL_SECONDS", "-1"),
    ("SUBSCRIPTION_MOCK_ACTIVATION_OUTCOME", "succeed"),
    ("SUBSCRIPTION_MOCK_ACTIVATION_OUTCOME", "RANDOM"),
])
def test_invalid_settings_are_rejected_at_boot(monkeypatch, key, value):
    monkeypatch.setenv(key, value)
    with pytest.raises(ValueError):
        SubscriptionSettings(_env_file=None)


def test_disabled_scheduler_does_not_start(subscription_env):
    subscription_env(SUBSCRIPTION_SCHEDULER_ENABLED="false")
    assert subscription_scheduler.start_subscription_scheduler() is None


def test_scheduler_starts_with_scans_and_dispatch_disabled(subscription_env):
    subscription_env(SUBSCRIPTION_SCHEDULER_ENABLED="true", SUBSCRIPTION_DUE_INTERVAL_SECONDS="45")
    assert get_collector_settings().SCAN_ENABLED is False and get_settings().DISPATCH_ENABLED is False

    async def scenario():
        scheduler = subscription_scheduler.start_subscription_scheduler()
        try:
            assert scheduler is not None and scheduler.running
            job = scheduler.get_job(subscription_scheduler.JOB_ID)
            assert job.trigger.interval == timedelta(seconds=45)
            assert job.max_instances == 1 and job.coalesce is True
        finally:
            scheduler.shutdown(wait=False)
        await asyncio.sleep(0)
        assert not scheduler.running

    asyncio.run(scenario())


def test_cycle_passes_boot_settings_and_an_aware_utc_now_and_closes_the_session(monkeypatch):
    calls = []
    closed = []

    class FakeSession:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            closed.append(True)

    def fake_process(db, *, settings, now):
        calls.append((db, settings, now))
        return "report"

    monkeypatch.setattr(subscription_scheduler, "get_session_factory", lambda: FakeSession)
    monkeypatch.setattr(subscription_scheduler, "process_subscription_cycle", fake_process)
    settings = SubscriptionSettings(_env_file=None, SUBSCRIPTION_MOCK_MARKETPLACE_ENABLED=False)

    assert subscription_scheduler.run_subscription_cycle(settings) == "report"

    [(db, passed, now)] = calls
    assert isinstance(db, FakeSession) and passed is settings
    assert now.utcoffset() == timedelta(0)
    assert closed == [True]


def test_real_lifespan_starts_and_stops_the_due_job_with_scans_disabled(subscription_env, monkeypatch):
    from fastapi.testclient import TestClient

    import main

    subscription_env(SUBSCRIPTION_SCHEDULER_ENABLED="true")
    started = []
    real_start = main.start_subscription_scheduler

    def recording_start():
        scheduler = real_start()
        started.append(scheduler)
        return scheduler

    monkeypatch.setattr(main, "start_subscription_scheduler", recording_start)
    with TestClient(main.create_app()) as client:
        [scheduler] = started
        assert scheduler is not None and scheduler.running
        assert scheduler.get_job(subscription_scheduler.JOB_ID) is not None
        assert client.get("/health").status_code == 200
    assert not scheduler.running
