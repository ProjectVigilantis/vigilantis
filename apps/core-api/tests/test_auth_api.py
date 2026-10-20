"""/api/v1/auth — 실제 PostgreSQL + 가짜 LoginFlow/시계. HTTPS TestClient 로 Secure 쿠키를 왕복시킨다."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import pytest
from auth import cognito
from auth.http import FLOW_COOKIE, SESSION_COOKIE, get_clock, get_login_flow
from auth.login_flow import MfaStep, StepResult, TotpSetup, VerifiedIdentity
from auth.provider import build_cognito_client
from config import AuthSettings, get_auth_settings
from db import models
from db.repositories import auth as auth_repo
from fastapi.testclient import TestClient
from schemas.api.auth import LoginStep, UserRole
from sqlalchemy import text

ORIGIN = "https://app.example.com"
ISSUER = "https://cognito-idp.ap-northeast-2.amazonaws.com/ap-northeast-2_pool"
SUB = "sub-1234"
PASSWORD = "Pw-secret-1-xyz"
NEW_PASSWORD = "NewPw-secret-123-xyz"
CODE = "123456"
CLIENT_SECRET = "client-secret-zzz-9"
T0 = datetime(2026, 10, 19, 9, 0, tzinfo=UTC)
IDENTITY = VerifiedIdentity(
    issuer=ISSUER, sub=SUB, email="u@example.com", mfa_step=MfaStep.SOFTWARE_TOKEN_MFA
)
PROVEN_IDENTITY = VerifiedIdentity(
    issuer=ISSUER,
    sub=SUB,
    email="u@example.com",
    mfa_step=MfaStep.SOFTWARE_TOKEN_MFA,
    email_proven=True,
)
COMPLETED = StepResult(LoginStep.COMPLETED, PROVEN_IDENTITY)
COMPLETED_UNPROVEN = StepResult(LoginStep.COMPLETED, IDENTITY)

STEP_CALLS = [
    ("/login", {"email": "u@example.com", "password": PASSWORD}, "start"),
    ("/login/new-password", {"new_password": NEW_PASSWORD}, "submit_new_password"),
    ("/login/totp-setup", None, "begin_totp_setup"),
    ("/login/totp-setup/verify", {"code": CODE}, "verify_totp_setup"),
    ("/login/totp", {"code": CODE}, "submit_totp"),
    ("/login/email-verification", {"code": CODE}, "submit_email_code"),
]
POSTS = [p for p, _, _ in STEP_CALLS] + ["/logout", "/activity"]


def settings(**overrides) -> AuthSettings:
    values = {
        "COGNITO_REGION": "ap-northeast-2",
        "COGNITO_USER_POOL_ID": "ap-northeast-2_pool",
        "COGNITO_CLIENT_ID": "client-id",
        "COGNITO_CLIENT_SECRET": CLIENT_SECRET,
        "AUTH_ALLOWED_ORIGINS": ORIGIN,
    }
    values.update(overrides)
    return AuthSettings(_env_file=None, **values)


class FakeFlow:
    """LoginFlow 대역 — 호출을 기록하고, 지정한 결과/예외를 돌려준다."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.result = StepResult(LoginStep.TOTP_REQUIRED)
        self.error: Exception | None = None

    def _go(self, name: str):
        self.calls.append(name)
        if self.error is not None:
            raise self.error
        return self.result

    def start(self, email, password):
        self._go("start")
        return "flowtok-abc123", self.result.step

    def submit_new_password(self, token, new_password):
        return self._go("submit_new_password")

    def begin_totp_setup(self, token):
        self._go("begin_totp_setup")
        secret = "JBSWY3DPEHPK3PXP"
        return TotpSetup(secret, f"otpauth://totp/Vigilantis:u?secret={secret}&issuer=Vigilantis")

    def verify_totp_setup(self, token, code):
        return self._go("verify_totp_setup")

    def submit_totp(self, token, code):
        return self._go("submit_totp")

    def submit_email_code(self, token, code):
        return self._go("submit_email_code")


class Clock:
    def __init__(self) -> None:
        self.now = T0


@pytest.fixture()
def flow():
    return FakeFlow()


