"""FE 관찰용 테스트 서버의 단계 진행 라우터(scripts/stepper/app.py) 테스트.

라우터가 지키는 것만 본다 — 토큰, 버튼 하나씩(409), 주기 함수의 보고를 ok로 가르는 규칙,
발행이 같은 앱의 WebSocket으로 나가는 것, 진입점의 기동 검사. 주기 함수는 대역으로 바꾼다.
제품 흐름에 실제로 붙는 확인(소비자·분석 주기·PostgreSQL)은 CI에 두지 않는다 — 제품 PR이
이 도구 때문에 멈추지 않게 하고, 쓸 때 확인한다(scripts/stepper/README.md §유지).
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parents[3]
STEPPER_APP = REPO_ROOT / "scripts" / "stepper" / "app.py"


def _load_stepper():
    spec = importlib.util.spec_from_file_location("stepper_app", STEPPER_APP)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


stepper = _load_stepper()

TOKEN = "t" * 32
AUTH = {"X-Stepper-Token": TOKEN}
EMPTY_SCAN = {"stored": [], "incidents": {"created": 0, "existing": 0, "failed": 0}}


def _product_app():
    """테스트 서버와 같은 조립의 바탕 — 설정 캐시를 비우고 제품 앱을 새로 만든다."""
    from config import get_settings

    get_settings.cache_clear()
    from main import create_app

    return create_app()


@pytest.fixture()
def stepper_client(tmp_path):
    """DB 없이 도는 조립 — 주기 함수는 각 테스트가 대역으로 바꾼다."""
    app = stepper.attach(_product_app(), token=TOKEN, inbox=tmp_path / "inbox")
    with TestClient(app) as client:
        yield client


def _press(client, button: str):
    response = client.post(f"/_stepper/{button}", headers=AUTH)
    assert response.status_code == 200, response.text
    return response.json()


# ------------------------------------------------------------------------------
# 경로와 토큰
# ------------------------------------------------------------------------------


def test_product_app_has_no_stepper_routes():
    with TestClient(_product_app()) as client:
        assert client.get("/_stepper/ping", headers=AUTH).status_code == 404


def test_stepper_routes_refuse_missing_or_wrong_token(stepper_client, monkeypatch):
    from services import scheduler

    monkeypatch.setattr(scheduler, "run_pipeline", lambda publish=None: pytest.fail("토큰 없이 돌았다"))
    for headers in ({}, {"X-Stepper-Token": "x" * 32}):
        assert stepper_client.get("/_stepper/ping", headers=headers).status_code == 401
        assert stepper_client.post("/_stepper/scan", headers=headers).status_code == 401
    ping = stepper_client.get("/_stepper/ping", headers=AUTH)
    assert ping.status_code == 200
    assert ping.json()["server"] == stepper.SERVER_ID and ping.json()["busy"] is None


# ------------------------------------------------------------------------------
# 버튼 하나씩
# ------------------------------------------------------------------------------


def test_second_button_while_one_runs_gets_409_with_the_running_button(stepper_client, monkeypatch):
    from services import scheduler

    entered, release = threading.Event(), threading.Event()

    def slow_scan(publish=None):
        entered.set()
        assert release.wait(5)
        return EMPTY_SCAN

    monkeypatch.setattr(scheduler, "run_pipeline", slow_scan)
    results = {}
    first = threading.Thread(
        target=lambda: results.update(first=stepper_client.post("/_stepper/scan", headers=AUTH))
    )
    first.start()
    try:
        assert entered.wait(5), "첫 버튼이 주기 함수에 들어가지 않았다"
        second = stepper_client.post("/_stepper/dispatch", headers=AUTH)
        ping = stepper_client.get("/_stepper/ping", headers=AUTH)
    finally:
        release.set()
        first.join(5)

    assert second.status_code == 409
    assert second.json()["detail"] == {"busy": "scan"}
    assert ping.json()["busy"] == "scan"
    assert results["first"].status_code == 200
    # 잠금이 풀리면 다음 버튼이 돈다
    assert _press(stepper_client, "scan")["ok"] is True


# ------------------------------------------------------------------------------
# 보고를 ok로 가르는 규칙 — 주기 함수는 대역
# ------------------------------------------------------------------------------


@pytest.mark.parametrize(("summary", "ok"), [
    ({"skipped": True}, False),
    ({"stored": [{"region": "ap-northeast-2", "status": "FAILED", "error": "x"}],
      "incidents": {"created": 0, "existing": 0, "failed": 0}}, False),
    ({"stored": [{"region": "ap-northeast-2", "total": 16}],
      "incidents": {"created": 2, "existing": 0, "failed": 1}}, False),
    ({"stored": [{"region": "ap-northeast-2", "total": 16}],
      "incidents": {"created": 3, "existing": 0, "failed": 0}}, True),
])
def test_scan_skip_and_failures_are_not_reported_as_success(stepper_client, monkeypatch, summary, ok):
    from services import scheduler

    publishers = []

    def fake_pipeline(publish=None):
        publishers.append(publish)
        return summary

    monkeypatch.setattr(scheduler, "run_pipeline", fake_pipeline)
    body = _press(stepper_client, "scan")
    assert body["ok"] is ok
    assert body["report"] == summary
    # 발행은 이 앱의 실시간 전송으로 간다 — 그래야 FE가 새로고침 없이 받는다
    assert publishers == [stepper_client.app.state.realtime.publish]


def test_collect_failed_region_is_not_reported_as_success(stepper_client, monkeypatch):
    from services import collector

    monkeypatch.setattr(collector, "collect_and_store", lambda: [
        {"region": "ap-northeast-2", "total": 16},
        {"region": "us-east-1", "status": "FAILED", "error": "EndpointConnectionError"},
    ])
    assert _press(stepper_client, "collect")["ok"] is False


def test_dispatch_and_analyze_pass_the_app_publisher_and_report_errors(stepper_client, monkeypatch):
    import agent_dispatcher
    import dispatcher

    calls = []

    def fake_dispatch(sessions, publish):
        calls.append(("dispatch", publish))
        return dispatcher.DispatchReport(scanned=2, closed=1, errored=1)

    def fake_analyze(sessions, publish):
        calls.append(("analyze", publish))
        return agent_dispatcher.AgentDispatchReport(scanned=1, claimed=1, failed=1)

    monkeypatch.setattr(dispatcher, "run_dispatch_cycle", fake_dispatch)
    monkeypatch.setattr(agent_dispatcher, "run_agent_dispatch_cycle", fake_analyze)

    dispatched = _press(stepper_client, "dispatch")
    analyzed = _press(stepper_client, "analyze")
    assert dispatched["ok"] is False and dispatched["report"]["closed"] == 1
    assert analyzed["ok"] is False and analyzed["report"]["failed"] == 1
    publish = stepper_client.app.state.realtime.publish
    assert calls == [("dispatch", publish), ("analyze", publish)]


# ------------------------------------------------------------------------------
# 진입점 기동 검사 — 설정마다 별도 프로세스
# ------------------------------------------------------------------------------

_BOOT = (
    "import importlib.util, sys\n"
    "spec = importlib.util.spec_from_file_location('stepper_app', sys.argv[1])\n"
    "module = importlib.util.module_from_spec(spec)\n"
    "spec.loader.exec_module(module)\n"
    "app = module.build_app()\n"
    "from fastapi.testclient import TestClient\n"
    "print(TestClient(app).get('/_stepper/ping', headers={'X-Stepper-Token': sys.argv[2]}).status_code)\n"
)


def _boot(tmp_path, **overrides) -> subprocess.CompletedProcess:
    env = {
        key: value for key, value in os.environ.items()
        if not key.startswith(("AWS_", "STEPPER_", "OPENAI_"))
    }
    env.update(
        # 엔진은 lazy라 접속하지 않는다
        DATABASE_URL="postgresql+psycopg://stepper:stepper@127.0.0.1:1/none",
        SCAN_ENABLED="false",
        DISPATCH_ENABLED="false",
        MOCK_THREAT_INBOX_DIR="",
        STEPPER_TOKEN=TOKEN,
        STEPPER_INBOX_DIR=str(tmp_path / "inbox"),
        AWS_ENDPOINT_URL="http://localstack:4566",
        PYTHONIOENCODING="utf-8",
    )
    env.update(overrides)
    # cwd를 비운 폴더로 둔다 — 개발자의 루트 .env가 설정에 섞이지 않게 한다
    return subprocess.run(
        [sys.executable, "-c", _BOOT, str(STEPPER_APP), TOKEN],
        env={key: value for key, value in env.items() if value is not None},
        cwd=tmp_path, capture_output=True, text=True, encoding="utf-8", timeout=180, check=False,
    )


@pytest.mark.parametrize(("overrides", "reason"), [
    ({"SCAN_ENABLED": "true"}, "SCAN_ENABLED"),
    ({"DISPATCH_ENABLED": "true"}, "DISPATCH_ENABLED"),
    ({"MOCK_THREAT_INBOX_DIR": "/app/apps/core-api/.mock-threat-inbox"}, "MOCK_THREAT_INBOX_DIR"),
    ({"STEPPER_TOKEN": None}, "STEPPER_TOKEN"),
    ({"STEPPER_TOKEN": "short"}, "STEPPER_TOKEN"),
    ({"STEPPER_INBOX_DIR": "relative/inbox"}, "STEPPER_INBOX_DIR"),
    ({"AWS_ENDPOINT_URL": None}, "AWS_ENDPOINT_URL"),
    ({"AWS_ENDPOINT_URL": "https://ec2.ap-northeast-2.amazonaws.com"}, "실 AWS"),
])
def test_entry_refuses_to_start_on_unsafe_settings(tmp_path, overrides, reason):
    proc = _boot(tmp_path, **overrides)
    assert proc.returncode != 0
    assert "테스트 서버 기동 거부" in proc.stderr
    assert reason in proc.stderr


def test_entry_attaches_router_to_product_app_on_safe_settings(tmp_path):
    proc = _boot(tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip().splitlines()[-1] == "200"
