# ==============================================================================
# [파일 설명]  담당: 김승철 (API 계약 · BE)
# /api/v1/tenants 의 판정 · 트랜잭션 · 응답 조립 (Post-MVP T-4 · 계약: schemas.api.tenants)
#
#   - 라우터는 얇게 두고 여기서 commit 한다. **응답을 다 만든 뒤에 commit 한다** — 온보딩 링크 설정이
#     없어 응답을 못 만들면(503) 쓰기도 남지 않는다. 생성 본체(create_tenant_uncommitted)는 commit 하지 않는다 —
#     도입 신청 승인(signup_requests_service.approve)이 자기 트랜잭션 안에서 같은 본체를 부른다(#572 묶음 B).
#   - 상태 전이의 받는 상태는 저장소(db.repositories.tenants)가 계약 상수로 판정한다. 0행이면 다시 읽어
#     없으면 404 TENANT_NOT_FOUND, 있으면 409 TENANT_STATE_CONFLICT 다. PATCH 의 `subscription` 이 진행 중인
#     모의 Marketplace 계약 · 그 투영을 덮으려 하면 저장소가 0행으로 거절해 같은 409 다(T-16 · #528).
#   - 연결 확인은 읽기 트랜잭션을 끝낸 뒤 AWS 에 묻고, 쓰기 직전 권한 · 대상 설정 · 상태를 다시 확인한다.
#     재개가 성공하면 같은 트랜잭션에서 구독 투영을 모의 계약의 현재 권리로 맞춘다(DB 만 · 서비스 상태 무관 · #528).
#   - external_id 는 생성 응답과 재발급(onboarding)에서만 내보낸다. 상세 응답에는 칸이 없다(계약).
#   - 범위(#572 7번): 목록은 SQL 로 범위를 건다(범위 밖 행 없음). 상세 · 하위 경로는 범위 없이 읽어 없거나 형식이 어긋난
#     ID 는 404 TENANT_NOT_FOUND, 있지만 범위 밖이면 403 AUTH_FORBIDDEN 이다. 검색은 범위 없이 이름 · ID 와
#     접근 가능 여부(can_access_tenant)만 싣는다.
#   - 그룹(#572 2번 · 3번): 고객사를 그룹에 넣고 빼기는 중앙 관리계정만 한다. 생성은 중앙 관리계정(그룹 선택 · 없는 그룹
#     404)과 관리자 직원계정(잠금 안에서 다시 읽은 담당 그룹 중 하나 필수 — 빠지면 422 · 담당 밖이면 403)이 한다.
# ==============================================================================

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime
from urllib.parse import urlencode
from uuid import UUID

from auth import admin_service, service
from auth.access import (
    can_access_tenant,
    is_central_admin,
    is_tenant_manager,
    tenant_scope_ids,
)
from config import TenantSettings
from db import models
from db.repositories import auth as auth_repo
from db.repositories import tenant_groups as groups_repo
from db.repositories import tenant_settings as settings_repo
from db.repositories import tenants as tenants_repo
from exceptions import ApiError
from identifiers import canonical_id
from schemas.api.errors import ErrorCode
from schemas.api.groups import TenantGroupRequest
from schemas.api.tenants import (
    RESUMABLE_STATUSES,
    TENANT_SEARCH_LIMIT,
    VERIFIABLE_STATUSES,
    TenantCreateRequest,
    TenantCreateResponse,
    TenantDetail,
    TenantListItem,
    TenantOnboardingResponse,
    TenantSearchItem,
    TenantSearchResponse,
    TenantsResponse,
    TenantStatus,
    TenantSubscription,
    TenantUpdateRequest,
    TenantVerification,
    TenantVerificationResult,
    TenantVerifyResponse,
)
from services.aws.client import TenantAccess
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from tenant_verification import verify_connection

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
        guardduty_failures=tenant.last_verification_guardduty_failures,
    )


