# ==============================================================================
# [파일 설명]
# 고객사 계정의 기능별 권한 · 계정 관리 관문 — auth.access 의 역할 · 등급 판정을 기능 단위로 넓힌다. (#572 5 · 9번)
#
#   - 기능별 권한(5번): 서버가 거절하는 기능은 조치 실행(EXECUTE_ACTION) · 인시던트 종료(RESOLVE_INCIDENT) ·
#     문의 등록(CREATE_INQUIRY)이다. VIEW 는 FE 가 자산 · 보안 · 감사 · 문의 메뉴를 그릴지 정하는 값이고 조회 API 는
#     막지 않는다 — 대시보드가 같은 조회 API 를 쓰고 VIEWER 도 대시보드는 본다.
#   - effective_permissions 하나가 /me 응답과 관문의 근거다. TENANT ADMIN 은 네 가지 전부, OPERATOR 는 판정 시점에 읽은
#     부여 권한(CurrentUser.granted_permissions), VIEWER 는 없음. PLATFORM 은 access.is_tenant_manager(명시 등급
#     CENTRAL_ADMIN · TENANT_MANAGER)일 때 VIEW · EXECUTE_ACTION · RESOLVE_INCIDENT 다 — 문의 등록은 고객사 계정만 한다.
#     등급 없는 PLATFORM · 그 밖은 없음. 호환 값 role=ADMIN 으로 플랫폼 권한을 판정하지 않는다(access 와 같다).
#   - require_permission(기능)은 require_user 다음에 도는 라우트 의존성이다 — 대상을 읽기 전에 403 AUTH_FORBIDDEN
#     (범위 404 보다 앞선다). 쓰기 직전 users 잠금 안의 재판정은 같은
#     check_permission 을 순수 함수로 부른다(workflows._live_actor · inquiries_service).
#   - 계정 관리 관문(9번): /api/v1/users 는 고객사 중앙계정(TENANT ADMIN, 자기 고객사)과 중앙 관리계정(CENTRAL_ADMIN,
#     모든 고객사)만 쓴다. 관리자 직원계정(TENANT_MANAGER)은 배정과 무관하게 403 이다. access.require_admin 은
#     테넌트 설정 · 구독 같은 다른 경로가 계속 쓴다(TENANT_MANAGER 포함) — 여기서 바꾸지 않는다.
# ==============================================================================

from __future__ import annotations

from collections.abc import Callable

from exceptions import ApiError
from fastapi import Depends
from schemas.api.auth import AccountType, FeaturePermission, UserRole
from schemas.api.errors import ErrorCode

from . import access, service

#: TENANT ADMIN(고객사 중앙계정)이 쓰는 기능 — 네 가지 전부.
ALL_PERMISSIONS: frozenset[FeaturePermission] = frozenset(FeaturePermission)
#: 플랫폼 관리 권한자(CENTRAL_ADMIN · TENANT_MANAGER)가 쓰는 기능 — 문의 등록은 고객사 계정만 한다.
PLATFORM_PERMISSIONS: frozenset[FeaturePermission] = frozenset(
    {
        FeaturePermission.VIEW,
        FeaturePermission.EXECUTE_ACTION,
        FeaturePermission.RESOLVE_INCIDENT,
    }
)

_USER = Depends(access.require_user)


def tenant_permissions(
    role: UserRole, granted: frozenset[FeaturePermission]
) -> frozenset[FeaturePermission]:
    """고객사(TENANT) 계정의 역할 + 부여 행 → 쓸 수 있는 권한. ADMIN 전부 · OPERATOR 부여분 · VIEWER 없음."""
    if role is UserRole.ADMIN:
        return ALL_PERMISSIONS
    if role is UserRole.OPERATOR:
        return granted
    return frozenset()


def effective_permissions(user: service.CurrentUser) -> frozenset[FeaturePermission]:
    """이 계정이 지금 쓸 수 있는 기능별 권한 — /me 응답과 관문이 같은 값을 쓴다."""
    if user.account_type is AccountType.TENANT:
        return tenant_permissions(user.role, user.granted_permissions)
    if access.is_tenant_manager(user):
        return PLATFORM_PERMISSIONS
    return frozenset()


def ordered(permissions: frozenset[FeaturePermission]) -> list[FeaturePermission]:
    """응답 · 이력에 싣는 순서 — 열거 선언 순서."""
    return [p for p in FeaturePermission if p in permissions]


def check_permission(user: service.CurrentUser, feature: FeaturePermission) -> service.CurrentUser:
    """기능 권한이 없으면 403 AUTH_FORBIDDEN. 대상 · 범위는 보지 않는다."""
    if feature not in effective_permissions(user):
        raise ApiError(ErrorCode.AUTH_FORBIDDEN)
    return user


def require_permission(
    feature: FeaturePermission,
) -> Callable[[service.CurrentUser], service.CurrentUser]:
    """기능 권한 라우트 의존성 — require_user 다음, 본문 검증 · 대상 조회보다 앞선다."""

    def dependency(user: service.CurrentUser = _USER) -> service.CurrentUser:
        return check_permission(user, feature)

    dependency.__name__ = f"require_{feature.value.lower()}"
    return dependency


def is_user_manager(user: service.CurrentUser) -> bool:
    """고객사 사용자 관리 자격 — TENANT ADMIN(자기 고객사) 또는 CENTRAL_ADMIN(모든 고객사). TENANT_MANAGER 는 아니다."""
    tenant_admin = user.account_type is AccountType.TENANT and user.role is UserRole.ADMIN
    return tenant_admin or access.is_central_admin(user)


def require_user_management(user: service.CurrentUser = _USER) -> service.CurrentUser:
    """/api/v1/users 관문(#572 9번). 그 밖(TENANT_MANAGER · OPERATOR · VIEWER · 등급 없는 계정)은 403 AUTH_FORBIDDEN.

    대상 고객사의 범위 확인은 서비스 몫이다(access.tenant_scope_ids — CENTRAL_ADMIN 전체, TENANT ADMIN 자기 고객사).
    """
    if not is_user_manager(user):
        raise ApiError(ErrorCode.AUTH_FORBIDDEN)
    return user
