# ==============================================================================
# [파일 설명]
# /api/v1/auth — 로그인 · 로그아웃 · 현재 사용자 라우터. (#492 PR 1, 계약: schemas.api.auth)
#
#   - 얇게 둔다: 로그인 상태 머신은 LoginFlow, 세션·트랜잭션은 auth.service 가 맡는다.
#   - 세션 쿠키는 StepResult 가 COMPLETED + identity 일 때, service 가 세션을 발급한 뒤에만 건다.
#   - 비밀값(SecretStr)은 LoginFlow 호출 지점에서만 벗긴다. 토큰은 어떤 본문에도 싣지 않는다.
#   - 모든 경로에 Origin 검사(상태 변경만)가 먼저 걸리고, Cognito 라우트(로그인 · 재설정)만 설정 확인이 뒤따른다.
# ==============================================================================

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from auth import password_reset as password_reset_service
from auth import service
from auth.cognito import AuthFailedError, AuthFlowExpiredError, CognitoAdapter
from auth.http import (
    FLOW_COOKIE,
    SESSION_COOKIE,
    delete_flow_cookie,
    delete_session_cookie,
    get_clock,
    get_cognito_adapter,
    get_login_flow,
    require_origin,
    set_flow_cookie,
    set_session_cookie,
)
from auth.login_flow import LoginFlow, StepResult
from config import AuthSettings, get_auth_settings
from db.session import get_db
from fastapi import APIRouter, Depends, Request, Response
from schemas.api.auth import (
    CodeRequest,
    CurrentUserResponse,
    LoginRequest,
    LoginStep,
    LoginStepResponse,
    NewPasswordRequest,
    PasswordResetConfirmRequest,
    PasswordResetRequest,
    TotpSetupResponse,
)
from sqlalchemy.orm import Session

_DB = Depends(get_db)
_SETTINGS = Depends(get_auth_settings)
_CLOCK = Depends(get_clock)
_FLOW = Depends(get_login_flow)
_ADAPTER = Depends(get_cognito_adapter)

# Origin 검사는 라우터 전체에 먼저 걸린다. Cognito 설정 확인(LoginFlow · CognitoAdapter 조립)은 그것이 필요한
# 라우트(로그인 6종 · 재설정 2종)만 의존성으로 받는다 — /me · /activity · /logout 은 미설정이어도 동작한다
router = APIRouter(
    prefix="/api/v1/auth",
    tags=["auth"],
    dependencies=[Depends(require_origin)],
)


def _flow_token(request: Request) -> str:
    token = request.cookies.get(FLOW_COOKIE)
    if not token:
        raise AuthFlowExpiredError()
    return token


def _step_response(
    result: StepResult,
    *,
    flow_token: str,
    request: Request,
    response: Response,
    db: Session,
    settings: AuthSettings,
    clock: Callable[[], datetime],
) -> LoginStepResponse:
    if result.step is not LoginStep.COMPLETED:
        set_flow_cookie(response, flow_token)
        return LoginStepResponse(next_step=result.step)
    if result.identity is None:
        raise AuthFailedError()
    issued = service.complete_login(
        db,
        result.identity,
        previous_token=request.cookies.get(SESSION_COOKIE),
        now=clock(),
        max_seconds=settings.AUTH_SESSION_MAX_SECONDS,
        idle_seconds=settings.AUTH_SESSION_IDLE_SECONDS,
    )
    set_session_cookie(response, issued.token, settings.AUTH_SESSION_MAX_SECONDS)
    delete_flow_cookie(response)
    return LoginStepResponse(next_step=LoginStep.COMPLETED)


@router.post("/login", response_model=LoginStepResponse)
def login(
    body: LoginRequest,
    response: Response,
    flow: LoginFlow = _FLOW,
) -> LoginStepResponse:
    token, step = flow.start(body.email, body.password.get_secret_value())
    set_flow_cookie(response, token)
    return LoginStepResponse(next_step=step)


@router.post("/login/new-password", response_model=LoginStepResponse)
def login_new_password(
    body: NewPasswordRequest,
    request: Request,
    response: Response,
    flow: LoginFlow = _FLOW,
) -> LoginStepResponse:
    token = _flow_token(request)
    result = flow.submit_new_password(token, body.new_password.get_secret_value())
    set_flow_cookie(response, token)
    return LoginStepResponse(next_step=result.step)