def subscription_of(tenant: models.Tenant) -> TenantSubscription | None:
    """구독 칸 → 계약. 아무 사실도 없으면(예약 없음 · 나머지 전부 NULL) null — 「등록 안 됨」이다.

    고객 구독 응답(subscription_workflow)도 이것을 쓴다 — 관리자와 고객이 같은 구독을 본다.
    """
    sub = TenantSubscription(**{field: getattr(tenant, field) for field in TenantSubscription.model_fields})
    return None if sub == TenantSubscription() else sub


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
        subscription=subscription_of(tenant),
        subscription_source=tenant.subscription_source,
        entitlement_ends_at=tenant.entitlement_ends_at,
        group_id=tenant.group_id,
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
        created_at=tenant.created_at,
        updated_at=tenant.updated_at,
    )


# --- 조회 ----------------------------------------------------------------------


def _load(db: Session, tenant_id: str, user: service.CurrentUser) -> tenants_repo.TenantWithCounts:
    """형식이 어긋난 식별자도 404 다 — DB 캐스트 오류(500)를 막는다(routers/incidents 와 같은 결).
    있지만 담당 범위 밖인 고객은 403 AUTH_FORBIDDEN 이다(#572 7번 — 검색에 뜬 고객을 열면 권한 없음으로 막는다)."""
    stored_id = canonical_id(tenant_id)
    row = None if stored_id is None else tenants_repo.get_tenant(db, stored_id)
    if row is None:
        raise ApiError(ErrorCode.TENANT_NOT_FOUND)
    if not can_access_tenant(user, stored_id):
        raise ApiError(ErrorCode.AUTH_FORBIDDEN)
    return row


def _rejected(db: Session, tenant_id: str, user: service.CurrentUser) -> ApiError:
    """전이가 0행이었다 — 없어서인지(404), 받는 상태가 아니어서인지(409) 다시 읽어 가른다.
    0행이라 쓴 것이 없고, 저장소의 다시 읽기는 DB 값으로 덮어 읽는다(populate_existing)."""
    _load(db, tenant_id, user)
    return ApiError(ErrorCode.TENANT_STATE_CONFLICT)


def list_tenants(
    db: Session, user: service.CurrentUser, *, unassigned_only: bool = False
) -> TenantsResponse:
    """unassigned_only 는 담당 그룹 없는(group_id null) 고객만 — 범위는 그대로 걸린다."""
    rows = tenants_repo.list_tenants(
        db, scope_ids=tenant_scope_ids(user), unassigned_only=unassigned_only
    )
    return TenantsResponse(tenants=[_list_item(row) for row in rows])


