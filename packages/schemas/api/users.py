"""ADMIN 사용자 관리 계약 (#492 PR 3). 계정 비밀값과 Cognito 원본은 반환하지 않는다."""

from __future__ import annotations

from enum import Enum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from .assets import UtcDateTime
from .auth import AccountScope, Email, PlatformRole, UserRole


class UserStatus(str, Enum):
    ACTIVE = "ACTIVE"
    DISABLED = "DISABLED"


class UserEventAction(str, Enum):
    USER_INVITED = "USER_INVITED"
    INVITE_RESENT = "INVITE_RESENT"
    ROLE_CHANGED = "ROLE_CHANGED"
    STATUS_CHANGED = "STATUS_CHANGED"
    SESSIONS_REVOKED = "SESSIONS_REVOKED"
    #: 플랫폼 계정의 등급(platform_role) 변경.
    PLATFORM_ROLE_CHANGED = "PLATFORM_ROLE_CHANGED"
    #: TENANT_MANAGER 의 담당 고객사(tenant_ids) 교체 · 회수.
    TENANT_ASSIGNMENTS_CHANGED = "TENANT_ASSIGNMENTS_CHANGED"


class UserListItem(AccountScope):
    model_config = ConfigDict(extra="forbid")

    user_id: str = Field(min_length=1)
    email: Email
    role: UserRole
    status: UserStatus
    last_login_at: UtcDateTime | None


class UsersResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[UserListItem]


class UserInviteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: Email
    role: UserRole
    # 플랫폼은 명시 필수, 고객사 ADMIN은 서버가 자기 소속으로 한정한다.
    tenant_id: UUID | None = None
    # 기본 초대에서 중복 계정을 자동 연결하지 않는다. PLATFORM ADMIN의 명시적 복구 선택만 허용한다(고객사 ADMIN은 403).
    recover_existing: bool = False


class UserRoleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: UserRole


class UserStatusRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: UserStatus


class UserEventState(BaseModel):
    """이력에 들어갈 수 있는 값의 닫힌 목록. 자유 문자열·토큰·코드 필드는 없다."""

    model_config = ConfigDict(extra="forbid")

    role: UserRole | None = None
    platform_role: PlatformRole | None = None
    status: UserStatus | None = None
    revoked_session_count: int | None = Field(default=None, ge=0)
    recover_existing: bool | None = None
    tenant_id: UUID | None = None
    #: 담당 고객사 배정 전후(TENANT_ASSIGNMENTS_CHANGED). 빈 목록 = 담당 없음.
    tenant_ids: list[UUID] | None = None


class UserEventItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(min_length=1)
    actor_user_id: str = Field(min_length=1)
    target_user_id: str = Field(min_length=1)
    action: UserEventAction
    before_state: UserEventState
    after_state: UserEventState
    occurred_at: UtcDateTime


class UserEventsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[UserEventItem]
