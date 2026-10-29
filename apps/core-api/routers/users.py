# ==============================================================================
# [파일 설명]
# /api/v1/users — ADMIN 사용자 관리 라우터. (#492 PR 3, 계약: schemas.api.users)
#
#   - 얇게 둔다: 판정 · 잠금 · 트랜잭션은 auth.admin_service 가 맡는다.
#   - require_admin 이 라우터 의존성이라 본문 검증 · Cognito 어댑터 조립(설정 없으면 503)보다 먼저
#     돈다. 비ADMIN 은 설정과 무관하게 403 이다. Cognito 의존성은 초대 · 재발송에만 있다.
#   - 관리 범위(PLATFORM 전체 · TENANT ADMIN 자기 테넌트)와 정지 테넌트 거절은 서비스가 행위자를 다시
#     판정해 건다(조회 포함) — 라우터는 범위를 계산하지 않는다(#492).
#   - 초대 · 재발송의 Cognito 어댑터는 지연 조립이다(get_cognito_adapter_factory). 서비스가 행위자 · 대상 ·
#     테넌트 사전 판정(422 · 404 · 409)을 마친 뒤에만 조립하므로, 설정이 비어도 그 판정이 503 보다 먼저다.
# ==============================================================================

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from functools import partial

from auth import admin_service
from auth.access import require_admin
from auth.cognito import CognitoAdapter
from auth.http import SESSION_COOKIE, get_clock, get_cognito_adapter
from config import AuthSettings, get_auth_settings
from db.session import get_db
from fastapi import APIRouter, Depends, Query, Request, Response
from schemas.api.users import (
    UserEventsResponse,
    UserInviteRequest,
    UserListItem,
    UserRoleRequest,
    UsersResponse,
    UserStatusRequest,
)
from sqlalchemy.orm import Session

_DB = Depends(get_db)
_SETTINGS = Depends(get_auth_settings)
_CLOCK = Depends(get_clock)

router = APIRouter(
    prefix="/api/v1/users", tags=["users"], dependencies=[Depends(require_admin)]
)


def get_cognito_adapter_factory(
    request: Request, settings: AuthSettings = _SETTINGS
) -> Callable[[], CognitoAdapter]:
    """지연 조립 — 부를 때 get_cognito_adapter 와 같이 조립한다(설정이 비면 그때 503). 테스트가 재정의한다."""
    return partial(get_cognito_adapter, request, settings)


_COGNITO = Depends(get_cognito_adapter_factory)


def get_actor(
    request: Request,
    settings: AuthSettings = _SETTINGS,
    clock: Callable[[], datetime] = _CLOCK,
) -> admin_service.Actor:
    return admin_service.Actor(
        token=request.cookies.get(SESSION_COOKIE),
        idle_seconds=settings.AUTH_SESSION_IDLE_SECONDS,
        clock=clock,
    )


_ACTOR = Depends(get_actor)


@router.get("", response_model=UsersResponse)
def list_users(db: Session = _DB, actor: admin_service.Actor = _ACTOR) -> UsersResponse:
    return admin_service.list_users(db, actor)


@router.post("", status_code=201, response_model=UserListItem)
def invite_user(
    body: UserInviteRequest,
    db: Session = _DB,
    actor: admin_service.Actor = _ACTOR,
    get_adapter: Callable[[], CognitoAdapter] = _COGNITO,
) -> UserListItem:
    return admin_service.invite_user(
        db,
        actor,
        get_adapter,
        email=body.email,
        role=body.role,
        tenant_id=body.tenant_id,
        recover_existing=body.recover_existing,
    )


@router.post("/{user_id}/invite/resend", status_code=204)
def resend_invite(
    user_id: str,
    db: Session = _DB,
    actor: admin_service.Actor = _ACTOR,
    get_adapter: Callable[[], CognitoAdapter] = _COGNITO,
) -> Response:
    admin_service.resend_invite(db, actor, get_adapter, user_id)
    return Response(status_code=204)


@router.put("/{user_id}/role", status_code=204)
def change_role(
    user_id: str,
    body: UserRoleRequest,
    db: Session = _DB,
    actor: admin_service.Actor = _ACTOR,
) -> Response:
    admin_service.change_role(db, actor, user_id, body.role)
    return Response(status_code=204)


@router.put("/{user_id}/status", status_code=204)
def change_status(
    user_id: str,
    body: UserStatusRequest,
    db: Session = _DB,
    actor: admin_service.Actor = _ACTOR,
) -> Response:
    admin_service.change_status(db, actor, user_id, body.status)
    return Response(status_code=204)


@router.post("/{user_id}/sessions/revoke", status_code=204)
def revoke_sessions(
    user_id: str,
    db: Session = _DB,
    actor: admin_service.Actor = _ACTOR,
) -> Response:
    admin_service.revoke_sessions(db, actor, user_id)
    return Response(status_code=204)


@router.get("/{user_id}/events", response_model=UserEventsResponse)
def list_events(
    user_id: str,
    limit: int = Query(default=50, ge=1, le=100),
    db: Session = _DB,
    actor: admin_service.Actor = _ACTOR,
) -> UserEventsResponse:
    return admin_service.list_events(db, actor, user_id, limit=limit)
