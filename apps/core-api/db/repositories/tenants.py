# ==============================================================================
# [파일 설명]  담당: 김승철 (API 계약 · BE)
# 테넌트 저장소 — tenants. (Post-MVP T-4 · #510 · #572 고객사 검색)
#
#   - commit하지 않는다. 트랜잭션은 호출부가 소유한다. 생성은 flush까지 한다.
#   - 시각은 호출부가 now로 넘긴다(시계를 읽지 않는다).
#   - 상태 전이는 「받는 상태일 때만」 한 행을 바꾸는 조건부 UPDATE다. 받는 상태는 계약
#     상수(schemas.api.tenants의 *_STATUSES)를 그대로 쓰고 여기 다시 적지 않는다. 그 사이
#     다른 관리자가 상태를 바꿨으면 0행(False)이 되고, 호출부가 TENANT_STATE_CONFLICT로 답한다.
#   - 연결 확인 자체(AssumeRole · GetCallerIdentity · 최소 읽기)는 하지 않는다. 그 **결과를
#     기록하는 자리**까지다 — 테넌트별 AWS 자격은 다음 카드다.
#   - 구독(T-16 · #528) 칸은 투영이다. 권위 있는 원천인 모의 Marketplace 계약은
#     db.repositories.mock_marketplace 가, 계약 → 투영 계산은 subscription_workflow 가 맡는다. 여기는 행을
#     잠그고(lock_tenant) 투영을 쓰는 자리와 운영자 PATCH 의 계약 보호까지다. 구독 쓰기는 서비스 상태(status)를
#     바꾸지 않고, 서비스 전이(정지 · 재개 · 연결 확인)는 구독을 바꾸지 않는다.
#   - 잠금 순서: 테넌트 행(FOR UPDATE) → 계약 행. 구매 · PATCH · 투영 · 갱신 끄기가 같은 순서로 잠근다.
# ==============================================================================

from __future__ import annotations

import secrets
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from schemas.api.tenants import (
    DELETABLE_STATUSES,
    RESUMABLE_STATUSES,
    SUSPENDABLE_STATUSES,
    VERIFIABLE_STATUSES,
    TenantStatus,
    TenantSubscription,
    TenantUpdateRequest,
    TenantVerificationResult,
    default_access_role_arn,
)
from schemas.incidents import INCIDENT_OPEN_STATUSES
from schemas.subscriptions import (
    CONTRACT_SOURCES,
    FREE_PLAN,
    MOCK_AGREEMENT_OPEN_STATUSES,
    SubscriptionSource,
)
from sqlalchemy import case, delete, exists, func, literal, select, update
from sqlalchemy.orm import Session

from .. import models

_T = models.Tenant

# ExternalId 는 플랫폼이 한 번 정한다 — 추측할 수 없어야 혼동된 대리인 방지가 성립한다.
# 16바이트 난수 = 32자 hex(DB CHECK external_id_min_length 는 16자 이상).
_EXTERNAL_ID_BYTES = 16

#: 앱 정책 Free — 기간 · 기준일 · 예약 · 요금 없음. 새 테넌트 · 모의 계약이 끝난 뒤의 도착점이다.
APP_FREE = TenantSubscription(plan=FREE_PLAN)


def _subscription_values(sub: Optional[TenantSubscription]) -> dict:
    """구독 칸 전부의 값(칸 이름 = 계약 TenantSubscription 의 칸). None 이면 미등록 — 모두 비우고 예약 없음."""
    return (sub or TenantSubscription()).model_dump()


def projection_values(
    sub: TenantSubscription | None,
    source: SubscriptionSource | None,
    entitlement_ends_at: datetime | None = None,
) -> dict:
    """투영 칸 전부(구독 칸 + 원천 + 권리 끝 시각). 비어 있는 구독은 원천이 없다(등록 안 됨 · DB CHECK)."""
    values = _subscription_values(sub)
    empty = values == _subscription_values(None)
    return values | {
        "subscription_source": None if empty else source,
        "entitlement_ends_at": entitlement_ends_at,
    }


