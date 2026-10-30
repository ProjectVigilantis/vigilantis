# ==============================================================================
# [파일 설명]
# 플랫폼 직원 관리 서비스 — 목록 · 권한(platform_role) 변경 · 상태 변경 · 담당 고객 배정 · 이력. (#492)
#
#   - 플랫폼 등급은 CENTRAL_ADMIN · TENANT_MANAGER 둘뿐이다. 직원 관리와 담당 고객 배정은 중앙 관리자만 한다.
#     직원 생성 · 초대는 API 에 없다(운영 CLI 전용).
#   - 담당 고객 배정(get_tenants · replace_tenants): 대상은 TENANT_MANAGER 뿐이다(중앙 관리자 대상은 409).
#     요청 테넌트가 하나라도 없으면 404 TENANT_NOT_FOUND 로 아무것도 쓰지 않고, 같은 집합은 409 다. 빈 집합은
#     모든 접근 제거다. 교체와 이력(TENANT_ASSIGNMENTS_CHANGED, before/after tenant_ids)은 같은 잠금 ·
#     같은 commit 이다. 배정은 계정 종류 · 소속 · 행위자 권한을 바꾸지 않고, 정지된 고객도 배정할 수 있다
#     (소유 범위이지 테넌트 활성 여부가 아니다 — 조작 가능 여부는 고객 조작 가드가 따로 정한다).
#   - 등급 변경은 저장된 담당 고객을 같은 트랜잭션에서 비운다(중앙 → 담당자 때 예전 범위가 되살아나지 않는다).
#   - 트랜잭션 · 행위자 재판정 · 이력은 admin_service 와 같은 것을 쓴다(users 테이블 잠금 안에서 행위자를
#     다시 읽는다 — 그 사이 강등 · 비활성된 중앙 관리자는 401/403 이고 아무것도 쓰지 않는다).
#   - 대상은 account_type == PLATFORM 뿐이다. 없음 · 고객(TENANT) · UNASSIGNED 는 같은 404 USER_NOT_FOUND.
#   - 자기 자신의 권한 · 상태는 바꾸지 못한다(403). 마지막 ACTIVE 중앙 관리자는 내리지 못한다(409) —
#     집계 · 판정 · 쓰기가 같은 잠금 안이다.
#   - 비활성화는 대상의 앱 세션을 같은 commit 에서 폐기한다. 권한 변경은 세션을 폐기하지 않는다 —
#     세션 판정이 매 요청 DB 의 현재 platform_role 을 읽는다.
# ==============================================================================

from __future__ import annotations

from uuid import UUID

from db import models
from db.repositories import auth as auth_repo
from exceptions import ApiError
from schemas.api.auth import AccountType, PlatformRole
from schemas.api.errors import ErrorCode
from schemas.api.staff import StaffItem, StaffResponse, StaffTenantsResponse
from schemas.api.users import UserEventAction, UserEventsResponse, UserStatus
from sqlalchemy.orm import Session

from . import admin_service, service, sessions
from .admin_service import Actor


def _staff_target(db: Session, user_id: str) -> models.AppUser:
    try:
        canonical = str(UUID(user_id))
    except ValueError:
        raise ApiError(ErrorCode.USER_NOT_FOUND) from None
    user = auth_repo.get_user(db, canonical)
    if user is None or user.account_type is not AccountType.PLATFORM:
        raise ApiError(ErrorCode.USER_NOT_FOUND)
    return user


def _require_other_central_remains(db: Session, target: models.AppUser) -> None:
    if (
        target.platform_role is PlatformRole.CENTRAL_ADMIN
        and target.status is models.AppUserStatus.ACTIVE
        and auth_repo.count_active_central_admins(db) <= 1
    ):
        raise ApiError(ErrorCode.USER_STATE_CONFLICT)


def _not_self(admin: service.CurrentUser, target: models.AppUser) -> None:
    if target.user_id == admin.user_id:
        raise ApiError(ErrorCode.AUTH_FORBIDDEN)


def list_staff(db: Session, actor: Actor) -> StaffResponse:
    admin_service._authorize(db, actor, central=True)
    rows = auth_repo.list_platform_users_with_last_login(db)
    return StaffResponse(
        items=[
            StaffItem(
                user_id=user.user_id,
                email=user.email,
                platform_role=user.platform_role,
                status=UserStatus(user.status.value),
                last_login_at=last,
            )
            for user, last in rows
        ]
    )


def list_events(db: Session, actor: Actor, user_id: str, *, limit: int) -> UserEventsResponse:
    admin_service._authorize(db, actor, central=True)
    target = _staff_target(db, user_id)
    return admin_service.events_response(db, target.user_id, limit=limit)


