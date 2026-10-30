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
from enum import Enum
from typing import Any, Literal

from botocore.exceptions import BotoCoreError, ClientError
from schemas.api.errors import ErrorCode

logger = logging.getLogger(__name__)

#: 호출 종류 — NotAuthorizedException 의 의미가 호출마다 다르다.
#: password: 비밀번호 검증(AdminInitiateAuth) → AUTH_FAILED / session: 챌린지 세션을 쓰는 호출 · 토큰을 쓰는 호출 → AUTH_FLOW_EXPIRED
#: admin: 관리 조회(어떤 실패든 장애 · 제한만 429) / invite: 초대 생성 · 재발송 / recovery: 비밀번호 재설정 코드 요청 /
#: confirm: 재설정 확정(코드 · 사용자 불일치는 구분 없이 AUTH_CODE_INVALID)
CallKind = Literal["password", "session", "admin", "invite", "recovery", "confirm"]


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


class UserStateConflictError(AuthError):
    code = ErrorCode.USER_STATE_CONFLICT


_FAILED = {"UserNotFoundException", "PasswordResetRequiredException", "UserNotConfirmedException"}
_CODE_INVALID = {"CodeMismatchException", "EnableSoftwareTokenMFAException", "ExpiredCodeException"}
_RATE_LIMITED = {"TooManyRequestsException", "LimitExceededException", "TooManyFailedAttemptsException"}
_EMAIL_DELIVERY = {"CodeDeliveryFailureException", "InvalidEmailRoleAccessPolicyException"}
_CONFIRM_CODE_INVALID = {
    "CodeMismatchException",
    "ExpiredCodeException",
    "UserNotFoundException",
    "NotAuthorizedException",
}
#: 비밀번호 재설정 코드 요청에서 존재 · 자격 · 이메일 인증 상태를 숨기려고 같은 결과로 삼키는 오류
_RECOVERY_NORMALIZED = frozenset(
    {
        "UserNotFoundException",
        "NotAuthorizedException",
        "InvalidParameterException",
        "UserNotConfirmedException",
    }
)
_USER_NOT_FOUND = frozenset({"UserNotFoundException"})


class SoftwareTokenDeletion(str, Enum):
    """AdminDeleteSoftwareToken 한 번의 결과. 제공자 응답만 분류한다 — 복구 완료 판정이 아니다."""

    #: 2xx — 제공자가 삭제를 확인했다.
    DELETED = "DELETED"
    #: 4xx ResourceNotFoundException — 등록 없음 또는 리소스 없음. 이 호출이 삭제했다는 증명이 아니다.
    NOT_FOUND = "NOT_FOUND"
    #: 그 밖의 4xx(권한 · 제한 · 사용자 없음 · 파라미터) — 제공자가 거절했다. 삭제되지 않았다.
    REJECTED = "REJECTED"
    #: 5xx · 상태 코드 없음 · 전송 실패 · 시간 초과 — 적용 여부를 모른다.
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class CognitoUser:
    username: str
    sub: str
    email: str
    status: str


