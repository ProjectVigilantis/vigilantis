"""Cognito 어댑터 — botocore Stubber 로 요청 파라미터 · 오류 분류 · 로그 위생을 검증한다 (#492 PR 1)."""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import sys
from pathlib import Path

import boto3
import pytest
from botocore.exceptions import EndpointConnectionError, ReadTimeoutError
from botocore.stub import Stubber

CORE_API = Path(__file__).resolve().parents[1]
PACKAGES = Path(__file__).resolve().parents[3] / "packages"
for p in (str(CORE_API), str(PACKAGES)):
    if p not in sys.path:
        sys.path.insert(0, p)

from auth import cognito
from schemas.api.errors import ErrorCode

POOL = "ap-northeast-2_TestPool"
CLIENT_ID = "client-id-123"
CLIENT_SECRET = "client-secret-value-0123456789"
UID = "11111111-2222-3333-4444-555555555555"


def make_client():
    return boto3.client(
        "cognito-idp",
        region_name="ap-northeast-2",
        aws_access_key_id="dummy",
        aws_secret_access_key="dummy",
    )


@pytest.fixture
def env():
    client = make_client()
    adapter = cognito.CognitoAdapter(
        client, user_pool_id=POOL, client_id=CLIENT_ID, client_secret=CLIENT_SECRET, region="ap-northeast-2"
    )
    with Stubber(client) as stubber:
        yield adapter, stubber
        stubber.assert_no_pending_responses()


def expected_hash(username: str) -> str:
    mac = hmac.new(CLIENT_SECRET.encode(), (username + CLIENT_ID).encode(), hashlib.sha256).digest()
    return base64.b64encode(mac).decode()


def test_issuer_and_secret_hash(env):
    adapter, _ = env
    assert adapter.issuer == f"https://cognito-idp.ap-northeast-2.amazonaws.com/{POOL}"
    assert adapter.secret_hash(UID) == expected_hash(UID)


def test_initiate_auth_params_and_challenge(env):
    adapter, stubber = env
    stubber.add_response(
        "admin_initiate_auth",
        {"ChallengeName": "SOFTWARE_TOKEN_MFA", "Session": "SESSION-1-0123456789abcdef", "ChallengeParameters": {"USER_ID_FOR_SRP": UID}},
        {
            "UserPoolId": POOL,
            "ClientId": CLIENT_ID,
            "AuthFlow": "ADMIN_USER_PASSWORD_AUTH",
            "AuthParameters": {"USERNAME": "a@b.com", "PASSWORD": "pw", "SECRET_HASH": expected_hash("a@b.com")},
        },
    )
    resp = adapter.initiate_auth("a@b.com", "pw")
    assert resp.challenge == "SOFTWARE_TOKEN_MFA"
    assert resp.session == "SESSION-1-0123456789abcdef"
    assert resp.params == {"USER_ID_FOR_SRP": UID}
    assert resp.tokens is None


def test_respond_uses_given_username_for_hash_and_parses_tokens(env):
    adapter, stubber = env
    stubber.add_response(
        "admin_respond_to_auth_challenge",
        {"AuthenticationResult": {"AccessToken": "AT", "RefreshToken": "RT", "ExpiresIn": 600}},
        {
            "UserPoolId": POOL,
            "ClientId": CLIENT_ID,
            "ChallengeName": "SOFTWARE_TOKEN_MFA",
            "Session": "SESSION-1-0123456789abcdef",
            "ChallengeResponses": {
                "SOFTWARE_TOKEN_MFA_CODE": "123456",
                "USERNAME": UID,
                "SECRET_HASH": expected_hash(UID),
            },
        },
    )
    resp = adapter.respond_to_challenge("SOFTWARE_TOKEN_MFA", "SESSION-1-0123456789abcdef", UID, {"SOFTWARE_TOKEN_MFA_CODE": "123456"})
    assert resp.tokens is not None
    assert (resp.tokens.access_token, resp.tokens.refresh_token, resp.tokens.expires_in) == ("AT", "RT", 600)
    assert "AT" not in repr(resp) and "RT" not in repr(resp)


