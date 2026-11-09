"""ADMIN 사용자 관리 계약 (#492 PR 3). 계정 비밀값과 Cognito 원본은 반환하지 않는다."""

from __future__ import annotations

from enum import Enum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .assets import UtcDateTime
from .auth import AccountScope, Email, FeaturePermission, PlatformRole, UserRole


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
    #: (#572 5번) OPERATOR 의 기능별 권한(permissions) 교체.
    PERMISSIONS_CHANGED = "PERMISSIONS_CHANGED"


#: 조회(VIEW)를 함께 요구하는 권한 — 실행 · 종료 화면은 조회 위에 있다(#572 5번 제안).
VIEW_DEPENDENT_PERMISSIONS = frozenset(
    {FeaturePermission.EXECUTE_ACTION, FeaturePermission.RESOLVE_INCIDENT}
)


def _ordered(permissions: list[FeaturePermission]) -> list[FeaturePermission]:
    """중복 없는 입력을 열거 선언 순서로 정렬한다 — 저장 · 응답 · 이력의 순서가 같다."""
    if len(set(permissions)) != len(permissions):
        raise ValueError("permissions 는 중복될 수 없습니다")
    return [p for p in FeaturePermission if p in permissions]


def _require_view_with_dependents(permissions: list[FeaturePermission]) -> None:
    if (
        VIEW_DEPENDENT_PERMISSIONS.intersection(permissions)
        and FeaturePermission.VIEW not in permissions
    ):
        raise ValueError("EXECUTE_ACTION · RESOLVE_INCIDENT 는 VIEW 와 함께 부여합니다")


class UserListItem(AccountScope):
    model_config = ConfigDict(extra="forbid")

    user_id: str = Field(min_length=1)
    email: Email
    role: UserRole
    status: UserStatus
    last_login_at: UtcDateTime | None
    #: (#572 5번) 이 계정이 지금 쓸 수 있는 기능별 권한 — ADMIN 은 네 가지 전부, OPERATOR 는 고객사 중앙계정이
    #: 준 것, VIEWER 는 빈 목록(대시보드 조회만). 열거 선언 순서다.
    permissions: list[FeaturePermission]


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
    #: (#572 5번) OPERATOR 초대에만 쓴다. 생략 · 빈 목록 = 권한 없이 초대(기본 선택 없음). 다른 역할에 주면 422.
    permissions: list[FeaturePermission] = Field(default_factory=list)

    @field_validator("permissions")
    @classmethod
    def _normalize_permissions(cls, value: list[FeaturePermission]) -> list[FeaturePermission]:
        _require_view_with_dependents(value)
        return _ordered(value)

    @model_validator(mode="after")
    def _permissions_only_for_operator(self):
        if self.permissions and self.role is not UserRole.OPERATOR:
            raise ValueError("permissions 는 OPERATOR 초대에만 줍니다")
        return self


class UserRoleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: UserRole


class UserStatusRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: UserStatus


class UserPermissionsRequest(BaseModel):
    """PUT /api/v1/users/{user_id}/permissions — OPERATOR 의 기능별 권한을 통째로 바꾼다(#572 5번).

    빈 목록은 전부 회수다. EXECUTE_ACTION · RESOLVE_INCIDENT 를 넣으려면 VIEW 도 넣어야 한다(422) —
    조회를 거두려면 실행 · 종료도 함께 거둔다. 대상이 OPERATOR 가 아니면 서버가 409 USER_STATE_CONFLICT 다.
    """

    model_config = ConfigDict(extra="forbid")

    permissions: list[FeaturePermission]

    @field_validator("permissions")
    @classmethod
    def _normalize_permissions(cls, value: list[FeaturePermission]) -> list[FeaturePermission]:
        _require_view_with_dependents(value)
        return _ordered(value)


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
    #: (#572 5번) OPERATOR 기능별 권한 전후(PERMISSIONS_CHANGED · USER_INVITED · ROLE_CHANGED). 빈 목록 = 권한 없음.
    permissions: list[FeaturePermission] | None = None


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