def classify_error(aws_code: str, status: int | None, kind: CallKind) -> AuthError:
    """AWS 오류 코드 → 도메인 예외. 5xx 는 코드와 무관하게 provider 장애다."""
    if status is not None and status >= 500:
        return AuthProviderUnavailableError()
    if kind == "admin":
        return AuthRateLimitedError() if aws_code in _RATE_LIMITED else AuthProviderUnavailableError()
    if kind == "invite":
        if aws_code in {"UsernameExistsException", "UnsupportedUserStateException"}:
            return UserStateConflictError()
        if aws_code in _EMAIL_DELIVERY:
            return AuthEmailDeliveryFailedError()
        return AuthRateLimitedError() if aws_code in _RATE_LIMITED else AuthProviderUnavailableError()
    if kind == "recovery":
        if aws_code in _EMAIL_DELIVERY:
            return AuthEmailDeliveryFailedError()
        return AuthRateLimitedError() if aws_code in _RATE_LIMITED else AuthProviderUnavailableError()
    if kind == "confirm":
        if aws_code in _CONFIRM_CODE_INVALID:
            return AuthCodeInvalidError()
        if aws_code == "InvalidPasswordException":
            return AuthPasswordPolicyError()
        return AuthRateLimitedError() if aws_code in _RATE_LIMITED else AuthProviderUnavailableError()
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
        mutation_client: Any = None,
    ) -> None:
        self._client = client
        #: TOTP 등록을 바꾸는 호출(AssociateSoftwareToken · VerifySoftwareToken · MFA_SETUP 응답 · MFA 선호 ·
        #: AdminDeleteSoftwareToken) 전용 — SDK 재시도가 없어야 한다(#515). 재시도하면 마지막 응답이 앞선 시도의
        #: 적용 여부를 가린다(첫 시도 시간 초과 · 재시도 4xx). None 이면 client 를 쓴다(테스트 대역 · 재시도 없는
        #: 복구 전용 클라이언트) — 운영 조립은 provider.build_adapter 가 재시도 없는 클라이언트를 넘긴다.
        self._mutation_client = client if mutation_client is None else mutation_client
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
        resp = self._send(operation, kind, frozenset(), params)
        assert resp is not None
        return resp

    def _mutate(self, operation: str, kind: CallKind, **params: Any) -> dict[str, Any]:
        """TOTP 등록 변경 — 재시도 없는 클라이언트로 한 번만 보낸다. 오류 분류는 _call 과 같다."""
        resp = self._send(operation, kind, frozenset(), params, self._mutation_client)
        assert resp is not None
        return resp

    def _send(
        self,
        operation: str,
        kind: CallKind,
        absorb: frozenset[str],
        params: dict[str, Any],
        client: Any = None,
    ) -> dict[str, Any] | None:
        """`absorb` 의 AWS 오류 코드(5xx 제외)는 예외 대신 None 으로 돌려준다 — 그 밖의 오류는 분류해 던진다."""
        try:
            return getattr(self._client if client is None else client, operation)(**params)
        except ClientError as exc:
            error = exc.response.get("Error", {})
            aws_code = str(error.get("Code", "Unknown"))
            status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        except BotoCoreError as exc:
            aws_code, status = type(exc).__name__, 503
        if aws_code in absorb and not (status is not None and status >= 500):
            return None
        logger.warning("cognito call failed operation=%s error_code=%s", operation, aws_code)
        raise classify_error(aws_code, status, kind) from None

    @staticmethod
    def _user(raw: dict[str, Any], attributes_key: str) -> CognitoUser:
        """AdminGetUser · AdminCreateUser 응답 → CognitoUser. 신원 필드가 비정상이면 장애로 본다."""
        attrs = {
            a.get("Name"): a.get("Value") for a in raw.get(attributes_key) or [] if isinstance(a, dict)
        }
        username, sub, email = raw.get("Username"), attrs.get("sub"), attrs.get("email")
        status = raw.get("UserStatus")
        if not all(isinstance(v, str) and v for v in (username, sub, email, status)):
            logger.error("cognito user response malformed")
            raise AuthProviderUnavailableError() from None
        return CognitoUser(username=username, sub=sub, email=email, status=status)

    def admin_get_user(self, username: str) -> CognitoUser | None:
        """없음이 증명된 경우(UserNotFoundException)만 None. 그 밖의 실패 · 비정상 응답은 예외다."""
        resp = self._send(
            "admin_get_user",
            "admin",
            _USER_NOT_FOUND,
            {"UserPoolId": self._pool_id, "Username": username},
        )
        return None if resp is None else self._user(resp, "UserAttributes")

    def admin_create_user(self, email: str, *, resend: bool = False) -> CognitoUser:
        """초대 메일을 Cognito 가 EMAIL 로 보낸다. 임시 비밀번호는 요청하지도 읽지도 않는다."""
        params: dict[str, Any] = {
            "UserPoolId": self._pool_id,
            "Username": email,
            "DesiredDeliveryMediums": ["EMAIL"],
        }
        if resend:
            params["MessageAction"] = "RESEND"
        else:
            params["UserAttributes"] = [{"Name": "email", "Value": email}]
        resp = self._call("admin_create_user", "invite", **params)
        return self._user(resp.get("User") or {}, "Attributes")

    def admin_delete_software_token(self, username: str) -> SoftwareTokenDeletion:
        """확인된 Cognito 신원의 TOTP를 삭제하고 결과를 분류해 돌려준다(예외로 바꾸지 않는다).

        앱 세션 회수 · 복구 완료 판정은 호출자(복구 상태를 가진 서비스) 책임이다. 삭제 성공은 DELETED
        하나뿐이고, ResourceNotFoundException 은 NOT_FOUND 로 따로 둔다(등록 없음 · 리소스 없음 — 삭제
        증명이 아니다). TOTP 등록 변경이라 재시도 없는 클라이언트(mutation_client)로 한 번만 보낸다.
        """
        operation = "admin_delete_software_token"
        try:
            self._mutation_client.admin_delete_software_token(
                UserPoolId=self._pool_id, Username=username
            )
        except ClientError as exc:
            aws_code = str(exc.response.get("Error", {}).get("Code", "Unknown"))
            status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            logger.warning("cognito call failed operation=%s error_code=%s", operation, aws_code)
            if status is None or status >= 500:
                return SoftwareTokenDeletion.UNKNOWN
            if aws_code == "ResourceNotFoundException":
                return SoftwareTokenDeletion.NOT_FOUND
            return SoftwareTokenDeletion.REJECTED
        except BotoCoreError as exc:
            logger.warning(
                "cognito call failed operation=%s error_code=%s", operation, type(exc).__name__
            )
            return SoftwareTokenDeletion.UNKNOWN
        return SoftwareTokenDeletion.DELETED

    def request_password_reset(self, email: str) -> None:
        """ForgotPassword. 존재 · 자격 · 이메일 미인증은 같은 결과(정상 반환)로 삼키고, CodeDeliveryDetails 는 버린다."""
        self._send(
            "forgot_password",
            "recovery",
            _RECOVERY_NORMALIZED,
            {"ClientId": self._client_id, "Username": email, "SecretHash": self.secret_hash(email)},
        )

    def confirm_password_reset(self, email: str, code: str, new_password: str) -> None:
        self._call(
            "confirm_forgot_password",
            "confirm",
            ClientId=self._client_id,
            Username=email,
            ConfirmationCode=code,
            Password=new_password,
            SecretHash=self.secret_hash(email),
        )

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
        """`username` 은 ChallengeParameters 의 USER_ID_FOR_SRP 여야 한다(SECRET_HASH 도 같은 값으로 계산).

        MFA_SETUP 응답은 TOTP 등록 변경이라 재시도 없이 보낸다. 그 밖의 챌린지(SOFTWARE_TOKEN_MFA 등)는 일반 재시도.
        """
        send = self._mutate if challenge == "MFA_SETUP" else self._call
        resp = send(
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
        resp = self._mutate("associate_software_token", "session", Session=session)
        return resp["SecretCode"], resp.get("Session")

    def verify_software_token(
        self, session: str, code: str, friendly_name: str
    ) -> tuple[str, str | None]:
        resp = self._mutate(
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
        self._mutate(
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
