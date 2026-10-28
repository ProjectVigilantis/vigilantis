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
#   - 테넌트 범위(#533 C): 인시던트 · 조치 · 자산 라우터는 요청자의 범위(tenant_scope_of — 플랫폼은 전부,
#     고객사는 자기 테넌트)로 거른다. 범위 밖은 404 이고 정지는 **대상 인시던트의 테넌트**로 Workflow 가 본다 — 판정 순서는
#     역할(여기 · 403) → 범위(404) → 같은 Key 재요청 → 정지(409) → 상태(packages/schemas/api/tenants.py).
#     그래서 require_action_role 은 역할만 본다. 정지 테넌트의 고객사 사용자는 조회 · 로그인만 한다 — 조치 실행 ·
#     종료와 사용자 관리 변경은 409 TENANT_SUSPENDED(사용자 관리는 auth.admin_service 가 본다).
#   - 임시 출시 관문(#492): 메트릭 라우터와 WebSocket 은 아직 테넌트 범위로 걸러지지 않는다(전 고객 데이터).
#     그래서 메트릭은 포함 시점(main)에 require_platform_account 를 걸고, WebSocket 은 연결 때 플랫폼 계정만
#     받는다 — 범위 필터가 들어오면 걷는다(WebSocket 은 #535).
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
    """조치 실행 · 인시던트 종료 권한 — 역할만 본다(403). 대상을 읽기 전이라 존재가 새지 않는다.

    정지(409)는 여기서 보지 않는다 — 계약 순서상 범위(404) · 같은 Key 재요청 뒤이고, 요청자 소속이 아니라
    대상 인시던트의 테넌트로 본다(workflows.reserve_execution · resolve_incident).
    """
    if user.role not in ACTION_ROLES:
        raise ApiError(ErrorCode.AUTH_FORBIDDEN)
    return user


def tenant_scope_of(user: service.CurrentUser) -> TenantScope:
    """요청자가 볼 수 있는 업무 기록의 범위 — 플랫폼 계정은 전부, 고객사 계정은 자기 테넌트."""
    if is_platform_account(user):
        return ALL_TENANTS
    # 세션 판정이 PLATFORM · TENANT 만 통과시키고, TENANT 는 tenant_id 가 있다(DB CHECK)
    assert user.tenant_id is not None, "TENANT 계정에 tenant_id 가 없다"
    return TenantScope(tenant_id=user.tenant_id)


def require_tenant_scope(user: service.CurrentUser = _USER) -> TenantScope:
    """조회 라우트용 — 요청자의 범위를 의존성으로 받는다."""
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
