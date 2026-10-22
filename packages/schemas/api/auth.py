# ==============================================================================
# [파일 설명]  담당: 안성일 (AI/Guardrail · Architect)
# /api/v1/auth — 로그인 · 로그아웃 · 현재 사용자(Post-MVP T-1, #492 PR 1)의 외부 DTO입니다.
# Dashboard(FE) 로그인 화면과의 공개 계약입니다.
#
# 로그인 방식: 서버가 Cognito Admin 인증 API(AdminInitiateAuth · AdminRespondToAuthChallenge)로
# 비밀번호 → (최초 비밀번호 변경) → (최초 TOTP 등록 | TOTP) → (이메일 검증) 단계를 진행하고,
# **이번 로그인에서 TOTP 단계 성공 응답을 직접 받은 뒤에만** 앱 세션 쿠키를 발급한다.
# 진행 중인 로그인은 서버가 로그인 진행 쿠키로 추적한다 — 브라우저는 단계 결과만 받는다.
#
# 계약 원칙
#   - 비밀번호 · TOTP 코드 · 이메일 코드는 SecretStr 로 받는다(repr · 로그에 값이 나가지 않는다).
#   - Cognito 토큰은 어떤 응답에도 싣지 않는다. 세션은 HttpOnly 쿠키로만 전달한다.
#   - 다음에 할 일은 `next_step` 하나로 알린다. COMPLETED 는 세션 쿠키가 발급됐다는 뜻이다.
#   - 실패는 공통 오류 봉투(errors.py)의 AUTH_* · ACCOUNT_* 코드로 돌려준다.
#     비밀번호 단계 실패는 사용자 존재 여부와 관계없이 AUTH_FAILED 하나다.
#
# 경로 (전부 /api/v1/auth, 상태 변경은 전부 POST + JSON + 허용 Origin)
#   POST /login                       LoginRequest        → LoginStepResponse
#   POST /login/new-password          NewPasswordRequest  → LoginStepResponse
#   POST /login/totp-setup                                → TotpSetupResponse (등록 비밀키 · otpauth URI)
#   POST /login/totp-setup/verify     CodeRequest         → LoginStepResponse
#   POST /login/totp                  CodeRequest         → LoginStepResponse
#   POST /login/email-verification    CodeRequest         → LoginStepResponse
#   POST /logout                                          → 204 (세션 회수 · 쿠키 삭제)
#   POST /activity                                        → 204 (사용자 입력이 있을 때만 — 유휴 연장)
#   GET  /me                                              → CurrentUserResponse (유휴를 연장하지 않는다)
# ==============================================================================

from __future__ import annotations

from enum import Enum
from typing import Annotated

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)

from .assets import UtcDateTime

#: Cognito 비밀번호 최대 길이(256)를 넘는 입력은 Cognito 에 보내지 않는다.
PASSWORD_MAX_LENGTH = 256
#: 새 비밀번호 최소 길이 — User Pool 비밀번호 정책(최소 12자)과 같은 값이다.
NEW_PASSWORD_MIN_LENGTH = 12

Email = Annotated[str, Field(min_length=3, max_length=254, pattern=r"^[^@\s]+@[^@\s]+$")]

#: 비밀값을 받는 요청 모델 공통 설정 — 검증 오류 메시지에 입력값을 싣지 않는다
#: (pydantic 기본값은 오류 문자열에 input_value 를 그대로 넣는다).
_SECRET_INPUT_CONFIG = ConfigDict(extra="forbid", hide_input_in_errors=True)


class LoginStep(str, Enum):
    """로그인의 다음 단계. 서버가 정하고, FE 는 이 값으로 화면을 고른다."""

    #: 초대 사용자의 첫 로그인 — 임시 비밀번호를 새 비밀번호로 바꾼다.
    NEW_PASSWORD_REQUIRED = "NEW_PASSWORD_REQUIRED"
    #: TOTP 미등록 — /login/totp-setup 으로 비밀키를 받아 등록하고 코드로 확인한다.
    TOTP_SETUP_REQUIRED = "TOTP_SETUP_REQUIRED"
    #: TOTP 등록 사용자 — 인증 앱의 코드를 낸다.
    TOTP_REQUIRED = "TOTP_REQUIRED"
    #: TOTP 는 성공했지만 이메일이 미검증이다 — 서버가 보낸 이메일 코드를 낸다.
    EMAIL_VERIFICATION_REQUIRED = "EMAIL_VERIFICATION_REQUIRED"
    #: 세션 쿠키가 발급됐다. 로그인 진행 쿠키는 지워졌다.
    COMPLETED = "COMPLETED"


