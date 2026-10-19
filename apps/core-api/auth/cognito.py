"""Cognito Admin 인증 API 어댑터 (Post-MVP T-1, #492 PR 1).

boto3 `cognito-idp` 클라이언트를 얇게 감싸고, Cognito 오류를 AUTH_* 오류 코드와 1:1 인 도메인 예외로
분류한다. HTTP 예외는 던지지 않는다(라우터가 매핑한다).

로그에는 AWS 오류 코드와 호출 이름만 남긴다 — 요청 파라미터 · 오류 메시지 · 토큰 · 비밀값은 싣지 않는다.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
from dataclasses import dataclass, field
from typing import Any, Literal

from botocore.exceptions import BotoCoreError, ClientError
from schemas.api.errors import ErrorCode

logger = logging.getLogger(__name__)

#: 호출 종류 — NotAuthorizedException 의 의미가 호출마다 다르다.
#: password: 비밀번호 검증(AdminInitiateAuth) → AUTH_FAILED / session: 챌린지 세션을 쓰는 호출 · 토큰을 쓰는 호출 → AUTH_FLOW_EXPIRED
CallKind = Literal["password", "session"]


class AuthError(Exception):
    """AUTH_* 오류 코드와 1:1 인 도메인 예외의 기반. 메시지는 코드 문자열 고정이다(입력 · 응답을 싣지 않는다)."""

    code: ErrorCode

    def __init__(self) -> None:
        super().__init__(self.code.value)


class AuthFailedError(AuthError):
    code = ErrorCode.AUTH_FAILED


class AuthCodeInvalidError(AuthError):
    code = ErrorCode.AUTH_CODE_INVALID


class AuthFlowExpiredError(AuthError):
    code = ErrorCode.AUTH_FLOW_EXPIRED


class AuthStepConflictError(AuthError):
    code = ErrorCode.AUTH_STEP_CONFLICT


class AuthPasswordPolicyError(AuthError):
    code = ErrorCode.AUTH_PASSWORD_POLICY


class AuthEmailDeliveryFailedError(AuthError):
    code = ErrorCode.AUTH_EMAIL_DELIVERY_FAILED


class AuthRateLimitedError(AuthError):
    code = ErrorCode.AUTH_RATE_LIMITED


class AuthProviderUnavailableError(AuthError):
    code = ErrorCode.AUTH_PROVIDER_UNAVAILABLE


_FAILED = {"UserNotFoundException", "PasswordResetRequiredException", "UserNotConfirmedException"}
_CODE_INVALID = {"CodeMismatchException", "EnableSoftwareTokenMFAException", "ExpiredCodeException"}
_RATE_LIMITED = {"TooManyRequestsException", "LimitExceededException", "TooManyFailedAttemptsException"}


def classify_error(aws_code: str, status: int | None, kind: CallKind) -> AuthError:
    """AWS 오류 코드 → 도메인 예외. 5xx 는 코드와 무관하게 provider 장애다."""
    if status is not None and status >= 500:
        return AuthProviderUnavailableError()
    if aws_code == "NotAuthorizedException":
        return AuthFailedError() if kind == "password" else AuthFlowExpiredError()
    if aws_code in _FAILED:
        return AuthFailedError()
    if aws_code in _CODE_INVALID:
        return AuthCodeInvalidError()
    if aws_code == "InvalidPasswordException":
        return AuthPasswordPolicyError()
    if aws_code == "CodeDeliveryFailureException":
        return AuthEmailDeliveryFailedError()
    if aws_code in _RATE_LIMITED:
        return AuthRateLimitedError()
    return AuthProviderUnavailableError()


@dataclass(frozen=True)
class Tokens:
    """Cognito 토큰. repr · 로그에 값이 나가지 않는다."""

    access_token: str = field(repr=False)
    refresh_token: str | None = field(repr=False)
    expires_in: int


@dataclass(frozen=True)
class CognitoResponse:
    """AdminInitiateAuth · AdminRespondToAuthChallenge 응답 — 토큰이거나 다음 챌린지다."""

    tokens: Tokens | None = field(default=None, repr=False)
    challenge: str | None = None
    session: str | None = field(default=None, repr=False)
    params: dict[str, str] = field(default_factory=dict, repr=False)


class CognitoAdapter:
    def __init__(
        self,
        client: Any,
        *,
        user_pool_id: str,
        client_id: str,
        client_secret: str,
        region: str,
    ) -> None:
        self._client = client
        self._pool_id = user_pool_id
        self._client_id = client_id
        self._client_secret = client_secret
        self.issuer = f"https://cognito-idp.{region}.amazonaws.com/{user_pool_id}"

    def secret_hash(self, username: str) -> str:
        digest = hmac.new(
            self._client_secret.encode(), (username + self._client_id).encode(), hashlib.sha256
        ).digest()
        return base64.b64encode(digest).decode()

    def _call(self, operation: str, kind: CallKind, **params: Any) -> dict[str, Any]:
        try:
            return getattr(self._client, operation)(**params)
        except ClientError as exc:
            error = exc.response.get("Error", {})
            aws_code = str(error.get("Code", "Unknown"))
            status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        except BotoCoreError as exc:
            aws_code, status = type(exc).__name__, 503
        logger.warning("cognito call failed operation=%s error_code=%s", operation, aws_code)
        raise classify_error(aws_code, status, kind) from None

    @staticmethod
    def _parse(resp: dict[str, Any]) -> CognitoResponse:
        result = resp.get("AuthenticationResult")
        if result:
            return CognitoResponse(
                tokens=Tokens(
                    access_token=result["AccessToken"],
                    refresh_token=result.get("RefreshToken"),
                    expires_in=int(result.get("ExpiresIn", 3600)),
                )
            )
        return CognitoResponse(
            challenge=resp.get("ChallengeName"),
            session=resp.get("Session"),
            params=dict(resp.get("ChallengeParameters") or {}),
        )

    def initiate_auth(self, email: str, password: str) -> CognitoResponse:
        resp = self._call(
            "admin_initiate_auth",
            "password",
            UserPoolId=self._pool_id,
            ClientId=self._client_id,
            AuthFlow="ADMIN_USER_PASSWORD_AUTH",
            AuthParameters={
                "USERNAME": email,
                "PASSWORD": password,
                "SECRET_HASH": self.secret_hash(email),
            },
        )
        return self._parse(resp)

    def respond_to_challenge(
        self, challenge: str, session: str, username: str, responses: dict[str, str]
    ) -> CognitoResponse:
        """`username` 은 ChallengeParameters 의 USER_ID_FOR_SRP 여야 한다(SECRET_HASH 도 같은 값으로 계산)."""
        resp = self._call(
            "admin_respond_to_auth_challenge",
            "session",
            UserPoolId=self._pool_id,
            ClientId=self._client_id,
            ChallengeName=challenge,
            Session=session,
            ChallengeResponses={
                **responses,
                "USERNAME": username,
                "SECRET_HASH": self.secret_hash(username),
            },
        )
        return self._parse(resp)

    def associate_software_token(self, session: str) -> tuple[str, str | None]:
        resp = self._call("associate_software_token", "session", Session=session)
        return resp["SecretCode"], resp.get("Session")

    def verify_software_token(
        self, session: str, code: str, friendly_name: str
    ) -> tuple[str, str | None]:
        resp = self._call(
            "verify_software_token",
            "session",
            Session=session,
            UserCode=code,
            FriendlyDeviceName=friendly_name,
        )
        return str(resp.get("Status", "")), resp.get("Session")

    def get_user(self, access_token: str) -> dict[str, str]:
        resp = self._call("get_user", "session", AccessToken=access_token)
        return {a["Name"]: a["Value"] for a in resp.get("UserAttributes", [])}

    def prefer_software_token_mfa(self, access_token: str) -> None:
        self._call(
            "set_user_mfa_preference",
            "session",
            AccessToken=access_token,
            SoftwareTokenMfaSettings={"Enabled": True, "PreferredMfa": True},
        )

    def send_email_code(self, access_token: str) -> None:
        self._call(
            "get_user_attribute_verification_code",
            "session",
            AccessToken=access_token,
            AttributeName="email",
        )

    def verify_email_code(self, access_token: str, code: str) -> None:
        self._call(
            "verify_user_attribute",
            "session",
            AccessToken=access_token,
            AttributeName="email",
            Code=code,
        )

    def revoke_refresh_token(self, refresh_token: str) -> None:
        self._call(
            "revoke_token",
            "session",
            Token=refresh_token,
            ClientId=self._client_id,
            ClientSecret=self._client_secret,
        )
