# ==============================================================================
# [파일 설명]
# ADMIN 사용자 관리 서비스 — 초대 · 재발송 · 역할 · 상태 · 세션 폐기 · 이력 (트랜잭션 소유자). (#492 PR 3)
#
#   - 이 계층이 commit한다. 저장소 · sessions 는 commit하지 않는다.
#   - 모든 DB 쓰기는 users 테이블 잠금(auth_repo.lock_users_table) 안에서 한다. 잠금을 잡은 직후
#     세션 캐시를 비우고 행위자 세션을 service.resolve_current_user(touch=False)로 다시 판정한다 —
#     그 사이 폐기 · 비활성 · 강등된 행위자는 401/403 이고 아무것도 쓰지 않는다.
#   - Cognito 호출은 DB 변경보다 먼저, 어떤 트랜잭션 밖에서 한다(짧은 읽기 트랜잭션으로 행위자를
#     확인하고 rollback 한 뒤 SDK 호출). SDK 뒤 행위자 재확인이 실패하면 DB 는 쓰지 않지만 제공자
#     부작용은 이미 있었을 수 있다 — 성공으로 취급하지 않는다. DB 실패 시 제공자 사용자를 지우지 않는다.
#   - 이력(user_admin_events)은 상태 변경과 같은 commit 에 들어간다. 값은 UserEventState 화이트리스트뿐이다.
# ==============================================================================

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from db import models
from db.repositories import auth as auth_repo
from exceptions import ApiError
from schemas.api.auth import UserRole
from schemas.api.errors import ErrorCode
from schemas.api.users import (
    UserEventItem,
    UserEventsResponse,
    UserEventState,
    UserEventAction,
    UserListItem,
    UsersResponse,
    UserStatus,
)
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from . import service, sessions

logger = logging.getLogger("vigilantis.auth")

FORCE_CHANGE_PASSWORD = "FORCE_CHANGE_PASSWORD"


@dataclass(frozen=True)
class Actor:
    """요청한 ADMIN. 쓰기 직전마다 token 으로 세션을 다시 판정한다(user_id 는 판정 결과에서 얻는다)."""

    token: str | None = field(repr=False)  # 세션 토큰 — repr · 예외 · 디버그 출력에 나가지 않는다
    idle_seconds: int
    clock: Callable[[], datetime]


def _state(**values: Any) -> dict[str, Any]:
    """UserEventState 화이트리스트를 거친 JSON 값 — None 은 뺀다."""
    return UserEventState(**values).model_dump(mode="json", exclude_none=True)


def _item(user: models.AppUser, last_login_at: datetime | None) -> UserListItem:
    return UserListItem(
        user_id=user.user_id,
        email=user.email,
        role=user.role,
        status=UserStatus(user.status.value),
        last_login_at=last_login_at,
    )


def _authorize(db: Session, actor: Actor) -> service.CurrentUser:
    """행위자 세션 판정. 무효면 401(AuthRequiredError), ADMIN 이 아니면 403 — 둘 다 rollback 한다."""
    try:
        admin = service.resolve_current_user(
            db, actor.token, now=actor.clock(), idle_seconds=actor.idle_seconds, touch=False
        )
        if admin.role is not UserRole.ADMIN:
            raise ApiError(ErrorCode.AUTH_FORBIDDEN)
    except BaseException:
        db.rollback()
        raise
    return admin


def _target(db: Session, user_id: str) -> models.AppUser:
    """대상 행. 경로 값은 정규 UUID 로 바꿔 조회한다 — 호출자는 이후 반드시 반환 행의 user_id(정규형)를 쓴다."""
    try:
        canonical = str(UUID(user_id))
    except ValueError:  # PG UUID 컬럼에 넣으면 DataError(500) — 없는 대상과 같다
        raise ApiError(ErrorCode.USER_NOT_FOUND) from None
    user = auth_repo.get_user(db, canonical)
    if user is None:
        raise ApiError(ErrorCode.USER_NOT_FOUND)
    return user


