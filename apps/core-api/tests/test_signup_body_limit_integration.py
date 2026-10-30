"""실제 create_app 경계의 413·422·Origin을 확인한다. DB·외부 AWS는 부르지 않는다."""

import pytest
from support_http import SIGNUP_BODY_MAX_BYTES

PATH = "/api/v1/signup-requests"


@pytest.mark.parametrize("path", [PATH, PATH + "%0A"])
def test_oversized_signup_has_common_envelope_and_security_headers(client, path):
    response = client.post(
        path,
        content=b" " * (SIGNUP_BODY_MAX_BYTES + 1),
        headers={"Content-Type": "application/json", "X-Request-ID": "signup-body-boundary"},
    )
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "REQUEST_BODY_TOO_LARGE"
    assert response.json()["error"]["request_id"] == "signup-body-boundary"
    assert response.headers["X-Request-ID"] == "signup-body-boundary"
    assert response.headers["X-Content-Type-Options"] == "nosniff"


def test_at_body_limit_invalid_json_is_validation_error_not_size_error(client):
    response = client.post(
        PATH,
        content=b" " * SIGNUP_BODY_MAX_BYTES,
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "REQUEST_VALIDATION_FAILED"


def test_under_limit_submission_still_requires_allowed_origin(client):
    response = client.post(PATH, json={})
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "AUTH_ORIGIN_REJECTED"
