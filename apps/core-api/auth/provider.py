"""Cognito 클라이언트 · LoginFlow 조립 (#492 PR 1).

엔드포인트를 항상 실 Cognito 주소로 못 박는다 — 전역 AWS_ENDPOINT_URL(LocalStack)이 인증 트래픽을
가로채지 못하게 한다. 자격증명은 COGNITO_AWS_PROFILE 이 있으면 그 프로파일, 없으면 AWS_PROFILE 을
무시한 프로파일 없는 기본 체인(배포: 태스크 역할).

TOTP 등록 변경(#515 — 로그인 흐름의 AssociateSoftwareToken · VerifySoftwareToken · MFA_SETUP 응답 · MFA 선호,
복구의 AdminDeleteSoftwareToken)은 SDK 재시도가 없는 클라이언트로 보낸다 — 재시도는 첫 시도의 적용 여부를 마지막
응답으로 가린다(첫 시도 시간 초과 뒤 재시도가 4xx 를 받는 등). build_adapter 가 조립하는 어댑터는 그 호출만 재시도 없는
클라이언트(mutation_client)로 보내고, 비밀번호 · SOFTWARE_TOKEN_MFA · 조회 · 폐기 등 일반 인증 재시도 정책은 그대로다.
복구 전용 어댑터(build_recovery_adapter)는 모든 호출이 재시도 없는 클라이언트다.
"""

from __future__ import annotations

from datetime import UTC, datetime

import boto3
import botocore.session
from botocore.config import Config
from config import AuthSettings
from db.session import get_engine

from .cognito import CognitoAdapter
from .login_flow import LoginFlow
from .totp_recovery import DbLoginGate

#: 일반 인증 호출 — 표준 모드, 재시도 최대 2회
_AUTH_RETRIES = {"max_attempts": 2, "mode": "standard"}
#: TOTP 등록 변경 · 복구 삭제 호출 — 첫 시도뿐(total_max_attempts 는 첫 요청을 포함한다)
_NO_RETRIES = {"total_max_attempts": 1, "mode": "standard"}


def cognito_endpoint(region: str) -> str:
    return f"https://cognito-idp.{region}.amazonaws.com"


def build_cognito_client(settings: AuthSettings):
    """호출 전에 settings.missing_cognito_keys() 가 비어 있어야 한다.

    COGNITO_AWS_PROFILE 이 있으면 그 프로파일만 쓴다. 비어 있으면 환경의 AWS_PROFILE 도 무시하고
    프로파일 없는 기본 체인(환경 키 · 컨테이너 태스크 역할)을 쓴다 — 운영 이미지의 AWS_PROFILE 은
    고객 계정 AssumeRole 용이라 인증 트래픽에 쓰이면 안 된다.
    """
    return _client(settings, _AUTH_RETRIES)


def _client(settings: AuthSettings, retries: dict):
    if settings.COGNITO_AWS_PROFILE:
        session = boto3.Session(profile_name=settings.COGNITO_AWS_PROFILE)
    else:
        # profile 변수의 환경변수 조회(AWS_PROFILE)를 끈다(값 없음 = 프로파일 미지정)
        core = botocore.session.Session(session_vars={"profile": (None, None, None, None)})
        session = boto3.Session(botocore_session=core)
    return session.client(
        "cognito-idp",
        region_name=settings.COGNITO_REGION,
        endpoint_url=cognito_endpoint(settings.COGNITO_REGION),
        config=Config(connect_timeout=3, read_timeout=10, retries=dict(retries)),
    )


def build_adapter(settings: AuthSettings, client=None) -> CognitoAdapter:
    """client 를 넘기지 않으면 일반 호출은 재시도 클라이언트, TOTP 등록 변경은 재시도 없는 클라이언트로 조립한다.
    client 를 넘기면 모든 호출이 그 클라이언트다(복구 전용 어댑터 · 테스트)."""
    return CognitoAdapter(
        client if client is not None else build_cognito_client(settings),
        mutation_client=None if client is not None else _client(settings, _NO_RETRIES),
        user_pool_id=settings.COGNITO_USER_POOL_ID,
        client_id=settings.COGNITO_CLIENT_ID,
        client_secret=settings.COGNITO_CLIENT_SECRET.get_secret_value(),
        region=settings.COGNITO_REGION,
    )


def build_recovery_adapter(settings: AuthSettings) -> CognitoAdapter:
    """TOTP 복구 전용 어댑터 — SDK 재시도 없음. 행위자 · 대상 판정을 마친 뒤에만 조립한다(지연 팩토리로 넘긴다)."""
    return build_adapter(settings, _client(settings, _NO_RETRIES))


def build_login_flow(settings: AuthSettings) -> LoginFlow:
    return LoginFlow(
        build_adapter(settings),
        max_failures=settings.AUTH_TOTP_MAX_ATTEMPTS,
        code_failure_limit=settings.AUTH_CODE_FAILURE_LIMIT,
        code_failure_window_seconds=settings.AUTH_CODE_FAILURE_WINDOW_SECONDS,
        gate=DbLoginGate(get_engine, clock=lambda: datetime.now(UTC)),
    )
