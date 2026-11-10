"""고객사 그룹 계약 (#572 2번). 그룹 생성 · 삭제 · 고객사 넣고 빼기 · 직원 배정은 중앙 관리계정만 한다.

관계: 고객사는 최대 한 그룹에 든다(`TenantListItem.group_id`, null = 담당 그룹 없음 · 연결 상태 `TenantStatus` 와
별개). 직원(관리자 직원계정)은 여러 그룹을 배정받고, 한 그룹을 여러 직원이 맡는다. 관리자 직원계정의 고객사 범위는
담당 그룹에 든 고객사뿐이다 — 고객사 단위 직접 배정은 없다. 그룹은 한 단계뿐이다.

경로 (전부 /api/v1 · 플랫폼 계정만 — 고객사(TENANT) 계정은 403 AUTH_FORBIDDEN)
  GET    /groups                     → GroupsResponse(이름순). 중앙 관리계정은 전체, 관리자 직원계정은 자기 담당 그룹만
                                       (고객사를 추가할 때 고를 그룹)
  POST   /groups                     GroupCreateRequest → 201 GroupItem(tenant_count 0). 중앙 관리계정만.
                                       같은 이름이 있으면 409 GROUP_NAME_CONFLICT. 이름은 받은 그대로 저장한다
  DELETE /groups/{group_id}          → 204. 중앙 관리계정만. 없거나 형식이 어긋난 ID 는 404 GROUP_NOT_FOUND.
                                       그 그룹의 고객사는 그룹 없음이 되고 직원의 그 그룹 배정은 지워진다
  PUT    /tenants/{tenant_id}/group  TenantGroupRequest → TenantDetail. 중앙 관리계정만(schemas.api.tenants 머리말)
  GET    /staff/{user_id}/groups     → StaffGroupsResponse · PUT StaffGroupsRequest → 204(schemas.api.staff)
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from .assets import UtcDateTime

GroupName = Annotated[str, Field(min_length=1, max_length=100)]


class GroupItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    group_id: str = Field(min_length=1)
    name: GroupName
    #: 그 그룹에 든 고객사 수(상태 무관).
    tenant_count: int = Field(ge=0)
    created_at: UtcDateTime
    updated_at: UtcDateTime


class GroupsResponse(BaseModel):
    """GET /api/v1/groups 응답. 정렬은 이름 오름차순(같으면 group_id)."""

    model_config = ConfigDict(extra="forbid")

    items: list[GroupItem]


class GroupCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: GroupName


class TenantGroupRequest(BaseModel):
    """PUT /api/v1/tenants/{tenant_id}/group 요청. null 이면 그 고객사를 그룹에서 뺀다(담당 그룹 없음). 칸은 필수다."""

    model_config = ConfigDict(extra="forbid")

    group_id: UUID | None
