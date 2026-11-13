# ==============================================================================
# [파일 설명]
# /api/v1/auth — 로그인 · 로그아웃 · 현재 사용자 라우터. (#492 PR 1, 계약: schemas.api.auth)
#
#   - 얇게 둔다: 로그인 상태 머신은 LoginFlow, 세션·트랜잭션은 auth.service 가 맡는다.
#   - 세션 쿠키는 StepResult 가 COMPLETED + identity 일 때, service 가 세션을 발급한 뒤에만 건다.
#   - 비밀값(SecretStr)은 LoginFlow 호출 지점에서만 벗긴다. 토큰은 어떤 본문에도 싣지 않는다.
#   - 모든 경로에 Origin 검사(상태 변경만)가 먼저 걸리고, Cognito 라우트(로그인 · 재설정)만 설정 확인이 뒤따른다.
#   - 활동 로그(#589 B): 실패 · 거절된 시도는 여기서 따로 남긴다(_attempt · auth.event_log.attempt_failed).
#     TOTP 코드 단계(등록 준비 · 등록 확인 · 코드 제출)는 AUTH_MFA, 그 밖의 단계와 세션 발급의 거절은 AUTH_LOGIN 이다.
#     성공은 세션 발급(auth.service.complete_login)이 같은 트랜잭션에 남긴다 — 중간 단계의 성공은 남기지 않는다.
# ==============================================================================

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import TypeVar

from auth import event_log, permissions, service
from auth import password_reset as password_reset_service
from auth.cognito import AuthError, AuthFailedError, AuthFlowExpiredError, CognitoAdapter
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
from db.session import get_db, get_session_factory
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
from schemas.platform_events import PlatformEventAction
from sqlalchemy.orm import Session

_T = TypeVar("_T")
_LOGIN = PlatformEventAction.AUTH_LOGIN
_MFA = PlatformEventAction.AUTH_MFA

_DB = Depends(get_db)
# 실패 · 거절된 시도를 요청 트랜잭션 밖에 남길 때 쓰는 세션(#589 B)
_SESSIONS = Depends(get_session_factory)
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


def _attempt(
    call: Callable[[], _T],
    action: PlatformEventAction,
    session_factory: Callable[[], Session],
    *,
    subject: event_log.Subject | None = None,
) -> _T:
    """로그인 흐름의 한 단계를 부르고, 거절 · 실패(AuthError)면 활동 로그에 따로 남긴 뒤 그대로 올린다.

    subject 는 단계를 부르기 **전에** 읽어 둔 대상 신원이다 — 실패한 흐름은 저장소에서 사라져 뒤에는 읽을 수 없다."""
    try:
        return call()
    except AuthError as exc:
        event_log.attempt_failed(session_factory, action, exc.code, subject=subject)
        raise


def _step_response(
    result: StepResult,
    *,
    flow_token: str,
    request: Request,
    response: Response,
    db: Session,
    settings: AuthSettings,
    clock: Callable[[], datetime],
    session_factory: Callable[[], Session],
) -> LoginStepResponse:
    if result.step is not LoginStep.COMPLETED:
        set_flow_cookie(response, flow_token)
        return LoginStepResponse(next_step=result.step)
    if result.identity is None:
        raise AuthFailedError()
    identity = result.identity
    # 세션 발급의 거절(미등록 · 비활성 · 세대 불일치 · 이메일 증명 없음)은 complete_login 이 되돌린 뒤 여기서 남긴다
    issued = _attempt(
        lambda: service.complete_login(
            db,
            identity,
            previous_token=request.cookies.get(SESSION_COOKIE),
            now=clock(),
            max_seconds=settings.AUTH_SESSION_MAX_SECONDS,
            idle_seconds=settings.AUTH_SESSION_IDLE_SECONDS,
        ),
        _LOGIN,
        session_factory,
        subject=(identity.issuer, identity.sub),
    )
    set_session_cookie(response, issued.token, settings.AUTH_SESSION_MAX_SECONDS)
    delete_flow_cookie(response)
    return LoginStepResponse(next_step=LoginStep.COMPLETED)