class UserRole(str, Enum):
    """앱 역할 3종. 조치 실행(POST /api/v1/actions/execute) · 인시던트 종료
    (POST /api/v1/incidents/{id}/resolve)는 ADMIN · OPERATOR 만 한다(PR 2)."""

    #: 사용자 관리 · 조치 승인 · 조회. 사용자 관리 API 는 PR 3.
    ADMIN = "ADMIN"
    #: 조치 승인 · 조회.
    OPERATOR = "OPERATOR"
    #: 조회 전용 — 조치 실행 · 종료 요청은 서버가 403 AUTH_FORBIDDEN 으로 거절한다.
    VIEWER = "VIEWER"


class LoginRequest(BaseModel):
    """POST /api/v1/auth/login — 비밀번호 단계. 세션은 절대 발급하지 않는다."""

    model_config = _SECRET_INPUT_CONFIG

    email: Email
    password: SecretStr = Field(min_length=1, max_length=PASSWORD_MAX_LENGTH)


class NewPasswordRequest(BaseModel):
    """POST /api/v1/auth/login/new-password — 최초 비밀번호 변경.

    길이는 여기서 막는다. 서버가 공통 순수 함수로 문자 규칙을 먼저 확인하고,
    통과한 입력만 Cognito로 보낸다. 내용 위반은 AUTH_PASSWORD_POLICY다.
    """

    model_config = _SECRET_INPUT_CONFIG

    new_password: SecretStr = Field(min_length=NEW_PASSWORD_MIN_LENGTH, max_length=PASSWORD_MAX_LENGTH)


class CodeRequest(BaseModel):
    """TOTP 코드 · TOTP 등록 확인 코드 · 이메일 검증 코드 — 모두 6자리 숫자다."""

    model_config = _SECRET_INPUT_CONFIG

    code: SecretStr

    @field_validator("code")
    @classmethod
    def _six_digits(cls, value: SecretStr) -> SecretStr:
        raw = value.get_secret_value()
        if len(raw) != 6 or not raw.isascii() or not raw.isdigit():
            raise ValueError("코드는 6자리 숫자입니다")
        return value


class PasswordResetRequest(BaseModel):
    """POST /password-reset — 발송 대상·사용자 존재 여부는 응답하지 않는다."""

    model_config = _SECRET_INPUT_CONFIG

    email: Email


class PasswordResetConfirmRequest(NewPasswordRequest, CodeRequest):
    """POST /password-reset/confirm — 기존 새 비밀번호 길이·코드 계약을 공유한다."""

    email: Email


class LoginStepResponse(BaseModel):
    """로그인 단계 API 공통 응답 — 다음 단계 하나만 싣는다."""

    model_config = ConfigDict(extra="forbid")

    next_step: LoginStep


class TotpSetupResponse(BaseModel):
    """POST /api/v1/auth/login/totp-setup — TOTP 등록 비밀키.

    **이 응답은 비밀값을 싣는다.** 서버는 Cache-Control: no-store 로 보내고 로그 · 저장하지 않는다.
    FE 는 `otpauth_uri` 를 QR 로 보여 주고, 스캔이 안 될 때만 `secret` 을 직접 입력용으로 보여 준다.
    """

    model_config = ConfigDict(extra="forbid")

    secret: str = Field(min_length=16, max_length=128, pattern=r"^[A-Z2-7]+=*$")
    otpauth_uri: str = Field(pattern=r"^otpauth://totp/")

    @model_validator(mode="after")
    def _enforce_contract(self):
        if f"secret={self.secret}" not in self.otpauth_uri:
            raise ValueError("otpauth_uri 는 같은 secret 을 담아야 합니다")
        return self


class SessionInfo(BaseModel):
    """현재 세션의 만료 시각 2개 — 먼저 오는 쪽에서 서버가 만료한다."""

    model_config = ConfigDict(extra="forbid")

    #: 발급 시점 + 최대 수명(기본 8시간). 활동으로 늘어나지 않는다.
    absolute_expires_at: UtcDateTime
    #: 마지막 사용자 활동 + 유휴 한도(기본 30분). 최대 수명을 넘지 않는다.
    idle_expires_at: UtcDateTime

    @model_validator(mode="after")
    def _enforce_contract(self):
        if self.idle_expires_at > self.absolute_expires_at:
            raise ValueError("idle_expires_at 은 absolute_expires_at 을 넘을 수 없습니다")
        return self


class CurrentUserResponse(BaseModel):
    """GET /api/v1/auth/me — 로그인한 사용자와 세션 만료 시각."""

    model_config = ConfigDict(extra="forbid")

    user_id: str = Field(min_length=1)
    email: Email
    role: UserRole
    session: SessionInfo