@pytest.fixture()
def clock():
    return Clock()


@pytest.fixture()
def app(client_pg_anon, flow, clock):
    # 세션 없는 앱 — 이 파일은 세션 행 수를 직접 센다(client_pg 는 OPERATOR 세션을 미리 만든다)
    application = client_pg_anon.app
    application.dependency_overrides[get_auth_settings] = lambda: settings()
    application.dependency_overrides[get_login_flow] = lambda: flow
    application.dependency_overrides[get_clock] = lambda: (lambda: clock.now)
    return application


@pytest.fixture()
def http(app):
    with TestClient(app, base_url="https://testserver") as test_client:
        yield test_client


@pytest.fixture()
def user(db):
    row = auth_repo.create_user(
        db, issuer=ISSUER, cognito_sub=SUB, email="u@example.com", role=UserRole.OPERATOR, now=T0
    )
    db.commit()
    return row


def post(c, path, body=None, origin=ORIGIN):
    return c.post(
        "/api/v1/auth" + path, json=body, headers={"Origin": origin} if origin else {}
    )


def session_count(db) -> int:
    return db.execute(text("SELECT count(*) FROM auth_sessions")).scalar_one()


def set_cookies(resp) -> dict[str, tuple[str, dict[str, str]]]:
    parsed = {}
    for raw in resp.headers.get_list("set-cookie"):
        name, _, rest = raw.partition("=")
        value, *attrs = [part.strip() for part in rest.split(";")]
        parsed[name] = (
            value.strip('"'),
            {k.lower(): v for k, _, v in (a.partition("=") for a in attrs)},
        )
    return parsed


def login(c, flow) -> str:
    """로그인 → 세션 토큰."""
    assert post(c, "/login", {"email": "u@example.com", "password": PASSWORD}).status_code == 200
    flow.result = COMPLETED
    resp = post(c, "/login/totp", {"code": CODE})
    assert resp.status_code == 200 and resp.json() == {"next_step": "COMPLETED"}
    return set_cookies(resp)[SESSION_COOKIE][0]


def replay(c, path, token, *, method="GET"):
    return c.request(
        method, path, headers={"Cookie": f"{SESSION_COOKIE}={token}", "Origin": ORIGIN}
    )


# --- 세션 쿠키는 COMPLETED + identity + 발급 뒤에만 ----------------------------------


@pytest.mark.parametrize("path,body,method", STEP_CALLS)
@pytest.mark.parametrize(
    "step",
    [
        LoginStep.NEW_PASSWORD_REQUIRED,
        LoginStep.TOTP_SETUP_REQUIRED,
        LoginStep.TOTP_REQUIRED,
        LoginStep.EMAIL_VERIFICATION_REQUIRED,
    ],
)
def test_non_completed_steps_never_set_session_cookie(http, db, flow, user, path, body, method, step):
    flow.result = StepResult(step)
    if path != "/login":
        post(http, "/login", {"email": "u@example.com", "password": PASSWORD})
    resp = post(http, path, body)
    assert resp.status_code == 200
    assert SESSION_COOKIE not in set_cookies(resp)
    assert session_count(db) == 0


def test_completed_without_identity_is_rejected_without_cookie(http, db, flow, user):
    post(http, "/login", {"email": "u@example.com", "password": PASSWORD})
    flow.result = StepResult(LoginStep.COMPLETED, None)
    resp = post(http, "/login/totp", {"code": CODE})
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "AUTH_FAILED"
    assert SESSION_COOKIE not in set_cookies(resp)
    assert session_count(db) == 0


FLOW_ENDING = {"AUTH_RATE_LIMITED", "AUTH_PROVIDER_UNAVAILABLE", "AUTH_EMAIL_DELIVERY_FAILED"}


