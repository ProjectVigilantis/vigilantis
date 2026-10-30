# ==============================================================================
# [파일 설명]
# TOTP 분실 복구 HTTP 어댑터의 권한 · 오류 매핑 (#515). 상태와 순서는 auth.totp_recovery 가 정한다.
#
#   - 행위자: 고객사(TENANT) ADMIN → 자기 테넌트의 TENANT 계정, CENTRAL_ADMIN → 모든 TENANT 계정.
#     TENANT_MANAGER(배정 여부와 무관) · 그 밖은 없다(account_type · platform_role 로만 판정 — role=ADMIN 이나 소속 NULL 로 추론하지 않는다).
#     access.require_admin 은 배정 고객사의 TENANT_MANAGER 도 통과시키는 고객 사용자 관리 자격일 뿐 복구 권한의 근거가 아니다 —
#     복구는 이 모듈의 관문(require_recovery_actor)과 잠금 안 재판정(make_authorize)이 따로 건다.
#   - require_recovery_actor 는 라우트 의존성이라 본문 검증 · Cognito 조립보다 먼저 돈다.
#   - make_authorize 의 콜백은 코어가 읽기 단계와 users 잠금 안에서 두 번 부른다. 행위자 세션을 매번 다시 판정하고
#     (admin_service._authorize_write: 정지 테넌트 409), 범위 밖 · PLATFORM · UNASSIGNED 대상은 없는 대상과 같은 404 다.
#   - 204 는 COMPLETED 기록뿐이다. 그 밖의 결과(제공자 거절 · 미확인 · 등록 없음 · 기록 실패)는 503 이다.
# ==============================================================================

from __future__ import annotations

from collections.abc import Callable
from uuid import UUID

from db import models
from exceptions import ApiError
from fastapi import Depends
from schemas.api.auth import AccountType, UserRole
from schemas.api.errors import ErrorCode
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from . import access, admin_service, service
from .cognito import CognitoAdapter
from .totp_recovery import (
    Authorize,
    RecoveryRejection,
    TotpRecoveryRejected,
    reset_totp,
)

_REJECTION_CODES = {
    RecoveryRejection.IDENTITY_NOT_CONFIRMED: ErrorCode.REQUEST_VALIDATION_FAILED,
    RecoveryRejection.TARGET_NOT_FOUND: ErrorCode.USER_NOT_FOUND,
    RecoveryRejection.TARGET_INELIGIBLE: ErrorCode.USER_STATE_CONFLICT,
    RecoveryRejection.SELF_TARGET: ErrorCode.AUTH_FORBIDDEN,
    RecoveryRejection.OPERATION_CONFLICT: ErrorCode.USER_STATE_CONFLICT,
    RecoveryRejection.PROVIDER_IDENTITY_MISMATCH: ErrorCode.AUTH_PROVIDER_UNAVAILABLE,
    RecoveryRejection.BUSY: ErrorCode.USER_STATE_CONFLICT,
}


def _is_tenant_admin(user: service.CurrentUser) -> bool:
    return user.account_type is AccountType.TENANT and user.role is UserRole.ADMIN


def _allowed_actor(user: service.CurrentUser) -> bool:
    return _is_tenant_admin(user) or access.is_central_admin(user)


_USER = Depends(access.require_user)


def require_recovery_actor(user: service.CurrentUser = _USER) -> service.CurrentUser:
    """TENANT ADMIN · CENTRAL_ADMIN 만 통과한다. TENANT_MANAGER 는 배정과 무관하게 403 AUTH_FORBIDDEN."""
    if not _allowed_actor(user):
        raise ApiError(ErrorCode.AUTH_FORBIDDEN)
    return user


def make_authorize(actor: admin_service.Actor) -> Authorize:
    def authorize(db: Session, target: models.AppUser | None) -> str:
        db.expire_all()  # 잠금 안의 재판정은 최신 행을 본다
        admin = admin_service._authorize_write(db, actor)
        try:
            if not _allowed_actor(admin):
                raise ApiError(ErrorCode.AUTH_FORBIDDEN)
            if target is not None and (
                target.account_type is not AccountType.TENANT
                or (_is_tenant_admin(admin) and target.tenant_id != admin.tenant_id)
            ):
                raise ApiError(ErrorCode.USER_NOT_FOUND)
        except BaseException:
            db.rollback()
            raise
        return admin.user_id

    return authorize


def recover_totp(
    engine: Engine,
    actor: admin_service.Actor,
    get_adapter: Callable[[], CognitoAdapter],
    *,
    user_id: str,
    operation_id: UUID,
    identity_verified: bool,
) -> None:
    """복구가 COMPLETED 로 기록됐을 때만 반환한다. 그 밖은 ApiError."""
    try:
        result = reset_totp(
            engine,
            operation_id=operation_id,
            target_user_id=user_id,
            identity_verified=identity_verified,
            authorize=make_authorize(actor),
            get_adapter=get_adapter,
            clock=actor.clock,
        )
    except TotpRecoveryRejected as exc:
        raise ApiError(_REJECTION_CODES[exc.reason]) from None
    if not result.completed:
        raise ApiError(ErrorCode.AUTH_PROVIDER_UNAVAILABLE)
