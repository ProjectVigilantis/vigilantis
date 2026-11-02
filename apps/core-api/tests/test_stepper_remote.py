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
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

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
    app = _product_app()
    assert "/_stepper/ws" not in {getattr(route, "path", None) for route in app.routes}
    with TestClient(app) as client:
        for path in ("/_stepper/ping", "/_stepper/incidents", "/_stepper/incidents/x", "/_stepper/assets"):
            assert client.get(path, headers=AUTH).status_code == 404, path
        with pytest.raises(WebSocketDisconnect), client.websocket_connect("/_stepper/ws", headers=AUTH):
            pass


def test_stepper_routes_refuse_missing_or_wrong_token(stepper_client, monkeypatch):
    from services import scheduler

    monkeypatch.setattr(scheduler, "run_pipeline", lambda publish=None: pytest.fail("토큰 없이 돌았다"))
    for headers in ({}, {"X-Stepper-Token": "x" * 32}):
        assert stepper_client.get("/_stepper/ping", headers=headers).status_code == 401
        assert stepper_client.post("/_stepper/scan", headers=headers).status_code == 401
    ping = stepper_client.get("/_stepper/ping", headers=AUTH)
    assert ping.status_code == 200
    assert ping.json()["server"] == stepper.SERVER_ID and ping.json()["busy"] is None


READ_PATHS = ("/_stepper/incidents", "/_stepper/incidents/x", "/_stepper/assets")


def test_read_routes_and_ws_refuse_missing_or_wrong_token(stepper_client, monkeypatch):
    monkeypatch.setattr(stepper, "_session_factory", lambda: pytest.fail("토큰 없이 DB를 열었다"))
    for headers in ({}, {"X-Stepper-Token": "x" * 32}):
        for path in READ_PATHS:
            assert stepper_client.get(path, headers=headers).status_code == 401, path
        with pytest.raises(WebSocketDisconnect) as refused, stepper_client.websocket_connect(
            "/_stepper/ws", headers=headers
        ):
            pass
        assert refused.value.code == 1008
    assert stepper_client.app.state.realtime.connection_count == 0


# ------------------------------------------------------------------------------
# 읽기 라우트 — 제품 핸들러와 같은 응답 (실제 PostgreSQL)
# ------------------------------------------------------------------------------


def test_read_routes_return_what_the_product_handlers_return(
    stepper_client, monkeypatch, db, make_incident
):
    from contextlib import nullcontext
    from datetime import UTC, datetime

    from db.repositories import assets as assets_repo
    from routers import assets as assets_router
    from routers import incidents as incidents_router
    from schemas.api.assets import AssetType
    from schemas.api.incidents import IncidentCategory, IncidentStatus
    from schemas.collections import CollectionRunStatus
    from tenancy import ALL_TENANTS

    monkeypatch.setattr(stepper, "_session_factory", lambda: lambda: nullcontext(db))
    monkeypatch.setattr("routers.assets._configured_regions", lambda: ["ap-northeast-2"])
    arn = "arn:aws:ec2:ap-northeast-2:123456789012:instance/i-0aaa"
    run = assets_repo.start_collection_run(
        db, account_id="123456789012", region="ap-northeast-2",
        mode="localstack", lookback_days=3, period_seconds=3600,
    )
    now = datetime(2026, 8, 19, 6, tzinfo=UTC)
    assets_repo.upsert_asset(
        db, arn=arn, asset_type=AssetType.EC2, resource_id="i-0aaa", account_id="123456789012",
        region="ap-northeast-2", spec={"instance_type": "t3.xlarge"},
        collection_run_id=run.collection_run_id, collected_at=now, name="seed-idle", state="running",
    )
    assets_repo.finish_collection_run(
        db, collection_run_id=run.collection_run_id,
        status=CollectionRunStatus.SUCCESS, finished_at=now,
    )
    incident = make_incident(
        db, category=IncidentCategory.SECOPS, status=IncidentStatus.ANALYZING,
    )
    db.flush()

    listing = stepper_client.get("/_stepper/incidents", headers=AUTH)
    detail = stepper_client.get(f"/_stepper/incidents/{incident.incident_id}", headers=AUTH)
    assets = stepper_client.get("/_stepper/assets", headers=AUTH)
    assert (listing.status_code, detail.status_code, assets.status_code) == (200, 200, 200)

    expected_listing = incidents_router.list_incidents(
        status=None, category=None, db=db, scope=ALL_TENANTS
    )
    expected_detail = incidents_router.get_incident(incident.incident_id, db=db, scope=ALL_TENANTS)
    expected_assets = assets_router.get_assets(db=db, scope=ALL_TENANTS)
    assert listing.json() == expected_listing.model_dump(mode="json")
    assert detail.json() == expected_detail.model_dump(mode="json")
    assert assets.json() == expected_assets.model_dump(mode="json")
    assert [item["incident_id"] for item in listing.json()["items"]] == [incident.incident_id]
    assert [item["arn"] for item in assets.json()["items"]] == [arn]

    missing = stepper_client.get(f"/_stepper/incidents/{uuid.uuid4()}", headers=AUTH)
    assert missing.status_code == 404
    assert "INCIDENT_NOT_FOUND" in missing.text