@contextmanager
def _transaction(db: Session, actor: Actor) -> Iterator[service.CurrentUser]:
    """users 잠금 → 캐시 비움 → 행위자 재판정 → (본문) → commit. 예외는 전부 rollback 이다."""
    try:
        auth_repo.lock_users_table(db)
        db.expire_all()
        admin = _authorize(db, actor)
        yield admin
        db.commit()
    except SQLAlchemyError as exc:
        db.rollback()
        logger.error("user_admin_db_failure error_type=%s", type(exc).__name__)
        raise ApiError(ErrorCode.INTERNAL_ERROR) from None
    except BaseException:
        db.rollback()
        raise


def _require_other_admin_remains(db: Session, target: models.AppUser) -> None:
    """활성 ADMIN 이 줄어드는 변경이면 마지막 한 명을 남긴다(잠금 안에서 센다)."""
    if (
        target.role is UserRole.ADMIN
        and target.status is models.AppUserStatus.ACTIVE
        and auth_repo.count_active_admins(db) <= 1
    ):
        raise ApiError(ErrorCode.USER_STATE_CONFLICT)


def _audit(
    db: Session,
    admin: service.CurrentUser,
    target_id: str,
    action: UserEventAction,
    before: dict[str, Any],
    after: dict[str, Any],
    now: datetime,
) -> None:
    auth_repo.add_user_event(
        db,
        actor_user_id=admin.user_id,
        target_user_id=target_id,
        action=action,
        before_state=before,
        after_state=after,
        now=now,
    )


# --- 조회 ----------------------------------------------------------------------


def list_users(db: Session) -> UsersResponse:
    return UsersResponse(
        items=[_item(user, last) for user, last in auth_repo.list_users_with_last_login(db)]
    )


def list_events(db: Session, user_id: str, *, limit: int) -> UserEventsResponse:
    target = _target(db, user_id)
    rows = auth_repo.list_user_events(db, user_id=target.user_id, limit=limit)
    return UserEventsResponse(
        items=[
            UserEventItem(
                event_id=row.event_id,
                actor_user_id=row.actor_user_id,
                target_user_id=row.target_user_id,
                action=row.action,
                before_state=UserEventState(**row.before_state),
                after_state=UserEventState(**row.after_state),
                occurred_at=row.occurred_at,
            )
            for row in rows
        ]
    )


# --- 쓰기 ----------------------------------------------------------------------


def change_role(db: Session, actor: Actor, user_id: str, role: UserRole) -> None:
    with _transaction(db, actor) as admin:
        target = _target(db, user_id)
        if target.user_id == admin.user_id:
            raise ApiError(ErrorCode.AUTH_FORBIDDEN)
        if target.role is role:
            raise ApiError(ErrorCode.USER_STATE_CONFLICT)
        if role is not UserRole.ADMIN:
            _require_other_admin_remains(db, target)
        now = actor.clock()
        before = _state(role=target.role)
        if not auth_repo.set_role(db, user_id=target.user_id, role=role, now=now):
            raise ApiError(ErrorCode.USER_NOT_FOUND)
        _audit(db, admin, target.user_id, UserEventAction.ROLE_CHANGED, before, _state(role=role), now)


def change_status(db: Session, actor: Actor, user_id: str, status: UserStatus) -> None:
    new_status = models.AppUserStatus(status.value)
    with _transaction(db, actor) as admin:
        target = _target(db, user_id)
        if target.user_id == admin.user_id:
            raise ApiError(ErrorCode.AUTH_FORBIDDEN)
        if target.status is new_status:
            raise ApiError(ErrorCode.USER_STATE_CONFLICT)
        if new_status is models.AppUserStatus.DISABLED:
            _require_other_admin_remains(db, target)
        now = actor.clock()
        before = _state(status=UserStatus(target.status.value))
        if not auth_repo.set_status(db, user_id=target.user_id, status=new_status, now=now):
            raise ApiError(ErrorCode.USER_NOT_FOUND)
        if new_status is models.AppUserStatus.DISABLED:
            sessions.revoke_user_sessions(
                db,
                user_id=target.user_id,
                now=now,
                reason=models.AuthSessionRevokeReason.ACCOUNT_DISABLED,
            )
        # 재활성화는 폐기된 세션을 되살리지 않는다 — 다시 로그인해야 한다
        _audit(db, admin, target.user_id, UserEventAction.STATUS_CHANGED, before, _state(status=status), now)


