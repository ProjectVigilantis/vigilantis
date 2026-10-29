# ==============================================================================
# [파일 설명]  담당: 김승철 (API 계약 · BE)
# /api/v1/tenants — 고객(테넌트) 관리 라우터 (Post-MVP T-4 · 계약: schemas.api.tenants)
#
#   - 얇게 둔다: 판정 · 트랜잭션 · 응답 조립은 tenant_admin 이 맡는다.
#   - require_platform_account 가 라우터 의존성이라 본문 검증보다 먼저 돈다 — 고객사(TENANT) 계정은
#     역할과 관계없이 403 이고, 플랫폼 계정만 통과한다(#492). 역할은 CENTRAL_ADMIN · TENANT_MANAGER 뿐이다.
#     중앙 관리자는 전체 고객, 담당자는 배정된 고객만 다룬다(배정 없으면 목록 빈 값 · 상세 404). 범위 밖 ID 는 404.
#     생성은 중앙 관리자만 한다(새 고객은 담당자의 기존 범위에 없다 · 자동 배정 없음).
#     쓰기는 서비스가 users 잠금 안에서 세션 · 권한 · 배정을 다시 판정한다(tenant_admin._management).
#   - 계약 경로 9개 중 7개다. 연결 확인(POST /{id}/verify · /{id}/resume)은 이 라우터에 없다(404) —
#     테넌트별 AssumeRole 이 있어야 하는 경로다.
# ==============================================================================

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

import tenant_admin
from auth import admin_service, service
from auth.access import (
    require_central_admin,
    require_platform_account,
    require_tenant_management,
)
from auth.http import get_clock
from config import TenantSettings, get_tenant_settings
from db.session import get_db
from fastapi import APIRouter, Depends, Response
from schemas.api.tenants import (
    TenantCreateRequest,
    TenantCreateResponse,
    TenantDetail,
    TenantOnboardingResponse,
    TenantsResponse,
    TenantSuspendRequest,
    TenantUpdateRequest,
)
from sqlalchemy.orm import Session

from routers.users import get_actor

_DB = Depends(get_db)
_SETTINGS = Depends(get_tenant_settings)
_CLOCK = Depends(get_clock)
_PLATFORM = Depends(require_platform_account)
_MANAGER = [Depends(require_tenant_management)]


_ACTOR = Depends(get_actor)

router = APIRouter(
    prefix="/api/v1/tenants",
    tags=["tenants"],
    dependencies=[Depends(require_platform_account)],
)


@router.get("", response_model=TenantsResponse)
def list_tenants(db: Session = _DB, user: service.CurrentUser = _PLATFORM) -> TenantsResponse:
    return tenant_admin.list_tenants(db, user)


@router.post(
    "",
    status_code=201,
    response_model=TenantCreateResponse,
    dependencies=[Depends(require_central_admin)],
)
def create_tenant(
    body: TenantCreateRequest,
    db: Session = _DB,
    actor: admin_service.Actor = _ACTOR,
    settings: TenantSettings = _SETTINGS,
    clock: Callable[[], datetime] = _CLOCK,
) -> TenantCreateResponse:
    return tenant_admin.create_tenant(db, actor, body, settings, now=clock())


@router.get("/{tenant_id}", response_model=TenantDetail)
def get_tenant(
    tenant_id: str,
    db: Session = _DB,
    settings: TenantSettings = _SETTINGS,
    user: service.CurrentUser = _PLATFORM,
) -> TenantDetail:
    return tenant_admin.get_tenant(db, tenant_id, settings, user)


@router.patch("/{tenant_id}", response_model=TenantDetail, dependencies=_MANAGER)
def update_tenant(
    tenant_id: str,
    body: TenantUpdateRequest,
    db: Session = _DB,
    actor: admin_service.Actor = _ACTOR,
    settings: TenantSettings = _SETTINGS,
    clock: Callable[[], datetime] = _CLOCK,
) -> TenantDetail:
    return tenant_admin.update_tenant(db, actor, tenant_id, body, settings, now=clock())


@router.delete("/{tenant_id}", status_code=204, dependencies=_MANAGER)
def delete_tenant(
    tenant_id: str, db: Session = _DB, actor: admin_service.Actor = _ACTOR
) -> Response:
    tenant_admin.delete_tenant(db, actor, tenant_id)
    return Response(status_code=204)


@router.get(
    "/{tenant_id}/onboarding", response_model=TenantOnboardingResponse, dependencies=_MANAGER
)
def get_onboarding(
    tenant_id: str,
    db: Session = _DB,
    settings: TenantSettings = _SETTINGS,
    user: service.CurrentUser = _PLATFORM,
) -> TenantOnboardingResponse:
    return tenant_admin.get_onboarding(db, tenant_id, settings, user)


@router.post("/{tenant_id}/suspend", response_model=TenantDetail, dependencies=_MANAGER)
def suspend_tenant(
    tenant_id: str,
    body: TenantSuspendRequest,
    db: Session = _DB,
    actor: admin_service.Actor = _ACTOR,
    settings: TenantSettings = _SETTINGS,
    clock: Callable[[], datetime] = _CLOCK,
) -> TenantDetail:
    return tenant_admin.suspend_tenant(
        db, actor, tenant_id, reason=body.reason, settings=settings, now=clock()
    )
