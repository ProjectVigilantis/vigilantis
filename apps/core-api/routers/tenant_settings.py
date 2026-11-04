# ==============================================================================
# [파일 설명]
# /api/v1/tenants/{tenant_id}/settings — 고객 설정 라우터 (T-7 · 계약: schemas.api.settings)
#
#   - 얇게 둔다: 판정 · 트랜잭션 · 공급자 호출은 tenant_settings Workflow 가 맡는다(SQL · AWS 없음).
#   - 세션(Origin · require_user)은 main 이 포함 시점에 건다. 테넌트 관리 라우터(routers/tenants.py)의
#     플랫폼 전용 의존성과 분리된 라우터라 고객사 계정도 들어온다 — 범위는 Workflow 가 auth.access 로 건다.
#   - PATCH 는 require_admin(고객사 ADMIN · 테넌트 관리 등급)이 본문 검증보다 먼저 돈다(그 밖 403).
# ==============================================================================

from __future__ import annotations

from collections.abc import Callable
from functools import lru_cache

import tenant_settings
from auth import admin_service, service
from auth.access import require_admin, require_user
from config import get_settings
from db.session import get_db
from fastapi import APIRouter, Depends
from schemas.api.settings import TenantSettings as TenantSettingsResponse
from schemas.api.settings import TenantSettingsUpdateRequest
from services.aws.settings_notifications import SettingsResources
from sqlalchemy.orm import Session

from routers.users import get_actor

_DB = Depends(get_db)
_ACTOR = Depends(get_actor)
_USER = Depends(require_user)

router = APIRouter(prefix="/api/v1/tenants", tags=["tenant-settings"])


@lru_cache
def _process_resources() -> SettingsResources:
    settings = get_settings()
    return SettingsResources(
        connect_timeout=settings.NOTIFICATION_CONNECT_TIMEOUT_SECONDS,
        read_timeout=settings.NOTIFICATION_READ_TIMEOUT_SECONDS,
    )


def get_settings_resources() -> Callable[[], SettingsResources]:
    """지연 조립 — 실제 공급자 호출이 필요할 때만 만든다. 테스트가 재정의한다."""
    return _process_resources


def get_write_options() -> tenant_settings.WriteOptions:
    settings = get_settings()
    return tenant_settings.WriteOptions(
        notifications_enabled=settings.NOTIFICATION_ENABLED,
        retire_secret_delay_seconds=(
            settings.NOTIFICATION_LEASE_SECONDS + tenant_settings.RETIRE_MARGIN_SECONDS
        ),
    )


_RESOURCES = Depends(get_settings_resources)
_OPTIONS = Depends(get_write_options)


@router.get("/{tenant_id}/settings", response_model=TenantSettingsResponse)
def get_tenant_settings(
    tenant_id: str, db: Session = _DB, user: service.CurrentUser = _USER
) -> TenantSettingsResponse:
    return tenant_settings.read_settings(db, user, tenant_id)


@router.patch(
    "/{tenant_id}/settings",
    response_model=TenantSettingsResponse,
    dependencies=[Depends(require_admin)],
)
def update_tenant_settings(
    tenant_id: str,
    body: TenantSettingsUpdateRequest,
    db: Session = _DB,
    actor: admin_service.Actor = _ACTOR,
    resources: Callable[[], SettingsResources] = _RESOURCES,
    options: tenant_settings.WriteOptions = _OPTIONS,
) -> TenantSettingsResponse:
    return tenant_settings.update_settings(
        db, actor, tenant_id, body, resources=resources, options=options,
    )
