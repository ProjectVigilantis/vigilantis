# ==============================================================================
# [파일 설명]  담당: 김승철 (API 계약 · BE)
# 테넌트 저장소 — tenants. (Post-MVP T-4 · #510)
#
#   - commit하지 않는다. 트랜잭션은 호출부가 소유한다. 생성은 flush까지 한다.
#   - 시각은 호출부가 now로 넘긴다(시계를 읽지 않는다).
#   - 상태 전이는 「받는 상태일 때만」 한 행을 바꾸는 조건부 UPDATE다. 받는 상태는 계약
#     상수(schemas.api.tenants의 *_STATUSES)를 그대로 쓰고 여기 다시 적지 않는다. 그 사이
#     다른 관리자가 상태를 바꿨으면 0행(False)이 되고, 호출부가 TENANT_STATE_CONFLICT로 답한다.
#   - 연결 확인 자체(AssumeRole · GetCallerIdentity · 최소 읽기)는 하지 않는다. 그 **결과를
#     기록하는 자리**까지다 — 테넌트별 AWS 자격은 다음 카드다.
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
    TenantUpdateRequest,
    TenantVerificationResult,
    default_access_role_arn,
)
from schemas.incidents import INCIDENT_OPEN_STATUSES
from sqlalchemy import case, delete, func, literal, select, update
from sqlalchemy.orm import Session

from .. import models

_T = models.Tenant

# ExternalId 는 플랫폼이 한 번 정한다 — 추측할 수 없어야 혼동된 대리인 방지가 성립한다.
# 16바이트 난수 = 32자 hex(DB CHECK external_id_min_length 는 16자 이상).
_EXTERNAL_ID_BYTES = 16


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
) -> models.Tenant:
    """PENDING_CONNECTION 으로 만든다. external_id 는 여기서 정하고 바꾸지 않는다.

    계정 ID · ExternalId 중복은 DB 유일성이 flush에서 거절한다(호출부가 409로 해석).
    역할 ARN 을 안 주면 계약의 기본값(`vigilantis-access`)이다.
    """
    row = _T(
        name=name,
        aws_account_id=aws_account_id,
        access_role_arn=access_role_arn or default_access_role_arn(aws_account_id),
        external_id=secrets.token_hex(_EXTERNAL_ID_BYTES),
        regions=list(regions),
        collect_vpc_ids=list(collect_vpc_ids),
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


def get_tenant(db: Session, tenant_id: str) -> Optional[TenantWithCounts]:
    row = db.execute(_with_counts().where(_T.tenant_id == tenant_id)).one_or_none()
    return None if row is None else TenantWithCounts(*row)


def list_tenants(db: Session) -> list[TenantWithCounts]:
    """생성 순(created_at 오름차순 · 같으면 tenant_id) — 계약 TenantsResponse 의 정렬."""
    rows = db.execute(_with_counts().order_by(_T.created_at, _T.tenant_id))
    return [TenantWithCounts(*row) for row in rows]


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

    계정 · 역할 · 상태는 계약이 받지 않으므로 여기에도 길이 없다. `subscription` 은 세 칸을
    한 덩어리로 바꾸고, null 이면 셋을 모두 지운다.
    """
    values: dict = {
        field: getattr(changes, field)
        for field in ("name", "regions", "collect_vpc_ids", "auto_action_enabled")
        if field in changes.model_fields_set
    }
    if "subscription" in changes.model_fields_set:
        sub = changes.subscription
        values |= {
            "plan": sub.plan if sub else None,
            "period_start": sub.period_start if sub else None,
            "period_end": sub.period_end if sub else None,
        }
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
    """
    return _changed_one(
        db, delete(_T).where(_T.tenant_id == tenant_id, _T.status.in_(DELETABLE_STATUSES))
    )
