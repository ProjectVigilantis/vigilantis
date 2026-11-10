"""플랫폼 직원 관리 계약 (#492 PR 5 · #572 1번 · 2번 · 4번). `/api/v1/staff` 전 경로는 중앙 관리자만 쓴다(관리자 직원계정 403).

직원 초대(`POST /api/v1/staff` `StaffInviteRequest` → 201 `StaffItem`)와 재발송(`POST /{user_id}/invite/resend` → 204)은
#572 1번으로 열렸다. 초대 본문은 이메일만 받고 등급 · 담당 그룹 · 기존 계정 연결 칸은 없다 — 등급은 `PUT /{user_id}/role`,
담당 그룹 배정은 `GET|PUT /{user_id}/groups`다(#572 2번 — 고객사 단위 배정 `/{user_id}/tenants` 는 없어졌다).
운영 CLI(auth.bootstrap_admin)는 첫 중앙 관리자 생성과 장애 복구용으로 남는다.

담당 그룹 교체(`PUT /{user_id}/groups` → 204): 대상은 PLATFORM 이어야 하고(아니면 404 USER_NOT_FOUND) 등급이
TENANT_MANAGER 여야 한다(아니면 409 USER_STATE_CONFLICT). 없는 그룹이 하나라도 있으면 404 GROUP_NOT_FOUND 이고 아무것도
쓰지 않는다. 지금과 같은 집합은 409 USER_STATE_CONFLICT, 빈 목록은 전부 회수다. 등급 변경은 담당 그룹을 비운다.
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
    """직원 초대(#572 1번). 만드는 계정은 담당 그룹이 없는 `TENANT_MANAGER`로 고정이라 등급 · 배정 칸이 없고,
    Cognito에 이미 있는 이메일은 409다(자동 연결 · `recover_existing` 없음 — 복구는 운영 CLI)."""

    model_config = ConfigDict(extra="forbid")

    email: Email


class StaffRoleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    platform_role: PlatformRole


class StaffGroupsRequest(BaseModel):
    """담당 그룹 전체 교체 요청. 빈 목록은 유효(모든 접근 제거). 중복 ID 는 거절한다."""

    model_config = ConfigDict(extra="forbid")

    group_ids: list[UUID]

    @field_validator("group_ids")
    @classmethod
    def _unique(cls, value: list[UUID]) -> list[UUID]:
        if len(set(value)) != len(value):
            raise ValueError("group_ids must not contain duplicates")
        return value


class StaffGroupsResponse(BaseModel):
    """직원의 담당 그룹(group_id 오름차순). 빈 목록 = 담당 그룹 없음(고객사 접근 없음)."""

    model_config = ConfigDict(extra="forbid")

    group_ids: list[UUID]


class StaffStatusRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: UserStatus