def test_software_token_get_user_email_and_revoke_params(env):
    adapter, stubber = env
    stubber.add_response("associate_software_token", {"SecretCode": "ABCDEFGHIJKLMNOP", "Session": "SESSION-2-0123456789abcdef"}, {"Session": "SESSION-1-0123456789abcdef"})
    stubber.add_response(
        "verify_software_token",
        {"Status": "SUCCESS", "Session": "SESSION-3-0123456789abcdef"},
        {"Session": "SESSION-2-0123456789abcdef", "UserCode": "123456", "FriendlyDeviceName": "Vigilantis"},
    )
    stubber.add_response(
        "get_user",
        {"Username": UID, "UserAttributes": [{"Name": "sub", "Value": "SUB"}]},
        {"AccessToken": "AT"},
    )
    stubber.add_response(
        "set_user_mfa_preference",
        {},
        {"AccessToken": "AT", "SoftwareTokenMfaSettings": {"Enabled": True, "PreferredMfa": True}},
    )
    stubber.add_response(
        "get_user_attribute_verification_code", {}, {"AccessToken": "AT", "AttributeName": "email"}
    )
    stubber.add_response("verify_user_attribute", {}, {"AccessToken": "AT", "AttributeName": "email", "Code": "654321"})
    stubber.add_response(
        "revoke_token", {}, {"Token": "RT", "ClientId": CLIENT_ID, "ClientSecret": CLIENT_SECRET}
    )
    assert adapter.associate_software_token("SESSION-1-0123456789abcdef") == ("ABCDEFGHIJKLMNOP", "SESSION-2-0123456789abcdef")
    assert adapter.verify_software_token("SESSION-2-0123456789abcdef", "123456", "Vigilantis") == ("SUCCESS", "SESSION-3-0123456789abcdef")
    assert adapter.get_user("AT") == {"sub": "SUB"}
    adapter.prefer_software_token_mfa("AT")
    adapter.send_email_code("AT")
    adapter.verify_email_code("AT", "654321")
    adapter.revoke_refresh_token("RT")


INIT = ("admin_initiate_auth", lambda a: a.initiate_auth("a@b.com", "pw"))
RESPOND = ("admin_respond_to_auth_challenge", lambda a: a.respond_to_challenge("MFA_SETUP", "SESSION-0123456789abcdef", UID, {}))
VERIFY = ("verify_software_token", lambda a: a.verify_software_token("SESSION-0123456789abcdef", "123456", "V"))
EMAIL_SEND = ("get_user_attribute_verification_code", lambda a: a.send_email_code("AT"))
EMAIL_VERIFY = ("verify_user_attribute", lambda a: a.verify_email_code("AT", "654321"))
NEWPW = ("admin_respond_to_auth_challenge", lambda a: a.respond_to_challenge("NEW_PASSWORD_REQUIRED", "SESSION-0123456789abcdef", UID, {}))


@pytest.mark.parametrize(
    ("call", "aws_code", "status", "expected"),
    [
        (INIT, "NotAuthorizedException", 400, cognito.AuthFailedError),
        (INIT, "UserNotFoundException", 400, cognito.AuthFailedError),
        (INIT, "PasswordResetRequiredException", 400, cognito.AuthFailedError),
        (INIT, "UserNotConfirmedException", 400, cognito.AuthFailedError),
        (RESPOND, "NotAuthorizedException", 400, cognito.AuthFlowExpiredError),
        (VERIFY, "NotAuthorizedException", 400, cognito.AuthFlowExpiredError),
        (RESPOND, "CodeMismatchException", 400, cognito.AuthCodeInvalidError),
        (VERIFY, "EnableSoftwareTokenMFAException", 400, cognito.AuthCodeInvalidError),
        (EMAIL_VERIFY, "ExpiredCodeException", 400, cognito.AuthCodeInvalidError),
        (EMAIL_VERIFY, "CodeMismatchException", 400, cognito.AuthCodeInvalidError),
        (NEWPW, "InvalidPasswordException", 400, cognito.AuthPasswordPolicyError),
        (EMAIL_SEND, "CodeDeliveryFailureException", 400, cognito.AuthEmailDeliveryFailedError),
        (INIT, "TooManyRequestsException", 400, cognito.AuthRateLimitedError),
        (EMAIL_SEND, "LimitExceededException", 400, cognito.AuthRateLimitedError),
        (RESPOND, "TooManyFailedAttemptsException", 400, cognito.AuthRateLimitedError),
        (INIT, "InternalErrorException", 500, cognito.AuthProviderUnavailableError),
        (INIT, "NotAuthorizedException", 503, cognito.AuthProviderUnavailableError),
        (INIT, "SomethingNewException", 400, cognito.AuthProviderUnavailableError),
    ],
)
def test_error_classification(env, caplog, call, aws_code, status, expected):
    adapter, stubber = env
    op, invoke = call
    stubber.add_client_error(op, service_error_code=aws_code, service_message="echo PRIVATE-TEXT", http_status_code=status)
    with caplog.at_level(logging.DEBUG), pytest.raises(cognito.AuthError) as info:
        invoke(adapter)
    assert type(info.value) is expected
    assert str(info.value) == expected.code.value
    assert "PRIVATE-TEXT" not in caplog.text
    assert aws_code in caplog.text and op in caplog.text


