"""로그인 상태 머신 — Stubber 로 Cognito 호출 순서 · 파라미터 · 세션 발급 정책을 검증한다 (#492 PR 1)."""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import sys
from contextlib import contextmanager
from pathlib import Path

import boto3
import pytest
from botocore.stub import Stubber

CORE_API = Path(__file__).resolve().parents[1]
PACKAGES = Path(__file__).resolve().parents[3] / "packages"
for p in (str(CORE_API), str(PACKAGES)):
    if p not in sys.path:
        sys.path.insert(0, p)

from auth import cognito, login_flow
from auth.flow_store import FlowStore
from schemas.api.auth import LoginStep, TotpSetupResponse

POOL = "ap-northeast-2_TestPool"
CLIENT_ID = "client-id-123"
CLIENT_SECRET = "client-secret-value-0123456789"
UID = "11111111-2222-3333-4444-555555555555"
EMAIL = "user@example.com"
PASSWORD = "Pa55-word-initial"
NEW_PASSWORD = "Br4nd-new-Pass-phrase"
TOTP_CODE = "482913"
EMAIL_CODE = "705164"
ACCESS, REFRESH, ID_TOKEN = "access-token-xyz", "refresh-token-xyz", "id-token-xyz"
TOTP_SECRET = "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP"


def sh(username: str) -> str:
    mac = hmac.new(CLIENT_SECRET.encode(), (username + CLIENT_ID).encode(), hashlib.sha256).digest()
    return base64.b64encode(mac).decode()


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


ISSUER = f"https://cognito-idp.ap-northeast-2.amazonaws.com/{POOL}"
SUB = "sub-123"
#: 시작이 받아들이는 챌린지 — 이 챌린지 뒤에만 AdminGetUser 로 신원을 묶는다(#515)
BOUND_CHALLENGES = {"SOFTWARE_TOKEN_MFA", "MFA_SETUP", "NEW_PASSWORD_REQUIRED"}


class FakeGate:
    """LoginGate 대역 — 포착 · 증명 조회 · 등록 구간을 기록한다(DB 동작은 test_auth_totp_recovery 가 검증한다)."""

    def __init__(self, *, proof=True, generation=0, setup_only=False) -> None:
        self.proof = proof
        self.generation = generation
        self.setup_only = setup_only
        self.binds: list[tuple[str, str]] = []
        self.proofs: list[login_flow.LoginTicket] = []
        self.setup_checks: list[login_flow.LoginTicket] = []
        self.enrollments: list[login_flow.LoginTicket] = []
        self.records: list[login_flow.LoginTicket] = []
        self.reject_bind: Exception | None = None
        self.reject_setup: Exception | None = None
        self.reject_enrollment: Exception | None = None
        self.reject_record: Exception | None = None

    def bind(self, issuer, sub):
        self.binds.append((issuer, sub))
        if self.reject_bind is not None:
            raise self.reject_bind
        return login_flow.LoginTicket(issuer, sub, self.generation, self.setup_only)

    def proof_recorded(self, ticket):
        self.proofs.append(ticket)
        return self.proof

    def check_setup(self, ticket):
        self.setup_checks.append(ticket)
        if self.reject_setup is not None:
            raise self.reject_setup

    @contextmanager
    def enrollment(self, ticket):
        self.enrollments.append(ticket)
        if self.reject_enrollment is not None:
            raise self.reject_enrollment
        yield lambda: self._record(ticket)

    def _record(self, ticket):
        if self.reject_record is not None:
            raise self.reject_record
        self.records.append(ticket)


