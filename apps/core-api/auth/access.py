# ==============================================================================
# [파일 설명]
# API · WebSocket 접근 보호 — 세션 확인 · 역할 검사 · 신원 전달. (#492 PR 2)
#
#   - 세션 판정은 auth.service.resolve_current_user 하나다(touch=False). 보호 API 요청과
#     WebSocket 은 유휴 시간을 늘리지 않는다 — 연장은 POST /api/v1/auth/activity 뿐이다.
#   - Cognito 설정을 읽지 않는다. Cognito 가 비어도 보호 API 는 세션만으로 동작한다.
#   - 거절: 미인증 401 AUTH_REQUIRED, 역할 불허 403 AUTH_FORBIDDEN — 라우터 본문 · Workflow 전이다.
#   - 계정 종류(#492): 플랫폼 계정은 account_type == PLATFORM 에서만 나온다(role · tenant_id NULL 로
#     추론하지 않는다). require_platform_account 가 테넌트 라우터의 플랫폼 전용 관문이다(등급 무관).
#   - 플랫폼 등급(#492): 관리 권한은 PLATFORM 의 platform_role 에서만 나온다(role=ADMIN 은 호환 값).
#     중앙 관리자(CENTRAL_ADMIN)만 직원 관리(require_central_admin), CENTRAL_ADMIN · TENANT_MANAGER 가
#     테넌트 관리(require_tenant_management — 호환을 위해 중앙 관리자도 포함한다).
#     require_admin 은 고객 사용자 관리(#492 PR 3) 관문 — 고객사(TENANT) ADMIN 또는 테넌트 관리 등급.
#   - 고객사 범위(#492): tenant_scope_ids 하나가 정한다. None(전체)은 명시 CENTRAL_ADMIN 뿐이고,
#     TENANT_MANAGER 는 배정된 고객사(빈 튜플 = 없음), TENANT 는 자기 소속, 그 밖(등급 없음 등)은 빈 튜플이다.
#     계정 종류 · 소속 NULL · role 로 범위를 추론하지 않는다. 관문은 등급까지만 보고, 대상 고객사가 범위 안인지는
#     각 호출부(서비스)가 tenant_scope_ids / can_access_tenant 로 건다.
#   - 조치 역할: TENANT 는 role(ACTION_ROLES), PLATFORM 은 명시 등급(CENTRAL_ADMIN · TENANT_MANAGER)이다 —
#     PLATFORM 의 호환 값 role=ADMIN 으로 판정하지 않는다. require_action_role 은 등급 · 역할만 보고 대상을 읽지
#     않는다(403 이 범위 404 보다 앞선다).
#   - 업무 범위(#533): 조치 · 자산 · 인시던트는 세션 배선 뒤 각 라우트가 tenant_scope_of(require_tenant_scope)로
#     받은 tenancy.TenantScope 로 제한한다. 범위 값은 tenant_scope_ids 를 그대로 옮긴다 — 전체(ALL_TENANTS)는
#     명시 CENTRAL_ADMIN 뿐이고 계정 종류(PLATFORM)만으로 전체를 주지 않는다. 범위 밖 · 소속 없는 기록은 목록에서
#     빠지고 상세 · 실행 · 종료는 없는 기록과 같은 404 다. 정지 고객사의 TENANT 계정도 자기 범위의 기록은 조회한다.
#   - 정지(SUSPENDED): 실행 · 종료의 정지 판정은 요청자 소속이 아니라 대상 인시던트의 고객사로 Workflow 가 범위 ·
#     멱등 재생 뒤에 한다. 사용자 관리 변경의 정지 판정은 auth.admin_service 몫이다.
#   - 메트릭: require_business_access 는 메트릭 라우터의 자격 관문 — 플랫폼 관리 등급(CENTRAL_ADMIN ·
#     TENANT_MANAGER)만 통과하고 범위는 라우트가 건다. 고객사 계정은 역할 · 정지와 관계없이 403 이다.
#     WebSocket 은 연결 수립 · 재확인 때 직접 판정한다(routers/ws.py).
#   - 통과한 신원(CurrentUser)은 request.state.current_user 에 둔다. 접근 로그(main)가 읽고,
#     조치 실행 · 종료 라우터는 역할 검사 결과로 같은 신원을 받는다(감사 기록은 T-2).
# ==============================================================================

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import datetime

from config import AuthSettings, get_auth_settings
from db.session import get_db, get_session_factory
from exceptions import ApiError
from fastapi import Depends, Request
from schemas.api.auth import AccountType, PlatformRole, UserRole
from schemas.api.errors import ErrorCode
from sqlalchemy.orm import Session, sessionmaker
from tenancy import TenantScope

from . import service
from .http import SESSION_COOKIE, get_clock

#: 고객사(TENANT) 계정의 조치 실행 · 인시던트 종료를 허용하는 역할. VIEWER 는 조회만 한다.
ACTION_ROLES = frozenset({UserRole.ADMIN, UserRole.OPERATOR})
#: 테넌트 관리 · 고객 사용자 관리 · 조치를 하는 플랫폼 등급(명시 값만 — 등급 없는 PLATFORM 은 거절).
_TENANT_MANAGEMENT_ROLES = frozenset({PlatformRole.CENTRAL_ADMIN, PlatformRole.TENANT_MANAGER})

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


def is_central_admin(user: service.CurrentUser) -> bool:
    """PLATFORM 이고 등급이 CENTRAL_ADMIN — 직원 관리 권한."""
    return is_platform_account(user) and user.platform_role is PlatformRole.CENTRAL_ADMIN


