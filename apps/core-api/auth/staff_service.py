# ==============================================================================
# [파일 설명]
# 플랫폼 직원 관리 서비스 — 초대 · 재발송 · 목록 · 권한(platform_role) 변경 · 상태 변경 · 담당 그룹 배정 · 이력.
# (#492 · #572 1번 · 2번 · 4번)
#
#   - 플랫폼 등급은 CENTRAL_ADMIN · TENANT_MANAGER 둘뿐이다. 직원 관리와 담당 그룹 배정은 중앙 관리자만 한다
#     (#572 4번 — 위임 없음. 직원 관리가 필요한 직원은 중앙 관리자로 지정한다).
#   - 초대(invite_staff, #572 1번): 중앙 관리자가 이메일로 초대하면 Cognito 사용자를 만들고(초대 메일은 Cognito 가
#     보낸다) 담당 그룹이 없는 PLATFORM · TENANT_MANAGER 앱 계정 + USER_INVITED 이력을 한 commit 으로 만든다 —
#     운영 CLI(bootstrap_admin --platform)가 만드는 계정과 같다. Cognito 에 이미 있는 이메일은 409
#     USER_STATE_CONFLICT 이고 자동 연결하지 않는다(고객 초대와 같은 규칙). 기존 Cognito 사용자 연결은 운영
#     CLI(--existing-cognito-user)만 한다. 등급은 PUT /{user_id}/role 로 따로 바꾼다.
#   - 재발송(resend_invite): 대상이 PLATFORM 이고 Cognito 상태가 FORCE_CHANGE_PASSWORD 일 때만 RESEND 한다
#     (admin_service.resend_invite 와 같은 제공자 검사 · INVITE_RESENT 이력).
#   - Cognito 호출은 admin_service 와 같이 DB 트랜잭션 밖 · DB 쓰기 전이다(짧은 읽기 트랜잭션으로 행위자 ·
#     대상을 확인하고 rollback → SDK → 잠금 안 재판정 → DB 쓰기). 초대가 Cognito 생성 뒤 재판정 실패(401 · 403)나
#     DB 실패(500)로 끝나면 이 요청이 만든 Cognito 사용자를 sub 로 지운다(보상 삭제 — PR #591 리뷰). 지우기 전에
#     새 트랜잭션에서 (issuer, sub) 가 app_users 에 없음을 확인하고 닫는다. 응답 코드는 그대로이고, 보상 삭제가
#     실패하면 Cognito 사용자가 남을 수 있다(경고 로그에 sub · 오류 종류 · 코드, 복구는 CLI --existing-cognito-user).
#   - 담당 그룹 배정(get_groups · replace_groups, #572 2번): 대상은 TENANT_MANAGER 뿐이다(중앙 관리자 대상은 409).
#     요청 그룹이 하나라도 없으면 404 GROUP_NOT_FOUND 로 아무것도 쓰지 않고, 같은 집합은 409 다. 빈 집합은
#     모든 접근 제거다. 교체와 이력(GROUP_ASSIGNMENTS_CHANGED, before/after group_ids)은 같은 잠금 ·
#     같은 commit 이다. 배정은 계정 종류 · 소속 · 행위자 권한을 바꾸지 않는다. 담당 범위는 그 그룹에 든 고객사이고
#     정지된 고객도 포함한다(소유 범위이지 테넌트 활성 여부가 아니다 — 조작 가능 여부는 고객 조작 가드가 정한다).
#     고객사 단위 직접 배정(TENANT_ASSIGNMENTS_CHANGED)은 없어졌고 그 값은 옛 이력에만 남는다.
#   - 등급 변경은 저장된 담당 그룹을 같은 트랜잭션에서 비운다(중앙 → 담당자 때 예전 범위가 되살아나지 않는다).
#   - 트랜잭션 · 행위자 재판정 · 이력은 admin_service 와 같은 것을 쓴다(users 테이블 잠금 안에서 행위자를
#     다시 읽는다 — 그 사이 강등 · 비활성된 중앙 관리자는 401/403 이고 아무것도 쓰지 않는다).
#   - 대상은 account_type == PLATFORM 뿐이다. 없음 · 고객(TENANT) · UNASSIGNED 는 같은 404 USER_NOT_FOUND.
#   - 자기 자신의 권한 · 상태는 바꾸지 못한다(403). 마지막 ACTIVE 중앙 관리자는 내리지 못한다(409) —
#     집계 · 판정 · 쓰기가 같은 잠금 안이다.
#   - 비활성화는 대상의 앱 세션을 같은 commit 에서 폐기한다. 권한 변경은 세션을 폐기하지 않는다 —
#     세션 판정이 매 요청 DB 의 현재 platform_role 을 읽는다.
# ==============================================================================

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime
from typing import Any
from uuid import UUID