class Env:
    def __init__(self, gate: FakeGate | None = None, **flow_kwargs) -> None:
        client = boto3.client(
            "cognito-idp", region_name="ap-northeast-2", aws_access_key_id="dummy", aws_secret_access_key="dummy"
        )
        self.stubber = Stubber(client)
        self.stubber.activate()
        self.clock = Clock()
        self.store: FlowStore = FlowStore()
        adapter = cognito.CognitoAdapter(
            client, user_pool_id=POOL, client_id=CLIENT_ID, client_secret=CLIENT_SECRET, region="ap-northeast-2"
        )
        self.gate = gate if gate is not None else FakeGate()
        self.flow = login_flow.LoginFlow(adapter, self.store, clock=self.clock, gate=self.gate, **flow_kwargs)

    # --- Stubber 응답 등록 도우미 --------------------------------------------
    def initiate(self, name=None, session="SESS-0-0123456789abcdef", params=None, tokens=False, bind=None):
        params = params if params is not None else {"USER_ID_FOR_SRP": UID}
        if tokens:
            resp = {"AuthenticationResult": self.tokens_payload()}
        else:
            resp = {"ChallengeName": name, "Session": session, "ChallengeParameters": params}
        self.stubber.add_response(
            "admin_initiate_auth",
            resp,
            {
                "UserPoolId": POOL,
                "ClientId": CLIENT_ID,
                "AuthFlow": "ADMIN_USER_PASSWORD_AUTH",
                "AuthParameters": {"USERNAME": EMAIL, "PASSWORD": PASSWORD, "SECRET_HASH": sh(EMAIL)},
            },
        )
        # 시작은 받아들인 챌린지 뒤(한도 확인 다음)에 AdminGetUser 로 sub 를 얻는다 — 거절 · 한도 경로는 bind=False
        if bind if bind is not None else (not tokens and name in BOUND_CHALLENGES):
            self.admin_get_user(params["USER_ID_FOR_SRP"])

    def admin_get_user(self, username, sub=SUB):
        self.stubber.add_response(
            "admin_get_user",
            {
                "Username": username,
                "UserAttributes": [{"Name": "sub", "Value": sub}, {"Name": "email", "Value": EMAIL}],
                "UserStatus": "CONFIRMED",
            },
            {"UserPoolId": POOL, "Username": username},
        )

    @staticmethod
    def tokens_payload():
        return {"AccessToken": ACCESS, "RefreshToken": REFRESH, "IdToken": ID_TOKEN, "ExpiresIn": 3600, "TokenType": "Bearer"}

    def respond(self, challenge, session, extra=None, *, next_challenge=None, next_session=None, tokens=False):
        if tokens:
            resp = {"AuthenticationResult": self.tokens_payload()}
        else:
            resp = {"ChallengeName": next_challenge, "Session": next_session, "ChallengeParameters": {}}
        self.stubber.add_response(
            "admin_respond_to_auth_challenge",
            resp,
            {
                "UserPoolId": POOL,
                "ClientId": CLIENT_ID,
                "ChallengeName": challenge,
                "Session": session,
                "ChallengeResponses": {**(extra or {}), "USERNAME": UID, "SECRET_HASH": sh(UID)},
            },
        )

    def respond_error(self, challenge, session, extra, code="CodeMismatchException"):
        self.stubber.add_client_error(
            "admin_respond_to_auth_challenge",
            service_error_code=code,
            service_message="Invalid code",
            http_status_code=400,
            expected_params={
                "UserPoolId": POOL,
                "ClientId": CLIENT_ID,
                "ChallengeName": challenge,
                "Session": session,
                "ChallengeResponses": {**extra, "USERNAME": UID, "SECRET_HASH": sh(UID)},
            },
        )

    def get_user(self, verified: bool | None):
        attrs = [{"Name": "sub", "Value": "sub-123"}, {"Name": "email", "Value": EMAIL}]
        if verified is not None:
            attrs.append({"Name": "email_verified", "Value": "true" if verified else "false"})
        self.stubber.add_response("get_user", {"Username": UID, "UserAttributes": attrs}, {"AccessToken": ACCESS})

    def mfa_pref(self):
        self.stubber.add_response(
            "set_user_mfa_preference",
            {},
            {"AccessToken": ACCESS, "SoftwareTokenMfaSettings": {"Enabled": True, "PreferredMfa": True}},
        )

    def send_email(self):
        self.stubber.add_response(
            "get_user_attribute_verification_code",
            {"CodeDeliveryDetails": {"Destination": "u***@e***.com", "DeliveryMedium": "EMAIL", "AttributeName": "email"}},
            {"AccessToken": ACCESS, "AttributeName": "email"},
        )

    def verify_email(self, ok=True):
        params = {"AccessToken": ACCESS, "AttributeName": "email", "Code": EMAIL_CODE}
        if ok:
            self.stubber.add_response("verify_user_attribute", {}, params)
        else:
            self.stubber.add_client_error(
                "verify_user_attribute",
                service_error_code="CodeMismatchException",
                service_message="m",
                http_status_code=400,
                expected_params=params,
            )

    def revoke(self):
        self.stubber.add_response(
            "revoke_token", {}, {"Token": REFRESH, "ClientId": CLIENT_ID, "ClientSecret": CLIENT_SECRET}
        )

    def done(self):
        self.stubber.assert_no_pending_responses()


@pytest.fixture
def env():
    e = Env()
    yield e
    e.stubber.deactivate()


def totp_extra(code=TOTP_CODE):
    return {"SOFTWARE_TOKEN_MFA_CODE": code}


# ---------------------------------------------------------------------------- 정상 경로
def test_enrolled_user_completes_and_revokes(env):
    env.initiate("SOFTWARE_TOKEN_MFA")
    env.respond("SOFTWARE_TOKEN_MFA", "SESS-0-0123456789abcdef", totp_extra(), tokens=True)
    env.get_user(True)
    env.revoke()
    token, step = env.flow.start(EMAIL, PASSWORD)
    assert step is LoginStep.TOTP_REQUIRED
    result = env.flow.submit_totp(token, TOTP_CODE)
    assert result.step is LoginStep.COMPLETED
    assert result.identity == login_flow.VerifiedIdentity(
        issuer=f"https://cognito-idp.ap-northeast-2.amazonaws.com/{POOL}",
        sub="sub-123",
        email=EMAIL,
        mfa_step=login_flow.MfaStep.SOFTWARE_TOKEN_MFA,
        generation=0,
    )
    assert env.gate.binds == [(ISSUER, SUB)]
    env.done()
    assert len(env.store) == 0


def queue_invited_until_setup(env):
    env.initiate("NEW_PASSWORD_REQUIRED", params={"USER_ID_FOR_SRP": UID, "requiredAttributes": "[]", "userAttributes": "{}"})
    env.respond("NEW_PASSWORD_REQUIRED", "SESS-0-0123456789abcdef", {"NEW_PASSWORD": NEW_PASSWORD}, next_challenge="MFA_SETUP", next_session="SESS-1-0123456789abcdef")
    env.stubber.add_response("associate_software_token", {"SecretCode": TOTP_SECRET, "Session": "SESS-2-0123456789abcdef"}, {"Session": "SESS-1-0123456789abcdef"})


def test_invited_user_completes_with_email_code_and_revokes(env):
    queue_invited_until_setup(env)
    env.stubber.add_response(
        "verify_software_token",
        {"Status": "SUCCESS", "Session": "SESS-3-0123456789abcdef"},
        {"Session": "SESS-2-0123456789abcdef", "UserCode": TOTP_CODE, "FriendlyDeviceName": "Vigilantis"},
    )
    env.respond("MFA_SETUP", "SESS-3-0123456789abcdef", tokens=True)
    env.mfa_pref()
    env.get_user(None)
    env.send_email()
    env.verify_email()
    env.revoke()

    token, step = env.flow.start(EMAIL, PASSWORD)
    assert step is LoginStep.NEW_PASSWORD_REQUIRED
    assert env.flow.submit_new_password(token, NEW_PASSWORD).step is LoginStep.TOTP_SETUP_REQUIRED
    setup = env.flow.begin_totp_setup(token)
    assert setup.secret == TOTP_SECRET
    TotpSetupResponse(secret=setup.secret, otpauth_uri=setup.otpauth_uri)  # 계약 검증
    assert setup.otpauth_uri.startswith("otpauth://totp/Vigilantis:user%40example.com?")
    assert setup.otpauth_uri.endswith("&issuer=Vigilantis")
    assert env.flow.verify_totp_setup(token, TOTP_CODE).step is LoginStep.EMAIL_VERIFICATION_REQUIRED
    result = env.flow.submit_email_code(token, EMAIL_CODE)
    assert result.step is LoginStep.COMPLETED
    assert result.identity.mfa_step is login_flow.MfaStep.MFA_SETUP
    assert result.identity.sub == "sub-123"
    assert result.identity.email_proven is True
    env.done()
    assert len(env.store) == 0


