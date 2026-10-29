# ==============================================================================
# [파일 설명]  담당: 김승철 (API 계약 · BE)
# /api/v1/tenants 의 판정 · 트랜잭션 · 응답 조립 (Post-MVP T-4 · 계약: schemas.api.tenants)
#
#   - 라우터는 얇게 두고 여기서 commit 한다. **응답을 다 만든 뒤에 commit 한다** — 온보딩 링크 설정이
#     없어 응답을 못 만들면(503) 쓰기도 남지 않는다.
#   - 상태 전이의 받는 상태는 저장소(db.repositories.tenants)가 계약 상수로 판정한다. 0행이면 다시 읽어
#     없으면 404 TENANT_NOT_FOUND, 있으면 409 TENANT_STATE_CONFLICT 다.
#   - 연결 확인(verify · resume)은 여기 없다 — 테넌트별 AssumeRole 이 있어야 하는 동작이다.
#   - external_id 는 생성 응답과 재발급(onboarding)에서만 내보낸다. 상세 응답에는 칸이 없다(계약).
# ==============================================================================

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from urllib.parse import urlencode

from auth import admin_service, service
from auth.access import is_central_admin, is_tenant_manager, tenant_scope_ids
from config import TenantSettings
from db import models
from db.repositories import auth as auth_repo
from db.repositories import tenants as tenants_repo
from exceptions import ApiError
from identifiers import canonical_id
from schemas.api.errors import ErrorCode
from schemas.api.tenants import (
    TenantCreateRequest,
    TenantCreateResponse,
    TenantDetail,
    TenantListItem,
    TenantOnboardingResponse,
    TenantsResponse,
    TenantStatus,
    TenantSubscription,
    TenantUpdateRequest,
    TenantVerification,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

# 같은 계정의 두 번째 테넌트를 DB 유일성이 거절할 때의 제약 이름(db.session NAMING_CONVENTION)
_ACCOUNT_UNIQUE = "uq_tenants_aws_account_id"

# CloudFormation 빠른 생성 링크. 템플릿(INFRA)이 onboarding_url 의 param_* 이름 그대로 파라미터를 받는다
# — 이름을 바꾸면 템플릿과 test_tenants_api 의 링크 파라미터 단언을 함께 바꾼다.
_QUICK_CREATE = (
    "https://{region}.console.aws.amazon.com/cloudformation/home?region={region}"
    "#/stacks/quickcreate?{query}"
)
ONBOARDING_STACK_NAME = "vigilantis-access"


# --- 응답 조립 ------------------------------------------------------------------


def onboarding_url(tenant: models.Tenant, settings: TenantSettings) -> str:
    """고객이 역할을 만드는 빠른 생성 링크. 설정이 없으면 503 — 링크 없는 응답을 지어내지 않는다."""
    if not settings.onboarding_ready():
        raise ApiError(ErrorCode.TENANT_ONBOARDING_UNAVAILABLE)
    query = urlencode({
        "templateURL": settings.TENANT_ONBOARDING_TEMPLATE_URL,
        "stackName": ONBOARDING_STACK_NAME,
        "param_ExternalId": tenant.external_id,
        "param_PlatformPrincipalArn": settings.PLATFORM_PRINCIPAL_ARN,
        "param_RoleName": tenant.access_role_arn.split(":role/", 1)[1],
    })
    # 역할은 전역이라 스택 리전은 어디든 같다 — 고객이 관제받는 첫 리전에 둔다
    return _QUICK_CREATE.format(region=tenant.regions[0], query=query)


def _verification(tenant: models.Tenant) -> TenantVerification | None:
    if tenant.last_verification_result is None:
        return None
    return TenantVerification(
        result=tenant.last_verification_result,
        verified_at=tenant.last_verified_at,
        reason_code=tenant.last_verification_reason_code,
    )


def _subscription(tenant: models.Tenant) -> TenantSubscription | None:
    if tenant.plan is None and tenant.period_start is None and tenant.period_end is None:
        return None
    return TenantSubscription(
        plan=tenant.plan, period_start=tenant.period_start, period_end=tenant.period_end
    )


def _list_item(row: tenants_repo.TenantWithCounts) -> TenantListItem:
    tenant = row.tenant
    return TenantListItem(
        tenant_id=tenant.tenant_id,
        name=tenant.name,
        status=tenant.status,
        aws_account_id=tenant.aws_account_id,
        last_verification=_verification(tenant),
        asset_count=row.asset_count,
        open_incident_count=row.open_incident_count,
    )


def _detail(row: tenants_repo.TenantWithCounts, settings: TenantSettings) -> TenantDetail:
    tenant = row.tenant
    pending = tenant.status is TenantStatus.PENDING_CONNECTION
    return TenantDetail(
        **_list_item(row).model_dump(),
        access_role_arn=tenant.access_role_arn,
        regions=tenant.regions,
        collect_vpc_ids=tenant.collect_vpc_ids,
        auto_action_enabled=tenant.auto_action_enabled,
        onboarding_url=onboarding_url(tenant, settings) if pending else None,
        suspended_at=tenant.suspended_at,
        suspended_reason=tenant.suspended_reason,
        subscription=_subscription(tenant),
        created_at=tenant.created_at,
        updated_at=tenant.updated_at,
    )


# --- 조회 ----------------------------------------------------------------------


def _load(db: Session, tenant_id: str, user: service.CurrentUser) -> tenants_repo.TenantWithCounts:
    """형식이 어긋난 식별자도 404 다 — DB 캐스트 오류(500)를 막는다(routers/incidents 와 같은 결).
    담당 범위 밖 고객도 404 다(존재 여부를 드러내지 않는다) — 범위는 SQL 조건으로 건다."""
    stored_id = canonical_id(tenant_id)
    row = (
        None
        if stored_id is None
        else tenants_repo.get_tenant(db, stored_id, scope_ids=tenant_scope_ids(user))
    )
    if row is None:
        raise ApiError(ErrorCode.TENANT_NOT_FOUND)
    return row


def _rejected(db: Session, tenant_id: str, user: service.CurrentUser) -> ApiError:
    """전이가 0행이었다 — 없어서인지(404), 받는 상태가 아니어서인지(409) 다시 읽어 가른다.
    0행이라 쓴 것이 없고, 저장소의 다시 읽기는 DB 값으로 덮어 읽는다(populate_existing)."""
    _load(db, tenant_id, user)
    return ApiError(ErrorCode.TENANT_STATE_CONFLICT)


def list_tenants(db: Session, user: service.CurrentUser) -> TenantsResponse:
    rows = tenants_repo.list_tenants(db, scope_ids=tenant_scope_ids(user))
    return TenantsResponse(tenants=[_list_item(row) for row in rows])


def get_tenant(
    db: Session, tenant_id: str, settings: TenantSettings, user: service.CurrentUser
) -> TenantDetail:
    """담당 고객(중앙 관리자는 전체)의 상세만 준다. PENDING_CONNECTION 상세의 onboarding_url 도 범위 안이면 준다."""
    return _detail(_load(db, tenant_id, user), settings)


def get_onboarding(
    db: Session, tenant_id: str, settings: TenantSettings, user: service.CurrentUser
) -> TenantOnboardingResponse:
    """모든 상태에서 받는다 — 생성 때 정한 external_id 를 그대로 돌려주는 재발급이다. 담당 범위 안에서만."""
    tenant = _load(db, tenant_id, user).tenant
    return TenantOnboardingResponse(
        onboarding_url=onboarding_url(tenant, settings), external_id=tenant.external_id
    )


# --- 쓰기 ----------------------------------------------------------------------


@contextmanager
def _management(db: Session, actor: admin_service.Actor) -> Iterator[service.CurrentUser]:
    """관리 쓰기의 경계 — users 잠금 → 캐시 비움 → 세션 · 권한 재판정 → (본문). 예외는 전부 rollback 이다.
    의존성이 본 신원은 스냅샷일 뿐이라, 잠금 안에서 다시 판정해야 그 사이 회수(역할 강등 · 세션 폐기)된
    행위자가 쓰지 못한다. 본문의 commit 은 각 쓰기 함수가 한다. 이 안에는 외부 호출이 없다."""
    try:
        auth_repo.lock_users_table(db)
        db.expire_all()
        user = service.resolve_current_user(
            db, actor.token, now=actor.clock(), idle_seconds=actor.idle_seconds, touch=False
        )
        if not is_tenant_manager(user):
            raise ApiError(ErrorCode.AUTH_FORBIDDEN)
        yield user
    except BaseException:
        db.rollback()
        raise


def _commit_after(db: Session, build):
    """응답을 만든 뒤에만 commit 한다. 만들다 실패하면(503 등) 이 요청의 쓰기를 되돌린다 —
    세션을 닫는 쪽(get_db)의 정리에 기대지 않는다."""
    try:
        response = build()
    except Exception:
        db.rollback()
        raise
    db.commit()
    return response


def create_tenant(
    db: Session, actor: admin_service.Actor, body: TenantCreateRequest, settings: TenantSettings, *, now: datetime
) -> TenantCreateResponse:
    with _management(db, actor) as user:
        # 새 고객은 어떤 담당자의 기존 범위에도 없다 — 생성은 중앙 관리자만(자동 배정 없음)
        if not is_central_admin(user):
            raise ApiError(ErrorCode.AUTH_FORBIDDEN)
        try:
            # SAVEPOINT — 유일성 거절이 이 쓰기만 되돌리고 세션은 계속 쓸 수 있게 둔다
            with db.begin_nested():
                tenant = tenants_repo.create_tenant(
                    db,
                    name=body.name,
                    aws_account_id=body.aws_account_id,
                    regions=body.regions,
                    collect_vpc_ids=body.collect_vpc_ids,
                    access_role_arn=body.access_role_arn,
                    now=now,
                )
        except IntegrityError as exc:
            if getattr(getattr(exc.orig, "diag", None), "constraint_name", None) == _ACCOUNT_UNIQUE:
                raise ApiError(ErrorCode.TENANT_ACCOUNT_CONFLICT) from exc
            raise
        # 새 테넌트에는 자산 · 인시던트가 없다 — 다시 세지 않는다
        return _commit_after(db, lambda: TenantCreateResponse(
            tenant=_detail(tenants_repo.TenantWithCounts(tenant, 0, 0), settings),
            external_id=tenant.external_id,
        ))


def update_tenant(
    db: Session,
    actor: admin_service.Actor,
    tenant_id: str,
    body: TenantUpdateRequest,
    settings: TenantSettings,
    *,
    now: datetime,
) -> TenantDetail:
    with _management(db, actor) as user:
        stored_id = _load(db, tenant_id, user).tenant.tenant_id
        if not tenants_repo.update_tenant(db, stored_id, body, now=now):
            raise _rejected(db, stored_id, user)  # 그 사이 지워졌다 → 404
        return _commit_after(db, lambda: _detail(_load(db, stored_id, user), settings))


def suspend_tenant(
    db: Session,
    actor: admin_service.Actor,
    tenant_id: str,
    *,
    reason: str,
    settings: TenantSettings,
    now: datetime,
) -> TenantDetail:
    """원격 회수 ① 앱 겹. IAM 회수(②)는 진행 중 실행이 끝난 뒤의 운영 절차다(ADR-0012 §3)."""
    with _management(db, actor) as user:
        stored_id = _load(db, tenant_id, user).tenant.tenant_id
        if not tenants_repo.suspend_tenant(db, stored_id, reason=reason, now=now):
            raise _rejected(db, stored_id, user)
        return _commit_after(db, lambda: _detail(_load(db, stored_id, user), settings))


def delete_tenant(db: Session, actor: admin_service.Actor, tenant_id: str) -> None:
    """연결된 적 없는 테넌트만 지운다. 그 테넌트를 가리키는 사용자 · 기록이 있으면 FK 가 막아 409 다."""
    with _management(db, actor) as user:
        stored_id = _load(db, tenant_id, user).tenant.tenant_id
        try:
            with db.begin_nested():  # FK 거절이 이 삭제만 되돌린다
                deleted = tenants_repo.delete_tenant(db, stored_id)
        except IntegrityError as exc:
            raise ApiError(ErrorCode.TENANT_STATE_CONFLICT) from exc
        if not deleted:
            raise _rejected(db, stored_id, user)
        db.commit()