@dataclass(frozen=True)
class TenantWithCounts:
    """목록 · 상세 응답의 원천 — 테넌트 행과 그 테넌트에 묶인 숫자 둘."""

    tenant: models.Tenant
    #: 소멸하지 않은 자산 수(absent_since IS NULL).
    asset_count: int
    #: 종료되지 않은 인시던트 수(INCIDENT_OPEN_STATUSES).
    open_incident_count: int


# --- 생성 · 조회 ----------------------------------------------------------------


def create_tenant(
    db: Session,
    *,
    name: str,
    aws_account_id: str,
    regions: Sequence[str],
    now: datetime,
    collect_vpc_ids: Sequence[str] = (),
    access_role_arn: Optional[str] = None,
    group_id: str | None = None,
) -> models.Tenant:
    """PENDING_CONNECTION 으로 만든다. external_id 는 여기서 정하고 바꾸지 않는다.

    계정 ID · ExternalId 중복은 DB 유일성이 flush에서 거절한다(호출부가 409로 해석).
    역할 ARN 을 안 주면 계약의 기본값(`vigilantis-access`)이다.
    구독은 앱 정책 Free(APP_FREE)로 시작한다(T-16) — 기간 · 요금 없이. 기존 테넌트의 NULL 구독은 Free 로 채우지 않는다.
    group_id 를 안 주면 그룹 없음(담당자 없음)이다(#572). 없는 그룹은 FK 가 거절한다 — 호출부가 먼저 확인한다.
    """
    row = _T(
        name=name,
        aws_account_id=aws_account_id,
        access_role_arn=access_role_arn or default_access_role_arn(aws_account_id),
        external_id=secrets.token_hex(_EXTERNAL_ID_BYTES),
        regions=list(regions),
        collect_vpc_ids=list(collect_vpc_ids),
        group_id=group_id,
        **projection_values(APP_FREE, SubscriptionSource.APP_FREE),
        created_at=now,
        updated_at=now,
    )
    db.add(row)
    db.flush()
    # INSERT 에 값을 주지 않은 칸(확인 기록 · 정지 기록 · 구독)은 세션이 「NULL 로 들어갔다」고만
    # 알고 싣지 않는다. 그런 칸은 뒤의 조건부 UPDATE 가 바꿔도 세션 동기화에서 빠져, 이 객체가
    # 낡은 None 을 돌려준다 — 한 번 읽어 모든 칸을 실어 둔다
    db.refresh(row)
    return row


def _with_counts():
    # 자산 · 인시던트의 tenant_id 가 NULL 인 행(MVP 단일 계정 기록)은 어느 테넌트에도 세지 않는다 —
    # 등호 비교라 NULL 은 걸리지 않는다
    asset_count = (
        select(func.count())
        .where(models.Asset.tenant_id == _T.tenant_id, models.Asset.absent_since.is_(None))
        .correlate(_T)
        .scalar_subquery()
    )
    open_incident_count = (
        select(func.count())
        .where(
            models.Incident.tenant_id == _T.tenant_id,
            models.Incident.status.in_(INCIDENT_OPEN_STATUSES),
        )
        .correlate(_T)
        .scalar_subquery()
    )
    # 전이가 0행이면 라우터는 같은 세션에서 다시 읽어 「지금 상태」로 409 를 답한다 — 그 사이 다른
    # 관리자가 커밋한 값을 identity map 의 낡은 값이 가리지 않게 DB 값으로 덮어 읽는다
    return select(_T, asset_count, open_incident_count).execution_options(populate_existing=True)


def get_tenant(
    db: Session, tenant_id: str, *, scope_ids: Optional[Sequence[str]] = None
) -> Optional[TenantWithCounts]:
    """scope_ids 가 None 이면 전체, 아니면 그 테넌트 안에서만 찾는다(범위 밖은 없는 것과 같다)."""
    statement = _with_counts().where(_T.tenant_id == tenant_id)
    if scope_ids is not None:
        statement = statement.where(_T.tenant_id.in_(list(scope_ids)))
    row = db.execute(statement).one_or_none()
    return None if row is None else TenantWithCounts(*row)


