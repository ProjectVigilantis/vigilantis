# ==============================================================================
# [파일 설명]
# /api/v1/groups — 고객사 그룹 라우터 (#572 2번 · 계약: schemas.api.groups)
#
#   - 얇게 둔다: 판정 · 잠금 · 트랜잭션 · 응답 조립은 tenant_groups 서비스가 맡는다(쓰기 직전 행위자를 다시 판정).
#   - require_platform_account 가 라우터 의존성이라 고객사(TENANT) 계정은 역할과 관계없이 403 이다. 조회는 플랫폼
#     계정 전부(관리자 직원계정은 담당 그룹만), 생성 · 삭제는 require_central_admin 이 본문 검증보다 먼저 막는다.
#   - 고객사를 그룹에 넣고 빼기는 /api/v1/tenants/{tenant_id}/group, 직원의 그룹 배정은 /api/v1/staff/{user_id}/groups 다.
# ==============================================================================

from __future__ import annotations

import tenant_groups
from auth import admin_service, service
from auth.access import require_central_admin, require_platform_account
from db.session import get_db
from fastapi import APIRouter, Depends, Response
from schemas.api.groups import GroupCreateRequest, GroupItem, GroupsResponse
from sqlalchemy.orm import Session

from routers.users import get_actor

_DB = Depends(get_db)
_ACTOR = Depends(get_actor)
_PLATFORM = Depends(require_platform_account)
_CENTRAL = [Depends(require_central_admin)]

router = APIRouter(
    prefix="/api/v1/groups",
    tags=["groups"],
    dependencies=[Depends(require_platform_account)],
)


@router.get("", response_model=GroupsResponse)
def list_groups(db: Session = _DB, user: service.CurrentUser = _PLATFORM) -> GroupsResponse:
    return tenant_groups.list_groups(db, user)


@router.post("", status_code=201, response_model=GroupItem, dependencies=_CENTRAL)
def create_group(
    body: GroupCreateRequest, db: Session = _DB, actor: admin_service.Actor = _ACTOR
) -> GroupItem:
    return tenant_groups.create_group(db, actor, body)


@router.delete("/{group_id}", status_code=204, dependencies=_CENTRAL)
def delete_group(group_id: str, db: Session = _DB, actor: admin_service.Actor = _ACTOR) -> Response:
    tenant_groups.delete_group(db, actor, group_id)
    return Response(status_code=204)