# ---------------------------------------------------------------------------- TOTP 성공 없이는 세션 없음
def test_tokens_straight_from_initiate_rejected_and_revoked(env, caplog):
    env.initiate(tokens=True)
    env.revoke()
    with caplog.at_level(logging.DEBUG), pytest.raises(cognito.AuthFailedError):
        env.flow.start(EMAIL, PASSWORD)
    env.done()
    assert len(env.store) == 0
    assert len([r for r in caplog.records if r.levelno == logging.ERROR]) == 1


def test_tokens_after_new_password_rejected_and_revoked(env):
    env.initiate("NEW_PASSWORD_REQUIRED")
    env.respond("NEW_PASSWORD_REQUIRED", "SESS-0-0123456789abcdef", {"NEW_PASSWORD": NEW_PASSWORD}, tokens=True)
    env.revoke()
    token, _ = env.flow.start(EMAIL, PASSWORD)
    with pytest.raises(cognito.AuthFailedError):
        env.flow.submit_new_password(token, NEW_PASSWORD)
    env.done()
    with pytest.raises(cognito.AuthFlowExpiredError):
        env.flow.submit_totp(token, TOTP_CODE)


@pytest.mark.parametrize(
    "challenge",
    ["SELECT_MFA_TYPE", "EMAIL_OTP", "SMS_MFA", "CUSTOM_CHALLENGE", "DEVICE_SRP_AUTH", "DEVICE_PASSWORD_VERIFIER"],
)
def test_unexpected_challenge_rejected_with_single_error_log(env, caplog, challenge):
    env.initiate(challenge)
    with caplog.at_level(logging.DEBUG), pytest.raises(cognito.AuthFailedError):
        env.flow.start(EMAIL, PASSWORD)
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1 and challenge in errors[0].getMessage()
    assert len(env.store) == 0


def test_unexpected_challenge_after_totp_rejected(env):
    env.initiate("SOFTWARE_TOKEN_MFA")
    env.respond("SOFTWARE_TOKEN_MFA", "SESS-0-0123456789abcdef", totp_extra(), next_challenge="SMS_MFA", next_session="SESS-1-0123456789abcdef")
    token, _ = env.flow.start(EMAIL, PASSWORD)
    with pytest.raises(cognito.AuthFailedError):
        env.flow.submit_totp(token, TOTP_CODE)
    assert len(env.store) == 0


def test_third_totp_failure_ends_flow(env):
    env.initiate("SOFTWARE_TOKEN_MFA")
    for _ in range(3):
        env.respond_error("SOFTWARE_TOKEN_MFA", "SESS-0-0123456789abcdef", totp_extra("000000"))
    token, _ = env.flow.start(EMAIL, PASSWORD)
    for _ in range(2):
        with pytest.raises(cognito.AuthCodeInvalidError):
            env.flow.submit_totp(token, "000000")
    with pytest.raises(cognito.AuthFlowExpiredError):
        env.flow.submit_totp(token, "000000")
    env.done()
    with pytest.raises(cognito.AuthFlowExpiredError):
        env.flow.submit_totp(token, TOTP_CODE)


def test_wrong_totp_then_correct_still_works_within_cap(env):
    env.initiate("SOFTWARE_TOKEN_MFA")
    env.respond_error("SOFTWARE_TOKEN_MFA", "SESS-0-0123456789abcdef", totp_extra("000000"))
    env.respond("SOFTWARE_TOKEN_MFA", "SESS-0-0123456789abcdef", totp_extra(), tokens=True)
    env.get_user(True)
    env.revoke()
    token, _ = env.flow.start(EMAIL, PASSWORD)
    with pytest.raises(cognito.AuthCodeInvalidError):
        env.flow.submit_totp(token, "000000")
    assert env.flow.submit_totp(token, TOTP_CODE).step is LoginStep.COMPLETED
    env.done()


def test_verify_software_token_non_success_rejected(env):
    queue_invited_until_setup(env)
    env.stubber.add_response(
        "verify_software_token",
        {"Status": "ERROR", "Session": "SESS-3-0123456789abcdef"},
        {"Session": "SESS-2-0123456789abcdef", "UserCode": TOTP_CODE, "FriendlyDeviceName": "Vigilantis"},
    )
    token, _ = env.flow.start(EMAIL, PASSWORD)
    env.flow.submit_new_password(token, NEW_PASSWORD)
    env.flow.begin_totp_setup(token)
    with pytest.raises(cognito.AuthFailedError):
        env.flow.verify_totp_setup(token, TOTP_CODE)
    env.done()
    assert len(env.store) == 0


def test_setup_wrong_code_counts_then_cognito_rejects_session_ends_flow(env):
    queue_invited_until_setup(env)
    params = {"Session": "SESS-2-0123456789abcdef", "UserCode": "000000", "FriendlyDeviceName": "Vigilantis"}
    env.stubber.add_client_error(
        "verify_software_token",
        service_error_code="EnableSoftwareTokenMFAException",
        service_message="Code mismatch",
        http_status_code=400,
        expected_params=params,
    )
    env.stubber.add_client_error(
        "verify_software_token",
        service_error_code="NotAuthorizedException",
        service_message="Invalid session for the user, session is expired.",
        http_status_code=400,
        expected_params=params,
    )
    token, _ = env.flow.start(EMAIL, PASSWORD)
    env.flow.submit_new_password(token, NEW_PASSWORD)
    env.flow.begin_totp_setup(token)
    with pytest.raises(cognito.AuthCodeInvalidError):
        env.flow.verify_totp_setup(token, "000000")
    with pytest.raises(cognito.AuthFlowExpiredError):
        env.flow.verify_totp_setup(token, "000000")
    assert len(env.store) == 0
    env.done()


