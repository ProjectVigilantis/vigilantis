# ==============================================================================
# [파일 설명]  담당: 안성일 (BE · 접근 보호)
# 플랫폼 상태 · 고객사별 API 호출 통계 저장소 (#572 6번).
#
#   - commit하지 않는다. 트랜잭션은 호출부(api_usage 의 기록 · platform_overview 의 조회)가 소유한다.
#   - 시각은 호출부가 넘긴다(시계를 읽지 않는다). 시간 칸(hour)은 호출부가 UTC 정시로 내려서 넘긴다.
#   - 상태 집계는 이미 저장된 tenants · collection_runs · incidents · assets · app_users 를 세기만 한다 — 새 수집 로직을
#     두지 않는다. 집계 대상 테이블의 쓰기는 각 저장소가 그대로 맡는다.
#   - 호출 통계(tenant_api_calls_hourly)의 쓰기는 한 칸 +1 의 UPSERT 와 기준 시각 이전 삭제뿐이다.
# ==============================================================================

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from enum import Enum
from typing import TypeVar

from schemas.api.incidents import IncidentStatus
from schemas.api.tenants import TenantStatus
from schemas.collections import CollectionRunStatus
from sqlalchemy import Row, delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from .. import models

_T = models.Tenant
_R = models.CollectionRun
_I = models.Incident
_C = models.TenantApiCallHourly

_E = TypeVar("_E", bound=Enum)


def _counts_by(db: Session, enum_cls: type[_E], statement) -> dict[_E, int]:
    """(상태, 수) 행을 enum 전체로 편다 — 없는 상태는 0."""
    counts = {status: 0 for status in enum_cls}
    for status, count in db.execute(statement):
        counts[status] = count
    return counts


# --- 플랫폼 상태 -----------------------------------------------------------------


def tenant_counts_by_status(db: Session) -> dict[TenantStatus, int]:
    """연결 상태별 고객사 수. 세 상태 전부."""
    return _counts_by(db, TenantStatus, select(_T.status, func.count()).group_by(_T.status))


def collection_run_counts_since(db: Session, since: datetime) -> dict[CollectionRunStatus, int]:
    """since 이후에 끝난(IN_PROGRESS 는 시작한) 수집 회차 수 — 상태별."""
    return _counts_by(
        db,
        CollectionRunStatus,
        select(_R.status, func.count())
        .where(func.coalesce(_R.finished_at, _R.started_at) >= since)
        .group_by(_R.status),
    )


def incident_counts_by_status(db: Session) -> dict[IncidentStatus, int]:
    """상태별 인시던트 수. 여섯 상태 전부."""
    return _counts_by(db, IncidentStatus, select(_I.status, func.count()).group_by(_I.status))


# --- 호출 통계 -------------------------------------------------------------------


def add_call(db: Session, *, tenant_id: str, hour: datetime) -> None:
    """(tenant_id, hour) 칸을 +1 한다. 없으면 1 로 만든다 — 한 문장의 UPSERT 라 동시 요청에도 수가 빠지지 않는다."""
    statement = insert(_C).values(tenant_id=tenant_id, hour=hour, calls=1)
    db.execute(
        statement.on_conflict_do_update(
            constraint="pk_tenant_api_calls_hourly", set_={"calls": _C.calls + statement.excluded.calls}
        )
    )


def delete_calls_before(db: Session, cutoff: datetime) -> int:
    """cutoff 보다 이른 칸을 지운다 → 지운 행 수."""
    return db.execute(delete(_C).where(_C.hour < cutoff)).rowcount


def list_tenants_for_usage(db: Session) -> Sequence[Row]:
    """호출 통계에 실을 고객사 전부 — (tenant_id, name, status, asset_count, staff_count). 호출 0인 고객사도 포함한다.

    asset_count 는 고객사 목록(tenants 저장소)과 같은 기준(사라진 자산 제외), staff_count 는 그 고객사에 묶인
    계정 전부(상태 무관)다.
    """
    asset_count = (
        select(func.count())
        .where(models.Asset.tenant_id == _T.tenant_id, models.Asset.absent_since.is_(None))
        .correlate(_T)
        .scalar_subquery()
    )
    staff_count = (
        select(func.count())
        .where(models.AppUser.tenant_id == _T.tenant_id)
        .correlate(_T)
        .scalar_subquery()
    )
    return db.execute(select(_T.tenant_id, _T.name, _T.status, asset_count, staff_count)).all()


def list_calls_between(db: Session, *, start: datetime, end: datetime) -> Sequence[Row]:
    """[start, end] 양 끝 포함 범위의 칸 — (tenant_id, hour, calls)."""
    return db.execute(
        select(_C.tenant_id, _C.hour, _C.calls).where(_C.hour >= start, _C.hour <= end)
    ).all()