from db import models
from db.repositories import auth as auth_repo
from db.repositories import tenant_groups as groups_repo
from exceptions import ApiError
from schemas.api.auth import AccountType, PlatformRole, UserRole
from schemas.api.errors import ErrorCode
from schemas.api.staff import StaffGroupsResponse, StaffItem, StaffResponse
from schemas.api.users import UserEventAction, UserEventsResponse, UserStatus
from schemas.platform_events import PlatformEventAction
from sqlalchemy.orm import Session

from . import admin_service, service, sessions
from .admin_service import Actor
from .cognito import AuthError

logger = logging.getLogger(__name__)


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


def _item(user: models.AppUser, last_login_at: datetime | None) -> StaffItem:
    return StaffItem(
        user_id=user.user_id,
        email=user.email,
        platform_role=user.platform_role,
        status=UserStatus(user.status.value),
        last_login_at=last_login_at,
    )


def list_staff(db: Session, actor: Actor) -> StaffResponse:
    admin_service._authorize(db, actor, central=True)
    rows = auth_repo.list_platform_users_with_last_login(db)
    return StaffResponse(items=[_item(user, last) for user, last in rows])


def invite_staff(
    db: Session, actor: Actor, get_adapter: Callable[[], Any], *, email: str
) -> StaffItem:
    """직원 초대(#572 1번) — Cognito 사용자를 먼저 만든 뒤 담당 그룹 없는 PLATFORM · TENANT_MANAGER 계정 + 이력을
    한 commit 으로 만든다. get_adapter 는 행위자 판정(401 · 403)을 통과한 뒤에만 부른다(Cognito 미설정 503 보다 먼저).
    Cognito 에 이미 있으면 AuthError USER_STATE_CONFLICT(409) 로 끝나고 DB 는 쓰지 않는다 — 자동 연결 없음.
    생성 뒤 단계가 실패하면 만든 Cognito 사용자를 지우고(_discard_created_user) 원래 오류를 그대로 던진다.
    신원 중복 409 는 예외다 — 그 sub 는 다른 앱 계정의 로그인 신원이라 지우지 않는다."""
    try:
        admin_service._authorize(db, actor, central=True)
    finally:
        db.rollback()  # 읽기 트랜잭션을 SDK 호출 전에 끝낸다
    adapter = get_adapter()
    provider_user = adapter.admin_create_user(email)
    foreign_identity = False
    try:
        with admin_service._transaction(db, actor, central=True) as admin:
            # 잠금 안에서 행위자를 다시 판정했다(그 사이 강등 · 회수면 401/403 — 아래에서 보상 삭제)
            try:
                admin_service._require_new_identity(db, adapter, provider_user)
            except ApiError:
                foreign_identity = True
                raise
            now = actor.clock()
            row = auth_repo.create_user(
                db,
                issuer=adapter.issuer,
                cognito_sub=provider_user.sub,
                email=email,
                role=UserRole.ADMIN,  # PLATFORM 호환 값 — 권한 근거가 아니다(bootstrap_admin 과 같다)
                account_type=AccountType.PLATFORM,
                platform_role=PlatformRole.TENANT_MANAGER,
                tenant_id=None,
                now=now,
                status=models.AppUserStatus.ACTIVE,
                email_verified_at=None,
            )
            admin_service._audit(
                db,
                admin,
                row.user_id,
                UserEventAction.USER_INVITED,
                admin_service._state(),
                admin_service._state(platform_role=PlatformRole.TENANT_MANAGER, status=UserStatus.ACTIVE),
                now,
            )
            admin_service._platform_event(
                db, admin, PlatformEventAction.USER_INVITE, row.user_id, None
            )
            item = _item(row, None)
    except Exception:
        if not foreign_identity:
            _discard_created_user(db, adapter, provider_user.sub)
        raise
    return item


def _discard_created_user(db: Session, adapter: Any, sub: str) -> None:
    """초대의 보상 삭제(PR #591 리뷰) — 앱 계정도 Cognito 사용자도 남지 않게 한다. 응답은 바꾸지 않는다.

    새 트랜잭션에서 (issuer, sub) 가 app_users 에 없음을 확인하고 닫은 뒤에만 지운다. 있으면 지우지 않는다 — 연결이
    끊긴 commit 이 실제로 반영됐을 수 있고, 지우면 그 앱 계정이 로그인할 수 없게 된다. 확인이 실패해도 지우지 않는다.
    최선 노력 경계다 — 확인 · 삭제의 어떤 예외(분류되지 않은 SDK 실패 포함)도 경고 로그로만 남기고 호출자의 원래
    오류를 바꾸지 않는다. 로그에는 sub · 예외 이름 · AWS 오류 코드만 싣는다(메시지 · 응답 본문 없음).
    """
    try:
        try:
            present = (
                auth_repo.get_user_by_identity(db, issuer=adapter.issuer, cognito_sub=sub)
                is not None
            )
        finally:
            db.rollback()  # 확인 트랜잭션을 Cognito 호출 전에 닫는다
    except Exception as exc:  # noqa: BLE001 - 보상은 원래 실패를 바꾸지 않는다
        logger.warning(
            "staff invite compensation skipped sub=%s reason=identity_check_failed error_type=%s",
            sub,
            type(exc).__name__,
        )
        return
    if present:
        logger.warning("staff invite compensation skipped sub=%s reason=identity_present", sub)
        return
    try:
        adapter.admin_delete_user(sub)
    except Exception as exc:  # noqa: BLE001 - 보상은 원래 실패를 바꾸지 않는다
        logger.warning(
            "staff invite compensation failed sub=%s error_type=%s error_code=%s",
            sub,
            type(exc).__name__,
            exc.provider_code if isinstance(exc, AuthError) else None,
        )


