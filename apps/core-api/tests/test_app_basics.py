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


def test_malformed_incident_id_returns_422_envelope(client):
    response = client.get("/api/v1/incidents/not-a-uuid")
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "REQUEST_VALIDATION_FAILED"


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
