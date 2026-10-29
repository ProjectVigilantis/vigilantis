# ==============================================================================
# [파일 설명]
# /api/v1/staff — 플랫폼 직원 관리 라우터 (중앙 관리자 전용). (#492 PR 5, 계약: schemas.api.staff)
#
#   - 얇게 둔다: 판정 · 잠금 · 트랜잭션은 auth.staff_service 가 맡는다(쓰기 직전 행위자를 다시 판정).
#   - require_central_admin 이 라우터 의존성이라 본문 검증보다 먼저 돈다. 직원 생성 경로는 없다(운영 CLI).
# ==============================================================================

from __future__ import annotations

from auth import admin_service, staff_service
from auth.access import require_central_admin
from db.session import get_db
from fastapi import APIRouter, Depends, Query, Response
from schemas.api.staff import (
    StaffResponse,
    StaffRoleRequest,
    StaffStatusRequest,
    StaffTenantsRequest,
    StaffTenantsResponse,
)
from schemas.api.users import UserEventsResponse
from sqlalchemy.orm import Session

from routers.users import get_actor

_DB = Depends(get_db)
_ACTOR = Depends(get_actor)

router = APIRouter(
    prefix="/api/v1/staff", tags=["staff"], dependencies=[Depends(require_central_admin)]
)


@router.get("", response_model=StaffResponse)
def list_staff(db: Session = _DB, actor: admin_service.Actor = _ACTOR) -> StaffResponse:
    return staff_service.list_staff(db, actor)


@router.put("/{user_id}/role", status_code=204)
def change_platform_role(
    user_id: str,
    body: StaffRoleRequest,
    db: Session = _DB,
    actor: admin_service.Actor = _ACTOR,
) -> Response:
    staff_service.change_platform_role(db, actor, user_id, body.platform_role)
    return Response(status_code=204)


@router.put("/{user_id}/status", status_code=204)
def change_status(
    user_id: str,
    body: StaffStatusRequest,
    db: Session = _DB,
    actor: admin_service.Actor = _ACTOR,
) -> Response:
    staff_service.change_status(db, actor, user_id, body.status)
    return Response(status_code=204)


@router.get("/{user_id}/tenants", response_model=StaffTenantsResponse)
def get_tenants(
    user_id: str, db: Session = _DB, actor: admin_service.Actor = _ACTOR
) -> StaffTenantsResponse:
    return staff_service.get_tenants(db, actor, user_id)


@router.put("/{user_id}/tenants", status_code=204)
def replace_tenants(
    user_id: str,
    body: StaffTenantsRequest,
    db: Session = _DB,
    actor: admin_service.Actor = _ACTOR,
) -> Response:
    staff_service.replace_tenants(db, actor, user_id, body.tenant_ids)
    return Response(status_code=204)


@router.get("/{user_id}/events", response_model=UserEventsResponse)
def list_events(
    user_id: str,
    limit: int = Query(default=50, ge=1, le=100),
    db: Session = _DB,
    actor: admin_service.Actor = _ACTOR,
) -> UserEventsResponse:
    return staff_service.list_events(db, actor, user_id, limit=limit)