def search_tenants(db: Session, user: service.CurrentUser, query: str) -> TenantSearchResponse:
    """담당 밖 고객도 뜬다(#572 7번) — 이름 · ID · 접근 가능 여부만 싣는다(상태 · 계정 · 숫자 없음)."""
    rows = tenants_repo.search_tenants(db, query, limit=TENANT_SEARCH_LIMIT)
    return TenantSearchResponse(items=[
        TenantSearchItem(tenant_id=tenant_id, name=name, accessible=can_access_tenant(user, tenant_id))
        for tenant_id, name in rows
    ])


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
    세션을 닫는 쪽(get_db)의 정리에 기대지 않는다. 고객 구독 쓰기(subscription_workflow)도 같은 규칙이다."""
    try:
        response = build()
    except Exception:
        db.rollback()
        raise
    db.commit()
    return response


@dataclass(frozen=True)
class _ConnectionTarget:
    status: TenantStatus
    access: TenantAccess
    account_id: str
    # 관제 리전 전부 — 최소 읽기는 첫 리전, GuardDuty 켜기는 리전마다(#588 B). 순서까지 확인한 입력이다.
    regions: tuple[str, ...]
    suspended_at: datetime | None
    suspended_reason: str | None


def _connection_target(tenant: models.Tenant) -> _ConnectionTarget:
    # rollback 뒤 ORM 속성을 읽으면 SELECT 와 트랜잭션이 다시 열리므로 값만 옮긴다.
    return _ConnectionTarget(
        status=tenant.status,
        access=TenantAccess(tenant.tenant_id, tenant.access_role_arn, tenant.external_id),
        account_id=tenant.aws_account_id,
        regions=tuple(tenant.regions),
        # 재개 뒤 다시 정지되면 상태명은 같아도 처음 확인을 시작한 정지가 아니다.
        suspended_at=tenant.suspended_at,
        suspended_reason=tenant.suspended_reason,
    )


def _verification_response(
    row: tenants_repo.TenantWithCounts, settings: TenantSettings
) -> TenantVerifyResponse:
    detail = _detail(row, settings)
    return TenantVerifyResponse(tenant=detail, verification=detail.last_verification)


def _verify_tenant(
    db: Session,
    actor: admin_service.Actor,
    tenant_id: str,
    settings: TenantSettings,
    *,
    resume: bool,
) -> TenantVerifyResponse:
    accepted = RESUMABLE_STATUSES if resume else VERIFIABLE_STATUSES
    write = tenants_repo.resume_tenant if resume else tenants_repo.record_verification
    try:
        with _management(db, actor) as user:
            target = _connection_target(_load(db, tenant_id, user).tenant)
            if target.status not in accepted:
                raise ApiError(ErrorCode.TENANT_STATE_CONFLICT)
    finally:
        db.rollback()

    outcome = verify_connection(
        target.access, expected_account_id=target.account_id, regions=target.regions
    )
    stored_id = target.access.tenant_id
    with _management(db, actor) as user:
        observed_at = actor.clock()
        _load(db, stored_id, user)
        # 설정 대조와 조건부 갱신 사이에도 다른 쓰기가 끼어들지 못하게 한다.
        current = tenants_repo.read_tenant_for_share(db, stored_id)
        if current is None:
            raise _rejected(db, stored_id, user)
        if _connection_target(current) != target:
            raise ApiError(ErrorCode.TENANT_STATE_CONFLICT)
        if not write(
            db,
            stored_id,
            result=outcome.result,
            reason_code=outcome.reason_code,
            guardduty_failures=[asdict(failure) for failure in outcome.guardduty_failures],
            now=observed_at,
        ):
            raise _rejected(db, stored_id, user)
        if resume and outcome.result is TenantVerificationResult.OK:
            # 재개는 구독을 바꾸지 않는다 — 처리 주기가 늦어 남은 투영만 계약의 현재 권리로 맞춘다(갱신을 끈 기간이
            # 끝났으면 Free). 자동 갱신 대기는 처리 주기가 다음 기간을 이을 때까지 그대로 둔다(T-16 · #528).
            # subscription_workflow 가 이 모듈을 가져오므로 여기서 가져온다(순환 import 회피).
            import subscription_workflow

            subscription_workflow.reconcile_tenant_subscription(db, stored_id, now=observed_at)
        return _commit_after(
            db, lambda: _verification_response(_load(db, stored_id, user), settings)
        )


def verify_tenant(
    db: Session, actor: admin_service.Actor, tenant_id: str, settings: TenantSettings
) -> TenantVerifyResponse:
    return _verify_tenant(db, actor, tenant_id, settings, resume=False)


def resume_tenant(
    db: Session, actor: admin_service.Actor, tenant_id: str, settings: TenantSettings
) -> TenantVerifyResponse:
    return _verify_tenant(db, actor, tenant_id, settings, resume=True)


def _creation_group(db: Session, user: service.CurrentUser, requested: UUID | None) -> str | None:
    """새 고객을 넣을 그룹(#572 3번). 중앙 관리계정은 고르거나 비우고(없는 그룹 404), 관리자 직원계정은 잠금 안에서
    다시 읽은 담당 그룹 중 하나가 필수다 — 빠지면 422, 담당 밖(없는 그룹 포함)이면 403 이다."""
    group_id = None if requested is None else str(requested)
    if is_central_admin(user):
        if group_id is not None and groups_repo.get_group(db, group_id) is None:
            raise ApiError(ErrorCode.GROUP_NOT_FOUND)
        return group_id
    if group_id is None:
        raise ApiError(ErrorCode.REQUEST_VALIDATION_FAILED)
    if group_id not in user.assigned_group_ids:
        raise ApiError(ErrorCode.AUTH_FORBIDDEN)
    return group_id


def create_tenant_uncommitted(
    db: Session,
    user: service.CurrentUser,
    body: TenantCreateRequest,
    settings: TenantSettings,
    *,
    now: datetime,
) -> TenantCreateResponse:
    """고객을 만들고 생성 응답(온보딩 링크 포함)까지 짓는다. **commit 하지 않는다** — 호출부가 잠금 안에서 다시 판정한
    user 와 자기 트랜잭션으로 부르고, 응답을 받은 뒤 확정한다(POST /tenants · 도입 신청 승인). 거절은 예외다."""
    group_id = _creation_group(db, user, body.group_id)
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
                group_id=group_id,
                now=now,
            )
    except IntegrityError as exc:
        if getattr(getattr(exc.orig, "diag", None), "constraint_name", None) == _ACCOUNT_UNIQUE:
            raise ApiError(ErrorCode.TENANT_ACCOUNT_CONFLICT) from exc
        raise
    # 새 테넌트에는 자산 · 인시던트가 없다 — 다시 세지 않는다
    return TenantCreateResponse(
        tenant=_detail(tenants_repo.TenantWithCounts(tenant, 0, 0), settings),
        external_id=tenant.external_id,
    )


def create_tenant(
    db: Session, actor: admin_service.Actor, body: TenantCreateRequest, settings: TenantSettings, *, now: datetime
) -> TenantCreateResponse:
    with _management(db, actor) as user:
        return _commit_after(
            db, lambda: create_tenant_uncommitted(db, user, body, settings, now=now)
        )


def set_tenant_group(
    db: Session,
    actor: admin_service.Actor,
    tenant_id: str,
    body: TenantGroupRequest,
    settings: TenantSettings,
    *,
    now: datetime,
) -> TenantDetail:
    """고객을 그룹에 넣거나(group_id) 뺀다(null) — 중앙 관리계정만(#572 2번). 고객 404 가 그룹 404 보다 먼저다."""
    with _management(db, actor) as user:
        if not is_central_admin(user):
            raise ApiError(ErrorCode.AUTH_FORBIDDEN)
        stored_id = _load(db, tenant_id, user).tenant.tenant_id
        group_id = None if body.group_id is None else str(body.group_id)
        if group_id is not None and groups_repo.get_group(db, group_id) is None:
            raise ApiError(ErrorCode.GROUP_NOT_FOUND)
        if not groups_repo.set_tenant_group(db, stored_id, group_id, now=now):
            raise _rejected(db, stored_id, user)  # 그 사이 지워졌다 → 404
        return _commit_after(db, lambda: _detail(_load(db, stored_id, user), settings))


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
            raise _rejected(db, stored_id, user)  # 그 사이 지워졌다 → 404 · 모의 계약 보호 → 409
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
    """원격 회수 ① 앱 겹. IAM 회수(②)는 진행 중 실행이 끝난 뒤의 운영 절차다(ADR-0012 §3).

    고객의 서비스 이용 중단(subscription_workflow.stop_service)과 같은 전이다 — 구독 계약을 해지하지 않는다.
    정지 중에도 모의 계약의 갱신 · 만료와 투영은 계속된다."""
    with _management(db, actor) as user:
        stored_id = _load(db, tenant_id, user).tenant.tenant_id
        if not tenants_repo.suspend_tenant(db, stored_id, reason=reason, now=now):
            raise _rejected(db, stored_id, user)
        return _commit_after(db, lambda: _detail(_load(db, stored_id, user), settings))


def delete_tenant(db: Session, actor: admin_service.Actor, tenant_id: str) -> None:
    """연결된 적 없는 테넌트만 지운다. 그 테넌트를 가리키는 사용자 · 기록(MANAGED 알림 설정 포함)이 있으면 FK 가 막아 409 다."""
    with _management(db, actor) as user:
        stored_id = _load(db, tenant_id, user).tenant.tenant_id
        try:
            with db.begin_nested():  # FK 거절이 이 삭제만 되돌린다
                # 가져온 옛 설정(LEGACY_*)이나 태그뿐인 행이 지우기를 막지 않게 먼저 지운다.
                # MANAGED 행은 남아 FK 가 거절하고, 아래 SAVEPOINT 가 이 삭제도 되돌린다
                settings_repo.delete_disposable(db, stored_id, tenants_repo.DELETABLE_STATUSES)
                deleted = tenants_repo.delete_tenant(db, stored_id)
        except IntegrityError as exc:
            raise ApiError(ErrorCode.TENANT_STATE_CONFLICT) from exc
        if not deleted:
            raise _rejected(db, stored_id, user)
        db.commit()
