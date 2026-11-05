# ==============================================================================
# [파일 설명]  담당: 안성일 (BE · T-16)
# /api/v1/me/subscription — 고객사 계정의 자기 구독 라우터 (Post-MVP T-16 · #528 · 계약: schemas.api.subscriptions)
#
#   - 얇게 둔다: 판정 · 잠금 · 트랜잭션 · 투영은 subscription_workflow 가 맡는다.
#   - 관문 순서: (main 의 포함 시점) Origin(상태 변경만) → 세션 → (라우터) 고객사 계정(TENANT) → (쓰기) 고객사 중앙계정(ADMIN).
#     플랫폼(PLATFORM) 계정은 역할과 관계없이 403 이다 — 「자기 구독」이 없다. 전부 본문 검증 · DB 쓰기보다 먼저다.
#   - 대상 테넌트는 세션의 소속(tenant_id)뿐이다. 경로 · 본문에 테넌트 · 날짜 · 금액 · 구매자 · 결과를 받지 않는다.
#   - 쓰기는 테넌트 행을 잠근 뒤 세션을 다시 판정한다(get_reauthorizer) — 그 사이 회수 · 비활성 · 강등 · 소속
#     변경된 요청자는 401 · 403 이고 아무것도 쓰지 않는다.
#   - 정지(SUSPENDED) 고객사도 조회와 갱신 끄기는 한다 — 정지는 운영 중단이지 계약 해지가 아니다.
#     구매 · 중단은 지금 상태가 받지 않아 409 다(TENANT_SUSPENDED 로 일괄 차단하지 않는다).
# ==============================================================================

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

import subscription_workflow
from auth import service as auth_service
from auth.access import require_admin, require_user
from auth.http import SESSION_COOKIE, get_clock
from config import (
    AuthSettings,
    SubscriptionSettings,
    get_auth_settings,
    get_subscription_settings,
)
from db.session import get_db
from exceptions import ApiError
from fastapi import APIRouter, Depends, Request
from schemas.api.auth import AccountType, UserRole
from schemas.api.errors import ErrorCode
from schemas.api.subscriptions import (
    CustomerSubscriptionResponse,
    SubscriptionPurchaseRequest,
    SubscriptionStopRequest,
)
from sqlalchemy.orm import Session

_DB = Depends(get_db)
_SETTINGS = Depends(get_subscription_settings)
_AUTH_SETTINGS = Depends(get_auth_settings)
_CLOCK = Depends(get_clock)
_USER = Depends(require_user)


def require_tenant_account(user: auth_service.CurrentUser = _USER) -> auth_service.CurrentUser:
    """고객사(TENANT) 계정만 — 소속이 비어 있으면 플랫폼 범위로 넘어가지 않고 403 이다."""
    if user.account_type is not AccountType.TENANT or user.tenant_id is None:
        raise ApiError(ErrorCode.AUTH_FORBIDDEN)
    return user


_TENANT_USER = Depends(require_tenant_account)


def require_tenant_admin(
    user: auth_service.CurrentUser = _TENANT_USER,
) -> auth_service.CurrentUser:
    return require_admin(user)


_TENANT_ADMIN = Depends(require_tenant_admin)


def get_reauthorizer(
    request: Request,
    db: Session = _DB,
    user: auth_service.CurrentUser = _TENANT_ADMIN,
    settings: AuthSettings = _AUTH_SETTINGS,
    clock: Callable[[], datetime] = _CLOCK,
) -> Callable[[], None]:
    """쓰기 직전(테넌트 행 잠금 뒤)의 재판정 — 같은 사용자 · 같은 소속의 고객사 중앙계정 세션이 아직 유효한가.

    세션이 무효면 401(AuthRequiredError · rollback), 역할 · 계정 종류 · 소속이 바뀌었으면 403(rollback)이다.
    """
    token = request.cookies.get(SESSION_COOKIE)

    def reauthorize() -> None:
        db.expire_all()
        current = auth_service.resolve_current_user(
            db, token, now=clock(), idle_seconds=settings.AUTH_SESSION_IDLE_SECONDS, touch=False
        )
        if (
            current.user_id != user.user_id
            or current.account_type is not AccountType.TENANT
            or current.tenant_id != user.tenant_id
            or current.role is not UserRole.ADMIN
        ):
            db.rollback()
            raise ApiError(ErrorCode.AUTH_FORBIDDEN)

    return reauthorize


_REAUTHORIZE = Depends(get_reauthorizer)

router = APIRouter(
    prefix="/api/v1/me/subscription",
    tags=["subscriptions"],
    dependencies=[_TENANT_USER],
)


@router.get("", response_model=CustomerSubscriptionResponse)
def get_my_subscription(
    db: Session = _DB,
    user: auth_service.CurrentUser = _TENANT_USER,
    settings: SubscriptionSettings = _SETTINGS,
    clock: Callable[[], datetime] = _CLOCK,
) -> CustomerSubscriptionResponse:
    return subscription_workflow.get_subscription(
        db,
        user.tenant_id,
        can_manage=user.role is UserRole.ADMIN,
        settings=settings,
        now=clock(),
    )


@router.post("/purchase", status_code=202, response_model=CustomerSubscriptionResponse)
def purchase_my_subscription(
    body: SubscriptionPurchaseRequest,
    db: Session = _DB,
    user: auth_service.CurrentUser = _TENANT_ADMIN,
    reauthorize: Callable[[], None] = _REAUTHORIZE,
    settings: SubscriptionSettings = _SETTINGS,
    clock: Callable[[], datetime] = _CLOCK,
) -> CustomerSubscriptionResponse:
    return subscription_workflow.purchase_subscription(
        db,
        user.tenant_id,
        body,
        requested_by_user_id=user.user_id,
        authorize=reauthorize,
        settings=settings,
        now=clock(),
    )


@router.post("/cancel", response_model=CustomerSubscriptionResponse)
def cancel_my_renewal(
    db: Session = _DB,
    user: auth_service.CurrentUser = _TENANT_ADMIN,
    reauthorize: Callable[[], None] = _REAUTHORIZE,
    settings: SubscriptionSettings = _SETTINGS,
    clock: Callable[[], datetime] = _CLOCK,
) -> CustomerSubscriptionResponse:
    return subscription_workflow.cancel_renewal(
        db, user.tenant_id, authorize=reauthorize, settings=settings, now=clock()
    )


@router.post("/stop", response_model=CustomerSubscriptionResponse)
def stop_my_service(
    body: SubscriptionStopRequest,
    db: Session = _DB,
    user: auth_service.CurrentUser = _TENANT_ADMIN,
    reauthorize: Callable[[], None] = _REAUTHORIZE,
    settings: SubscriptionSettings = _SETTINGS,
    clock: Callable[[], datetime] = _CLOCK,
) -> CustomerSubscriptionResponse:
    return subscription_workflow.stop_service(
        db, user.tenant_id, body, authorize=reauthorize, settings=settings, now=clock()
    )
