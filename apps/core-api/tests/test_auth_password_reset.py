"""비밀번호 재설정 API — Stubber(정확한 파라미터 · 호출 순서) + 실제 PostgreSQL 세션 회수 (#492 PR 3)."""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
from datetime import UTC, datetime

import boto3
import pytest
from auth import cognito, password_reset, sessions
from auth.http import SESSION_COOKIE, get_clock, get_cognito_adapter
from botocore.stub import Stubber
from config import AuthSettings, get_auth_settings
from db import models
from db.repositories import auth as auth_repo
from fastapi.testclient import TestClient
from schemas.api.auth import UserRole
from sqlalchemy import text

ORIGIN = "https://app.example.com"
POOL = "ap-northeast-2_pool"
CLIENT_ID = "client-id"
CLIENT_SECRET = "client-secret-zzz-9"
ISSUER = f"https://cognito-idp.ap-northeast-2.amazonaws.com/{POOL}"
EMAIL = "u@example.com"
SUB = "sub-reset-1"
UNAME = "11111111-2222-3333-4444-555555555555"
CODE = "123456"
NEW_PASSWORD = "NewPw-secret-123-xyz"
NOW = datetime(2026, 10, 19, 9, 0, tzinfo=UTC)
PREFIX = "/api/v1/auth/password-reset"


def secret_hash(username: str) -> str:
    mac = hmac.new(CLIENT_SECRET.encode(), (username + CLIENT_ID).encode(), hashlib.sha256).digest()
    return base64.b64encode(mac).decode()


class Cog:
    """Stubber 로 감싼 실제 CognitoAdapter — 등록하지 않은 SDK 호출은 즉시 실패한다."""

    def __init__(self) -> None:
        client = boto3.client(
            "cognito-idp", region_name="ap-northeast-2", aws_access_key_id="dummy", aws_secret_access_key="dummy"
        )
        self.stubber = Stubber(client)
        self.stubber.activate()
        self.adapter = cognito.CognitoAdapter(
            client, user_pool_id=POOL, client_id=CLIENT_ID, client_secret=CLIENT_SECRET, region="ap-northeast-2"
        )

    def get_user(self, sub=SUB):
        self.stubber.add_response(
            "admin_get_user",
            {
                "Username": UNAME,
                "UserAttributes": [{"Name": "sub", "Value": sub}, {"Name": "email", "Value": EMAIL}],
                "UserStatus": "CONFIRMED",
            },
            {"UserPoolId": POOL, "Username": EMAIL},
        )

    def get_user_absent(self):
        self.error("admin_get_user", "UserNotFoundException")

    def confirm(self, password=NEW_PASSWORD):
        self.stubber.add_response(
            "confirm_forgot_password",
            {},
            {
                "ClientId": CLIENT_ID,
                "Username": EMAIL,
                "ConfirmationCode": CODE,
                "Password": password,
                "SecretHash": secret_hash(EMAIL),
            },
        )

    def error(self, op, code, status=400):
        self.stubber.add_client_error(op, service_error_code=code, service_message="echo PRIVATE-TEXT", http_status_code=status)

    def forgot(self):
        self.stubber.add_response(
            "forgot_password",
            {"CodeDeliveryDetails": {"Destination": "u***@e***", "DeliveryMedium": "EMAIL", "AttributeName": "email"}},
            {"ClientId": CLIENT_ID, "Username": EMAIL, "SecretHash": secret_hash(EMAIL)},
        )


@pytest.fixture()
def cog():
    c = Cog()
    yield c
    c.stubber.assert_no_pending_responses()


@pytest.fixture()
def http(client_pg_anon, cog):
    app = client_pg_anon.app
    app.dependency_overrides[get_auth_settings] = lambda: AuthSettings(
        _env_file=None,
        COGNITO_REGION="ap-northeast-2",
        COGNITO_USER_POOL_ID=POOL,
        COGNITO_CLIENT_ID=CLIENT_ID,
        COGNITO_CLIENT_SECRET=CLIENT_SECRET,
        AUTH_ALLOWED_ORIGINS=ORIGIN,
    )
    app.dependency_overrides[get_cognito_adapter] = lambda: cog.adapter
    app.dependency_overrides[get_clock] = lambda: (lambda: NOW)
    with TestClient(app, base_url="https://testserver") as c:
        yield c


