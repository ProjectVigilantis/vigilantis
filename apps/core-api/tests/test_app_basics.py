# ==============================================================================
# [파일 설명]
# 앱 골격 DB 비의존 검증 — /health·request_id·오류 봉투(422)·CORS. (Issue #68)
# ==============================================================================

from __future__ import annotations


def test_health_ok(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_every_response_carries_request_id_header(client):
    response = client.get("/health")
    assert len(response.headers["X-Request-ID"]) == 32  # uuid4().hex


def test_valid_incoming_request_id_is_reused(client):
    response = client.get("/health", headers={"X-Request-ID": "fe-trace_01.a"})
    assert response.headers["X-Request-ID"] == "fe-trace_01.a"


def test_invalid_incoming_request_id_is_replaced(client):
    response = client.get("/health", headers={"X-Request-ID": "bad id!with spaces"})
    replaced = response.headers["X-Request-ID"]
    assert replaced != "bad id!with spaces"
    assert len(replaced) == 32


def test_invalid_query_enum_returns_422_envelope(client):
    response = client.get("/api/v1/incidents", params={"status": "NOPE"})
    assert response.status_code == 422
    body = response.json()
    assert set(body) == {"error"}
    assert body["error"]["code"] == "REQUEST_VALIDATION_FAILED"
    assert "status" in body["error"]["message"]
    # 봉투의 request_id와 응답 헤더가 같은 값이어야 한다
    assert body["error"]["request_id"] == response.headers["X-Request-ID"]
    # 입력 값은 응답에 싣지 않는다
    assert "NOPE" not in body["error"]["message"]


def test_malformed_incident_id_returns_404_envelope(client):
    # 계약이 UUID를 요구하지 않으므로 형식 오류는 계약 위반이 아니라 없는 인시던트다.
    # POST /actions/execute와 같은 코드로 답한다 (PR #119 리뷰).
    # 정규화 실패는 조회 전에 걸러져 DB 접속이 없다 — DB 비의존 픽스처로 검증한다
    response = client.get("/api/v1/incidents/not-a-uuid")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "INCIDENT_NOT_FOUND"


def test_unhandled_exception_returns_500_envelope(client, capsys):
    @client.app.get("/boom")
    def boom():
        raise RuntimeError("boom-marker-1234")

    response = client.get("/boom")
    assert response.status_code == 500
    body = response.json()
    assert body["error"]["code"] == "INTERNAL_ERROR"
    assert body["error"]["request_id"] == response.headers["X-Request-ID"]
    # 예외 내용은 응답에 노출하지 않고, 스택은 진단용으로 로그에만 남긴다
    assert "boom-marker-1234" not in response.text
    assert "boom-marker-1234" in capsys.readouterr().out


def test_cors_preflight_allows_fe_dev_origin(client):
    response = client.options(
        "/api/v1/incidents",
        headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://localhost:3000"


def test_lifespan_starts_and_stops_both_schedulers(monkeypatch):
    """lifespan 배선 자체를 여기서만 고정한다 — conftest가 두 게이트(SCAN/DISPATCH_ENABLED)를
    끄므로 다른 테스트는 이 두 줄을 지나가도 아무것도 확인하지 못한다(이 PR이 고친 결함이 그
    종류다: 배선이 빠진 줄 아무도 몰랐다). fake 스케줄러로 기동·종료를 직접 고정한다(#287 리뷰: 김세혁).
    """
    from fastapi.testclient import TestClient

    import main as main_module

    started: list[str] = []
    stopped: list[str] = []

    class FakeScheduler:
        def __init__(self, name: str):
            self.name = name

        def shutdown(self, wait=False):
            stopped.append(self.name)

    def fake_scan():
        started.append("scan")
        return FakeScheduler("scan")

    def fake_dispatch(_publish):
        started.append("dispatch")
        return FakeScheduler("dispatch")

    monkeypatch.setattr(main_module, "start_scan_scheduler", fake_scan)
    monkeypatch.setattr(main_module.dispatcher, "start_dispatcher", fake_dispatch)

    with TestClient(main_module.create_app()) as test_client:
        assert set(started) == {"scan", "dispatch"}  # 배선이 둘 다 기동
        assert test_client.get("/health").status_code == 200
    assert set(stopped) == {"scan", "dispatch"}  # 종료 시 둘 다 정리