def test_codes_map_one_to_one():
    assert {
        cognito.AuthFailedError.code,
        cognito.AuthCodeInvalidError.code,
        cognito.AuthFlowExpiredError.code,
        cognito.AuthStepConflictError.code,
        cognito.AuthPasswordPolicyError.code,
        cognito.AuthEmailDeliveryFailedError.code,
        cognito.AuthRateLimitedError.code,
        cognito.AuthProviderUnavailableError.code,
    } == {
        ErrorCode.AUTH_FAILED,
        ErrorCode.AUTH_CODE_INVALID,
        ErrorCode.AUTH_FLOW_EXPIRED,
        ErrorCode.AUTH_STEP_CONFLICT,
        ErrorCode.AUTH_PASSWORD_POLICY,
        ErrorCode.AUTH_EMAIL_DELIVERY_FAILED,
        ErrorCode.AUTH_RATE_LIMITED,
        ErrorCode.AUTH_PROVIDER_UNAVAILABLE,
    }


def test_connection_error_is_provider_unavailable(caplog):
    class Broken:
        def admin_initiate_auth(self, **_):
            raise EndpointConnectionError(endpoint_url="https://secret-host.example")

    adapter = cognito.CognitoAdapter(
        Broken(), user_pool_id=POOL, client_id=CLIENT_ID, client_secret=CLIENT_SECRET, region="ap-northeast-2"
    )
    with caplog.at_level(logging.DEBUG), pytest.raises(cognito.AuthProviderUnavailableError) as info:
        adapter.initiate_auth("a@b.com", "pw")
    assert "secret-host" not in caplog.text and "secret-host" not in str(info.value)
    assert info.value.__cause__ is None and info.value.__suppress_context__


# --- #492 PR 3: 관리 조회 · 초대 · 비밀번호 재설정 -------------------------------------------

EMAIL = "a@b.com"
NEWPW_OK = "Br4nd-new-Pass-phrase"
USER_ATTRS = [{"Name": "sub", "Value": "sub-1"}, {"Name": "email", "Value": EMAIL}]


def add_error(stubber, op, code, status=400):
    stubber.add_client_error(op, service_error_code=code, service_message="echo PRIVATE-TEXT", http_status_code=status)


def test_admin_get_user_params_and_parsing(env):
    adapter, stubber = env
    stubber.add_response(
        "admin_get_user",
        {"Username": UID, "UserAttributes": USER_ATTRS, "UserStatus": "CONFIRMED"},
        {"UserPoolId": POOL, "Username": EMAIL},
    )
    assert adapter.admin_get_user(EMAIL) == cognito.CognitoUser(UID, "sub-1", EMAIL, "CONFIRMED")


def test_admin_get_user_returns_none_only_for_user_not_found(env):
    adapter, stubber = env
    add_error(stubber, "admin_get_user", "UserNotFoundException")
    assert adapter.admin_get_user(EMAIL) is None


@pytest.mark.parametrize(
    "code,status,expected",
    [
        ("NotAuthorizedException", 400, cognito.AuthProviderUnavailableError),
        ("AccessDeniedException", 400, cognito.AuthProviderUnavailableError),
        ("InternalErrorException", 500, cognito.AuthProviderUnavailableError),
        ("UserNotFoundException", 503, cognito.AuthProviderUnavailableError),
        ("TooManyRequestsException", 400, cognito.AuthRateLimitedError),
    ],
)
def test_admin_get_user_failures_are_never_absence(env, code, status, expected):
    adapter, stubber = env
    add_error(stubber, "admin_get_user", code, status)
    with pytest.raises(expected):
        adapter.admin_get_user(EMAIL)


def test_admin_get_user_network_failure_is_unavailable():
    class Down:
        def admin_get_user(self, **_):
            raise EndpointConnectionError(endpoint_url="https://x")

    adapter = cognito.CognitoAdapter(
        Down(), user_pool_id=POOL, client_id=CLIENT_ID, client_secret=CLIENT_SECRET, region="ap-northeast-2"
    )
    with pytest.raises(cognito.AuthProviderUnavailableError):
        adapter.admin_get_user(EMAIL)