def seed_session(db, *, sub=SUB):
    user = auth_repo.create_user(
        db, issuer=ISSUER, cognito_sub=sub, email=f"{sub}@example.com", role=UserRole.OPERATOR, now=NOW,
        email_verified_at=NOW,
    )
    issued = sessions.issue_session(
        db, user_id=user.user_id, mfa_step=models.AuthSessionMfaStep.SOFTWARE_TOKEN_MFA,
        now=NOW, max_seconds=3600, idle_seconds=3600,
    )
    db.commit()
    return {"Cookie": f"{SESSION_COOKIE}={issued.token}", "Origin": ORIGIN}


def alive(c, headers) -> bool:
    return c.get("/api/v1/auth/me", headers=headers).status_code == 200


def request(c, body=None, origin=ORIGIN):
    return c.post(PREFIX, json=body or {"email": EMAIL}, headers={"Origin": origin} if origin else {})


def confirm(c, **overrides):
    body = {"email": EMAIL, "code": CODE, "new_password": NEW_PASSWORD, **overrides}
    return c.post(PREFIX + "/confirm", json=body, headers={"Origin": ORIGIN})


def assert_plain_204(resp):
    assert resp.status_code == 204 and resp.content == b""
    assert "set-cookie" not in resp.headers
    assert resp.headers["cache-control"] == "no-store"


# --- 요청 -----------------------------------------------------------------------------


def test_request_sends_exact_params_and_returns_empty_204(http, cog):
    cog.forgot()
    assert_plain_204(request(http))


@pytest.mark.parametrize(
    "code", ["UserNotFoundException", "NotAuthorizedException", "InvalidParameterException", "UserNotConfirmedException"]
)
def test_request_response_is_identical_for_missing_ineligible_and_unverified(http, cog, code):
    cog.forgot()
    ok = request(http)
    cog.error("forgot_password", code)
    hidden = request(http)
    assert_plain_204(hidden)
    assert (hidden.status_code, hidden.content, dict(hidden.headers).keys()) == (
        ok.status_code,
        ok.content,
        dict(ok.headers).keys(),
    )


@pytest.mark.parametrize(
    "code,status,http_status,envelope",
    [
        ("TooManyRequestsException", 400, 429, "AUTH_RATE_LIMITED"),
        ("LimitExceededException", 400, 429, "AUTH_RATE_LIMITED"),
        ("TooManyFailedAttemptsException", 400, 429, "AUTH_RATE_LIMITED"),
        ("CodeDeliveryFailureException", 400, 503, "AUTH_EMAIL_DELIVERY_FAILED"),
        ("InvalidEmailRoleAccessPolicyException", 400, 503, "AUTH_EMAIL_DELIVERY_FAILED"),
        ("AccessDeniedException", 400, 503, "AUTH_PROVIDER_UNAVAILABLE"),
        ("InternalErrorException", 500, 503, "AUTH_PROVIDER_UNAVAILABLE"),
    ],
)
def test_request_surfaces_throttle_delivery_and_provider_failures(http, cog, code, status, http_status, envelope):
    cog.error("forgot_password", code, status)
    resp = request(http)
    assert resp.status_code == http_status
    assert resp.json()["error"]["code"] == envelope
    assert "PRIVATE-TEXT" not in resp.text


@pytest.mark.parametrize("path", ["", "/confirm"])
@pytest.mark.parametrize("origin", [None, "https://evil.example.com", ORIGIN + "/", "null"])
def test_bad_origin_is_403_with_no_provider_call_or_session_change(http, db, cog, path, origin):
    headers = seed_session(db)
    body = {"email": EMAIL, "code": CODE, "new_password": NEW_PASSWORD}
    resp = http.post(PREFIX + path, json=body, headers={"Origin": origin} if origin else {})
    assert resp.status_code == 403 and resp.json()["error"]["code"] == "AUTH_ORIGIN_REJECTED"
    assert alive(http, headers)  # 스텁 큐가 비어 있으므로 SDK 호출이 있었다면 이미 실패했다


# --- 확정: 입력 검증 ---------------------------------------------------------------------


