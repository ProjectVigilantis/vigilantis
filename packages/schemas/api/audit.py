# ==============================================================================
# [파일 설명]  담당: 김승철 (Data & Rule Engine · API 계약)
# GET /api/v1/audit-events — 조치 승인 · 종료 판단의 감사 기록(Post-MVP T-2 · #544) 외부 DTO입니다.
# 대시보드(FE)와의 공개 계약이며, **구현은 이 계약 뒤에 붙는다**.
#
# 무엇을 남기나 — 고객 계정의 상태를 바꾸는 판단 두 가지
#   - ACTION_EXECUTION_STARTED: 실행이 새로 예약됐다(POST /actions/execute 의 202 · 시스템 발동).
#     사람 승인(USER_APPROVAL)과 자동 발동(선차단 · 1분 격리 · 자동 원복)을 같은 목록에 싣고,
#     `trigger_source` 로 가른다. 수동 복구 3종도 실행 예약이라 여기에 든다.
#   - INCIDENT_RESOLVED: 종료 판단이 새로 저장됐다(POST /incidents/{id}/resolve).
#   기록은 그 쓰기와 같은 트랜잭션에서 남고 고칠 수 없다(append-only). **새 행이 생길 때만**
#   남는다 — 같은 Key 재요청(200 재생) · 거절(403 · 404 · 409)은 조치가 일어나지 않아 기록이 없다.
#
# 계약 원칙 (users.py 의 이력과 같다)
#   - snake_case, UTC ISO 8601("Z") 문자열, nullable 은 null 반환, extra 금지.
#   - 항목은 `action` 으로 갈리는 닫힌 합(discriminated union)이다. 자유 문자열 칸이 없다 —
#     종료 사유(`resolution_note`)는 인시던트 상세에만 있고 이 기록에 복사하지 않는다.
#   - 행위자는 **보는 사람에 따라 드러나는 정도가 다르다**(#544 Q3).
#       TENANT_USER    같은 고객사 사용자 — 이메일을 싣는다.
#       PLATFORM_USER  플랫폼 운영자 — 플랫폼 계정이 볼 때만 이메일을 싣고, 고객사 계정에는 null 이다.
#       SYSTEM         자동 발동 — 사람이 없다. 이메일은 언제나 null 이다.
#     사람의 식별자(user_id)는 싣지 않는다 — 화면은 이메일로 충분하고, 고객사 계정에 플랫폼 운영자의
#     식별자를 내보내지 않는다.
#
# 경로 (/api/v1)
#   GET /audit-events → AuditEventsResponse. 모든 역할(VIEWER 포함 · 인시던트를 볼 수 있는 사람)이 본다.
#     질의: incident_id · since · until(occurred_at ∈ [since, until)) · cursor · limit(기본 50 · 1–100)
#     순서: occurred_at 내림차순, 같은 시각은 event_id 내림차순. `next_cursor` 가 null 이면 마지막 쪽이다.
#     커서는 불투명 문자열이다 — FE 는 받은 값을 그대로 돌려줄 뿐 해석하지 않는다. 깨진 커서는 422
#     REQUEST_VALIDATION_FAILED.
#   범위: 고객사 계정은 자기 테넌트의 기록만, 플랫폼 계정은 전부(인시던트 목록과 같은 규칙).
#     범위 밖 incident_id 는 404 가 아니라 빈 목록이다 — 목록 질의는 존재를 알리지 않는다.
# ==============================================================================

from __future__ import annotations

from enum import Enum, unique
from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..runbooks import RunbookId, TriggerSource
from .assets import UtcDateTime
from .auth import Email
from .incidents import ResolutionJudgement

AUDIT_EVENTS_DEFAULT_LIMIT = 50
AUDIT_EVENTS_MAX_LIMIT = 100


@unique
class AuditEventAction(str, Enum):
    ACTION_EXECUTION_STARTED = "ACTION_EXECUTION_STARTED"
    INCIDENT_RESOLVED = "INCIDENT_RESOLVED"


@unique
class AuditActorKind(str, Enum):
    TENANT_USER = "TENANT_USER"
    PLATFORM_USER = "PLATFORM_USER"
    SYSTEM = "SYSTEM"


class AuditActor(BaseModel):
    """누가 · 무엇이 시작했나. 이메일을 보일지는 보는 사람이 정한다(머리말 「행위자」)."""

    model_config = ConfigDict(extra="forbid")

    kind: AuditActorKind
    email: Email | None

    @model_validator(mode="after")
    def _email_follows_kind(self):
        if self.kind == AuditActorKind.TENANT_USER and self.email is None:
            raise ValueError("고객사 사용자의 기록에는 이메일이 있어야 합니다")
        if self.kind == AuditActorKind.SYSTEM and self.email is not None:
            raise ValueError("자동 발동에는 사람이 없어 이메일을 싣지 않습니다")
        return self


class _AuditEventBase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(min_length=1)
    occurred_at: UtcDateTime
    actor: AuditActor
    incident_id: str = Field(min_length=1)
    # 인시던트의 테넌트를 옮겨 싣는다. 테넌트가 없는 기존 인시던트(T-4 이전)는 null 이다.
    tenant_id: str | None


class ExecutionStartedEvent(_AuditEventBase):
    action: Literal[AuditEventAction.ACTION_EXECUTION_STARTED]
    trigger_source: TriggerSource
    execution_id: str = Field(min_length=1)
    runbook_id: RunbookId
    target_arn: str = Field(min_length=1)

    @model_validator(mode="after")
    def _person_iff_user_approval(self):
        by_person = self.actor.kind != AuditActorKind.SYSTEM
        if by_person != (self.trigger_source == TriggerSource.USER_APPROVAL):
            raise ValueError("사람 승인 실행만 사람이 시작하고, 자동 발동은 SYSTEM 이 시작합니다")
        return self


class IncidentResolvedEvent(_AuditEventBase):
    action: Literal[AuditEventAction.INCIDENT_RESOLVED]
    resolution: ResolutionJudgement

    @model_validator(mode="after")
    def _resolved_by_person(self):
        if self.actor.kind == AuditActorKind.SYSTEM:
            raise ValueError("종료 판단은 사람만 합니다")
        return self


AuditEventItem = Annotated[
    Union[ExecutionStartedEvent, IncidentResolvedEvent],
    Field(discriminator="action"),
]


class AuditEventsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[AuditEventItem] = Field(max_length=AUDIT_EVENTS_MAX_LIMIT)
    next_cursor: str | None = Field(default=..., min_length=1)
