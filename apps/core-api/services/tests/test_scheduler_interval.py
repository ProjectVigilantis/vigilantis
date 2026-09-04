"""스캔 주기 설정 — 검증된 config 에서만 읽는지 확인한다(#255). DB·AWS 불필요.

기존엔 scheduler 가 os.getenv 로 직접 읽어 0·음수·비정수가 걸러지지 않았다. 잘못된 값은
잡 등록(IntervalTrigger) 시점까지 흘러가 기동 실패로만 드러났다.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

CORE_API = Path(__file__).resolve().parents[2]
REPO_ROOT = Path(__file__).resolve().parents[4]
for p in (str(CORE_API), str(REPO_ROOT / "packages")):
    if p not in sys.path:
        sys.path.insert(0, p)

from config import CollectorSettings, get_collector_settings  # noqa: E402
from services.scheduler import _interval_seconds, start_scheduler  # noqa: E402


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    get_collector_settings.cache_clear()
    yield
    get_collector_settings.cache_clear()


def test_default_is_300_seconds():
    assert CollectorSettings().SCAN_INTERVAL_SECONDS == 300


def test_interval_comes_from_settings(monkeypatch):
    monkeypatch.setenv("SCAN_INTERVAL_SECONDS", "1800")
    assert _interval_seconds() == 1800


@pytest.mark.parametrize("bad", ["0", "-1", "abc", ""])
def test_invalid_interval_is_rejected_at_settings(monkeypatch, bad):
    # 잡 등록까지 가지 않고 설정 단계에서 걸려야 한다.
    monkeypatch.setenv("SCAN_INTERVAL_SECONDS", bad)
    with pytest.raises(ValidationError):
        _interval_seconds()


def test_scan_disabled_returns_none(monkeypatch):
    # SCAN_ENABLED=false 면 스캔 스케줄러를 기동하지 않는다 — 테스트/특정 배포에서
    # 실제 수집·판정이 도는 것을 막는 안전장치(dispatcher.start_dispatcher 와 같은 결).
    monkeypatch.setenv("SCAN_ENABLED", "false")
    assert start_scheduler() is None