def revoke_sessions(db: Session, actor: Actor, user_id: str) -> None:
    """대상의 살아 있는 세션을 모두 폐기한다. 본인도 허용(자기 세션도 끊긴다)."""
    with _transaction(db, actor) as admin:
        target = _target(db, user_id)
        now = actor.clock()
        count = sessions.revoke_user_sessions(
            db,
            user_id=target.user_id,
            now=now,
            reason=models.AuthSessionRevokeReason.ADMIN_REVOKED,
        )
        _audit(
            db,
            admin,
            target.user_id,
            UserEventAction.SESSIONS_REVOKED,
            _state(),
            _state(revoked_session_count=count),
            now,
        )


def invite_user(
    db: Session,
    actor: Actor,
    adapter: Any,
    *,
    email: str,
    role: UserRole,
    recover_existing: bool,
) -> UserListItem:
    """초대 — 제공자 사용자를 먼저 확보한 뒤 앱 사용자 + 이력을 한 commit 으로 만든다.

    기본은 새 Cognito 사용자 생성(중복이면 AuthError USER_STATE_CONFLICT 409)이다. 자동 연결은 없다.
    recover_existing=True 는 ADMIN 의 명시적 선택이다: 이미 있는 Cognito 사용자를 조회해(없으면 404)
    앱에 아직 없는 신원이면 연결한다 — 이메일 소유 증명이 아니다.
    """
    _authorize(db, actor)
    db.rollback()  # 읽기 트랜잭션을 SDK 호출 전에 끝낸다
    if recover_existing:
        provider_user = adapter.admin_get_user(email)
        if provider_user is None:
            raise ApiError(ErrorCode.USER_NOT_FOUND)
    else:
        provider_user = adapter.admin_create_user(email)
    with _transaction(db, actor) as admin:
        if (
            auth_repo.get_user_by_identity(
                db, issuer=adapter.issuer, cognito_sub=provider_user.sub
            )
            is not None
        ):
            raise ApiError(ErrorCode.USER_STATE_CONFLICT)
        now = actor.clock()
        row = auth_repo.create_user(
            db,
            issuer=adapter.issuer,
            cognito_sub=provider_user.sub,
            email=email,
            role=role,
            now=now,
            status=models.AppUserStatus.ACTIVE,
            email_verified_at=None,
        )
        _audit(
            db,
            admin,
            row.user_id,
            UserEventAction.USER_INVITED,
            _state(),
            _state(role=role, status=UserStatus.ACTIVE, recover_existing=recover_existing),
            now,
        )
        item = _item(row, None)
    return item


def resend_invite(db: Session, actor: Actor, adapter: Any, user_id: str) -> None:
    """임시 비밀번호가 아직 FORCE_CHANGE_PASSWORD 인 앱 사용자에게만 초대를 다시 보낸다."""
    _authorize(db, actor)
    try:
        target = _target(db, user_id)
        identity = (target.issuer, target.cognito_sub, target.email)
    finally:
        db.rollback()
    issuer, sub, email = identity
    if issuer != adapter.issuer:
        raise ApiError(ErrorCode.USER_STATE_CONFLICT)
    provider_user = adapter.admin_get_user(email)
    if (
        provider_user is None
        or provider_user.sub != sub
        or provider_user.status != FORCE_CHANGE_PASSWORD
    ):
        raise ApiError(ErrorCode.USER_STATE_CONFLICT)
    adapter.admin_create_user(email, resend=True)
    with _transaction(db, actor) as admin:
        current = _target(db, user_id)
        if (current.issuer, current.cognito_sub, current.email) != identity:
            raise ApiError(ErrorCode.USER_STATE_CONFLICT)
        _audit(
            db, admin, current.user_id, UserEventAction.INVITE_RESENT, _state(), _state(), actor.clock()
        )
