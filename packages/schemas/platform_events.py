# ==============================================================================
# [파일 설명]  담당: 김승철 (API 계약 · BE)
# 플랫폼 활동 로그(platform_events · #589)의 기록 계약 — 행동 목록 · 행동별 규칙 · details 스키마.
#
#   - 한 행이 플랫폼 활동 한 건이다. 행에는 일어난 사실만 남긴다(ISMS 보고서 · 사고 조사의 근거 로그).
#   - 행동(action)은 DB enum 이 아니라 문자열 칸이다 — 기능마다 늘어나므로 값마다 마이그레이션을 두지 않는다.
#     대신 이 목록(PlatformEventAction) 밖 값은 기록 계약(PlatformEventRecord)이 거절한다.
#   - 행동마다 분류 · 허용 결과 · 허용 행위자 · 대상 종류 · details 모델이 하나씩 정해져 있다(ACTION_SPECS).
#     분류와 대상 종류는 행동에서 나오므로 호출부가 따로 넘기지 않는다.
#   - 거절 · 실패(DENIED · FAILED)면 details.reason_code 가 있고, 성공이면 없다(DB CHECK 도 같다).
#   - details 는 extra="forbid" 다 — 정한 칸 말고는 들어가지 않는다. 비밀번호 · 토큰 · 세션 원문 · TOTP 비밀 ·
#     ExternalId · AWS 자격 · 이메일 같은 개인정보는 칸 자체가 없다(사람은 user_id 로 잇는다).
#   - 이 파일은 내부 기록 계약이다. 조회 API 의 공개 계약은 schemas.api 쪽에 따로 둔다.
# ==============================================================================

from __future__ import annotations

import uuid
from dataclasses import dataclass
from enum import Enum, unique
from typing import Any, Literal, Mapping, Optional

from pydantic import BaseModel, ConfigDict, Field, model_serializer, model_validator

from .api.auth import AccountType, FeaturePermission, PlatformRole
from .api.tenants import TenantVerificationResult

#: action 칸 길이(varchar). 행동 이름이 이보다 길면 계약 테스트가 깨진다.
ACTION_MAX_LENGTH = 64
#: target_id 칸 길이(varchar).
TARGET_ID_MAX_LENGTH = 128
#: user_agent 칸 길이(varchar). 더 긴 값은 기록 함수가 자른다.
USER_AGENT_MAX_LENGTH = 512
#: request_id 칸 길이 — main.request_context 가 받는 X-Request-ID 의 상한과 같다.
REQUEST_ID_MAX_LENGTH = 64
#: 경로 칸 길이(ACCESS 행동의 details.path).
PATH_MAX_LENGTH = 512

#: 사유 코드 — 계약 오류 코드(ErrorCode) · AWS 오류 코드 · 예외 클래스명을 받는다.
_REASON_PATTERN = r"^[A-Za-z0-9_.:-]{1,128}$"
#: 상태 · 역할 · 등급 같은 짧은 enum 값.
_STATE_PATTERN = r"^[A-Za-z0-9_]{1,64}$"


@unique
class PlatformEventCategory(str, Enum):
    AUTH = "AUTH"
    ACCESS = "ACCESS"
    USER_ADMIN = "USER_ADMIN"
    TENANT_ADMIN = "TENANT_ADMIN"
    INCIDENT = "INCIDENT"
    ACTION = "ACTION"
    SUPPORT = "SUPPORT"
    COLLECTION = "COLLECTION"
    NOTIFICATION = "NOTIFICATION"


@unique
class PlatformEventOutcome(str, Enum):
    SUCCEEDED = "SUCCEEDED"
    #: 권한 · 정책 · 자격으로 거절했다(틀린 비밀번호 · 403 · 가드레일 거절 등).
    DENIED = "DENIED"
    #: 오류로 끝나지 못했다(AWS 오류 · 외부 서비스 실패 등).
    FAILED = "FAILED"


@unique
class PlatformActorKind(str, Enum):
    #: 로그인한 사람 — actor_user_id 가 있다.
    USER = "USER"
    #: 플랫폼의 루프 · 자동 발동.
    SYSTEM = "SYSTEM"
    #: 로그인 전(로그인 시도 · 도입 신청 등).
    ANONYMOUS = "ANONYMOUS"


