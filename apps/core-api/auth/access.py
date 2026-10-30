# ==============================================================================
# [파일 설명]
# API · WebSocket 접근 보호 — 세션 확인 · 역할 검사 · 신원 전달. (#492 PR 2)
#
#   - 세션 판정은 auth.service.resolve_current_user 하나다(touch=False). 보호 API 요청과
#     WebSocket 은 유휴 시간을 늘리지 않는다 — 연장은 POST /api/v1/auth/activity 뿐이다.
#   - Cognito 설정을 읽지 않는다. Cognito 가 비어도 보호 API 는 세션만으로 동작한다.
#   - 거절: 미인증 401 AUTH_REQUIRED, 역할 불허 403 AUTH_FORBIDDEN — 라우터 본문 · Workflow 전이다.
#     require_admin 은 사용자 관리(#492 PR 3)용 ADMIN 전용 검사다.
#   - 계정 종류(#492): 플랫폼 권한은 account_type == PLATFORM 에서만 나온다(role · tenant_id NULL 로
#     추론하지 않는다). require_platform_account 가 테넌트 관리 라우터의 플랫폼 전용 관문이다.
#   - 인시던트 · 조치 · 자산은 요청자의 테넌트 범위로 제한한다. 정지된 고객사도 자기 기록은 조회한다.
#     실행 · 종료의 정지 검사는 Workflow가 대상 인시던트 기준으로 범위 · 멱등 재생 뒤에 수행한다.
#     사용자 관리 변경의 정지 검사는 auth.admin_service가 소유한다.
#   - 메트릭과 WebSocket은 플랫폼 전용이다. require_business_access는 메트릭에 남은 임시 관문이고,
#     WebSocket은 연결 수립 때 계정 종류를 직접 검사한다.
#   - 통과한 신원(CurrentUser)은 request.state.current_user 에 둔다. 접근 로그(main)가 읽고,
#     조치 실행 · 종료 라우터는 역할 검사 결과로 같은 신원을 받는다(감사 기록은 T-2).
# ==============================================================================

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from config import AuthSettings, get_auth_settings
from db.session import get_db, get_session_factory
from exceptions import ApiError
from fastapi import Depends, Request
from schemas.api.auth import AccountType, UserRole
from schemas.api.errors import ErrorCode
from sqlalchemy.orm import Session, sessionmaker
from tenancy import ALL_TENANTS, TenantScope

from . import service
from .http import SESSION_COOKIE, get_clock

#: 조치 실행 · 인시던트 종료를 허용하는 역할. VIEWER 는 조회만 한다.
ACTION_ROLES = frozenset({UserRole.ADMIN, UserRole.OPERATOR})

_DB = Depends(get_db)
_SETTINGS = Depends(get_auth_settings)
_CLOCK = Depends(get_clock)


def require_user(
    request: Request,
    db: Session = _DB,
    settings: AuthSettings = _SETTINGS,
    clock: Callable[[], datetime] = _CLOCK,
) -> service.CurrentUser:
    """유효한 세션 + ACTIVE 사용자만 통과한다. 그 밖은 401 AUTH_REQUIRED(AuthRequiredError)."""
    user = service.resolve_current_user(
        db,
        request.cookies.get(SESSION_COOKIE),
        now=clock(),
        idle_seconds=settings.AUTH_SESSION_IDLE_SECONDS,
        touch=False,
    )
    request.state.current_user = user
    return user


_USER = Depends(require_user)


def is_platform_account(user: service.CurrentUser) -> bool:
    return user.account_type is AccountType.PLATFORM


def require_platform_account(user: service.CurrentUser = _USER) -> service.CurrentUser:
    """플랫폼 계정만 통과한다. 고객사(TENANT) 계정은 역할과 관계없이 403 AUTH_FORBIDDEN."""
    if not is_platform_account(user):
        raise ApiError(ErrorCode.AUTH_FORBIDDEN)
    return user


def require_action_role(user: service.CurrentUser = _USER) -> service.CurrentUser:
    """대상을 읽기 전에 역할을 검사한다. 대상 정지는 Workflow의 범위 · 멱등 검사 뒤에 본다."""
    if user.role not in ACTION_ROLES:
        raise ApiError(ErrorCode.AUTH_FORBIDDEN)
    return user


def require_business_access(user: service.CurrentUser = _USER) -> service.CurrentUser:
    """메트릭 조회에 남은 플랫폼 전용 관문."""
    return require_platform_account(user)


def tenant_scope_of(user: service.CurrentUser) -> TenantScope:
    """계정 종류로 플랫폼 권한을 판정하고 고객사에는 소속 테넌트 범위만 부여한다."""
    if is_platform_account(user):
        return ALL_TENANTS
    if user.tenant_id is None:
        raise ApiError(ErrorCode.AUTH_FORBIDDEN)
    return TenantScope(tenant_id=user.tenant_id)


def require_tenant_scope(user: service.CurrentUser = _USER) -> TenantScope:
    return tenant_scope_of(user)


def require_admin(user: service.CurrentUser = _USER) -> service.CurrentUser:
    """사용자 관리 권한(ADMIN 만). require_user 다음에 돈다 — 본문 검증 · Cognito 의존성보다 앞선다."""
    if user.role is not UserRole.ADMIN:
        raise ApiError(ErrorCode.AUTH_FORBIDDEN)
    return user


def get_auth_session_factory() -> sessionmaker[Session]:
    """WebSocket 세션 확인용 세션 팩토리 — 확인마다 짧은 DB 세션을 연다. 테스트가 재정의한다."""
    return get_session_factory()


def resolve_session_user(
    session_factory: Callable[[], Session],
    token: str | None,
    *,
    now: datetime,
    idle_seconds: int,
) -> service.CurrentUser | None:
    """짧은 DB 세션 하나로 세션을 판정한다(touch=False, 동기). 무효면 None."""
    if not token:
        return None
    with session_factory() as db:
        try:
            return service.resolve_current_user(
                db, token, now=now, idle_seconds=idle_seconds, touch=False
            )
        except service.AuthRequiredError:
            return None