def test_password_failure_vs_reused_session(env):
    env.stubber.add_client_error(
        "admin_initiate_auth",
        service_error_code="NotAuthorizedException",
        service_message="Incorrect username or password.",
        http_status_code=400,
    )
    with pytest.raises(cognito.AuthFailedError):
        env.flow.start(EMAIL, PASSWORD)
    env.initiate("SOFTWARE_TOKEN_MFA")
    env.respond_error("SOFTWARE_TOKEN_MFA", "SESS-0-0123456789abcdef", totp_extra(), code="NotAuthorizedException")
    token, _ = env.flow.start(EMAIL, PASSWORD)
    with pytest.raises(cognito.AuthFlowExpiredError):
        env.flow.submit_totp(token, TOTP_CODE)
    assert len(env.store) == 0


def test_email_not_verified_until_code_accepted(env):
    env.initiate("SOFTWARE_TOKEN_MFA")
    env.respond("SOFTWARE_TOKEN_MFA", "SESS-0-0123456789abcdef", totp_extra(), tokens=True)
    env.get_user(False)
    env.send_email()
    env.verify_email(ok=False)
    env.verify_email(ok=True)
    env.revoke()
    token, _ = env.flow.start(EMAIL, PASSWORD)
    result = env.flow.submit_totp(token, TOTP_CODE)
    assert result == (LoginStep.EMAIL_VERIFICATION_REQUIRED, None)
    with pytest.raises(cognito.AuthStepConflictError):
        env.flow.submit_totp(token, TOTP_CODE)
    with pytest.raises(cognito.AuthCodeInvalidError):
        env.flow.submit_email_code(token, EMAIL_CODE)
    final = env.flow.submit_email_code(token, EMAIL_CODE)
    assert final.step is LoginStep.COMPLETED and final.identity is not None
    env.done()


def test_email_code_failures_capped_and_tokens_revoked(env):
    env.initiate("SOFTWARE_TOKEN_MFA")
    env.respond("SOFTWARE_TOKEN_MFA", "SESS-0-0123456789abcdef", totp_extra(), tokens=True)
    env.get_user(False)
    env.send_email()
    for _ in range(3):
        env.verify_email(ok=False)
    env.revoke()
    token, _ = env.flow.start(EMAIL, PASSWORD)
    env.flow.submit_totp(token, TOTP_CODE)
    for _ in range(2):
        with pytest.raises(cognito.AuthCodeInvalidError):
            env.flow.submit_email_code(token, EMAIL_CODE)
    with pytest.raises(cognito.AuthFlowExpiredError):
        env.flow.submit_email_code(token, EMAIL_CODE)
    env.done()


def test_flow_expires_after_181_seconds(env):
    env.initiate("SOFTWARE_TOKEN_MFA")
    token, _ = env.flow.start(EMAIL, PASSWORD)
    env.clock.now += 181
    with pytest.raises(cognito.AuthFlowExpiredError):
        env.flow.submit_totp(token, TOTP_CODE)
    env.done()
    assert len(env.store) == 0


def test_abandoned_flow_with_tokens_is_revoked_on_expiry(env):
    env.initiate("SOFTWARE_TOKEN_MFA")
    env.respond("SOFTWARE_TOKEN_MFA", "SESS-0-0123456789abcdef", totp_extra(), tokens=True)
    env.get_user(False)
    env.send_email()
    env.revoke()
    token, _ = env.flow.start(EMAIL, PASSWORD)
    env.flow.submit_totp(token, TOTP_CODE)
    env.clock.now += 301
    with pytest.raises(cognito.AuthFlowExpiredError):
        env.flow.submit_email_code(token, EMAIL_CODE)
    env.done()


def test_reused_flow_token_after_completed(env):
    env.initiate("SOFTWARE_TOKEN_MFA")
    env.respond("SOFTWARE_TOKEN_MFA", "SESS-0-0123456789abcdef", totp_extra(), tokens=True)
    env.get_user(True)
    env.revoke()
    token, _ = env.flow.start(EMAIL, PASSWORD)
    env.flow.submit_totp(token, TOTP_CODE)
    with pytest.raises(cognito.AuthFlowExpiredError):
        env.flow.submit_totp(token, TOTP_CODE)
    with pytest.raises(cognito.AuthFlowExpiredError):
        env.flow.submit_email_code(token, EMAIL_CODE)


def test_unknown_token_expired(env):
    with pytest.raises(cognito.AuthFlowExpiredError):
        env.flow.submit_totp("nope", TOTP_CODE)


def test_step_mismatch_keeps_flow(env):
    env.initiate("SOFTWARE_TOKEN_MFA")
    env.respond("SOFTWARE_TOKEN_MFA", "SESS-0-0123456789abcdef", totp_extra(), tokens=True)
    env.get_user(True)
    env.revoke()
    token, _ = env.flow.start(EMAIL, PASSWORD)
    with pytest.raises(cognito.AuthStepConflictError):
        env.flow.submit_new_password(token, NEW_PASSWORD)
    with pytest.raises(cognito.AuthStepConflictError):
        env.flow.begin_totp_setup(token)
    with pytest.raises(cognito.AuthStepConflictError):
        env.flow.verify_totp_setup(token, TOTP_CODE)
    with pytest.raises(cognito.AuthStepConflictError):
        env.flow.submit_email_code(token, EMAIL_CODE)
    assert env.flow.submit_totp(token, TOTP_CODE).step is LoginStep.COMPLETED


def test_verify_setup_before_begin_is_conflict(env):
    queue_invited_until_setup(env)
    token, _ = env.flow.start(EMAIL, PASSWORD)
    env.flow.submit_new_password(token, NEW_PASSWORD)
    with pytest.raises(cognito.AuthStepConflictError):
        env.flow.verify_totp_setup(token, TOTP_CODE)


def test_password_policy_keeps_flow(env):
    env.initiate("NEW_PASSWORD_REQUIRED")
    cognito_rejected = "Weak-but-valid-1"  # 서버 정책은 통과하고 User Pool 정책이 거절하는 값
    env.respond_error("NEW_PASSWORD_REQUIRED", "SESS-0-0123456789abcdef", {"NEW_PASSWORD": cognito_rejected}, code="InvalidPasswordException")
    env.respond("NEW_PASSWORD_REQUIRED", "SESS-0-0123456789abcdef", {"NEW_PASSWORD": NEW_PASSWORD}, next_challenge="MFA_SETUP", next_session="SESS-1-0123456789abcdef")
    token, _ = env.flow.start(EMAIL, PASSWORD)
    with pytest.raises(cognito.AuthPasswordPolicyError):
        env.flow.submit_new_password(token, cognito_rejected)
    assert env.flow.submit_new_password(token, NEW_PASSWORD).step is LoginStep.TOTP_SETUP_REQUIRED