@pytest.mark.parametrize(
    "response",
    [
        {"Username": UID, "UserAttributes": [{"Name": "email", "Value": EMAIL}], "UserStatus": "CONFIRMED"},
        {"Username": UID, "UserAttributes": [{"Name": "sub", "Value": "s"}], "UserStatus": "CONFIRMED"},
        {"Username": UID, "UserAttributes": USER_ATTRS},
    ],
)
def test_admin_get_user_malformed_identity_is_unavailable(env, response):
    adapter, stubber = env
    stubber.add_response("admin_get_user", response, {"UserPoolId": POOL, "Username": EMAIL})
    with pytest.raises(cognito.AuthProviderUnavailableError):
        adapter.admin_get_user(EMAIL)


def test_admin_create_user_params_for_create_and_resend(env):
    adapter, stubber = env
    created = {"User": {"Username": UID, "Attributes": USER_ATTRS, "UserStatus": "FORCE_CHANGE_PASSWORD"}}
    stubber.add_response(
        "admin_create_user",
        created,
        {
            "UserPoolId": POOL,
            "Username": EMAIL,
            "DesiredDeliveryMediums": ["EMAIL"],
            "UserAttributes": [{"Name": "email", "Value": EMAIL}],
        },
    )
    stubber.add_response(
        "admin_create_user",
        created,
        {"UserPoolId": POOL, "Username": EMAIL, "DesiredDeliveryMediums": ["EMAIL"], "MessageAction": "RESEND"},
    )
    expected = cognito.CognitoUser(UID, "sub-1", EMAIL, "FORCE_CHANGE_PASSWORD")
    assert adapter.admin_create_user(EMAIL) == expected
    assert adapter.admin_create_user(EMAIL, resend=True) == expected


def test_admin_delete_software_token_params(env):
    adapter, stubber = env
    stubber.add_response(
        "admin_delete_software_token",
        {},
        {"UserPoolId": POOL, "Username": UID},
    )
    assert adapter.admin_delete_software_token(UID) is cognito.SoftwareTokenDeletion.DELETED


D = cognito.SoftwareTokenDeletion


@pytest.mark.parametrize(
    "code,status,expected",
    [
        # 등록 없음 · 리소스 없음 — 삭제 증명이 아니라 따로 둔다(#515)
        ("ResourceNotFoundException", 400, D.NOT_FOUND),
        # 그 밖의 4xx 는 제공자의 거절 — 삭제되지 않았다
        ("UserNotFoundException", 400, D.REJECTED),
        ("NotAuthorizedException", 400, D.REJECTED),
        ("OperationNotEnabledException", 400, D.REJECTED),
        ("InvalidParameterException", 400, D.REJECTED),
        ("UserNotConfirmedException", 400, D.REJECTED),
        ("TooManyRequestsException", 400, D.REJECTED),
        # 5xx 는 코드와 무관하게 적용 여부를 모른다
        ("InternalErrorException", 500, D.UNKNOWN),
        ("TooManyRequestsException", 503, D.UNKNOWN),
        ("ResourceNotFoundException", 500, D.UNKNOWN),
    ],
)
def test_admin_delete_software_token_errors_are_classified_never_deleted(
    env, caplog, code, status, expected
):
    adapter, stubber = env
    add_error(stubber, "admin_delete_software_token", code, status)
    with caplog.at_level(logging.DEBUG):
        outcome = adapter.admin_delete_software_token(UID)
    assert outcome is expected
    assert "PRIVATE-TEXT" not in caplog.text
    assert CLIENT_SECRET not in caplog.text
    assert code in caplog.text and "admin_delete_software_token" in caplog.text


@pytest.mark.parametrize("error_type", [EndpointConnectionError, ReadTimeoutError])
def test_admin_delete_software_token_transport_failure_is_unknown(caplog, error_type):
    class Down:
        def admin_delete_software_token(self, **_):
            raise error_type(endpoint_url="https://secret-host.invalid")

    adapter = cognito.CognitoAdapter(
        Down(),
        user_pool_id=POOL,
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        region="ap-northeast-2",
    )
    with caplog.at_level(logging.DEBUG):
        assert adapter.admin_delete_software_token(UID) is D.UNKNOWN
    assert "secret-host" not in caplog.text