def is_tenant_manager(user: service.CurrentUser) -> bool:
    """PLATFORM 이고 등급이 CENTRAL_ADMIN · TENANT_MANAGER — 테넌트 관리 · 고객 사용자 관리 권한.

    등급 판정일 뿐 범위가 아니다 — TENANT_MANAGER 가 다룰 고객사는 tenant_scope_ids 가 정한다.
    """
    return is_platform_account(user) and user.platform_role in _TENANT_MANAGEMENT_ROLES


def tenant_scope_ids(user: service.CurrentUser) -> tuple[str, ...] | None:
    """사용자가 다룰 수 있는 고객사(정규 UUID 문자열). None = 전체이며 명시 CENTRAL_ADMIN 뿐이다.

    TENANT_MANAGER 는 판정 시점의 배정(빈 튜플 = 없음), TENANT 는 자기 소속 하나, 그 밖은 빈 튜플(닫힘)이다.
    """
    if is_central_admin(user):
        return None
    if is_platform_account(user) and user.platform_role is PlatformRole.TENANT_MANAGER:
        return user.assigned_tenant_ids
    if user.account_type is AccountType.TENANT and user.tenant_id is not None:
        return (user.tenant_id,)
    return ()


def can_access_tenant(user: service.CurrentUser, tenant_id: str) -> bool:
    """그 고객사가 사용자의 범위 안인지. 비교는 정규 UUID 문자열로 하며, UUID 가 아니면 누구에게나 False 다."""
    try:
        canonical = str(uuid.UUID(tenant_id))
    except ValueError:
        return False
    scope = tenant_scope_ids(user)
    return scope is None or canonical in scope


def tenant_scope_of(user: service.CurrentUser) -> TenantScope:
    """업무 기록의 조회 범위 — tenant_scope_ids 를 그대로 옮긴다.

    전체(ALL_TENANTS)는 명시 CENTRAL_ADMIN 뿐이다. TENANT_MANAGER 는 배정(빈 튜플 = 아무것도 없음),
    TENANT 는 자기 소속, 그 밖은 빈 범위다 — 계정 종류(PLATFORM)만으로 전체를 주지 않는다.
    """
    return TenantScope(tenant_ids=tenant_scope_ids(user))


def require_tenant_scope(user: service.CurrentUser = _USER) -> TenantScope:
    """업무 조회 라우트의 범위 의존성 — 요청마다 세션 판정 시점의 등급 · 배정 · 소속으로 만든다."""
    return tenant_scope_of(user)


def require_platform_account(user: service.CurrentUser = _USER) -> service.CurrentUser:
    """플랫폼 계정만 통과한다(등급 무관). 고객사(TENANT) 계정은 역할과 관계없이 403 AUTH_FORBIDDEN."""
    if not is_platform_account(user):
        raise ApiError(ErrorCode.AUTH_FORBIDDEN)
    return user


def require_central_admin(user: service.CurrentUser = _USER) -> service.CurrentUser:
    """직원 관리 권한(중앙 관리자만). 그 밖은 403 AUTH_FORBIDDEN."""
    if not is_central_admin(user):
        raise ApiError(ErrorCode.AUTH_FORBIDDEN)
    return user


def require_tenant_management(user: service.CurrentUser = _USER) -> service.CurrentUser:
    """테넌트 관리 권한(CENTRAL_ADMIN · TENANT_MANAGER). 고객사 계정 · 등급 없는 계정은 403 AUTH_FORBIDDEN.

    대상 고객사의 범위 확인은 호출부 몫이다(tenant_scope_ids / can_access_tenant).
    """
    if not is_tenant_manager(user):
        raise ApiError(ErrorCode.AUTH_FORBIDDEN)
    return user


def require_action_role(user: service.CurrentUser = _USER) -> service.CurrentUser:
    """조치 실행 · 인시던트 종료 권한 — 대상을 읽기 전에 등급 · 역할만 본다(403). require_user 다음에 돈다.

    TENANT 는 role 이 ADMIN · OPERATOR, PLATFORM 은 명시 등급(CENTRAL_ADMIN · TENANT_MANAGER)이다 — 호환 값
    role=ADMIN 으로 플랫폼 권한을 판정하지 않는다. 범위(tenant_scope_of)와 대상 고객사 정지는 Workflow 몫이다.
    """
    tenant_actor = user.account_type is AccountType.TENANT and user.role in ACTION_ROLES
    if not (tenant_actor or is_tenant_manager(user)):
        raise ApiError(ErrorCode.AUTH_FORBIDDEN)
    return user


def require_business_access(user: service.CurrentUser = _USER) -> service.CurrentUser:
    """메트릭 라우터의 자격 관문 — 플랫폼 관리 등급(CENTRAL_ADMIN · TENANT_MANAGER)만. 그 밖은 403 AUTH_FORBIDDEN.

    자격만 본다. 범위(TENANT_MANAGER 는 배정 고객사)는 라우트가 tenant_scope_of 로 건다.
    """
    return require_tenant_management(user)


def require_admin(user: service.CurrentUser = _USER) -> service.CurrentUser:
    """고객 사용자 관리 권한 — 고객사(TENANT) ADMIN 또는 테넌트 관리 등급(CENTRAL_ADMIN · TENANT_MANAGER).

    PLATFORM 의 role=ADMIN 은 호환 값이라 근거가 아니다(등급 없는 PLATFORM 은 403). require_user 다음에 돈다 —
    본문 검증 · Cognito 의존성보다 앞선다. 대상 고객사의 범위 확인은 서비스 몫이다(tenant_scope_ids).
    """
    tenant_admin = user.account_type is AccountType.TENANT and user.role is UserRole.ADMIN
    if not (tenant_admin or is_tenant_manager(user)):
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
