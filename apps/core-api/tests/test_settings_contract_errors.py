"""T-7 요청 DTO와 기존 HTTP 오류 처리기의 비밀값 경계.

테스트용 앱으로 DTO 검증과 실제 공통 오류 봉투를 연결한다.
설정 저장 API · 인증 · SNS 구현의 검증은 아니다.
"""

import pytest
from exceptions import register_error_handlers
from fastapi import FastAPI
from fastapi.testclient import TestClient
from schemas.api.settings import TenantSettingsUpdateRequest

WEBHOOK = "https://hooks.slack.com/services/TTEAM/BCHANNEL/PrivateToken"


@pytest.fixture
def client():
    app = FastAPI()
    register_error_handlers(app)

    @app.patch("/settings-contract")
    def validate_settings(body: TenantSettingsUpdateRequest):
        # 성공 응답에도 요청을 되돌리지 않는다.
        return {"accepted": True}

    with TestClient(app) as test_client:
        yield test_client


@pytest.mark.parametrize(
    "payload",
    [
        {"notifications": {"slack_webhook_url": WEBHOOK + "?token=private"}},
        {"notifications": {"slack_webhook_url": WEBHOOK}, "protected_tags": None},
        {
            "notifications": {
                "slack_webhook_url": WEBHOOK,
                "email_address": "invalid-email",
            },
        },
        {
            "notifications": {"slack_webhook_url": WEBHOOK},
            "tenant_id": "server-owned",
        },
    ],
)
def test_http_validation_error_and_logs_never_include_webhook(client, caplog, payload):
    response = client.patch("/settings-contract", json=payload)

    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "REQUEST_VALIDATION_FAILED"
    assert set(error) == {"code", "message", "request_id"}
    assert "위치: body" in error["message"]
    assert WEBHOOK not in response.text
    assert "PrivateToken" not in response.text
    assert WEBHOOK not in caplog.text
    assert "PrivateToken" not in caplog.text


def test_valid_secret_reaches_dto_without_being_echoed(client):
    response = client.patch(
        "/settings-contract", json={"notifications": {"slack_webhook_url": WEBHOOK}},
    )
    assert response.status_code == 200
    assert response.json() == {"accepted": True}