@pytest.mark.parametrize(
    "error,status,code",
    [
        (cognito.AuthFailedError(), 401, "AUTH_FAILED"),
        (cognito.AuthCodeInvalidError(), 401, "AUTH_CODE_INVALID"),
        (cognito.AuthFlowExpiredError(), 401, "AUTH_FLOW_EXPIRED"),
        (cognito.AuthStepConflictError(), 409, "AUTH_STEP_CONFLICT"),
        (cognito.AuthPasswordPolicyError(), 422, "AUTH_PASSWORD_POLICY"),
        (cognito.AuthEmailDeliveryFailedError(), 503, "AUTH_EMAIL_DELIVERY_FAILED"),
        (cognito.AuthRateLimitedError(), 429, "AUTH_RATE_LIMITED"),
        (cognito.AuthProviderUnavailableError(), 503, "AUTH_PROVIDER_UNAVAILABLE"),
    ],
)
def test_flow_errors_map_to_envelope_and_leave_no_session(http, db, flow, user, error, status, code):
    post(http, "/login", {"email": "u@example.com", "password": PASSWORD})
    flow.error = error
    resp = post(http, "/login/totp", {"code": CODE})
    assert resp.status_code == status
    assert resp.json()["error"]["code"] == code
    assert SESSION_COOKIE not in set_cookies(resp)
    assert session_count(db) == 0
    flow_cookie = set_cookies(resp).get(FLOW_COOKIE)
    if code == "AUTH_FLOW_EXPIRED" or code in FLOW_ENDING:
        assert flow_cookie is not None and flow_cookie[1]["max-age"] == "0"
    else:
        assert flow_cookie is None


def test_step_without_flow_cookie_is_flow_expired_and_flow_untouched(http, db, flow):
    resp = post(http, "/login/totp", {"code": CODE})
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "AUTH_FLOW_EXPIRED"
    assert flow.calls == []


def test_completed_without_app_user_is_403_without_cookie_or_row(http, db, flow):
    post(http, "/login", {"email": "u@example.com", "password": PASSWORD})
    flow.result = COMPLETED
    resp = post(http, "/login/totp", {"code": CODE})
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "ACCOUNT_NOT_PROVISIONED"
    assert SESSION_COOKIE not in set_cookies(resp)
    assert session_count(db) == 0


def test_completed_for_disabled_user_is_403(http, db, flow, user):
    auth_repo.set_status(db, user_id=user.user_id, status=models.AppUserStatus.DISABLED, now=T0)
    db.commit()
    post(http, "/login", {"email": "u@example.com", "password": PASSWORD})
    flow.result = COMPLETED
    resp = post(http, "/login/totp", {"code": CODE})
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "ACCOUNT_DISABLED"
    assert SESSION_COOKIE not in set_cookies(resp)
    assert session_count(db) == 0


def test_email_verification_completes_and_marks_email_verified(http, db, flow, user):
    post(http, "/login", {"email": "u@example.com", "password": PASSWORD})
    flow.result = StepResult(LoginStep.EMAIL_VERIFICATION_REQUIRED)
    post(http, "/login/totp", {"code": CODE})
    flow.result = COMPLETED
    resp = post(http, "/login/email-verification", {"code": CODE})
    assert resp.status_code == 200
    assert SESSION_COOKIE in set_cookies(resp)
    db.refresh(user)
    assert user.email_verified_at == T0


def test_unproven_identity_for_user_without_email_proof_is_rejected(http, db, flow, user):
    post(http, "/login", {"email": "u@example.com", "password": PASSWORD})
    flow.result = COMPLETED_UNPROVEN
    resp = post(http, "/login/totp", {"code": CODE})
    assert resp.json()["error"]["code"] == "AUTH_FAILED"
    assert SESSION_COOKIE not in set_cookies(resp)
    assert session_count(db) == 0
    db.refresh(user)
    assert user.email_verified_at is None


def test_unproven_identity_for_user_with_recorded_proof_completes_unchanged(http, db, flow, user):
    auth_repo.mark_email_verified(db, user_id=user.user_id, now=T0)
    db.commit()
    post(http, "/login", {"email": "u@example.com", "password": PASSWORD})
    flow.result = COMPLETED_UNPROVEN
    resp = post(http, "/login/totp", {"code": CODE})
    assert resp.status_code == 200
    assert SESSION_COOKIE in set_cookies(resp)
    db.refresh(user)
    assert user.email_verified_at == T0


