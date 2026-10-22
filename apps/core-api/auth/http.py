# ==============================================================================
# [파일 설명]
# /api/v1/auth HTTP 경계 — 쿠키 · Origin 검사 · 보안 헤더 · 의존성 · 오류 매핑. (#492 PR 1)
#
#   - 세션 쿠키 __Host-vg_session, 로그인 진행 쿠키 __Host-vg_login. 둘 다 Secure · HttpOnly ·
#     SameSite=Lax · Path=/ 이고 Domain 이 없다(__Host- 접두 요건).
#   - 상태 변경(GET/HEAD/OPTIONS 이외) 요청은 Origin 헤더가 AUTH_ALLOWED_ORIGINS 와 정확히 같아야
#     하며, Cognito · 흐름 저장소 · DB 에 닿기 전에 거절한다.
#   - /api/v1/* 의 모든 응답(성공 · 오류)에 no-store 등 보안 헤더를 붙인다. 로그인 응답뿐 아니라
#     보호 API 의 관제 데이터도 브라우저 · 중간 캐시에 남지 않게 한다(#492 PR 2 에서 /api/v1/auth/* 에서 넓힘).
# ==============================================================================

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from datetime import UTC, datetime

from config import AuthSettings, get_auth_settings, get_settings
from exceptions import ApiError, api_error_response
from fastapi import Depends, FastAPI, Request, Response
from schemas.api.errors import ErrorCode

from .cognito import AuthError, CognitoAdapter
from .login_flow import LoginFlow
from .provider import build_adapter, build_login_flow

logger = logging.getLogger("vigilantis.auth")

API_PREFIX = "/api/v1"
SESSION_COOKIE = "__Host-vg_session"
FLOW_COOKIE = "__Host-vg_login"
FLOW_COOKIE_MAX_AGE = 300

_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "strict-origin",
    "X-Content-Type-Options": "nosniff",
}
# 로그인 진행이 끝난 오류 — 진행 쿠키를 지운다(LoginFlow 가 흐름을 지우는 오류 + 이미 소비된 완료 시점 거절).
# FE 는 비밀번호 단계부터 다시 시작한다
_LOGIN_PATH = "/api/v1/auth/login"
_FLOW_ENDED_CODES = frozenset(
    {
        ErrorCode.AUTH_FLOW_EXPIRED,
        ErrorCode.ACCOUNT_NOT_PROVISIONED,
        ErrorCode.ACCOUNT_DISABLED,
        ErrorCode.AUTH_RATE_LIMITED,
        ErrorCode.AUTH_PROVIDER_UNAVAILABLE,
        ErrorCode.AUTH_EMAIL_DELIVERY_FAILED,
    }
)

_flow_lock = threading.Lock()


# --- 쿠키 ----------------------------------------------------------------------


def set_session_cookie(response: Response, token: str, max_age: int) -> None:
    response.set_cookie(
        SESSION_COOKIE, token, max_age=max_age, path="/", secure=True, httponly=True, samesite="lax"
    )


def set_flow_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        FLOW_COOKIE,
        token,
        max_age=FLOW_COOKIE_MAX_AGE,
        path="/",
        secure=True,
        httponly=True,
        samesite="lax",
    )


def delete_session_cookie(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/", secure=True, httponly=True, samesite="lax")


def delete_flow_cookie(response: Response) -> None:
    response.delete_cookie(FLOW_COOKIE, path="/", secure=True, httponly=True, samesite="lax")


# --- 의존성 --------------------------------------------------------------------


def get_clock() -> Callable[[], datetime]:
    return lambda: datetime.now(UTC)


def allowed_origins(settings: AuthSettings) -> list[str]:
    return settings.allowed_origins(get_settings().cors_allow_origins_list())


_SETTINGS = Depends(get_auth_settings)


def require_origin(request: Request, settings: AuthSettings = _SETTINGS) -> None:
    if request.method in _SAFE_METHODS:
        return
    origin = request.headers.get("origin")
    if origin is None or origin not in allowed_origins(settings):
        raise ApiError(ErrorCode.AUTH_ORIGIN_REJECTED)


def get_login_flow(request: Request, settings: AuthSettings = _SETTINGS) -> LoginFlow:
    """앱당 하나의 LoginFlow(+FlowStore). Cognito 설정이 비면 503 — 첫 사용 때 조립한다."""
    flow = getattr(request.app.state, "login_flow", None)
    if flow is not None:
        return flow
    if settings.missing_cognito_keys():
        raise ApiError(ErrorCode.AUTH_PROVIDER_UNAVAILABLE)
    with _flow_lock:
        flow = getattr(request.app.state, "login_flow", None)
        if flow is None:
            flow = build_login_flow(settings)
            request.app.state.login_flow = flow
    return flow


_adapter_lock = threading.Lock()


def get_cognito_adapter(request: Request, settings: AuthSettings = _SETTINGS) -> CognitoAdapter:
    """앱당 하나의 CognitoAdapter(관리 · 재설정 라우트용). 설정이 비면 503 — 첫 사용 때 조립한다."""
    adapter = getattr(request.app.state, "cognito_adapter", None)
    if adapter is not None:
        return adapter
    if settings.missing_cognito_keys():
        raise ApiError(ErrorCode.AUTH_PROVIDER_UNAVAILABLE)
    with _adapter_lock:
        adapter = getattr(request.app.state, "cognito_adapter", None)
        if adapter is None:
            adapter = build_adapter(settings)
            request.app.state.cognito_adapter = adapter
    return adapter


# --- 앱 등록 -------------------------------------------------------------------


def log_provider_status(settings: AuthSettings) -> None:
    missing = settings.missing_cognito_keys()
    if missing:
        logger.warning(
            "auth_provider_unconfigured: Cognito 가 필요한 로그인 · 비밀번호 재설정 · 사용자 초대/재발송 라우트는 503"
            " (/me · activity · logout 은 영향 없음) — 누락 키: %s",
            ", ".join(missing),
        )


def register_auth_http(app: FastAPI) -> None:
    """/api/v1/* 보안 헤더 미들웨어 + AuthError → 봉투 매핑. CORS 보다 안쪽에 두려면 그 앞에서 부른다."""

    @app.exception_handler(AuthError)
    async def handle_auth_error(request: Request, exc: AuthError):
        response = api_error_response(request, exc.code)
        # 진행 쿠키는 로그인 경로의 오류에서만 지운다 — 재설정 · 관리자 경로 오류가 다른 탭의 로그인 진행을 끊지 않는다
        path = request.url.path
        is_login = path == _LOGIN_PATH or path.startswith(_LOGIN_PATH + "/")
        if is_login and exc.code in _FLOW_ENDED_CODES:
            delete_flow_cookie(response)
        return response

    @app.middleware("http")
    async def api_security_headers(request: Request, call_next):
        response = await call_next(request)
        path = request.url.path
        if path == API_PREFIX or path.startswith(API_PREFIX + "/"):
            for name, value in _SECURITY_HEADERS.items():
                response.headers[name] = value
        return response
