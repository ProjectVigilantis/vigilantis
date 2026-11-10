"""#552 지원 API의 신원 재판정과 트랜잭션 경계.

등급·담당 범위는 auth.access를, 문의 등록의 기능 권한(#572 5번)은 auth.permissions를 재사용한다.
정지 고객의 지원 문의는 허용하지만 auth.admin_service의 정지 고객 관리 거절을 바꾸지 않는다.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime

from auth import access, permissions, service
from auth.admin_service import Actor
from auth.http import SESSION_COOKIE, get_clock
from config import AuthSettings, get_auth_settings
from db.repositories import auth as auth_repo
from exceptions import ApiError
from fastapi import Depends, Request
from schemas.api.auth import AccountType, FeaturePermission
from schemas.api.errors import ErrorCode
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

_SETTINGS = Depends(get_auth_settings)
_CLOCK = Depends(get_clock)
_logger = logging.getLogger("vigilantis.support")
Gate = Callable[[service.CurrentUser], service.CurrentUser]


def get_actor(
    request: Request,
    settings: AuthSettings = _SETTINGS,
    clock: Callable[[], datetime] = _CLOCK,
) -> Actor:
    return Actor(
        token=request.cookies.get(SESSION_COOKIE),
        idle_seconds=settings.AUTH_SESSION_IDLE_SECONDS,
        clock=clock,
    )


def require_customer(user: service.CurrentUser) -> service.CurrentUser:
    """고객사 사용자만(역할 무관). 활성 세션의 정지 고객사도 지원 경로는 쓸 수 있다 — 문의 목록 조회의 고객 쪽 관문."""
    if user.account_type is not AccountType.TENANT or user.tenant_id is None:
        raise ApiError(ErrorCode.AUTH_FORBIDDEN)
    return user


def require_inquiry_author(user: service.CurrentUser) -> service.CurrentUser:
    """접수(POST)는 고객사 사용자 중 문의 등록 권한(CREATE_INQUIRY)이 있는 계정만(#572 5번) — ADMIN, 권한을 받은
    OPERATOR. VIEWER 와 권한 없는 OPERATOR 는 403 이다. 정지 고객사 여부는 보지 않는다(require_customer 와 같다)."""
    return permissions.check_permission(require_customer(user), FeaturePermission.CREATE_INQUIRY)


def require_support_user(user: service.CurrentUser) -> service.CurrentUser:
    if user.account_type is AccountType.TENANT:
        return require_customer(user)
    return access.require_tenant_management(user)


def authorize(db: Session, actor: Actor, gate: Gate) -> service.CurrentUser:
    try:
        user = service.resolve_current_user(
            db,
            actor.token,
            now=actor.clock(),
            idle_seconds=actor.idle_seconds,
            touch=False,
        )
        return gate(user)
    except SQLAlchemyError as exc:
        db.rollback()
        _logger.error("support_db_failure error_type=%s", type(exc).__name__)
        raise ApiError(ErrorCode.INTERNAL_ERROR) from None
    except BaseException:
        db.rollback()
        raise


@contextmanager
def write_transaction(db: Session, actor: Actor, gate: Gate) -> Iterator[service.CurrentUser]:
    """#553과 같은 사용자 잠금 아래 재판정. 일반 관리 API의 정지 검사와 별개다."""
    try:
        auth_repo.lock_users_table(db)
        db.expire_all()
        user = authorize(db, actor, gate)
        yield user
        db.commit()
    except SQLAlchemyError as exc:
        db.rollback()
        _logger.error("support_db_failure error_type=%s", type(exc).__name__)
        raise ApiError(ErrorCode.INTERNAL_ERROR) from None
    except BaseException:
        db.rollback()
        raise
