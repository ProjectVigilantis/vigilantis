"""플랫폼 직원 관리 계약 (#492 PR 5). 중앙 관리자만 쓴다. 직원 생성·초대 경로는 없다(운영 CLI 전용)."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .assets import UtcDateTime
from .auth import Email, PlatformRole
from .users import UserStatus


class StaffItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: str = Field(min_length=1)
    email: Email
    platform_role: PlatformRole
    status: UserStatus
    last_login_at: UtcDateTime | None


class StaffResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[StaffItem]


class StaffRoleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    platform_role: PlatformRole


class StaffTenantsRequest(BaseModel):
    """담당 고객 전체 교체 요청. 빈 목록은 유효(모든 접근 제거). 중복 ID 는 거절한다."""

    model_config = ConfigDict(extra="forbid")

    tenant_ids: list[UUID]

    @field_validator("tenant_ids")
    @classmethod
    def _unique(cls, value: list[UUID]) -> list[UUID]:
        if len(set(value)) != len(value):
            raise ValueError("tenant_ids must not contain duplicates")
        return value


class StaffTenantsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tenant_ids: list[UUID]


class StaffStatusRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: UserStatus