@unique
class PlatformEventTargetType(str, Enum):
    USER = "USER"
    TENANT = "TENANT"
    INCIDENT = "INCIDENT"
    EXECUTION = "EXECUTION"
    SIGNUP_REQUEST = "SIGNUP_REQUEST"
    INQUIRY = "INQUIRY"
    COLLECTION_RUN = "COLLECTION_RUN"
    NOTIFICATION = "NOTIFICATION"
    TENANT_GROUP = "TENANT_GROUP"


@unique
class PlatformEventAction(str, Enum):
    """1차 행동 목록(#589 B). 결과는 outcome 칸이 싣는다 — 이름에 성공 · 실패를 넣지 않는다.

    **값은 더하기만 한다.** 빼거나 이름을 바꾸면 그 값을 가진 기록이 조회되지 않는다 — action 칸은 문자열이고
    조회 응답(schemas.api.platform_events)은 이 목록으로 검증해, 목록 밖 값이 한 행이라도 있으면 그 쪽 전체가
    500 이다. 행은 고칠 수 없다.
    """

    # 로그인 · 세션
    AUTH_LOGIN = "AUTH_LOGIN"
    AUTH_MFA = "AUTH_MFA"
    AUTH_LOGOUT = "AUTH_LOGOUT"
    AUTH_PASSWORD_RESET = "AUTH_PASSWORD_RESET"
    AUTH_TOTP_RECOVERY = "AUTH_TOTP_RECOVERY"
    # 권한 거절
    ACCESS_FORBIDDEN = "ACCESS_FORBIDDEN"
    ACCESS_OUT_OF_SCOPE = "ACCESS_OUT_OF_SCOPE"
    # 사용자 · 직원 관리 — user_admin_events(UserEventAction)와 짝이다
    USER_INVITE = "USER_INVITE"
    USER_INVITE_RESEND = "USER_INVITE_RESEND"
    USER_ROLE_CHANGE = "USER_ROLE_CHANGE"
    USER_STATUS_CHANGE = "USER_STATUS_CHANGE"
    USER_SESSIONS_REVOKE = "USER_SESSIONS_REVOKE"
    USER_PLATFORM_ROLE_CHANGE = "USER_PLATFORM_ROLE_CHANGE"
    USER_GROUP_ASSIGNMENTS_CHANGE = "USER_GROUP_ASSIGNMENTS_CHANGE"
    USER_PERMISSIONS_CHANGE = "USER_PERMISSIONS_CHANGE"
    # 고객사 관리
    TENANT_GROUP_CREATE = "TENANT_GROUP_CREATE"
    TENANT_GROUP_DELETE = "TENANT_GROUP_DELETE"
    TENANT_GROUP_ASSIGNMENT_CHANGE = "TENANT_GROUP_ASSIGNMENT_CHANGE"
    TENANT_CREATE = "TENANT_CREATE"
    TENANT_UPDATE = "TENANT_UPDATE"
    TENANT_SUSPEND = "TENANT_SUSPEND"
    TENANT_RESUME = "TENANT_RESUME"
    TENANT_VERIFY = "TENANT_VERIFY"
    TENANT_AUTO_ACTION_CHANGE = "TENANT_AUTO_ACTION_CHANGE"
    TENANT_DELETE = "TENANT_DELETE"
    # 인시던트 · 조치
    INCIDENT_CREATE = "INCIDENT_CREATE"
    INCIDENT_ANALYZE = "INCIDENT_ANALYZE"
    INCIDENT_RESOLVE = "INCIDENT_RESOLVE"
    INCIDENT_EVIDENCE_EXPORT = "INCIDENT_EVIDENCE_EXPORT"
    ACTION_APPROVE = "ACTION_APPROVE"
    ACTION_AUTO_TRIGGER = "ACTION_AUTO_TRIGGER"
    ACTION_BLOCK = "ACTION_BLOCK"
    ACTION_COMPLETE = "ACTION_COMPLETE"
    ACTION_ROLLBACK = "ACTION_ROLLBACK"
    # 신청 · 문의
    SIGNUP_REQUEST_CREATE = "SIGNUP_REQUEST_CREATE"
    SIGNUP_REQUEST_DECIDE = "SIGNUP_REQUEST_DECIDE"
    INQUIRY_CREATE = "INQUIRY_CREATE"
    INQUIRY_STATUS_CHANGE = "INQUIRY_STATUS_CHANGE"
    INQUIRY_NOTE_ADD = "INQUIRY_NOTE_ADD"
    # 수집 · 알림 — 회차 · 건 단위(자산 하나하나는 남기지 않는다)
    COLLECTION_RUN = "COLLECTION_RUN"
    NOTIFICATION_SEND = "NOTIFICATION_SEND"