@pytest.mark.parametrize("bad", ["weak", "Short-1aA", "no-upper-case-1", "NO-LOWER-CASE-1", "No-digits-here-x", "NoSymbolsHere123", "Has space-Pass-1"])
def test_local_password_policy_rejects_before_any_cognito_call_and_keeps_flow(env, bad):
    env.initiate("NEW_PASSWORD_REQUIRED")
    env.respond("NEW_PASSWORD_REQUIRED", "SESS-0-0123456789abcdef", {"NEW_PASSWORD": NEW_PASSWORD}, next_challenge="MFA_SETUP", next_session="SESS-1-0123456789abcdef")
    token, _ = env.flow.start(EMAIL, PASSWORD)
    with pytest.raises(cognito.AuthPasswordPolicyError):
        env.flow.submit_new_password(token, bad)
    # 위반 입력은 응답을 소비하지 않았다 — 같은 흐름이 정상 값으로 이어진다
    assert env.flow.submit_new_password(token, NEW_PASSWORD).step is LoginStep.TOTP_SETUP_REQUIRED


UID2 = "99999999-8888-7777-6666-555555555555"


def run_to_setup_verify(env):
    queue_invited_until_setup(env)
    token, _ = env.flow.start(EMAIL, PASSWORD)
    env.flow.submit_new_password(token, NEW_PASSWORD)
    env.flow.begin_totp_setup(token)
    return token


def queue_setup_success(env):
    env.stubber.add_response(
        "verify_software_token",
        {"Status": "SUCCESS", "Session": "SESS-3-0123456789abcdef"},
        {"Session": "SESS-2-0123456789abcdef", "UserCode": TOTP_CODE, "FriendlyDeviceName": "Vigilantis"},
    )
    env.respond("MFA_SETUP", "SESS-3-0123456789abcdef", tokens=True)


def test_first_enrollment_requires_email_code_even_if_verified_and_sets_preference(env):
    queue_invited_until_setup(env)
    queue_setup_success(env)
    env.mfa_pref()
    env.get_user(True)
    env.send_email()
    env.verify_email()
    env.revoke()
    token, _ = env.flow.start(EMAIL, PASSWORD)
    env.flow.submit_new_password(token, NEW_PASSWORD)
    env.flow.begin_totp_setup(token)
    assert env.flow.verify_totp_setup(token, TOTP_CODE) == (LoginStep.EMAIL_VERIFICATION_REQUIRED, None)
    assert env.flow.submit_email_code(token, EMAIL_CODE).step is LoginStep.COMPLETED
    env.done()


def test_first_enrollment_email_send_failure_fails_closed_and_revokes(env):
    queue_invited_until_setup(env)
    queue_setup_success(env)
    env.mfa_pref()
    env.get_user(True)
    env.stubber.add_client_error(
        "get_user_attribute_verification_code",
        service_error_code="CodeDeliveryFailureException",
        service_message="m",
        http_status_code=400,
        expected_params={"AccessToken": ACCESS, "AttributeName": "email"},
    )
    env.revoke()
    token, _ = env.flow.start(EMAIL, PASSWORD)
    env.flow.submit_new_password(token, NEW_PASSWORD)
    env.flow.begin_totp_setup(token)
    with pytest.raises(cognito.AuthEmailDeliveryFailedError):
        env.flow.verify_totp_setup(token, TOTP_CODE)
    assert len(env.store) == 0
    env.done()


def test_mfa_preference_failure_ends_flow_and_revokes(env):
    queue_invited_until_setup(env)
    queue_setup_success(env)
    env.stubber.add_client_error(
        "set_user_mfa_preference",
        service_error_code="InternalErrorException",
        service_message="m",
        http_status_code=500,
        expected_params={"AccessToken": ACCESS, "SoftwareTokenMfaSettings": {"Enabled": True, "PreferredMfa": True}},
    )
    env.revoke()
    token, _ = env.flow.start(EMAIL, PASSWORD)
    env.flow.submit_new_password(token, NEW_PASSWORD)
    env.flow.begin_totp_setup(token)
    with pytest.raises(cognito.AuthProviderUnavailableError):
        env.flow.verify_totp_setup(token, TOTP_CODE)
    assert len(env.store) == 0
    env.done()


def test_flow_expiring_between_sweep_and_take_still_revokes(env, monkeypatch):
    env.initiate("SOFTWARE_TOKEN_MFA")
    env.respond("SOFTWARE_TOKEN_MFA", "SESS-0-0123456789abcdef", totp_extra(), tokens=True)
    env.get_user(False)
    env.send_email()
    env.revoke()
    token, _ = env.flow.start(EMAIL, PASSWORD)
    env.flow.submit_totp(token, TOTP_CODE)
    # 정리(sweep)는 아직 만료 전에 끝났고, 곧바로 take 시점에 만료된 상황
    monkeypatch.setattr(env.flow, "_sweep", lambda: setattr(env.clock, "now", env.clock.now + 301))
    with pytest.raises(cognito.AuthFlowExpiredError):
        env.flow.submit_email_code(token, EMAIL_CODE)
    assert len(env.store) == 0
    env.done()


def test_totp_setup_repr_hides_secret():
    setup = login_flow.TotpSetup(TOTP_SECRET, f"otpauth://totp/V:u?secret={TOTP_SECRET}")
    assert TOTP_SECRET not in repr(setup) and TOTP_SECRET not in str(setup)


def fail_totp(env, token, n):
    for _ in range(n):
        env.respond_error("SOFTWARE_TOKEN_MFA", "SESS-0-0123456789abcdef", totp_extra("000000"))
    for _ in range(n):
        with pytest.raises((cognito.AuthCodeInvalidError, cognito.AuthFlowExpiredError)):
            env.flow.submit_totp(token, "000000")