@pytest.mark.parametrize("length", [11, 257])
def test_schema_length_violations_are_422_without_provider_call(http, cog, length):
    resp = confirm(http, new_password="Aa1-" + "a" * (length - 4))
    assert resp.status_code == 422 and resp.json()["error"]["code"] == "REQUEST_VALIDATION_FAILED"
    assert NEW_PASSWORD not in resp.text


@pytest.mark.parametrize(
    "password",
    ["aaaa1111-bbbb", "AAAA1111-BBBB", "Aaaaaaaaaaa-b", "Aaaaaaaaaaa1b", " Abcdefgh1-xyz", "Abcdefgh 1-xyz", "Abcdefgh1-한글비번"],
    ids=["no-upper", "no-lower", "no-digit", "no-symbol", "leading-space", "mid-space", "hangul"],
)
def test_server_policy_violation_is_422_before_any_provider_call(http, cog, password):
    resp = confirm(http, new_password=password)
    assert resp.status_code == 422 and resp.json()["error"]["code"] == "AUTH_PASSWORD_POLICY"


# --- 확정: 성공 · 세션 회수 ---------------------------------------------------------------


def test_success_revokes_only_that_users_sessions_and_sets_no_cookie(http, db, cog):
    target = seed_session(db)
    other = seed_session(db, sub="sub-other")
    cog.get_user()
    cog.confirm()
    assert_plain_204(confirm(http))
    assert not alive(http, target)
    assert http.get("/api/v1/auth/me", headers=target).status_code == 401
    assert alive(http, other)
    reasons = db.execute(text("SELECT revoke_reason FROM auth_sessions WHERE revoked_at IS NOT NULL")).scalars().all()
    assert reasons == ["PASSWORD_RESET"]


def test_proven_absent_user_still_confirms_and_is_valid_204(http, db, cog):
    other = seed_session(db, sub="sub-other")
    cog.get_user_absent()
    cog.error("confirm_forgot_password", "UserNotFoundException")
    resp = confirm(http)
    assert resp.status_code == 401 and resp.json()["error"]["code"] == "AUTH_CODE_INVALID"
    assert alive(http, other)


def test_known_sub_without_app_row_is_204_and_touches_no_session(http, db, cog):
    other = seed_session(db, sub="sub-other")
    cog.get_user(sub="sub-not-in-app")
    cog.confirm()
    assert_plain_204(confirm(http))
    assert alive(http, other)


def test_confirmed_without_known_sub_is_500_not_a_revoke_claim(http, db, cog):
    other = seed_session(db, sub="sub-other")
    cog.get_user_absent()
    cog.confirm()
    resp = confirm(http)
    assert resp.status_code == 500 and resp.json()["error"]["code"] == "INTERNAL_ERROR"
    assert alive(http, other)


# --- 확정: 실패 매핑 (공급자 상태는 바꾸지 않는다) ----------------------------------------


@pytest.mark.parametrize(
    "code,status,http_status,envelope",
    [
        ("CodeMismatchException", 400, 401, "AUTH_CODE_INVALID"),
        ("ExpiredCodeException", 400, 401, "AUTH_CODE_INVALID"),
        ("UserNotFoundException", 400, 401, "AUTH_CODE_INVALID"),
        ("NotAuthorizedException", 400, 401, "AUTH_CODE_INVALID"),
        ("InvalidPasswordException", 400, 422, "AUTH_PASSWORD_POLICY"),
        ("TooManyFailedAttemptsException", 400, 429, "AUTH_RATE_LIMITED"),
        ("InternalErrorException", 500, 503, "AUTH_PROVIDER_UNAVAILABLE"),
    ],
)
def test_confirm_failures_map_and_keep_sessions(http, db, cog, code, status, http_status, envelope):
    target = seed_session(db)
    cog.get_user()
    cog.error("confirm_forgot_password", code, status)
    resp = confirm(http)
    assert resp.status_code == http_status and resp.json()["error"]["code"] == envelope
    assert alive(http, target)


