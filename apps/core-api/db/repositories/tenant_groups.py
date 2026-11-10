# ==============================================================================
# [파일 설명]
# 고객사 그룹 저장소 — tenant_groups 와 고객사의 그룹 배치(tenants.group_id). (#572 2번)
#
#   - commit하지 않는다. 트랜잭션은 호출부(Workflow)가 소유한다. 생성은 flush까지 한다.
#   - 시각은 호출부가 now로 넘긴다(시계를 읽지 않는다).
#   - 그룹 이름 중복은 DB 유일성(GROUP_NAME_UNIQUE)이 flush 에서 거절한다 — 호출부가 409 로 해석한다.
#   - 그룹을 지우면 DB 가 그 고객사들을 그룹 없음으로 돌리고(FK SET NULL) 직원의 그 그룹 배정을 지운다(CASCADE).
#   - 직원 ↔ 그룹 배정은 db.repositories.auth 가 맡는다. 식별자는 호출부가 정규 UUID 로 맞춰 넘긴다.
# ==============================================================================

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from .. import models

_G = models.TenantGroup

#: 같은 이름의 두 번째 그룹을 DB 유일성이 거절할 때의 제약 이름(db.session NAMING_CONVENTION).
GROUP_NAME_UNIQUE = "uq_tenant_groups_name"


@dataclass(frozen=True)
class GroupWithCount:
    """목록 응답의 원천 — 그룹 행과 그 그룹에 든 고객사 수."""

    group: models.TenantGroup
    tenant_count: int


def create_group(db: Session, *, name: str, now: datetime) -> models.TenantGroup:
    """그룹 1건. 같은 이름은 DB 유일성이 flush 에서 IntegrityError 로 거절한다."""
    row = _G(name=name, created_at=now, updated_at=now)
    db.add(row)
    db.flush()
    return row


def get_group(db: Session, group_id: str) -> models.TenantGroup | None:
    return db.get(_G, group_id, populate_existing=True)


def existing_group_ids(db: Session, group_ids: Sequence[str]) -> set[str]:
    """group_ids 중 실제로 있는 그룹 ID."""
    if not group_ids:
        return set()
    return set(db.scalars(select(_G.group_id).where(_G.group_id.in_(list(group_ids)))))


def list_groups(
    db: Session, *, scope_group_ids: Sequence[str] | None = None
) -> list[GroupWithCount]:
    """이름순(같으면 group_id). scope_group_ids 가 None 이면 전체, 아니면 그 그룹만(빈 목록 = 빈 결과)."""
    tenant_count = (
        select(func.count())
        .where(models.Tenant.group_id == _G.group_id)
        .correlate(_G)
        .scalar_subquery()
    )
    statement = select(_G, tenant_count).execution_options(populate_existing=True)
    if scope_group_ids is not None:
        statement = statement.where(_G.group_id.in_(list(scope_group_ids)))
    rows = db.execute(statement.order_by(_G.name, _G.group_id))
    return [GroupWithCount(group, count) for group, count in rows]


def delete_group(db: Session, group_id: str) -> bool:
    """그룹을 지운다. 없으면 False. 그 그룹의 고객사는 그룹 없음(SET NULL), 직원 배정은 함께 지워진다(CASCADE)."""
    result = db.execute(delete(_G).where(_G.group_id == group_id))
    return result.rowcount == 1


def set_tenant_group(db: Session, tenant_id: str, group_id: str | None, *, now: datetime) -> bool:
    """고객사를 그 그룹에 넣는다(None = 그룹에서 뺀다). 없는 고객사면 False. 고객사 updated_at 을 now 로 올린다.

    없는 그룹은 FK 가 거절한다 — 호출부가 먼저 확인한다. 세션 동기화는 DB 가 바꾼 행만 따라간다(fetch).
    """
    result = db.execute(
        update(models.Tenant)
        .where(models.Tenant.tenant_id == tenant_id)
        .values(group_id=group_id, updated_at=now),
        execution_options={"synchronize_session": "fetch"},
    )
    return result.rowcount == 1
