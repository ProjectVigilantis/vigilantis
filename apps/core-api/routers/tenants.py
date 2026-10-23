# ==============================================================================
# [파일 설명]  담당: 김승철 (API 계약 · BE)
# /api/v1/tenants — 고객(테넌트) 관리 라우터 (Post-MVP T-4 · 계약: schemas.api.tenants)
#
#   - 얇게 둔다: 판정 · 트랜잭션 · 응답 조립은 tenant_admin 이 맡는다.
#   - require_platform_account 가 라우터 의존성이라 본문 검증보다 먼저 돈다 — 고객사(TENANT) 계정은
#     역할과 관계없이 403 이고, 플랫폼 계정만 통과한다(#492). 이 라우터에 업무 관문을 따로 두지 않는다.
#   - 계약 경로 9개 중 7개다. 연결 확인(POST /{id}/verify · /{id}/resume)은 테넌트별 AssumeRole 과
#     함께 다음 카드에서 붙인다 — 그 전에는 경로가 없다(404).
# ==============================================================================

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

import tenant_admin
from auth.access import require_platform_account
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

_DB = Depends(get_db)
_SETTINGS = Depends(get_tenant_settings)
_CLOCK = Depends(get_clock)

router = APIRouter(
    prefix="/api/v1/tenants",
    tags=["tenants"],
    dependencies=[Depends(require_platform_account)],
)


@router.get("", response_model=TenantsResponse)
def list_tenants(db: Session = _DB) -> TenantsResponse:
    return tenant_admin.list_tenants(db)


@router.post("", status_code=201, response_model=TenantCreateResponse)
def create_tenant(
    body: TenantCreateRequest,
    db: Session = _DB,
    settings: TenantSettings = _SETTINGS,
    clock: Callable[[], datetime] = _CLOCK,
) -> TenantCreateResponse:
    return tenant_admin.create_tenant(db, body, settings, now=clock())


@router.get("/{tenant_id}", response_model=TenantDetail)
def get_tenant(
    tenant_id: str, db: Session = _DB, settings: TenantSettings = _SETTINGS
) -> TenantDetail:
    return tenant_admin.get_tenant(db, tenant_id, settings)


@router.patch("/{tenant_id}", response_model=TenantDetail)
def update_tenant(
    tenant_id: str,
    body: TenantUpdateRequest,
    db: Session = _DB,
    settings: TenantSettings = _SETTINGS,
    clock: Callable[[], datetime] = _CLOCK,
) -> TenantDetail:
    return tenant_admin.update_tenant(db, tenant_id, body, settings, now=clock())


@router.delete("/{tenant_id}", status_code=204)
def delete_tenant(tenant_id: str, db: Session = _DB) -> Response:
    tenant_admin.delete_tenant(db, tenant_id)
    return Response(status_code=204)


@router.get("/{tenant_id}/onboarding", response_model=TenantOnboardingResponse)
def get_onboarding(
    tenant_id: str, db: Session = _DB, settings: TenantSettings = _SETTINGS
) -> TenantOnboardingResponse:
    return tenant_admin.get_onboarding(db, tenant_id, settings)


@router.post("/{tenant_id}/suspend", response_model=TenantDetail)
def suspend_tenant(
    tenant_id: str,
    body: TenantSuspendRequest,
    db: Session = _DB,
    settings: TenantSettings = _SETTINGS,
    clock: Callable[[], datetime] = _CLOCK,
) -> TenantDetail:
    return tenant_admin.suspend_tenant(
        db, tenant_id, reason=body.reason, settings=settings, now=clock()
    )