@router.post("/login", response_model=LoginStepResponse)
def login(
    body: LoginRequest,
    response: Response,
    flow: LoginFlow = _FLOW,
    session_factory: Callable[[], Session] = _SESSIONS,
) -> LoginStepResponse:
    # 비밀번호 단계의 실패는 대상 없이 남는다 — 이메일로 앱 계정을 고르지 않는다(auth.event_log)
    token, step = _attempt(
        lambda: flow.start(body.email, body.password.get_secret_value()), _LOGIN, session_factory
    )
    set_flow_cookie(response, token)
    return LoginStepResponse(next_step=step)


@router.post("/login/new-password", response_model=LoginStepResponse)
def login_new_password(
    body: NewPasswordRequest,
    request: Request,
    response: Response,
    flow: LoginFlow = _FLOW,
    session_factory: Callable[[], Session] = _SESSIONS,
) -> LoginStepResponse:
    token = _flow_token(request)
    result = _attempt(
        lambda: flow.submit_new_password(token, body.new_password.get_secret_value()),
        _LOGIN,
        session_factory,
        subject=flow.subject(token),
    )
    set_flow_cookie(response, token)
    return LoginStepResponse(next_step=result.step)


@router.post("/login/totp-setup", response_model=TotpSetupResponse)
def login_totp_setup(
    request: Request,
    response: Response,
    flow: LoginFlow = _FLOW,
    session_factory: Callable[[], Session] = _SESSIONS,
) -> TotpSetupResponse:
    token = _flow_token(request)
    setup = _attempt(
        lambda: flow.begin_totp_setup(token), _MFA, session_factory, subject=flow.subject(token)
    )
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
    session_factory: Callable[[], Session] = _SESSIONS,
) -> LoginStepResponse:
    token = _flow_token(request)
    result = _attempt(
        lambda: flow.verify_totp_setup(token, body.code.get_secret_value()),
        _MFA,
        session_factory,
        subject=flow.subject(token),
    )
    return _step_response(
        result, flow_token=token, request=request, response=response, db=db, settings=settings, clock=clock,
        session_factory=session_factory,
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
    session_factory: Callable[[], Session] = _SESSIONS,
) -> LoginStepResponse:
    token = _flow_token(request)
    result = _attempt(
        lambda: flow.submit_totp(token, body.code.get_secret_value()),
        _MFA,
        session_factory,
        subject=flow.subject(token),
    )
    return _step_response(
        result, flow_token=token, request=request, response=response, db=db, settings=settings, clock=clock,
        session_factory=session_factory,
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
    session_factory: Callable[[], Session] = _SESSIONS,
) -> LoginStepResponse:
    token = _flow_token(request)
    # 이메일 코드는 TOTP 를 지난 뒤의 이메일 증명이다 — MFA 가 아니라 로그인 단계로 남긴다
    result = _attempt(
        lambda: flow.submit_email_code(token, body.code.get_secret_value()),
        _LOGIN,
        session_factory,
        subject=flow.subject(token),
    )
    return _step_response(
        result, flow_token=token, request=request, response=response, db=db, settings=settings, clock=clock,
        session_factory=session_factory,
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
    session_factory: Callable[[], Session] = _SESSIONS,
) -> Response:
    password_reset_service.confirm_reset(
        adapter,
        db,
        email=body.email,
        code=body.code.get_secret_value(),
        new_password=body.new_password.get_secret_value(),
        now=clock(),
        session_factory=session_factory,
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
    user = service.resolve_current_user(
        db,
        request.cookies.get(SESSION_COOKIE),
        now=clock(),
        idle_seconds=settings.AUTH_SESSION_IDLE_SECONDS,
        touch=True,
    )
    # 호출 집계(api_usage, #572 6번)가 응답 뒤 읽는 신원 — 보호 API 의 require_user 와 같은 자리
    request.state.current_user = user
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
    request.state.current_user = user
    # 테넌트 상태(tenant_status)는 내부 판정용이라 싣지 않는다. platform_role · assigned_tenant_ids · permissions 는
    # 이번 판정이 읽은 현재 값이다 — permissions 는 관문과 같은 판정(auth.permissions.effective_permissions)이다
    return CurrentUserResponse(
        user_id=user.user_id,
        email=user.email,
        role=user.role,
        account_type=user.account_type,
        tenant_id=user.tenant_id,
        platform_role=user.platform_role,
        session=user.session,
        assigned_tenant_ids=list(user.assigned_tenant_ids),
        permissions=permissions.ordered(permissions.effective_permissions(user)),
    )