@pytest.mark.parametrize(
    "code,status,http_status,envelope",
    [
        ("TooManyRequestsException", 400, 429, "AUTH_RATE_LIMITED"),
        ("AccessDeniedException", 400, 503, "AUTH_PROVIDER_UNAVAILABLE"),
        ("NotAuthorizedException", 400, 503, "AUTH_PROVIDER_UNAVAILABLE"),
        ("InternalErrorException", 500, 503, "AUTH_PROVIDER_UNAVAILABLE"),
    ],
)
def test_lookup_failure_stops_before_password_change(http, db, cog, code, status, http_status, envelope):
    target = seed_session(db)
    cog.error("admin_get_user", code, status)  # confirm 이 호출되면 스텁이 없어 테스트가 실패한다
    resp = confirm(http)
    assert resp.status_code == http_status and resp.json()["error"]["code"] == envelope
    assert alive(http, target)


# --- 확정: 부분 실패 ------------------------------------------------------------------------


def test_provider_success_with_revoke_failure_is_500_and_old_session_survives(http, db, cog, monkeypatch, caplog):
    target = seed_session(db)

    def boom(*args, **kwargs):
        raise RuntimeError(f"INSERT secret {NEW_PASSWORD} {CODE}")

    monkeypatch.setattr(password_reset.sessions, "revoke_user_sessions", boom)
    cog.get_user()
    cog.confirm()
    with caplog.at_level(logging.DEBUG):
        resp = confirm(http)
    assert resp.status_code == 500 and resp.json()["error"]["code"] == "INTERNAL_ERROR"
    assert "set-cookie" not in resp.headers
    assert alive(http, target)  # 승인된 부분 실패 구간: 다음 로그인 · 관리자 회수 · 새 재설정까지 남는다
    for secret in (NEW_PASSWORD, CODE, "INSERT", CLIENT_SECRET):
        assert secret not in caplog.text and secret not in resp.text


def test_database_unavailable_before_change_is_500_without_provider_call(http, cog, monkeypatch):
    def down(*args, **kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(password_reset, "text", down)
    resp = confirm(http)  # 스텁 큐가 비어 있다 — AdminGetUser · Confirm 호출이 있었다면 실패
    assert resp.status_code == 500 and resp.json()["error"]["code"] == "INTERNAL_ERROR"


def test_logs_never_contain_reset_secrets(http, db, cog, caplog):
    seed_session(db)
    cog.get_user()
    cog.error("confirm_forgot_password", "CodeMismatchException")
    with caplog.at_level(logging.DEBUG):
        confirm(http)
    for secret in (NEW_PASSWORD, CODE, CLIENT_SECRET, "PRIVATE-TEXT"):
        assert secret not in caplog.text


# --- 진행 중인 로그인 쿠키 보존 ---------------------------------------------------------

FLOW_COOKIE_HEADER = "__Host-vg_login=in-progress-flow"


@pytest.mark.parametrize(
    "code,http_status",
    [
        ("TooManyRequestsException", 429),
        ("CodeDeliveryFailureException", 503),
        ("AccessDeniedException", 503),
    ],
)
def test_request_errors_do_not_touch_an_in_progress_login_cookie(http, cog, code, http_status):
    cog.error("forgot_password", code)
    resp = http.post(PREFIX, json={"email": EMAIL}, headers={"Origin": ORIGIN, "Cookie": FLOW_COOKIE_HEADER})
    assert resp.status_code == http_status
    assert "set-cookie" not in resp.headers


@pytest.mark.parametrize(
    "lookup_code,http_status",
    [("TooManyRequestsException", 429), ("AccessDeniedException", 503)],
)
def test_confirm_errors_do_not_touch_an_in_progress_login_cookie(http, cog, lookup_code, http_status):
    cog.error("admin_get_user", lookup_code)
    resp = http.post(
        PREFIX + "/confirm",
        json={"email": EMAIL, "code": CODE, "new_password": NEW_PASSWORD},
        headers={"Origin": ORIGIN, "Cookie": FLOW_COOKIE_HEADER},
    )
    assert resp.status_code == http_status
    assert "set-cookie" not in resp.headers


def test_confirm_provider_failure_after_lookup_keeps_login_cookie(http, cog):
    cog.get_user()
    cog.error("confirm_forgot_password", "TooManyFailedAttemptsException")
    resp = http.post(
        PREFIX + "/confirm",
        json={"email": EMAIL, "code": CODE, "new_password": NEW_PASSWORD},
        headers={"Origin": ORIGIN, "Cookie": FLOW_COOKIE_HEADER},
    )
    assert resp.status_code == 429
    assert "set-cookie" not in resp.headers