def test_service_sets_email_verified_at_only_when_proven(db, user):
    from auth import service

    kwargs = {"previous_token": None, "now": T0, "max_seconds": 3600, "idle_seconds": 600}
    with pytest.raises(cognito.AuthFailedError):
        service.complete_login(db, IDENTITY, **kwargs)
    db.refresh(user)
    assert user.email_verified_at is None
    service.complete_login(db, PROVEN_IDENTITY, **kwargs)
    db.refresh(user)
    assert user.email_verified_at == T0


# --- 쿠키 속성 -------------------------------------------------------------------


def test_cookie_attributes_exact(http, flow, user):
    start = post(http, "/login", {"email": "u@example.com", "password": PASSWORD})
    value, attrs = set_cookies(start)[FLOW_COOKIE]
    assert value == "flowtok-abc123"
    assert attrs == {"httponly": "", "max-age": "300", "path": "/", "samesite": "lax", "secure": ""}

    flow.result = COMPLETED
    done = post(http, "/login/totp", {"code": CODE})
    cookies = set_cookies(done)
    value, attrs = cookies[SESSION_COOKIE]
    assert value
    assert attrs == {
        "httponly": "",
        "max-age": "28800",
        "path": "/",
        "samesite": "lax",
        "secure": "",
    }
    # 완료 시 로그인 진행 쿠키는 지워진다
    assert cookies[FLOW_COOKIE][0] == "" and cookies[FLOW_COOKIE][1]["max-age"] == "0"
    for raw in done.headers.get_list("set-cookie"):
        assert "domain" not in raw.lower()
    assert "token" not in done.text.lower()


def test_session_max_age_follows_setting(app, http, flow, user):
    app.dependency_overrides[get_auth_settings] = lambda: settings(AUTH_SESSION_MAX_SECONDS=3600)
    post(http, "/login", {"email": "u@example.com", "password": PASSWORD})
    flow.result = COMPLETED
    done = post(http, "/login/totp", {"code": CODE})
    assert set_cookies(done)[SESSION_COOKIE][1]["max-age"] == "3600"


def test_relogin_supersedes_previous_session(http, db, flow, user):
    first = login(http, flow)
    second = login(http, flow)
    assert first != second
    rows = dict(
        db.execute(text("SELECT token_hash, revoke_reason::text FROM auth_sessions")).all()
    )
    from auth.sessions import hash_token

    assert rows[hash_token(first)] == "SUPERSEDED"
    assert rows[hash_token(second)] is None


# --- Origin 검사 ------------------------------------------------------------------


@pytest.mark.parametrize("path", POSTS)
@pytest.mark.parametrize(
    "origin", [None, "https://evil.example.com", ORIGIN + "/", ORIGIN.upper(), "null"]
)
def test_bad_origin_is_403_before_flow_or_db(http, db, flow, user, path, origin):
    body = next((b for p, b, _ in STEP_CALLS if p == path), None)
    before = db.execute(text("SELECT count(*) FROM auth_sessions")).scalar_one()
    resp = post(http, path, body, origin=origin)
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "AUTH_ORIGIN_REJECTED"
    assert flow.calls == []
    assert session_count(db) == before == 0
    assert resp.headers["cache-control"] == "no-store"


def test_bad_origin_logout_does_not_revoke(http, db, flow, user):
    token = login(http, flow)
    assert post(http, "/logout", origin="https://evil.example.com").status_code == 403
    assert replay(http, "/api/v1/auth/me", token).status_code == 200


def test_me_does_not_require_origin(http, flow, user):
    login(http, flow)
    assert http.get("/api/v1/auth/me").status_code == 200


# --- 세션 정책 ---------------------------------------------------------------------


def me(c, clock, at, token=None):
    clock.now = at
    return replay(c, "/api/v1/auth/me", token) if token else c.get("/api/v1/auth/me")


def test_me_returns_user_and_session_info(http, flow, user):
    login(http, flow)
    body = http.get("/api/v1/auth/me").json()
    assert body["user_id"] == user.user_id
    assert (body["email"], body["role"]) == ("u@example.com", "OPERATOR")
    assert body["session"]["idle_expires_at"].startswith("2026-10-19T09:30:00")
    assert body["session"]["absolute_expires_at"].startswith("2026-10-19T17:00:00")


