# ==============================================================================
# [파일 설명]  담당: 안성일 (BE · 접근 보호)
# 플랫폼 상태 · 고객사별 API 호출 통계 저장소 (#572 6번).
#
#   - commit하지 않는다. 트랜잭션은 호출부(api_usage 의 기록 · platform_overview 의 조회)가 소유한다.
#   - 시각 · 날짜는 호출부가 넘긴다(시계를 읽지 않는다).
#   - 상태 집계는 이미 저장된 tenants · collection_runs · incidents 를 세기만 한다 — 새 수집 로직을 두지 않는다.
#     집계 대상 테이블의 쓰기는 각 저장소(tenants · assets · incidents)가 그대로 맡는다.
#   - 호출 통계(tenant_api_calls_daily)의 쓰기는 한 버킷 +1 의 UPSERT 와 기준일 이전 삭제뿐이다.
# ==============================================================================

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime

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
_C = models.TenantApiCallDaily


# --- 플랫폼 상태 -----------------------------------------------------------------


def tenant_counts_by_status(db: Session) -> dict[TenantStatus, int]:
    """연결 상태별 고객사 수. 없는 상태는 0 으로 채워 세 상태를 전부 돌려준다."""
    counts = {status: 0 for status in TenantStatus}
    for status, count in db.execute(select(_T.status, func.count()).group_by(_T.status)):
        counts[status] = count
    return counts


def collection_run_counts_since(db: Session, since: datetime) -> dict[CollectionRunStatus, int]:
    """since 이후에 끝난(IN_PROGRESS 는 시작한) 수집 회차 수 — 상태별, 없는 상태는 0."""
    counts = {status: 0 for status in CollectionRunStatus}
    statement = (
        select(_R.status, func.count())
        .where(func.coalesce(_R.finished_at, _R.started_at) >= since)
        .group_by(_R.status)
    )
    for status, count in db.execute(statement):
        counts[status] = count
    return counts


def incidents_awaiting_approval(db: Session) -> int:
    return db.scalar(
        select(func.count()).where(_I.status == IncidentStatus.AWAITING_APPROVAL)
    )


# --- 호출 통계 -------------------------------------------------------------------


def add_call(db: Session, *, tenant_id: str, day: date) -> None:
    """(tenant_id, day) 버킷을 +1 한다. 없으면 1 로 만든다 — 한 문장의 UPSERT 라 동시 요청에도 수가 빠지지 않는다."""
    statement = insert(_C).values(tenant_id=tenant_id, day=day, calls=1)
    db.execute(
        statement.on_conflict_do_update(
            constraint="pk_tenant_api_calls_daily", set_={"calls": _C.calls + statement.excluded.calls}
        )
    )


def delete_calls_before(db: Session, day: date) -> int:
    """day 보다 이전 날짜의 버킷을 지운다 → 지운 행 수."""
    return db.execute(delete(_C).where(_C.day < day)).rowcount


def list_tenants_for_usage(db: Session) -> Sequence[Row]:
    """호출 통계에 실을 고객사 전부 — (tenant_id, name, status). 호출 0인 고객사도 포함한다."""
    return db.execute(select(_T.tenant_id, _T.name, _T.status)).all()


def list_calls_between(db: Session, *, start: date, end: date) -> Sequence[Row]:
    """[start, end] 양 끝 포함 기간의 버킷 — (tenant_id, day, calls), tenant_id · day 오름차순."""
    return db.execute(
        select(_C.tenant_id, _C.day, _C.calls)
        .where(_C.day >= start, _C.day <= end)
        .order_by(_C.tenant_id, _C.day)
    ).all()