def test_user_wide_code_failure_limit_across_flows(caplog):
    env = Env(code_failure_limit=10)
    try:
        # 흐름 3개에서 3회씩(각 흐름은 상한으로 종료) + 1회 = 10회
        for _ in range(3):
            env.initiate("SOFTWARE_TOKEN_MFA")
            token, _ = env.flow.start(EMAIL, PASSWORD)
            fail_totp(env, token, 3)
        env.initiate("SOFTWARE_TOKEN_MFA")
        token, _ = env.flow.start(EMAIL, PASSWORD)
        fail_totp(env, token, 1)
        env.done()
        # 한도 도달: 제출은 Cognito 호출 없이 거절되고 흐름이 끝난다
        with caplog.at_level(logging.DEBUG):
            with pytest.raises(cognito.AuthRateLimitedError):
                env.flow.submit_totp(token, TOTP_CODE)
            assert len(env.store) == 0
            # start 는 AdminInitiateAuth 로 사용자를 식별한 뒤 흐름 저장 전에 거절(AdminGetUser 전)
            env.initiate("SOFTWARE_TOKEN_MFA", bind=False)
            with pytest.raises(cognito.AuthRateLimitedError):
                env.flow.start(EMAIL, PASSWORD)
        assert len(env.store) == 0
        env.done()
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING and "limit" in r.getMessage()]
        assert len(warnings) == 2
        assert hashlib.sha256(UID.encode()).hexdigest()[:12] in caplog.text
        assert UID not in caplog.text and EMAIL not in caplog.text
        # 다른 사용자는 영향 없음
        env.initiate("SOFTWARE_TOKEN_MFA", params={"USER_ID_FOR_SRP": UID2})
        assert env.flow.start(EMAIL, PASSWORD)[1] is LoginStep.TOTP_REQUIRED
        # 창이 지나면 초기화
        env.clock.now += 901
        env.initiate("SOFTWARE_TOKEN_MFA")
        assert env.flow.start(EMAIL, PASSWORD)[1] is LoginStep.TOTP_REQUIRED
        env.done()
    finally:
        env.stubber.deactivate()


def test_setup_verify_failures_count_toward_user_limit():
    env = Env(code_failure_limit=2)
    try:
        queue_invited_until_setup(env)
        for _ in range(2):
            env.stubber.add_client_error(
                "verify_software_token",
                service_error_code="EnableSoftwareTokenMFAException",
                service_message="Code mismatch",
                http_status_code=400,
                expected_params={
                    "Session": "SESS-2-0123456789abcdef",
                    "UserCode": "000000",
                    "FriendlyDeviceName": "Vigilantis",
                },
            )
        token, _ = env.flow.start(EMAIL, PASSWORD)
        env.flow.submit_new_password(token, NEW_PASSWORD)
        env.flow.begin_totp_setup(token)
        for _ in range(2):
            with pytest.raises(cognito.AuthCodeInvalidError):
                env.flow.verify_totp_setup(token, "000000")
        with pytest.raises(cognito.AuthRateLimitedError):  # Cognito 호출 없이 거절
            env.flow.verify_totp_setup(token, TOTP_CODE)
        env.done()
    finally:
        env.stubber.deactivate()


def test_enrollment_email_proof_not_bypassed_by_restart_with_verified_cognito_email():
    env = Env(FakeGate(proof=False))
    try:
        # 1) 최초 등록 → 이메일 코드 단계에서 방치 (Cognito 는 이미 email_verified=true)
        queue_invited_until_setup(env)
        queue_setup_success(env)
        env.mfa_pref()
        env.get_user(True)
        env.send_email()
        token, _ = env.flow.start(EMAIL, PASSWORD)
        env.flow.submit_new_password(token, NEW_PASSWORD)
        env.flow.begin_totp_setup(token)
        assert env.flow.verify_totp_setup(token, TOTP_CODE).step is LoginStep.EMAIL_VERIFICATION_REQUIRED
        # 2) 방치된 흐름은 만료 정리에서 폐기, 재로그인 → SOFTWARE_TOKEN_MFA 성공
        env.clock.now += 400
        env.revoke()
        env.initiate("SOFTWARE_TOKEN_MFA")
        env.respond("SOFTWARE_TOKEN_MFA", "SESS-0-0123456789abcdef", totp_extra(), tokens=True)
        env.get_user(True)
        env.send_email()
        token2, _ = env.flow.start(EMAIL, PASSWORD)
        result = env.flow.submit_totp(token2, TOTP_CODE)
        assert result.step is LoginStep.EMAIL_VERIFICATION_REQUIRED
        assert result.identity is None
        assert env.gate.proofs == [login_flow.LoginTicket(ISSUER, SUB, 0)]
        env.done()
    finally:
        env.stubber.deactivate()


def test_verified_and_proof_recorded_completes_without_email_code(env):
    env.initiate("SOFTWARE_TOKEN_MFA")
    env.respond("SOFTWARE_TOKEN_MFA", "SESS-0-0123456789abcdef", totp_extra(), tokens=True)
    env.get_user(True)
    env.revoke()
    token, _ = env.flow.start(EMAIL, PASSWORD)
    result = env.flow.submit_totp(token, TOTP_CODE)
    assert result.step is LoginStep.COMPLETED
    assert result.identity.email_proven is False
    env.done()


def test_email_code_failures_count_toward_user_limit():
    env = Env(code_failure_limit=2)
    try:
        env.initiate("SOFTWARE_TOKEN_MFA")
        env.respond("SOFTWARE_TOKEN_MFA", "SESS-0-0123456789abcdef", totp_extra(), tokens=True)
        env.get_user(False)
        env.send_email()
        env.verify_email(ok=False)
        env.verify_email(ok=False)
        env.revoke()
        token, _ = env.flow.start(EMAIL, PASSWORD)
        env.flow.submit_totp(token, TOTP_CODE)
        for _ in range(2):
            with pytest.raises(cognito.AuthCodeInvalidError):
                env.flow.submit_email_code(token, EMAIL_CODE)
        with pytest.raises(cognito.AuthRateLimitedError):  # Cognito 호출 없이 거절, 흐름 종료 + refresh 폐기
            env.flow.submit_email_code(token, EMAIL_CODE)
        env.initiate("SOFTWARE_TOKEN_MFA", bind=False)
        with pytest.raises(cognito.AuthRateLimitedError):
            env.flow.start(EMAIL, PASSWORD)
        env.done()
    finally:
        env.stubber.deactivate()