def test_idle_expiry_and_get_me_never_extends(http, clock, flow, user):
    login(http, flow)
    assert me(http, clock, T0 + timedelta(minutes=20)).status_code == 200
    resp = me(http, clock, T0 + timedelta(minutes=30))
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "AUTH_REQUIRED"


def test_activity_extends_idle(http, clock, flow, user):
    login(http, flow)
    clock.now = T0 + timedelta(minutes=20)
    assert post(http, "/activity").status_code == 204
    assert me(http, clock, T0 + timedelta(minutes=45)).status_code == 200


def test_absolute_lifetime_wins_over_regular_activity(http, clock, flow, user):
    login(http, flow)
    for minutes in range(20, 480, 20):
        clock.now = T0 + timedelta(minutes=minutes)
        assert post(http, "/activity").status_code == 204
    assert me(http, clock, T0 + timedelta(hours=8)).status_code == 401


def test_activity_without_valid_session_is_401(http, user):
    resp = post(http, "/activity")
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "AUTH_REQUIRED"


def test_logout_revokes_and_is_idempotent(http, db, clock, flow, user):
    token = login(http, flow)
    out = post(http, "/logout")
    assert out.status_code == 204 and out.content == b""
    assert set_cookies(out)[SESSION_COOKIE][1]["max-age"] == "0"
    assert replay(http, "/api/v1/auth/me", token).status_code == 401
    assert (
        db.execute(text("SELECT revoke_reason::text FROM auth_sessions")).scalar_one() == "LOGOUT"
    )
    assert post(http, "/logout").status_code == 204
    # 쿠키 없이도 · 이미 폐기된 토큰으로도 204
    assert replay(http, "/api/v1/auth/logout", token, method="POST").status_code == 204


def test_disabled_user_session_is_unauthenticated(http, db, flow, user):
    token = login(http, flow)
    auth_repo.set_status(db, user_id=user.user_id, status=models.AppUserStatus.DISABLED, now=T0)
    db.commit()
    assert replay(http, "/api/v1/auth/me", token).status_code == 401
    assert replay(http, "/api/v1/auth/activity", token, method="POST").status_code == 401


# --- 헤더 · 로그 · 검증 오류 --------------------------------------------------------


def _assert_security_headers(resp):
    assert resp.headers["cache-control"] == "no-store"
    assert resp.headers["referrer-policy"] == "strict-origin"
    assert resp.headers["x-content-type-options"] == "nosniff"


def test_security_headers_on_success_and_errors(http, flow, user):
    _assert_security_headers(post(http, "/login", {"email": "u@example.com", "password": PASSWORD}))
    flow.result = COMPLETED
    _assert_security_headers(post(http, "/login/totp", {"code": CODE}))
    _assert_security_headers(http.get("/api/v1/auth/me"))
    _assert_security_headers(post(http, "/logout"))
    _assert_security_headers(http.get("/api/v1/auth/me"))  # 401
    _assert_security_headers(post(http, "/login", {"email": "x"}))  # 422
    _assert_security_headers(post(http, "/login", {}, origin=None))  # 403
    flow.error = cognito.AuthRateLimitedError()
    _assert_security_headers(post(http, "/login", {"email": "u@example.com", "password": PASSWORD}))
    _assert_security_headers(post(http, "/login/totp-setup"))


def test_totp_setup_response_is_no_store_and_has_secret(http, flow, user):
    post(http, "/login", {"email": "u@example.com", "password": PASSWORD})
    resp = post(http, "/login/totp-setup")
    assert resp.status_code == 200
    assert resp.json()["secret"] == "JBSWY3DPEHPK3PXP"
    _assert_security_headers(resp)


def test_validation_errors_never_echo_secrets(http, flow, user):
    cases = [
        ("/login", {"email": "not-an-email", "password": PASSWORD}),
        ("/login/new-password", {"new_password": "short"}),
        ("/login/totp", {"code": "12ab56"}),
        ("/login/totp", {"code": CODE, "extra": PASSWORD}),
    ]
    post(http, "/login", {"email": "u@example.com", "password": PASSWORD})
    for path, body in cases:
        resp = post(http, path, body)
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "REQUEST_VALIDATION_FAILED"
        for secret in (PASSWORD, "12ab56", "short"):
            assert secret not in resp.text


