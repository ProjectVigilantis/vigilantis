"""플랫폼 직원 관리 계약 (#492 PR 5 · #572 1번). 중앙 관리자만 쓴다.

직원 초대(`POST /api/v1/staff` `StaffInviteRequest` → 201 `StaffItem`)와 재발송(`POST /{user_id}/invite/resend` → 204)은
#572 1번으로 열렸다. 초대 본문은 이메일만 받고 등급 · 담당 고객 · 기존 계정 연결 칸은 없다 — 등급은 `PUT /{user_id}/role`,
배정은 `PUT /{user_id}/tenants`다. 운영 CLI(auth.bootstrap_admin)는 첫 중앙 관리자 생성과 장애 복구용으로 남는다.
"""

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


class StaffInviteRequest(BaseModel):
    """직원 초대(#572 1번). 만드는 계정은 담당 고객이 없는 `TENANT_MANAGER`로 고정이라 등급 · 배정 칸이 없고,
    Cognito에 이미 있는 이메일은 409다(자동 연결 · `recover_existing` 없음 — 복구는 운영 CLI)."""

    model_config = ConfigDict(extra="forbid")

    email: Email


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