@router.post("/login/totp-setup", response_model=TotpSetupResponse)
def login_totp_setup(
    request: Request,
    response: Response,
    flow: LoginFlow = _FLOW,
) -> TotpSetupResponse:
    token = _flow_token(request)
    setup = flow.begin_totp_setup(token)
    set_flow_cookie(response, token)
    return TotpSetupResponse(secret=setup.secret, otpauth_uri=setup.otpauth_uri)


@router.post("/login/totp-setup/verify", response_model=LoginStepResponse)
def login_totp_setup_verify(
    body: CodeRequest,
    request: Request,
    response: Response,
    flow: LoginFlow = _FLOW,
    db: Session = _DB,
    settings: AuthSettings = _SETTINGS,
    clock: Callable[[], datetime] = _CLOCK,
) -> LoginStepResponse:
    token = _flow_token(request)
    result = flow.verify_totp_setup(token, body.code.get_secret_value())
    return _step_response(
        result, flow_token=token, request=request, response=response, db=db, settings=settings, clock=clock
    )


@router.post("/login/totp", response_model=LoginStepResponse)
def login_totp(
    body: CodeRequest,
    request: Request,
    response: Response,
    flow: LoginFlow = _FLOW,
    db: Session = _DB,
    settings: AuthSettings = _SETTINGS,
    clock: Callable[[], datetime] = _CLOCK,
) -> LoginStepResponse:
    token = _flow_token(request)
    result = flow.submit_totp(token, body.code.get_secret_value())
    return _step_response(
        result, flow_token=token, request=request, response=response, db=db, settings=settings, clock=clock
    )


@router.post("/login/email-verification", response_model=LoginStepResponse)
def login_email_verification(
    body: CodeRequest,
    request: Request,
    response: Response,
    flow: LoginFlow = _FLOW,
    db: Session = _DB,
    settings: AuthSettings = _SETTINGS,
    clock: Callable[[], datetime] = _CLOCK,
) -> LoginStepResponse:
    token = _flow_token(request)
    result = flow.submit_email_code(token, body.code.get_secret_value())
    return _step_response(
        result, flow_token=token, request=request, response=response, db=db, settings=settings, clock=clock
    )


@router.post("/password-reset", status_code=204)
def password_reset(
    body: PasswordResetRequest,
    adapter: CognitoAdapter = _ADAPTER,
) -> Response:
    password_reset_service.request_reset(adapter, body.email)
    return Response(status_code=204)


@router.post("/password-reset/confirm", status_code=204)
def password_reset_confirm(
    body: PasswordResetConfirmRequest,
    adapter: CognitoAdapter = _ADAPTER,
    db: Session = _DB,
    clock: Callable[[], datetime] = _CLOCK,
) -> Response:
    password_reset_service.confirm_reset(
        adapter,
        db,
        email=body.email,
        code=body.code.get_secret_value(),
        new_password=body.new_password.get_secret_value(),
        now=clock(),
    )
    return Response(status_code=204)


@router.post("/logout", status_code=204)
def logout(
    request: Request,
    db: Session = _DB,
    clock: Callable[[], datetime] = _CLOCK,
) -> Response:
    service.logout(db, request.cookies.get(SESSION_COOKIE), now=clock())
    response = Response(status_code=204)
    delete_session_cookie(response)
    return response


@router.post("/activity", status_code=204)
def activity(
    request: Request,
    db: Session = _DB,
    settings: AuthSettings = _SETTINGS,
    clock: Callable[[], datetime] = _CLOCK,
) -> Response:
    service.resolve_current_user(
        db,
        request.cookies.get(SESSION_COOKIE),
        now=clock(),
        idle_seconds=settings.AUTH_SESSION_IDLE_SECONDS,
        touch=True,
    )
    return Response(status_code=204)


@router.get("/me", response_model=CurrentUserResponse)
def me(
    request: Request,
    db: Session = _DB,
    settings: AuthSettings = _SETTINGS,
    clock: Callable[[], datetime] = _CLOCK,
) -> CurrentUserResponse:
    user = service.resolve_current_user(
        db,
        request.cookies.get(SESSION_COOKIE),
        now=clock(),
        idle_seconds=settings.AUTH_SESSION_IDLE_SECONDS,
        touch=False,
    )
    # 테넌트 상태(tenant_status)는 내부 판정용이라 싣지 않는다
    return CurrentUserResponse(
        user_id=user.user_id,
        email=user.email,
        role=user.role,
        account_type=user.account_type,
        tenant_id=user.tenant_id,
        session=user.session,
    )