class _Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.text: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.text.append(f"{record.getMessage()} {record.__dict__}")


def test_logs_never_contain_secrets(http, flow, user):
    # create_app 의 setup_logging 이 루트 핸들러를 갈아끼우므로 앱 생성 뒤에 직접 붙인다
    capture = _Capture()
    root = logging.getLogger()
    old_level = root.level
    root.addHandler(capture)
    root.setLevel(logging.DEBUG)
    try:
        token = login(http, flow)
        post(http, "/login/new-password", {"new_password": "short"})
        http.get("/api/v1/auth/me")
        post(http, "/logout")
        flow.error = cognito.AuthFailedError()
        post(http, "/login", {"email": "u@example.com", "password": PASSWORD})
    finally:
        root.removeHandler(capture)
        root.setLevel(old_level)
    logged = "\n".join(capture.text)
    assert "http_request" in logged  # 캡처가 실제로 동작했다
    for secret in (PASSWORD, NEW_PASSWORD, CODE, "flowtok-abc123", token, CLIENT_SECRET):
        assert secret not in logged


# --- Cognito 미설정 / 클라이언트 조립 / CORS -----------------------------------------


def test_unconfigured_cognito_is_503_but_rest_of_app_works(client_pg_anon, db, make_auth_session):
    app = client_pg_anon.app
    app.dependency_overrides[get_auth_settings] = lambda: AuthSettings(
        _env_file=None, AUTH_ALLOWED_ORIGINS=ORIGIN
    )
    # 보호 API 는 Cognito 설정과 무관하게 세션만으로 열린다(#492 PR 2)
    seed = make_auth_session()
    with TestClient(app, base_url="https://testserver") as c:
        for method, path in [
            ("POST", "/login"),
            ("POST", "/login/totp"),
            ("POST", "/logout"),
            ("POST", "/activity"),
            ("GET", "/me"),
        ]:
            resp = c.request(method, "/api/v1/auth" + path, headers={"Origin": ORIGIN}, json=None)
            assert resp.status_code == 503, path
            assert resp.json()["error"]["code"] == "AUTH_PROVIDER_UNAVAILABLE"
            assert resp.headers["cache-control"] == "no-store"
        assert c.get("/health").json() == {"status": "ok"}
        assert c.get("/api/v1/incidents", headers={"Cookie": seed.cookie}).status_code == 200
        assert c.get("/api/v1/incidents").status_code == 401
    assert session_count(db) == 1  # 시드한 세션뿐 — 503 경로는 세션을 만들지 않았다


def test_missing_keys_are_named_without_values():
    partial = AuthSettings(_env_file=None, COGNITO_REGION="ap-northeast-2", COGNITO_CLIENT_ID="  ")
    assert partial.missing_cognito_keys() == [
        "COGNITO_USER_POOL_ID",
        "COGNITO_CLIENT_ID",
        "COGNITO_CLIENT_SECRET",
    ]


def test_startup_logs_missing_keys_only(caplog):
    from auth.http import log_provider_status

    with caplog.at_level(logging.WARNING, logger="vigilantis.auth"):
        log_provider_status(AuthSettings(_env_file=None, COGNITO_REGION="ap-northeast-2"))
    assert len(caplog.records) == 1
    assert "COGNITO_USER_POOL_ID" in caplog.text
    assert "ap-northeast-2" not in caplog.text


def test_cognito_client_ignores_global_endpoint_override(monkeypatch):
    monkeypatch.setenv("AWS_ENDPOINT_URL", "http://localhost:4566")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    client = build_cognito_client(settings())
    assert client.meta.endpoint_url == "https://cognito-idp.ap-northeast-2.amazonaws.com"