def resend_invite(db: Session, actor: Actor, get_adapter: Callable[[], Any], user_id: str) -> None:
    """직원 초대 재발송 — 대상은 PLATFORM 계정(없음 · TENANT · UNASSIGNED 는 404, Cognito 미호출).
    제공자 검사 · RESEND 는 admin_service 와 같고, 잠금 안에서 대상 신원이 처음 읽은 값과 같을 때만 이력을 남긴다."""
    try:
        admin_service._authorize(db, actor, central=True)
        target = _staff_target(db, user_id)
        identity = (target.issuer, target.cognito_sub, target.email)
    finally:
        db.rollback()
    admin_service._provider_resend_invite(get_adapter(), identity)
    with admin_service._transaction(db, actor, central=True) as admin:
        current = _staff_target(db, user_id)
        if (current.issuer, current.cognito_sub, current.email) != identity:
            raise ApiError(ErrorCode.USER_STATE_CONFLICT)
        admin_service._audit(
            db,
            admin,
            current.user_id,
            UserEventAction.INVITE_RESENT,
            admin_service._state(),
            admin_service._state(),
            actor.clock(),
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
        # 등급이 바뀌면 담당 그룹을 비운다 — 중앙 → 담당자로 내려도 예전 범위가 되살아나지 않는다.
        previous = _canonical_ids(auth_repo.list_assigned_group_ids(db, user_id=target.user_id))
        if previous:
            auth_repo.replace_assigned_groups(db, user_id=target.user_id, group_ids=(), now=now)
        admin_service._audit(
            db,
            admin,
            target.user_id,
            UserEventAction.PLATFORM_ROLE_CHANGED,
            before,
            admin_service._state(platform_role=platform_role),
            now,
        )
        admin_service._platform_event(
            db,
            admin,
            PlatformEventAction.USER_PLATFORM_ROLE_CHANGE,
            target.user_id,
            None,
            {"before": before["platform_role"], "after": platform_role.value},
        )
        if previous:
            admin_service._audit(
                db,
                admin,
                target.user_id,
                UserEventAction.GROUP_ASSIGNMENTS_CHANGED,
                admin_service._state(group_ids=list(previous)),
                admin_service._state(group_ids=[]),
                now,
            )
            admin_service._platform_event(
                db,
                admin,
                PlatformEventAction.USER_GROUP_ASSIGNMENTS_CHANGE,
                target.user_id,
                None,
                {"before": list(previous), "after": []},
            )


def _canonical_ids(ids) -> tuple[str, ...]:
    return tuple(sorted(str(UUID(str(i))) for i in ids))


def get_groups(db: Session, actor: Actor, user_id: str) -> StaffGroupsResponse:
    admin_service._authorize(db, actor, central=True)
    target = _staff_target(db, user_id)
    ids = _canonical_ids(auth_repo.list_assigned_group_ids(db, user_id=target.user_id))
    return StaffGroupsResponse(group_ids=[UUID(i) for i in ids])


def replace_groups(db: Session, actor: Actor, user_id: str, group_ids: list[UUID]) -> None:
    requested = _canonical_ids(group_ids)
    if len(set(requested)) != len(requested):
        raise ApiError(ErrorCode.REQUEST_VALIDATION_FAILED)
    with admin_service._transaction(db, actor, central=True) as admin:
        target = _staff_target(db, user_id)
        if target.platform_role is not PlatformRole.TENANT_MANAGER:
            raise ApiError(ErrorCode.USER_STATE_CONFLICT)
        if requested and groups_repo.existing_group_ids(db, requested) != set(requested):
            raise ApiError(ErrorCode.GROUP_NOT_FOUND)
        current = _canonical_ids(auth_repo.list_assigned_group_ids(db, user_id=target.user_id))
        if current == requested:
            raise ApiError(ErrorCode.USER_STATE_CONFLICT)
        now = actor.clock()
        auth_repo.replace_assigned_groups(db, user_id=target.user_id, group_ids=requested, now=now)
        admin_service._audit(
            db,
            admin,
            target.user_id,
            UserEventAction.GROUP_ASSIGNMENTS_CHANGED,
            admin_service._state(group_ids=list(current)),
            admin_service._state(group_ids=list(requested)),
            now,
        )
        admin_service._platform_event(
            db,
            admin,
            PlatformEventAction.USER_GROUP_ASSIGNMENTS_CHANGE,
            target.user_id,
            None,
            {"before": list(current), "after": list(requested)},
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
