"""Cognito 클라이언트 · LoginFlow 조립 (#492 PR 1).

엔드포인트를 항상 실 Cognito 주소로 못 박는다 — 전역 AWS_ENDPOINT_URL(LocalStack)이 인증 트래픽을
가로채지 못하게 한다. 자격증명은 COGNITO_AWS_PROFILE 이 있으면 그 프로파일, 없으면 AWS_PROFILE 을
무시한 프로파일 없는 기본 체인(배포: 태스크 역할).
"""

from __future__ import annotations

import boto3
import botocore.session
from botocore.config import Config
from config import AuthSettings
from db.repositories import auth as auth_repo
from db.session import get_session_factory

from .cognito import CognitoAdapter
from .login_flow import LoginFlow


def cognito_endpoint(region: str) -> str:
    return f"https://cognito-idp.{region}.amazonaws.com"


def build_cognito_client(settings: AuthSettings):
    """호출 전에 settings.missing_cognito_keys() 가 비어 있어야 한다.

    COGNITO_AWS_PROFILE 이 있으면 그 프로파일만 쓴다. 비어 있으면 환경의 AWS_PROFILE 도 무시하고
    프로파일 없는 기본 체인(환경 키 · 컨테이너 태스크 역할)을 쓴다 — 운영 이미지의 AWS_PROFILE 은
    고객 계정 AssumeRole 용이라 인증 트래픽에 쓰이면 안 된다.
    """
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
        config=Config(
            connect_timeout=3, read_timeout=10, retries={"max_attempts": 2, "mode": "standard"}
        ),
    )


def build_adapter(settings: AuthSettings, client=None) -> CognitoAdapter:
    return CognitoAdapter(
        client if client is not None else build_cognito_client(settings),
        user_pool_id=settings.COGNITO_USER_POOL_ID,
        client_id=settings.COGNITO_CLIENT_ID,
        client_secret=settings.COGNITO_CLIENT_SECRET.get_secret_value(),
        region=settings.COGNITO_REGION,
    )


def email_proof_recorded(issuer: str, sub: str) -> bool:
    """앱 DB 에 이메일 코드 검증 기록이 있는지. 짧은 세션 하나만 열고 닫는다(Cognito 호출 중 트랜잭션 없음)."""
    with get_session_factory()() as db:
        user = auth_repo.get_user_by_identity(db, issuer=issuer, cognito_sub=sub)
        return user is not None and user.email_verified_at is not None


def build_login_flow(settings: AuthSettings) -> LoginFlow:
    return LoginFlow(
        build_adapter(settings),
        max_failures=settings.AUTH_TOTP_MAX_ATTEMPTS,
        code_failure_limit=settings.AUTH_CODE_FAILURE_LIMIT,
        code_failure_window_seconds=settings.AUTH_CODE_FAILURE_WINDOW_SECONDS,
        email_proof_recorded=email_proof_recorded,
    )