def test_cors_allows_credentials_for_explicit_origin_only(client_pg):
    resp = client_pg.options(
        "/api/v1/auth/me",
        headers={"Origin": "http://localhost:3000", "Access-Control-Request-Method": "GET"},
    )
    assert resp.headers["access-control-allow-origin"] == "http://localhost:3000"
    assert resp.headers["access-control-allow-credentials"] == "true"
    assert resp.headers["access-control-allow-origin"] != "*"
    denied = client_pg.options(
        "/api/v1/auth/me",
        headers={"Origin": "https://evil.example.com", "Access-Control-Request-Method": "GET"},
    )
    # Starlette 는 거절한 사전요청(400)에도 allow-credentials 를 싣지만 allow-origin 이 없어 브라우저가 막는다
    assert denied.status_code == 400
    assert "access-control-allow-origin" not in denied.headers


@pytest.mark.parametrize(
    "error",
    [
        cognito.AuthRateLimitedError(),
        cognito.AuthProviderUnavailableError(),
        cognito.AuthEmailDeliveryFailedError(),
    ],
)
def test_flow_ending_provider_errors_clear_the_flow_cookie(http, flow, user, error):
    post(http, "/login", {"email": "u@example.com", "password": PASSWORD})
    flow.error = error
    resp = post(http, "/login/totp", {"code": CODE})
    cookie = set_cookies(resp)[FLOW_COOKIE]
    assert cookie[1]["max-age"] == "0"


def test_cognito_client_ignores_aws_profile_env_when_no_cognito_profile(monkeypatch, tmp_path):
    monkeypatch.setenv("AWS_PROFILE", "no-such-profile")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "env-key")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "env-secret")
    monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "config"))
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "credentials"))
    client = build_cognito_client(settings(COGNITO_AWS_PROFILE=None))
    assert client._request_signer._credentials.access_key == "env-key"


def test_cognito_client_uses_cognito_aws_profile_when_set(monkeypatch, tmp_path):
    credentials = tmp_path / "credentials"
    credentials.write_text(
        "[cognito-dev]\naws_access_key_id = profile-key\naws_secret_access_key = profile-secret\n"
    )
    monkeypatch.setenv("AWS_PROFILE", "no-such-profile")
    monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "config"))
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(credentials))
    for key in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
        monkeypatch.delenv(key, raising=False)
    client = build_cognito_client(settings(COGNITO_AWS_PROFILE="cognito-dev"))
    assert client._request_signer._credentials.access_key == "profile-key"


def test_code_failure_limiter_settings_defaults_and_bounds():
    s = AuthSettings(_env_file=None)
    assert (s.AUTH_CODE_FAILURE_LIMIT, s.AUTH_CODE_FAILURE_WINDOW_SECONDS) == (10, 900)
    with pytest.raises(ValueError):
        AuthSettings(_env_file=None, AUTH_CODE_FAILURE_LIMIT=0)
    with pytest.raises(ValueError):
        AuthSettings(_env_file=None, AUTH_CODE_FAILURE_WINDOW_SECONDS=0)


def test_build_login_flow_passes_attempt_and_limiter_settings(monkeypatch):
    from auth.provider import build_login_flow

    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "env-key")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "env-secret")
    flow = build_login_flow(
        settings(AUTH_TOTP_MAX_ATTEMPTS=2, AUTH_CODE_FAILURE_LIMIT=4, AUTH_CODE_FAILURE_WINDOW_SECONDS=60)
    )
    assert flow._max_failures == 2
    assert (flow._limiter._limit, flow._limiter._window) == (4, 60)


def test_allowed_origins_fall_back_to_cors_list_and_reject_wildcard():
    assert AuthSettings(_env_file=None).allowed_origins(["http://a"]) == ["http://a"]
    assert settings().allowed_origins(["http://a"]) == [ORIGIN]
    with pytest.raises(ValueError):
        AuthSettings(_env_file=None, AUTH_ALLOWED_ORIGINS="*")


def test_cors_wildcard_is_rejected_because_credentials_are_allowed():
    from config import Settings

    with pytest.raises(ValueError):
        Settings(_env_file=None, DATABASE_URL="postgresql://u@h/d", CORS_ALLOW_ORIGINS="*")
    with pytest.raises(ValueError):
        Settings(_env_file=None, DATABASE_URL="postgresql://u@h/d", CORS_ALLOW_ORIGINS="http://a,*")
