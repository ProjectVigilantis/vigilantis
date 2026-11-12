# ==============================================================================
# [파일 설명]  담당: 김승철 (API 계약 · BE)
# GET /api/v1/platform-events — 플랫폼 활동 로그(platform_events · #589 C) 조회의 외부 DTO입니다.
# 관리자 웹의 활동 로그 화면(#589 E · FE)이 부릅니다. **구현은 이 계약 뒤에 붙는다.**
#
# 원천은 DB `platform_events`(append-only)다. 무엇을 언제 남기는지(행동 목록 · 행동별 details)는 내부 기록
# 계약 schemas.platform_events 이고, 이 파일은 그 행을 읽어 내보내는 모양만 정한다.
#
# 계약 원칙
#   - snake_case, UTC ISO 8601("Z") 문자열, nullable 은 null 반환, extra 금지.
#   - details 는 행동별 기록 계약(schemas.platform_events.ACTION_SPECS 의 details 모델)으로 검증해 저장한 값
#     그대로다. 비밀번호 · 토큰 · 이메일 같은 값은 칸 자체가 없다.
#   - 행위자 이메일은 저장하지 않고 조회 때 actor_user_id 로 이어 싣는다(지금 값이다 — 기록 시점 값이 아니다).
#     계정 종류 · 플랫폼 등급은 기록 시점 값이다.
#
# 읽는 쪽이 알아 둘 것(#589 코멘트 · #592 리뷰)
#   - request_id 는 클라이언트가 X-Request-ID 로 정할 수 있다. 같은 값이라고 같은 요청이라는 증거가 아니다.
#   - user_agent 는 받은 값을 잘라 그대로 둔 것이다 — 마크업이 남아 있을 수 있어 화면은 글자로만 출력한다.
#   - source_ip 는 앱이 본 접속지다. 실제 접속지 설정(#589 D) 전의 행은 프록시(ALB) 주소이고, 행은 고쳐 쓰지
#     않으므로 그 값이 그대로 남는다.
#
# 경로 (/api/v1)
#   GET /platform-events → PlatformEventsResponse. 플랫폼 관리 등급(CENTRAL_ADMIN · TENANT_MANAGER)만 본다.
#     고객사 계정 · 등급 없는 플랫폼 계정은 403 AUTH_FORBIDDEN(고객사 계정의 조회는 2차).
#     질의(모두 선택, 함께 주면 모두 맞는 행)
#       since · until            occurred_at ∈ [since, until). 시간대가 없으면 422.
#       category · action · outcome · actor_kind   각 enum 값 하나. 목록 밖 값은 422.
#       actor_user_id · tenant_id                  UUID. 형식이 어긋나거나 범위 밖이면 빈 목록이다.
#       target_type · target_id                    target_id 는 target_type 과 함께만 받는다(없으면 422).
#       cursor · limit           limit 기본 50 · 1–100. 커서는 불투명 문자열이다 — 깨지면 422.
#     순서: occurred_at 내림차순, 같은 시각은 event_id 내림차순. next_cursor 가 null 이면 마지막 쪽이다.
#   범위(#589 Q4)
#     CENTRAL_ADMIN   전부(고객사 없는 행 포함).
#     TENANT_MANAGER  담당 그룹 고객사의 행 + 고객사 없는 행 가운데 본인 행 — 자기가 한 행과 자기 계정이
#                     대상인 인증(AUTH) 행(자기 로그인 · MFA · 비밀번호 재설정 시도. 행위자는 ANONYMOUS 다).
#                     인증 밖에서 자기를 대상으로 한 일(등급 변경 등)은 그 직원의 행이라 넣지 않는다.
#     범위 판정은 auth.access.tenant_scope_ids 그대로다 — 담당 그룹이 바뀌면 다음 요청부터 따라간다.
#     범위 밖 고객사를 질의로 주면 404 가 아니라 빈 목록이다(존재를 알리지 않는다).
# ==============================================================================

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..platform_events import (
    ACTION_SPECS,
    REQUEST_ID_MAX_LENGTH,
    TARGET_ID_MAX_LENGTH,
    USER_AGENT_MAX_LENGTH,
    PlatformActorKind,
    PlatformEventAction,
    PlatformEventCategory,
    PlatformEventOutcome,
    PlatformEventTargetType,
)
from .assets import UtcDateTime
from .auth import AccountType, Email, PlatformRole

PLATFORM_EVENTS_DEFAULT_LIMIT = 50
PLATFORM_EVENTS_MAX_LIMIT = 100


class PlatformEventActor(BaseModel):
    """누가 · 무엇이 했나. 사람(USER)만 user_id · email · account_type 이 있다."""

    model_config = ConfigDict(extra="forbid")

    kind: PlatformActorKind
    user_id: str | None
    email: Email | None
    #: 기록 시점의 계정 종류 · 플랫폼 등급(PLATFORM 만). 뒤에 바뀌어도 그때 값이다
    account_type: AccountType | None
    platform_role: PlatformRole | None

    @model_validator(mode="after")
    def _person_only(self):
        is_user = self.kind is PlatformActorKind.USER
        person = (self.user_id, self.email, self.account_type)
        if is_user and None in person:
            raise ValueError("사람(USER)은 user_id · email · account_type 이 있습니다")
        if not is_user and any(v is not None for v in (*person, self.platform_role)):
            raise ValueError("사람이 아니면 user_id · email · 계정 종류 · 등급이 없습니다")
        if is_user and (self.account_type is AccountType.PLATFORM) != (self.platform_role is not None):
            raise ValueError("PLATFORM 계정만 플랫폼 등급이 있습니다")
        return self


class PlatformEventTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: PlatformEventTargetType
    id: str = Field(min_length=1, max_length=TARGET_ID_MAX_LENGTH)


class PlatformEventItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(min_length=1)
    occurred_at: UtcDateTime
    category: PlatformEventCategory
    action: PlatformEventAction
    outcome: PlatformEventOutcome
    actor: PlatformEventActor
    #: 그 일이 속한 고객사. 고객사 없는 일(플랫폼 계정 로그인 등)은 null
    tenant_id: str | None
    target: PlatformEventTarget | None
    #: 행동별 기록 계약의 값(거절 · 실패면 reason_code 가 있다)
    details: dict[str, Any]
    session_id: str | None
    request_id: str | None = Field(max_length=REQUEST_ID_MAX_LENGTH)
    source_ip: str | None
    user_agent: str | None = Field(max_length=USER_AGENT_MAX_LENGTH)

    @model_validator(mode="after")
    def _follows_the_action(self):
        spec = ACTION_SPECS[self.action]
        if self.category is not spec.category:
            raise ValueError(f"{self.action.value} 의 분류는 {spec.category.value} 입니다")
        if self.target is not None and self.target.type is not spec.target:
            raise ValueError(f"{self.action.value} 의 대상 종류가 아닙니다")
        return self


class PlatformEventsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[PlatformEventItem] = Field(max_length=PLATFORM_EVENTS_MAX_LIMIT)
    next_cursor: str | None = Field(default=..., min_length=1)