def test_stepper_ws_accepts_valid_token_and_unregisters_on_close(stepper_client):
    realtime = stepper_client.app.state.realtime
    with stepper_client.websocket_connect("/_stepper/ws", headers=AUTH) as socket:
        socket.send_text("ignored")
        socket.send_bytes(b"ignored")
        assert realtime.connection_count == 1
    assert realtime.connection_count == 0


def test_stepper_ws_receives_published_events_of_any_incident(stepper_client):
    """토큰 연결은 전역 진단이다 — 테넌트 소속을 모르는(DB에도 없는) 인시던트의 이벤트도 받는다.
    제품 /api/v1/ws 는 같은 이벤트를 걸러 보내지 않는다(routers/ws.py) — 도구 연결만의 명시 허용이다."""
    from datetime import UTC, datetime
    from time import monotonic, sleep

    from realtime import incident_event
    from schemas.api.ws import WsEventType

    realtime = stepper_client.app.state.realtime
    event = incident_event(
        WsEventType.INCIDENT_UPDATED, incident_id=str(uuid.uuid4()), occurred_at=datetime.now(UTC)
    )
    with stepper_client.websocket_connect("/_stepper/ws", headers=AUTH) as socket:
        deadline = monotonic() + 5
        while realtime.connection_count != 1:  # accept 뒤 등록이 끝나야 발행 대상이 된다
            assert monotonic() < deadline, "stepper WS가 등록되지 않았다"
            sleep(0.01)
        realtime.publish(event)
        received = socket.receive_json()
    assert (received["event_id"], received["data"]["incident_id"]) == (
        event.event_id, event.data.incident_id,
    )


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


def test_collect_drops_open_websockets_so_the_web_refetches(stepper_client, monkeypatch):
    """collect는 이벤트가 없다 — 연결을 끊어 FE가 재연결하며 다시 조회하게 한다."""
    from services import collector
    from starlette.websockets import WebSocketDisconnect

    monkeypatch.setattr(collector, "collect_and_store", lambda: [{"region": "ap-northeast-2", "total": 16}])
    with stepper_client.websocket_connect("/_stepper/ws", headers=AUTH) as socket:
        body = _press(stepper_client, "collect")
        with pytest.raises(WebSocketDisconnect):
            socket.receive_text()
    assert body["ok"] is True and body["reconnected"] == 1
    assert stepper_client.app.state.realtime.connection_count == 0


def test_websocket_drop_that_overruns_its_deadline_does_not_fail_collect(monkeypatch):
    """끊기가 제한시간을 넘겨도 수집은 저장됐다 — 500이 아니라 reconnected=None으로 보고한다."""
    import asyncio

    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()

    class StuckRealtime:
        _loop = loop
        _connections = {object()}
        _send_timeout = 0.05

        def unregister(self, websocket):
            self._connections = set()

        async def _close_quietly(self, websocket):
            await asyncio.sleep(30)  # 반쯤 열린 TCP처럼 응답이 없는 연결

    monkeypatch.setattr(stepper, "_DROP_MARGIN_SECONDS", 0.05)
    try:
        assert stepper._drop_websockets(StuckRealtime()) is None
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=5)
        loop.close()


@pytest.mark.parametrize("failed_lane", ["main", "autoscaling"])
def test_dispatch_and_analyze_pass_the_app_publisher_and_report_errors(
    stepper_client, monkeypatch, failed_lane
):
    import agent_dispatcher
    import dispatcher

    calls = []

    def fake_dispatch(sessions, publish, *, lane):
        calls.append(("dispatch", lane, publish))
        return dispatcher.DispatchReport(
            lane=lane, scanned=2, closed=1, errored=int(lane == failed_lane)
        )

    def fake_analyze(sessions, publish):
        calls.append(("analyze", publish))
        return agent_dispatcher.AgentDispatchReport(scanned=1, claimed=1, failed=1)

    monkeypatch.setattr(dispatcher, "run_dispatch_cycle", fake_dispatch)
    monkeypatch.setattr(agent_dispatcher, "run_agent_dispatch_cycle", fake_analyze)

    dispatched = _press(stepper_client, "dispatch")
    analyzed = _press(stepper_client, "analyze")
    assert dispatched["ok"] is False and dispatched["report"]["closed"] == 2
    assert dispatched["report"]["scanned"] == 4
    assert dispatched["report"]["errored"] == 1
    assert [report["lane"] for report in dispatched["lane_reports"]] == [
        dispatcher.LANE_MAIN, dispatcher.LANE_AUTOSCALING
    ]
    assert analyzed["ok"] is False and analyzed["report"]["failed"] == 1
    publish = stepper_client.app.state.realtime.publish
    assert calls == [
        ("dispatch", dispatcher.LANE_MAIN, publish),
        ("dispatch", dispatcher.LANE_AUTOSCALING, publish),
        ("analyze", publish),
    ]


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
