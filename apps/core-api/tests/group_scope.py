"""담당자(TENANT_MANAGER) 범위 시드 — 담당 범위를 고객사 단위 배정에서 그룹 배정으로 옮긴 뒤(#572 2번)의 테스트 도구.

assign_tenants 는 옛 replace_assigned_tenants 처럼 사용자의 담당 범위를 정확히 그 고객사들로 바꾼다. 그룹이 없는
고객사는 그 고객사 하나만 든 그룹(`group-<tenant_id>`)에 넣고, 이미 그룹이 있는 고객사는 그 그룹을 쓴 뒤 모은 그룹을
사용자에게 통째로 배정한다(빈 목록 = 전부 회수). 고객사마다 그룹이 따로라 두 담당자의 범위가 겹치거나 한 담당자의
범위를 좁혀도 다른 고객사가 딸려 오지 않는다. 테스트가 API 로 여러 고객사를 묶은 그룹에 든 고객사를 고르면 그 그룹
전체가 범위가 된다. 저장소 함수만 쓰고 commit 하지 않는다 — 호출부가 정한다.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime

from db import models
from db.repositories import auth as auth_repo
from db.repositories import tenant_groups as groups_repo


def assign_tenants(session, *, user_id: str, tenant_ids: Iterable[str], now: datetime) -> tuple[str, ...]:
    """사용자의 담당 범위를 tenant_ids 로 바꾼다 → 배정한 그룹 ID(처음 나온 순서)."""
    group_ids: list[str] = []
    for tenant_id in tenant_ids:
        tenant = session.get(models.Tenant, tenant_id, populate_existing=True)
        assert tenant is not None, tenant_id
        group_id = tenant.group_id
        if group_id is None:
            # 시드 시각이 고객 생성보다 이르면 고객 updated_at 이 created_at 보다 앞서 상세 응답 계약이 깨진다
            stamp = max(now, tenant.updated_at)
            group_id = groups_repo.create_group(session, name=f"group-{tenant.tenant_id}", now=stamp).group_id
            assert groups_repo.set_tenant_group(session, tenant.tenant_id, group_id, now=stamp)
        if group_id not in group_ids:
            group_ids.append(group_id)
    auth_repo.replace_assigned_groups(session, user_id=user_id, group_ids=tuple(group_ids), now=now)
    return tuple(group_ids)