# ---------------------------------------------------------------------------- #515 신원 결합 · 세대 · 등록 구간
def test_recovery_block_at_bind_stores_no_flow_and_reaches_no_totp_step():
    env = Env(FakeGate())
    try:
        env.gate.reject_bind = cognito.AuthFailedError()
        env.initiate("SOFTWARE_TOKEN_MFA")
        with pytest.raises(cognito.AuthFailedError):
            env.flow.start(EMAIL, PASSWORD)
        assert env.gate.binds == [(ISSUER, SUB)]  # AdminGetUser 의 sub 로 묶는다 — 이메일 · Username 이 아니다
        assert len(env.store) == 0
        env.done()
    finally:
        env.stubber.deactivate()


def test_admin_get_user_not_found_after_password_step_is_rejected(env):
    env.initiate("SOFTWARE_TOKEN_MFA", bind=False)
    env.stubber.add_client_error(
        "admin_get_user",
        service_error_code="UserNotFoundException",
        service_message="m",
        http_status_code=400,
        expected_params={"UserPoolId": POOL, "Username": UID},
    )
    with pytest.raises(cognito.AuthFailedError):
        env.flow.start(EMAIL, PASSWORD)
    assert env.gate.binds == [] and len(env.store) == 0
    env.done()


def test_totp_proof_for_another_sub_than_the_bound_one_is_rejected_and_revoked(env):
    env.initiate("SOFTWARE_TOKEN_MFA")
    env.respond("SOFTWARE_TOKEN_MFA", "SESS-0-0123456789abcdef", totp_extra(), tokens=True)
    env.stubber.add_response(
        "get_user",
        {"Username": UID, "UserAttributes": [{"Name": "sub", "Value": "sub-other"}, {"Name": "email", "Value": EMAIL}]},
        {"AccessToken": ACCESS},
    )
    env.revoke()
    token, _ = env.flow.start(EMAIL, PASSWORD)
    with pytest.raises(cognito.AuthFailedError):
        env.flow.submit_totp(token, TOTP_CODE)
    assert len(env.store) == 0
    env.done()


def test_reenrollment_ticket_refuses_an_existing_totp_challenge():
    env = Env(FakeGate(generation=1, setup_only=True))
    try:
        env.initiate("SOFTWARE_TOKEN_MFA")  # 시작에서 바로
        with pytest.raises(cognito.AuthFailedError):
            env.flow.start(EMAIL, PASSWORD)
        env.initiate("NEW_PASSWORD_REQUIRED")  # 새 비밀번호 단계 뒤에 와도
        env.respond(
            "NEW_PASSWORD_REQUIRED", "SESS-0-0123456789abcdef", {"NEW_PASSWORD": NEW_PASSWORD},
            next_challenge="SOFTWARE_TOKEN_MFA", next_session="SESS-1-0123456789abcdef",
        )
        token, step = env.flow.start(EMAIL, PASSWORD)
        assert step is LoginStep.NEW_PASSWORD_REQUIRED
        with pytest.raises(cognito.AuthFailedError):
            env.flow.submit_new_password(token, NEW_PASSWORD)
        assert len(env.store) == 0
        env.done()
    finally:
        env.stubber.deactivate()


def test_enrollment_gate_refusal_makes_no_provider_registration_call():
    env = Env(FakeGate())
    try:
        queue_invited_until_setup(env)
        token, _ = env.flow.start(EMAIL, PASSWORD)
        env.flow.submit_new_password(token, NEW_PASSWORD)
        env.flow.begin_totp_setup(token)  # 준비 조회 뒤 AssociateSoftwareToken — 등록 울타리는 만들지 않는다
        env.gate.reject_enrollment = cognito.AuthFailedError()
        with pytest.raises(cognito.AuthFailedError):  # VerifySoftwareToken 을 보내지 않는다(대기 응답 없음)
            env.flow.verify_totp_setup(token, TOTP_CODE)
        assert len(env.gate.setup_checks) == 1
        assert len(env.gate.enrollments) == 1 and env.gate.records == []
        assert len(env.store) == 0
        env.done()
    finally:
        env.stubber.deactivate()


@pytest.mark.parametrize("error", [cognito.AuthFailedError, cognito.AuthProviderUnavailableError])
def test_setup_check_refusal_makes_no_provider_association_call(error):
    env = Env(FakeGate())
    try:
        env.initiate("MFA_SETUP")
        token, _ = env.flow.start(EMAIL, PASSWORD)
        env.gate.reject_setup = error()
        with pytest.raises(error):  # Associate 응답을 넣지 않는다 — 호출되면 Stubber 가 실패시킨다
            env.flow.begin_totp_setup(token)
        assert len(env.gate.setup_checks) == 1
        assert env.gate.enrollments == [] and env.gate.records == []
        assert len(env.store) == 0
        env.done()
    finally:
        env.stubber.deactivate()


def test_setup_records_enrollment_once_after_verify_success_and_carries_the_generation():
    env = Env(FakeGate(generation=4, setup_only=True))
    try:
        queue_invited_until_setup(env)
        queue_setup_success(env)
        env.mfa_pref()
        env.get_user(True)
        env.send_email()
        env.verify_email()
        env.revoke()
        token, _ = env.flow.start(EMAIL, PASSWORD)
        env.flow.submit_new_password(token, NEW_PASSWORD)
        env.flow.begin_totp_setup(token)
        assert env.gate.records == []
        assert env.flow.verify_totp_setup(token, TOTP_CODE).step is LoginStep.EMAIL_VERIFICATION_REQUIRED
        ticket = login_flow.LoginTicket(ISSUER, SUB, 4, True)
        assert env.gate.records == [ticket]
        result = env.flow.submit_email_code(token, EMAIL_CODE)
        assert (result.identity.generation, result.identity.mfa_step) == (4, login_flow.MfaStep.MFA_SETUP)
        env.done()
    finally:
        env.stubber.deactivate()