def read_tenant_for_share(db: Session, tenant_id: str) -> Optional[models.Tenant]:
    """테넌트 행을 FOR SHARE 로 읽는다 — 트랜잭션이 끝날 때까지 그 행의 UPDATE(끄기 · 정지)를 막는다.

    자동 발동 접수 관문(T-6)이 쓴다. 끄기가 접수와 겹치면 끄기가 접수 commit 을 기다리므로, 끄기가
    commit 된 뒤에 접수되는 자동 격리는 없다. 읽기끼리는 막지 않는다.
    """
    return db.execute(
        select(_T)
        .where(_T.tenant_id == tenant_id)
        .with_for_update(read=True)
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()


def get_tenant_status(db: Session, tenant_id: str) -> Optional[TenantStatus]:
    """상태 칸만 DB 에서 바로 읽는다 — 잠그지 않고, 세션이 든 낡은 객체도 거치지 않는다.

    디스패처의 runner 진입 전 정지 관문이 쓴다. 그 트랜잭션은 runner 의 백업 캡처(AWS 읽기)까지
    이어지므로 여기서 FOR SHARE 를 걸면 정지가 AWS 호출을 기다리게 된다 — 결정적인 경계는 첫 단계
    commit 의 FOR SHARE(read_tenant_for_share)이고, 이 읽기는 그 전에 AWS 를 부르지 않게 하는 사전 판정이다.
    """
    return db.scalar(select(_T.status).where(_T.tenant_id == tenant_id))


def lock_tenant(db: Session, tenant_id: str) -> models.Tenant | None:
    """테넌트 행을 FOR UPDATE 로 읽는다(DB 값으로 덮어 읽기). 구독의 쓰기들이 이 잠금으로 줄을 선다 —
    잠근 뒤의 다음 조회는 먼저 commit 된 계약을 본다(READ COMMITTED 의 문장별 스냅숏)."""
    return db.execute(
        select(_T)
        .where(_T.tenant_id == tenant_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()


def has_open_agreement(db: Session, tenant_id: str) -> bool:
    """진행 중(PENDING · ACTIVE)인 모의 Marketplace 계약이 있는가."""
    agreement = models.MockMarketplaceAgreement
    return db.scalar(
        select(
            exists().where(
                agreement.tenant_id == tenant_id,
                agreement.status.in_(MOCK_AGREEMENT_OPEN_STATUSES),
            )
        )
    )


def tenant_id_for_account(db: Session, aws_account_id: str) -> Optional[str]:
    """그 AWS 계정을 등록한 테넌트 — 상태와 무관하다. 계정은 테넌트마다 하나뿐이다(DB 유일성)."""
    return db.scalar(select(_T.tenant_id).where(_T.aws_account_id == aws_account_id))


def list_tenants(
    db: Session, *, scope_ids: Optional[Sequence[str]] = None, unassigned_only: bool = False
) -> list[TenantWithCounts]:
    """생성 순(created_at 오름차순 · 같으면 tenant_id) — 계약 TenantsResponse 의 정렬.
    scope_ids 가 None 이면 전체, 빈 목록이면 빈 결과다. unassigned_only 면 그 안에서 그룹 없는(group_id NULL) 행만."""
    statement = _with_counts()
    if scope_ids is not None:
        statement = statement.where(_T.tenant_id.in_(list(scope_ids)))
    if unassigned_only:
        statement = statement.where(_T.group_id.is_(None))
    rows = db.execute(statement.order_by(_T.created_at, _T.tenant_id))
    return [TenantWithCounts(*row) for row in rows]


def _like_escaped(value: str) -> str:
    # LIKE 의 와일드카드(% · _)와 이스케이프 문자를 글자 그대로 찾게 한다
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def search_tenants(db: Session, query: str, *, limit: int) -> list[tuple[str, str]]:
    """이름에 query 가 든(대소문자 무시) 고객사의 (tenant_id, name) — 이름순 · 같으면 tenant_id, 최대 limit 건.

    범위를 걸지 않는다(#572 7번 — 담당 밖 고객사도 검색에 뜬다). 접근 가능 여부는 호출부가 가린다.
    """
    pattern = f"%{_like_escaped(query)}%"
    rows = db.execute(
        select(_T.tenant_id, _T.name)
        .where(_T.name.ilike(pattern, escape="\\"))
        .order_by(_T.name, _T.tenant_id)
        .limit(limit)
    )
    return [(tenant_id, name) for tenant_id, name in rows]


# --- 수정 ----------------------------------------------------------------------


def _changed_one(db: Session, statement) -> bool:
    """쓰기 1건을 실행하고 한 행이 바뀌었는지 돌려준다.

    세션 동기화는 **DB 가 실제로 바꾼 행만**(RETURNING) 따라가게 한다. 기본값(evaluate)은 조건을
    세션 메모리의 객체로 판정해, 다른 세션이 이미 정지해 DB 에서는 0행인 전이도 이 세션이 들고 있는
    낡은 ACTIVE 객체에는 값을 써 버린다 — 거절된 확인 결과가 응답에 실린다.
    """
    result = db.execute(statement, execution_options={"synchronize_session": "fetch"})
    return result.rowcount == 1


def update_tenant(
    db: Session, tenant_id: str, changes: TenantUpdateRequest, *, now: datetime
) -> bool:
    """보낸 칸만 바꾼다(changes.model_fields_set). 상태와 무관하다. 없는 테넌트면 False.

    계정 · 역할 · 상태는 계약이 받지 않으므로 여기에도 길이 없다. `subscription` 은 구독 칸 전부
    (예약 · 기준일 · 요금 포함)를 한 덩어리로 바꾸고(원천 MANUAL), null 이면 모두 지운다(미등록). 보내지 않으면
    그대로다. **유효한 모의 계약은 지우지 않는다** — `subscription` 을 보냈는데 진행 중(PENDING · ACTIVE) 계약이
    있거나 지금 구독이 계약의 투영이면 아무것도 바꾸지 않고 False 다(호출부가 다시 읽어 409). 행을 먼저 잠가
    동시 구매(같은 잠금 뒤에 계약을 만든다)와 줄을 세운다.
    """
    values: dict = {
        field: getattr(changes, field)
        for field in ("name", "regions", "collect_vpc_ids", "auto_action_enabled")
        if field in changes.model_fields_set
    }
    if "subscription" in changes.model_fields_set:
        locked = lock_tenant(db, tenant_id)
        if (
            locked is None
            or locked.subscription_source in CONTRACT_SOURCES
            or has_open_agreement(db, tenant_id)
        ):
            return False
        values |= projection_values(changes.subscription, SubscriptionSource.MANUAL)
    if changes.auto_action_enabled is True:
        # 꺼져 있던 것을 켤 때만 그 시각을 남긴다(T-6 · #514 Q2) — 이미 켜져 있으면 그대로 둔다.
        # SET 의 오른쪽은 바뀌기 전 값을 읽으므로 같은 UPDATE 안에서 판정이 맞다
        values["auto_action_resumed_at"] = case(
            (_T.auto_action_enabled.is_(False), literal(now, _T.auto_action_resumed_at.type)),
            else_=_T.auto_action_resumed_at,
        )
    return _changed_one(
        db, update(_T).where(_T.tenant_id == tenant_id).values(**values, updated_at=now)
    )


# --- 상태 전이 ------------------------------------------------------------------


def _verification(result: TenantVerificationResult, reason_code: Optional[str], now: datetime) -> dict:
    # OK 와 사유의 짝은 DB CHECK(verification_shape)가 거절한다 — 여기서 고쳐 쓰지 않는다
    return {
        "last_verification_result": result,
        "last_verified_at": now,
        "last_verification_reason_code": reason_code,
    }


def record_verification(
    db: Session,
    tenant_id: str,
    *,
    result: TenantVerificationResult,
    reason_code: Optional[str],
    now: datetime,
) -> bool:
    """POST /verify 의 결과를 기록한다. VERIFIABLE_STATUSES 밖이면 False.

    PENDING_CONNECTION 에서 OK 면 ACTIVE 로 올린다. **그 밖에는 상태를 바꾸지 않는다** —
    ACTIVE 의 확인 실패로 자동 정지하지 않는다(정지는 사람의 판단이다). 확인하는 사이 다른
    관리자가 정지했으면 0행이라, 정지된 테넌트에 확인 결과가 덧씌워지지 않는다.
    """
    values = _verification(result, reason_code, now)
    if result is TenantVerificationResult.OK:
        # 올리는 것은 PENDING_CONNECTION 하나다 — 받는 상태 상수가 넓어져도 OK 가 정지를 풀지 않게
        # 「모두 ACTIVE 로」가 아니라 조건식으로 쓴다(정지를 푸는 길은 resume_tenant 하나다)
        values["status"] = case(
            (
                _T.status == TenantStatus.PENDING_CONNECTION,
                literal(TenantStatus.ACTIVE, _T.status.type),
            ),
            else_=_T.status,
        )
    return _changed_one(
        db,
        update(_T)
        .where(_T.tenant_id == tenant_id, _T.status.in_(VERIFIABLE_STATUSES))
        .values(**values, updated_at=now),
    )


def suspend_tenant(db: Session, tenant_id: str, *, reason: str, now: datetime) -> bool:
    """원격 회수 ① 앱 겹. SUSPENDABLE_STATUSES 밖이면 False. 연결 확인 기록은 그대로 둔다."""
    return _changed_one(
        db,
        update(_T)
        .where(_T.tenant_id == tenant_id, _T.status.in_(SUSPENDABLE_STATUSES))
        .values(
            status=TenantStatus.SUSPENDED,
            suspended_at=now,
            suspended_reason=reason,
            updated_at=now,
        ),
    )


def resume_tenant(
    db: Session,
    tenant_id: str,
    *,
    result: TenantVerificationResult,
    reason_code: Optional[str],
    now: datetime,
) -> bool:
    """POST /resume — 먼저 돌린 연결 확인의 결과와 함께 받는다. RESUMABLE_STATUSES 밖이면 False.

    OK 면 ACTIVE 로 올리고 정지 기록을 지운다. 실패면 결과만 남기고 SUSPENDED 그대로다 —
    정지 사이에 고객이 역할을 지웠을 수 있다. 상태와 확인 기록을 한 UPDATE로 쓴다.

    **구독은 바꾸지 않는다**(T-16 · #528) — 정지 중에도 모의 계약의 갱신 · 만료와 투영이 계속되므로, 재개는
    지금 권리를 그대로 이어 간다(Free 로 다시 시작하지 않고 tenant_subscription_history 에 새 행을 쓰지 않는다).
    """
    values = _verification(result, reason_code, now)
    if result is TenantVerificationResult.OK:
        values |= {"status": TenantStatus.ACTIVE, "suspended_at": None, "suspended_reason": None}
    return _changed_one(
        db,
        update(_T)
        .where(_T.tenant_id == tenant_id, _T.status.in_(RESUMABLE_STATUSES))
        .values(**values, updated_at=now),
    )


def delete_tenant(db: Session, tenant_id: str) -> bool:
    """연결된 적 없는 테넌트만 지운다. DELETABLE_STATUSES 밖이면 False.

    **그 테넌트를 가리키는 행(사용자 · 뿌리 테이블)이 있으면 FK 가 IntegrityError 로 거절한다** —
    지우지도, 고쳐 쓰지도 않고 호출부에 올린다. 연결 전 테넌트에는 수집 기록이 없고, 고객사
    사용자는 연결(ACTIVE) 뒤에 초대하는 것이 전제라 정상 경로에서는 일어나지 않는다.
    설정 행(tenant_settings)도 FK 로 막는다 — 일회용 행(MANAGED 가 아닌 것)은 호출부(tenant_admin)가
    같은 SAVEPOINT 안에서 먼저 지운다.
    """
    return _changed_one(
        db, delete(_T).where(_T.tenant_id == tenant_id, _T.status.in_(DELETABLE_STATUSES))
    )


# --- 구독 투영 (T-16 · #528) ------------------------------------------------------


def project_subscription(db: Session, tenant_id: str, values: dict, *, now: datetime) -> bool:
    """투영 칸(projection_values)을 쓴다. 서비스 상태와 무관하다 — 정지 중에도 계약을 따라간다.

    호출부(subscription_workflow)가 lock_tenant 로 행을 잠그고 계약의 현재 상태를 읽은 뒤 부른다.
    """
    return _changed_one(
        db, update(_T).where(_T.tenant_id == tenant_id).values(**values, updated_at=now)
    )