@pytest.mark.parametrize(
    "code,expected",
    [
        ("UsernameExistsException", cognito.UserStateConflictError),
        ("UnsupportedUserStateException", cognito.UserStateConflictError),
        ("CodeDeliveryFailureException", cognito.AuthEmailDeliveryFailedError),
        ("TooManyRequestsException", cognito.AuthRateLimitedError),
        ("InvalidParameterException", cognito.AuthProviderUnavailableError),
        ("AccessDeniedException", cognito.AuthProviderUnavailableError),
    ],
)
def test_admin_create_user_error_mapping(env, code, expected):
    adapter, stubber = env
    add_error(stubber, "admin_create_user", code)
    with pytest.raises(expected):
        adapter.admin_create_user(EMAIL)


def test_password_reset_request_params_and_discarded_delivery_details(env):
    adapter, stubber = env
    stubber.add_response(
        "forgot_password",
        {"CodeDeliveryDetails": {"Destination": "a***@b***", "DeliveryMedium": "EMAIL", "AttributeName": "email"}},
        {"ClientId": CLIENT_ID, "Username": EMAIL, "SecretHash": expected_hash(EMAIL)},
    )
    assert adapter.request_password_reset(EMAIL) is None


@pytest.mark.parametrize(
    "code",
    ["UserNotFoundException", "NotAuthorizedException", "InvalidParameterException", "UserNotConfirmedException"],
)
def test_password_reset_request_normalizes_existence_and_eligibility(env, code):
    adapter, stubber = env
    add_error(stubber, "forgot_password", code)
    assert adapter.request_password_reset(EMAIL) is None


@pytest.mark.parametrize(
    "code,status,expected",
    [
        ("TooManyRequestsException", 400, cognito.AuthRateLimitedError),
        ("TooManyFailedAttemptsException", 400, cognito.AuthRateLimitedError),
        ("LimitExceededException", 400, cognito.AuthRateLimitedError),
        ("CodeDeliveryFailureException", 400, cognito.AuthEmailDeliveryFailedError),
        ("InvalidEmailRoleAccessPolicyException", 400, cognito.AuthEmailDeliveryFailedError),
        ("InvalidSmsRoleAccessPolicyException", 400, cognito.AuthProviderUnavailableError),
        ("ResourceNotFoundException", 400, cognito.AuthProviderUnavailableError),
        ("UserNotFoundException", 500, cognito.AuthProviderUnavailableError),
        ("InternalErrorException", 500, cognito.AuthProviderUnavailableError),
    ],
)
def test_password_reset_request_surfaces_non_normalized_failures(env, code, status, expected):
    adapter, stubber = env
    add_error(stubber, "forgot_password", code, status)
    with pytest.raises(expected):
        adapter.request_password_reset(EMAIL)


def test_password_reset_confirm_params(env):
    adapter, stubber = env
    stubber.add_response(
        "confirm_forgot_password",
        {},
        {
            "ClientId": CLIENT_ID,
            "Username": EMAIL,
            "ConfirmationCode": "123456",
            "Password": NEWPW_OK,
            "SecretHash": expected_hash(EMAIL),
        },
    )
    assert adapter.confirm_password_reset(EMAIL, "123456", NEWPW_OK) is None


@pytest.mark.parametrize(
    "code,status,expected",
    [
        ("CodeMismatchException", 400, cognito.AuthCodeInvalidError),
        ("ExpiredCodeException", 400, cognito.AuthCodeInvalidError),
        ("UserNotFoundException", 400, cognito.AuthCodeInvalidError),
        ("NotAuthorizedException", 400, cognito.AuthCodeInvalidError),
        ("InvalidPasswordException", 400, cognito.AuthPasswordPolicyError),
        ("TooManyRequestsException", 400, cognito.AuthRateLimitedError),
        ("TooManyFailedAttemptsException", 400, cognito.AuthRateLimitedError),
        ("LimitExceededException", 400, cognito.AuthRateLimitedError),
        ("InternalErrorException", 500, cognito.AuthProviderUnavailableError),
        ("CodeMismatchException", 503, cognito.AuthProviderUnavailableError),
        ("ResourceNotFoundException", 400, cognito.AuthProviderUnavailableError),
    ],
)
def test_password_reset_confirm_error_mapping(env, caplog, code, status, expected):
    adapter, stubber = env
    add_error(stubber, "confirm_forgot_password", code, status)
    with caplog.at_level(logging.DEBUG), pytest.raises(expected):
        adapter.confirm_password_reset(EMAIL, "123456", NEWPW_OK)
    logged = caplog.text
    for secret in ("PRIVATE-TEXT", NEWPW_OK, "123456", EMAIL, CLIENT_SECRET):
        assert secret not in logged