def test_setup_verify_non_success_records_nothing(env):
    queue_invited_until_setup(env)
    env.stubber.add_response(
        "verify_software_token",
        {"Status": "ERROR", "Session": "SESS-3-0123456789abcdef"},
        {"Session": "SESS-2-0123456789abcdef", "UserCode": TOTP_CODE, "FriendlyDeviceName": "Vigilantis"},
    )
    token, _ = env.flow.start(EMAIL, PASSWORD)
    env.flow.submit_new_password(token, NEW_PASSWORD)
    env.flow.begin_totp_setup(token)
    with pytest.raises(cognito.AuthFailedError):
        env.flow.verify_totp_setup(token, TOTP_CODE)
    assert env.gate.records == []
    env.done()


def _setup_until_verify(env):
    token, _ = env.flow.start(EMAIL, PASSWORD)
    env.flow.submit_new_password(token, NEW_PASSWORD)
    env.flow.begin_totp_setup(token)
    return token


def test_setup_is_not_recorded_when_mfa_setup_fails_after_verify_success():
    # VerifySoftwareToken SUCCESS 만으로는 새 등록이 아니다 — MFA_SETUP 의 AuthenticationResult 가 있어야 한다
    env = Env(FakeGate(generation=4, setup_only=True))
    try:
        queue_invited_until_setup(env)
        env.stubber.add_response(
            "verify_software_token",
            {"Status": "SUCCESS", "Session": "SESS-3-0123456789abcdef"},
            {"Session": "SESS-2-0123456789abcdef", "UserCode": TOTP_CODE, "FriendlyDeviceName": "Vigilantis"},
        )
        env.respond_error("MFA_SETUP", "SESS-3-0123456789abcdef", {}, code="NotAuthorizedException")
        token = _setup_until_verify(env)
        with pytest.raises(cognito.AuthFlowExpiredError):
            env.flow.verify_totp_setup(token, TOTP_CODE)
        assert env.gate.records == []
        assert len(env.store) == 0
        env.done()
    finally:
        env.stubber.deactivate()


def test_setup_is_not_recorded_when_get_user_identity_differs():
    env = Env(FakeGate(generation=4, setup_only=True))
    try:
        queue_invited_until_setup(env)
        queue_setup_success(env)
        env.mfa_pref()
        env.stubber.add_response(
            "get_user",
            {"Username": UID, "UserAttributes": [{"Name": "sub", "Value": "sub-other"}]},
            {"AccessToken": ACCESS},
        )
        env.revoke()
        token = _setup_until_verify(env)
        with pytest.raises(cognito.AuthFailedError):
            env.flow.verify_totp_setup(token, TOTP_CODE)
        assert env.gate.records == []
        assert len(env.store) == 0
        env.done()
    finally:
        env.stubber.deactivate()


@pytest.mark.parametrize("error", [cognito.AuthFailedError, cognito.AuthProviderUnavailableError])
def test_setup_record_failure_revokes_the_issued_tokens_and_sends_no_email_code(error):
    env = Env(FakeGate(generation=4, setup_only=True))
    env.gate.reject_record = error()
    try:
        queue_invited_until_setup(env)
        queue_setup_success(env)
        env.mfa_pref()
        env.get_user(True)
        env.revoke()  # 이메일 코드 발송 응답은 없다 — 보내면 Stubber 가 실패시킨다
        token = _setup_until_verify(env)
        with pytest.raises(error):
            env.flow.verify_totp_setup(token, TOTP_CODE)
        assert env.gate.records == []
        assert len(env.store) == 0
        env.done()
    finally:
        env.stubber.deactivate()


# ---------------------------------------------------------------------------- 비밀값 노출 방지
def test_secrets_never_logged_or_in_errors(env, caplog):
    secrets_ = [
        PASSWORD, NEW_PASSWORD, TOTP_CODE, EMAIL_CODE, ACCESS, REFRESH, ID_TOKEN, CLIENT_SECRET,
        TOTP_SECRET, "SESS-0-0123456789abcdef", "SESS-1-0123456789abcdef", "SESS-2-0123456789abcdef", "SESS-3-0123456789abcdef",
    ]
    queue_invited_until_setup(env)
    env.stubber.add_client_error(
        "verify_software_token",
        service_error_code="EnableSoftwareTokenMFAException",
        service_message=f"Code mismatch {TOTP_CODE} {PASSWORD}",
        http_status_code=400,
        expected_params={"Session": "SESS-2-0123456789abcdef", "UserCode": TOTP_CODE, "FriendlyDeviceName": "Vigilantis"},
    )
    env.stubber.add_response(
        "verify_software_token",
        {"Status": "SUCCESS", "Session": "SESS-3-0123456789abcdef"},
        {"Session": "SESS-2-0123456789abcdef", "UserCode": TOTP_CODE, "FriendlyDeviceName": "Vigilantis"},
    )
    env.respond("MFA_SETUP", "SESS-3-0123456789abcdef", tokens=True)
    env.mfa_pref()
    env.get_user(False)
    env.send_email()
    env.verify_email(ok=False)
    env.verify_email(ok=True)
    env.revoke()
    errors: list[BaseException] = []
    with caplog.at_level(logging.DEBUG):
        token, _ = env.flow.start(EMAIL, PASSWORD)
        env.flow.submit_new_password(token, NEW_PASSWORD)
        env.flow.begin_totp_setup(token)
        for call in (
            lambda: env.flow.verify_totp_setup(token, TOTP_CODE),
            lambda: env.flow.verify_totp_setup(token, TOTP_CODE),
            lambda: env.flow.submit_email_code(token, EMAIL_CODE),
            lambda: env.flow.submit_email_code(token, EMAIL_CODE),
        ):
            try:
                call()
            except cognito.AuthError as exc:
                errors.append(exc)
    assert len(errors) == 2
    blob = caplog.text + " ".join(str(e) + repr(e) for e in errors)
    for s in secrets_:
        assert s not in blob, s
    env.done()