def change_platform_role(
    db: Session, actor: Actor, user_id: str, platform_role: PlatformRole
) -> None:
    with admin_service._transaction(db, actor, central=True) as admin:
        target = _staff_target(db, user_id)
        _not_self(admin, target)
        if target.platform_role is platform_role:
            raise ApiError(ErrorCode.USER_STATE_CONFLICT)
        if platform_role is not PlatformRole.CENTRAL_ADMIN:
            _require_other_central_remains(db, target)
        now = actor.clock()
        before = admin_service._state(platform_role=target.platform_role)
        if not auth_repo.set_platform_role(
            db, user_id=target.user_id, platform_role=platform_role, now=now
        ):
            raise ApiError(ErrorCode.USER_NOT_FOUND)
        # 등급이 바뀌면 담당 고객을 비운다 — 중앙 → 담당자로 내려도 예전 범위가 되살아나지 않는다.
        previous = _canonical_ids(auth_repo.list_assigned_tenant_ids(db, user_id=target.user_id))
        if previous:
            auth_repo.replace_assigned_tenants(db, user_id=target.user_id, tenant_ids=(), now=now)
        admin_service._audit(
            db,
            admin,
            target.user_id,
            UserEventAction.PLATFORM_ROLE_CHANGED,
            before,
            admin_service._state(platform_role=platform_role),
            now,
        )
        if previous:
            admin_service._audit(
                db,
                admin,
                target.user_id,
                UserEventAction.TENANT_ASSIGNMENTS_CHANGED,
                admin_service._state(tenant_ids=list(previous)),
                admin_service._state(tenant_ids=[]),
                now,
            )


def _canonical_ids(ids) -> tuple[str, ...]:
    return tuple(sorted(str(UUID(str(i))) for i in ids))


def get_tenants(db: Session, actor: Actor, user_id: str) -> StaffTenantsResponse:
    admin_service._authorize(db, actor, central=True)
    target = _staff_target(db, user_id)
    ids = _canonical_ids(auth_repo.list_assigned_tenant_ids(db, user_id=target.user_id))
    return StaffTenantsResponse(tenant_ids=[UUID(i) for i in ids])


def replace_tenants(db: Session, actor: Actor, user_id: str, tenant_ids: list[UUID]) -> None:
    requested = _canonical_ids(tenant_ids)
    if len(set(requested)) != len(requested):
        raise ApiError(ErrorCode.REQUEST_VALIDATION_FAILED)
    with admin_service._transaction(db, actor, central=True) as admin:
        target = _staff_target(db, user_id)
        if target.platform_role is not PlatformRole.TENANT_MANAGER:
            raise ApiError(ErrorCode.USER_STATE_CONFLICT)
        if requested:
            if any(db.get(models.Tenant, tenant_id) is None for tenant_id in requested):
                raise ApiError(ErrorCode.TENANT_NOT_FOUND)
        current = _canonical_ids(auth_repo.list_assigned_tenant_ids(db, user_id=target.user_id))
        if current == requested:
            raise ApiError(ErrorCode.USER_STATE_CONFLICT)
        now = actor.clock()
        auth_repo.replace_assigned_tenants(
            db, user_id=target.user_id, tenant_ids=requested, now=now
        )
        admin_service._audit(
            db,
            admin,
            target.user_id,
            UserEventAction.TENANT_ASSIGNMENTS_CHANGED,
            admin_service._state(tenant_ids=list(current)),
            admin_service._state(tenant_ids=list(requested)),
            now,
        )


def change_status(db: Session, actor: Actor, user_id: str, status: UserStatus) -> None:
    new_status = models.AppUserStatus(status.value)
    with admin_service._transaction(db, actor, central=True) as admin:
        target = _staff_target(db, user_id)
        _not_self(admin, target)
        if target.status is new_status:
            raise ApiError(ErrorCode.USER_STATE_CONFLICT)
        if new_status is models.AppUserStatus.DISABLED:
            _require_other_central_remains(db, target)
        now = actor.clock()
        before = admin_service._state(status=UserStatus(target.status.value))
        if not auth_repo.set_status(db, user_id=target.user_id, status=new_status, now=now):
            raise ApiError(ErrorCode.USER_NOT_FOUND)
        if new_status is models.AppUserStatus.DISABLED:
            sessions.revoke_user_sessions(
                db,
                user_id=target.user_id,
                now=now,
                reason=models.AuthSessionRevokeReason.ACCOUNT_DISABLED,
            )
        admin_service._audit(
            db,
            admin,
            target.user_id,
            UserEventAction.STATUS_CHANGED,
            before,
            admin_service._state(status=status),
            now,
        )