# --- details 모델 -----------------------------------------------------------------


class PlatformEventDetails(BaseModel):
    """모든 details 의 바탕. reason_code 는 거절 · 실패에만 있다(PlatformEventRecord 가 결과와 맞춘다)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    reason_code: Optional[str] = Field(default=None, pattern=_REASON_PATTERN)

    def check_outcome(self, outcome: "PlatformEventOutcome") -> None:
        """결과에 따라 칸이 달라지는 모델이 덮어쓴다(PlatformEventRecord 가 reason_code 검사 뒤 부른다)."""


class TransitionDetails(PlatformEventDetails):
    """무엇이 무엇으로 바뀌었나 — 상태 · 역할 · 등급 · 자동 조치 켜짐 같은 짧은 값."""

    before: Optional[str] = Field(default=None, pattern=_STATE_PATTERN)
    after: Optional[str] = Field(default=None, pattern=_STATE_PATTERN)


class GroupAssignmentsDetails(PlatformEventDetails):
    """담당 그룹 교체 — 전 · 후 그룹 ID 목록(정규 UUID). 빈 목록은 담당 없음이다."""

    before: tuple[uuid.UUID, ...] = ()
    after: tuple[uuid.UUID, ...] = ()


class TenantGroupChangeDetails(PlatformEventDetails):
    """고객사의 그룹 지정 · 해제. null 은 그룹 없음이며 전 · 후를 모두 명시한다."""

    before: Optional[uuid.UUID]
    after: Optional[uuid.UUID]

    @model_serializer(mode="wrap")
    def _with_null_groups(self, handler):
        values = handler(self)
        # 기록 정규화의 exclude_none 에도 그룹 해제라는 사실은 남긴다.
        if self.before is None:
            values["before"] = None
        if self.after is None:
            values["after"] = None
        return values


class PermissionsChangeDetails(PlatformEventDetails):
    """기능별 권한 교체 — 계약에 있는 권한 이름만 받으며 빈 목록은 전부 회수다."""

    before: tuple[FeaturePermission, ...]
    after: tuple[FeaturePermission, ...]


class TenantGroupDeleteDetails(PlatformEventDetails):
    """그룹 삭제로 소속이 풀린 고객사와 담당 배정이 지워진 직원 ID. 이름은 남기지 않는다."""

    tenant_ids: tuple[uuid.UUID, ...]
    #: 계정 활성 여부와 무관하게 삭제된 직원 담당 배정을 모두 남긴다.
    staff_user_ids: tuple[uuid.UUID, ...]


#: PATCH /tenants/{id} 가 받는 칸 가운데 TENANT_UPDATE 로 남기는 것. `auto_action_enabled` 는 여기 없다 —
#: 켜고 끄는 일은 TENANT_AUTO_ACTION_CHANGE 가 전 · 후 값과 함께 따로 남긴다.
TenantUpdateField = Literal["name", "regions", "collect_vpc_ids", "subscription"]


class TenantUpdateDetails(PlatformEventDetails):
    """고객사 수정에서 **값이 실제로 바뀐** 칸의 이름(이름순 · 중복 없음). 값은 싣지 않는다.

    보냈지만 지금 값과 같은 칸은 들어가지 않는다. 바뀐 칸이 하나도 없으면 호출부가 행을 남기지 않는다.
    """

    changed_fields: tuple[TenantUpdateField, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _sorted_and_unique(self) -> "TenantUpdateDetails":
        if list(self.changed_fields) != sorted(set(self.changed_fields)):
            raise ValueError("changed_fields 는 이름순이고 중복이 없습니다")
        return self


class TenantVerifyDetails(PlatformEventDetails):
    """연결 확인 결과(TenantVerificationResult)를 그대로 싣는다.

    확인을 돌렸으면(성공 · 실패) 결과가 있고, 성공은 OK 뿐이다. 거절(상태 충돌)은 결과가 있어도 없어도 된다 —
    확인 전에 멈추면 없고, 확인 뒤 다시 읽어 충돌을 만나면 그 결과를 싣는다. 403 은 ACCESS_FORBIDDEN 으로
    따로 남는다.
    """

    result: Optional[TenantVerificationResult] = None

    def check_outcome(self, outcome: "PlatformEventOutcome") -> None:
        if outcome is PlatformEventOutcome.DENIED:
            return
        if self.result is None:
            raise ValueError("연결 확인을 돌렸으면(성공 · 실패) result 가 있습니다")
        if (outcome is PlatformEventOutcome.SUCCEEDED) != (
            self.result is TenantVerificationResult.OK
        ):
            raise ValueError("연결 확인은 결과가 OK 일 때만 성공입니다")


class AccessDetails(PlatformEventDetails):
    """권한 거절이 일어난 요청. 경로만 싣는다 — 질의 문자열 · 본문은 싣지 않는다."""

    method: str = Field(pattern=r"^[A-Z]{3,7}$")
    path: str = Field(min_length=1, max_length=PATH_MAX_LENGTH, pattern=r"^/[^?#\s]*$")


class DecisionDetails(PlatformEventDetails):
    """신청의 승인 · 반려 같은 판정 값."""

    decision: str = Field(pattern=_STATE_PATTERN)


class EvidenceExportDetails(PlatformEventDetails):
    """파일을 준비해 반출 감사 기록을 확정했다. 클라이언트의 수신 완료를 뜻하지 않는다."""

    format: Literal["md", "json"]
    evidence_count: int = Field(ge=0, strict=True)


# --- 행동별 규칙 --------------------------------------------------------------------

_S = PlatformEventOutcome.SUCCEEDED
_D = PlatformEventOutcome.DENIED
_F = PlatformEventOutcome.FAILED
_USER = PlatformActorKind.USER
_SYSTEM = PlatformActorKind.SYSTEM
_ANON = PlatformActorKind.ANONYMOUS
_C = PlatformEventCategory
_T = PlatformEventTargetType
_A = PlatformEventAction


@dataclass(frozen=True)
class ActionSpec:
    category: PlatformEventCategory
    outcomes: frozenset[PlatformEventOutcome]
    actors: frozenset[PlatformActorKind]
    #: 대상 종류. None 이면 대상이 없다. 값이 있어도 대상 ID 는 없을 수 있다(없는 계정으로 로그인 시도).
    target: Optional[PlatformEventTargetType]
    details: type[PlatformEventDetails] = PlatformEventDetails


def _spec(
    category, outcomes, actors, target, details=PlatformEventDetails
) -> ActionSpec:
    return ActionSpec(category, frozenset(outcomes), frozenset(actors), target, details)


ACTION_SPECS: Mapping[PlatformEventAction, ActionSpec] = {
    # 로그인 시도의 행위자는 로그인 전이라 ANONYMOUS 다 — 시도한 계정은 대상(USER)으로 잇는다
    _A.AUTH_LOGIN: _spec(_C.AUTH, {_S, _D, _F}, {_ANON}, _T.USER),
    _A.AUTH_MFA: _spec(_C.AUTH, {_S, _D, _F}, {_ANON}, _T.USER),
    _A.AUTH_LOGOUT: _spec(_C.AUTH, {_S}, {_USER}, None),
    _A.AUTH_PASSWORD_RESET: _spec(_C.AUTH, {_S, _D, _F}, {_ANON}, _T.USER),
    _A.AUTH_TOTP_RECOVERY: _spec(_C.AUTH, {_S, _D, _F}, {_USER, _SYSTEM}, _T.USER),
    _A.ACCESS_FORBIDDEN: _spec(_C.ACCESS, {_D}, {_USER}, None, AccessDetails),
    _A.ACCESS_OUT_OF_SCOPE: _spec(_C.ACCESS, {_D}, {_USER}, None, AccessDetails),
    _A.USER_INVITE: _spec(_C.USER_ADMIN, {_S, _F}, {_USER, _SYSTEM}, _T.USER),
    _A.USER_INVITE_RESEND: _spec(_C.USER_ADMIN, {_S, _F}, {_USER}, _T.USER),
    _A.USER_ROLE_CHANGE: _spec(
        _C.USER_ADMIN, {_S}, {_USER}, _T.USER, TransitionDetails
    ),
    _A.USER_STATUS_CHANGE: _spec(
        _C.USER_ADMIN, {_S}, {_USER}, _T.USER, TransitionDetails
    ),
    _A.USER_SESSIONS_REVOKE: _spec(_C.USER_ADMIN, {_S}, {_USER}, _T.USER),
    _A.USER_PLATFORM_ROLE_CHANGE: _spec(
        _C.USER_ADMIN, {_S}, {_USER}, _T.USER, TransitionDetails
    ),
    _A.USER_GROUP_ASSIGNMENTS_CHANGE: _spec(
        _C.USER_ADMIN, {_S}, {_USER}, _T.USER, GroupAssignmentsDetails
    ),
    _A.USER_PERMISSIONS_CHANGE: _spec(
        _C.USER_ADMIN, {_S}, {_USER}, _T.USER, PermissionsChangeDetails
    ),
    _A.TENANT_GROUP_CREATE: _spec(_C.TENANT_ADMIN, {_S}, {_USER}, _T.TENANT_GROUP),
    _A.TENANT_GROUP_DELETE: _spec(
        _C.TENANT_ADMIN, {_S}, {_USER}, _T.TENANT_GROUP, TenantGroupDeleteDetails
    ),
    _A.TENANT_GROUP_ASSIGNMENT_CHANGE: _spec(
        _C.TENANT_ADMIN, {_S}, {_USER}, _T.TENANT, TenantGroupChangeDetails
    ),
    _A.TENANT_CREATE: _spec(_C.TENANT_ADMIN, {_S}, {_USER}, _T.TENANT),
    _A.TENANT_UPDATE: _spec(
        _C.TENANT_ADMIN, {_S}, {_USER}, _T.TENANT, TenantUpdateDetails
    ),
    _A.TENANT_SUSPEND: _spec(
        _C.TENANT_ADMIN, {_S}, {_USER, _SYSTEM}, _T.TENANT, TransitionDetails
    ),
    _A.TENANT_RESUME: _spec(
        _C.TENANT_ADMIN, {_S, _D, _F}, {_USER}, _T.TENANT, TenantVerifyDetails
    ),
    _A.TENANT_VERIFY: _spec(
        _C.TENANT_ADMIN, {_S, _D, _F}, {_USER}, _T.TENANT, TenantVerifyDetails
    ),
    _A.TENANT_AUTO_ACTION_CHANGE: _spec(
        _C.TENANT_ADMIN, {_S}, {_USER}, _T.TENANT, TransitionDetails
    ),
    _A.TENANT_DELETE: _spec(_C.TENANT_ADMIN, {_S}, {_USER}, _T.TENANT),
    _A.INCIDENT_CREATE: _spec(_C.INCIDENT, {_S}, {_SYSTEM}, _T.INCIDENT),
    _A.INCIDENT_ANALYZE: _spec(
        _C.INCIDENT, {_S, _F}, {_SYSTEM}, _T.INCIDENT, TransitionDetails
    ),
    _A.INCIDENT_RESOLVE: _spec(
        _C.INCIDENT, {_S}, {_USER}, _T.INCIDENT, TransitionDetails
    ),
    _A.INCIDENT_EVIDENCE_EXPORT: _spec(
        _C.INCIDENT, {_S}, {_USER}, _T.INCIDENT, EvidenceExportDetails
    ),
    _A.ACTION_APPROVE: _spec(_C.ACTION, {_S}, {_USER}, _T.EXECUTION),
    _A.ACTION_AUTO_TRIGGER: _spec(_C.ACTION, {_S}, {_SYSTEM}, _T.EXECUTION),
    # 테넌트 관문 · 가드레일이 실행 전에 거절했다 — 실행 행이 없을 수 있어 대상은 인시던트다
    _A.ACTION_BLOCK: _spec(_C.ACTION, {_D}, {_USER, _SYSTEM}, _T.INCIDENT),
    _A.ACTION_COMPLETE: _spec(_C.ACTION, {_S, _F}, {_SYSTEM}, _T.EXECUTION),
    _A.ACTION_ROLLBACK: _spec(_C.ACTION, {_S, _F}, {_USER, _SYSTEM}, _T.EXECUTION),
    _A.SIGNUP_REQUEST_CREATE: _spec(_C.SUPPORT, {_S}, {_ANON}, _T.SIGNUP_REQUEST),
    _A.SIGNUP_REQUEST_DECIDE: _spec(
        _C.SUPPORT, {_S}, {_USER}, _T.SIGNUP_REQUEST, DecisionDetails
    ),
    _A.INQUIRY_CREATE: _spec(_C.SUPPORT, {_S}, {_USER}, _T.INQUIRY),
    _A.INQUIRY_STATUS_CHANGE: _spec(
        _C.SUPPORT, {_S}, {_USER}, _T.INQUIRY, TransitionDetails
    ),
    _A.INQUIRY_NOTE_ADD: _spec(_C.SUPPORT, {_S}, {_USER}, _T.INQUIRY),
    _A.COLLECTION_RUN: _spec(_C.COLLECTION, {_S, _F}, {_SYSTEM}, _T.COLLECTION_RUN),
    _A.NOTIFICATION_SEND: _spec(_C.NOTIFICATION, {_S, _F}, {_SYSTEM}, _T.NOTIFICATION),
}


# --- 기록 계약 ----------------------------------------------------------------------


class PlatformEventRecord(BaseModel):
    """한 행으로 남길 값 — 기록 함수(db/repositories/platform_events.py)가 이것을 거쳐 쓴다.

    분류(category)와 대상 종류(target_type)는 행동에서 정해진다(ACTION_SPECS). details 는 행동의 모델로
    검증한 뒤 JSON 으로 정규화한 dict 다(None 칸은 빠진다).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    action: PlatformEventAction
    outcome: PlatformEventOutcome = PlatformEventOutcome.SUCCEEDED
    actor_kind: PlatformActorKind
    actor_user_id: Optional[uuid.UUID] = None
    actor_account_type: Optional[AccountType] = None
    actor_platform_role: Optional[PlatformRole] = None
    tenant_id: Optional[uuid.UUID] = None
    target_id: Optional[str] = Field(
        default=None, min_length=1, max_length=TARGET_ID_MAX_LENGTH
    )
    details: dict[str, Any] = Field(default_factory=dict)

    @property
    def spec(self) -> ActionSpec:
        return ACTION_SPECS[self.action]

    @property
    def category(self) -> PlatformEventCategory:
        return self.spec.category

    @property
    def target_type(self) -> Optional[PlatformEventTargetType]:
        return self.spec.target if self.target_id is not None else None

    @model_validator(mode="after")
    def _follows_the_action(self) -> "PlatformEventRecord":
        spec = ACTION_SPECS[self.action]
        if self.outcome not in spec.outcomes:
            raise ValueError(
                f"{self.action.value} 에 없는 결과입니다: {self.outcome.value}"
            )
        if self.actor_kind not in spec.actors:
            raise ValueError(
                f"{self.action.value} 의 행위자가 될 수 없습니다: {self.actor_kind.value}"
            )
        is_user = self.actor_kind is PlatformActorKind.USER
        if is_user != (self.actor_user_id is not None):
            raise ValueError(
                "사람(USER)만 actor_user_id 가 있고, 사람은 반드시 있어야 합니다"
            )
        if is_user:
            if self.actor_account_type not in (
                AccountType.PLATFORM,
                AccountType.TENANT,
            ):
                raise ValueError("사람의 계정 종류는 PLATFORM 또는 TENANT 입니다")
            if (self.actor_account_type is AccountType.PLATFORM) != (
                self.actor_platform_role is not None
            ):
                raise ValueError(
                    "PLATFORM 계정만 플랫폼 등급이 있고, 반드시 있어야 합니다"
                )
        elif (
            self.actor_account_type is not None or self.actor_platform_role is not None
        ):
            raise ValueError("사람이 아니면 계정 종류 · 플랫폼 등급이 없습니다")
        if spec.target is None and self.target_id is not None:
            raise ValueError(f"{self.action.value} 에는 대상이 없습니다")
        if spec.target is PlatformEventTargetType.TENANT:
            # 고객사가 대상이면 소속도 그 고객사다 — 한쪽만 넘기면 고객사 범위 조회에서 빠진다
            if self.tenant_id is None or self.target_id != str(self.tenant_id):
                raise ValueError("고객사가 대상이면 tenant_id 와 target_id 가 같은 고객사입니다")
        details = spec.details.model_validate(self.details)
        failed = self.outcome is not PlatformEventOutcome.SUCCEEDED
        if failed != (details.reason_code is not None):
            raise ValueError("거절 · 실패면 reason_code 가 있고, 성공이면 없습니다")
        details.check_outcome(self.outcome)
        # 정규화한 값으로 바꿔 둔다 — 저장하는 값이 곧 검증한 값이다
        object.__setattr__(
            self, "details", details.model_dump(mode="json", exclude_none=True)
        )
        return self
