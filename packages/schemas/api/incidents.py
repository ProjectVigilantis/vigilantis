# ==============================================================================
# [파일 설명]  담당: 안성일 (AI/Guardrail · Architect)
# GET /api/v1/incidents/{id}(상세)·GET /api/v1/incidents(목록) 외부 응답 DTO입니다.
# Dashboard(FE)와의 공개 계약입니다. (확정 설계 4.3 + PROJECT_STATUS API 계약)
#
# 계약 원칙
#   - title은 nullable — 분석 전 null이며, 그 경우 FE가 category+대상 ARN 축약으로
#     표시한다(2026-08-14 합의, Issue #45 코멘트).
#   - 목록(IncidentListItem)은 상세의 부분집합 10필드다. 정렬 created_at 내림차순·
#     전체 반환(페이지네이션 Post-MVP)·필터 검증은 라우터 계약이다.
#   - 초기 판정과 AI 사후 평가 분리: initial_risk_level(불변)과 reviewed_risk_level을
#     서로 덮어쓰지 않는다. 평가 전·실패 시 reviewed는 null.
#   - FINOPS는 두 위험도·response_mode가 전부 null이다(위험 대응 축 없음).
#   - summary_lines는 분석 완료 시 정확히 3개, 분석 중·분석 실패 시 빈 배열.
#   - recommendations는 AI 추천 가능(본편 7종)·Guardrail PASS 제안만 담고,
#     Incident당 같은 runbook_id는 최대 1개 — (incident_id, runbook_id)가 외부 식별자.
#     display_parameters는 화면 표시 전용이라 실행 요청에 되돌려 받지 않는다.
#   - executions의 available_recovery_runbook_ids는 롤백 3종만 — 관제자 복구 조치.
#     (RUNBOOK_NACL_RESTORE는 AI 추천 가능한 주 조치라 recommendations 경로)
#     이 필드는 PR #44에서 팀 계약으로 확정됐다.
# ==============================================================================

from __future__ import annotations

from enum import Enum, unique
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..runbooks import AI_RECOMMENDABLE_RUNBOOK_IDS, ROLLBACK_RUNBOOK_IDS, RunbookId
from .actions import ExecutionStatus
from .assets import UtcDateTime


@unique
class IncidentCategory(str, Enum):
    FINOPS = "FINOPS"
    SECOPS = "SECOPS"


@unique
class IncidentStatus(str, Enum):
    ANALYZING = "ANALYZING"                  # AI 분석 또는 Guardrail 검증 미완
    AWAITING_APPROVAL = "AWAITING_APPROVAL"  # 실행 가능한 제안 ≥1, 진행 중 실행 없음
    ACTION_IN_PROGRESS = "ACTION_IN_PROGRESS"
    RESOLVED = "RESOLVED"                    # 더 진행할 제안·실행 없음(자산 원복 의미 아님)
    FAILED = "FAILED"                        # 흐름 진행 불가(수행된 조치 결과는 executions)


@unique
class RiskLevel(str, Enum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


@unique
class ResponseMode(str, Enum):
    """SSOT 3단계 위험 대응 — 실제 적용된 현재 대응 경로."""

    PRE_MITIGATION_0_5S = "PRE_MITIGATION_0_5S"
    AGENT_WAIT = "AGENT_WAIT"
    TIMEOUT_ISOLATION_1M = "TIMEOUT_ISOLATION_1M"


class RecommendationItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    runbook_id: RunbookId
    target_arn: str = Field(min_length=1)
    display_parameters: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _ai_recommendable_only(self):
        # ADR-0004 정책 ②: AI 추천은 본편 7종만 — 롤백 3종은 recommendations에 못 온다
        if self.runbook_id.value not in AI_RECOMMENDABLE_RUNBOOK_IDS:
            raise ValueError("recommendations에는 AI 추천 가능 Runbook(본편 7종)만 올 수 있습니다")
        return self


class ExecutionSummaryItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    execution_id: str = Field(min_length=1)
    runbook_id: RunbookId
    status: ExecutionStatus
    available_recovery_runbook_ids: list[RunbookId] = Field(default_factory=list)
    updated_at: UtcDateTime

    @model_validator(mode="after")
    def _recovery_is_rollback_only(self):
        for rid in self.available_recovery_runbook_ids:
            if rid.value not in ROLLBACK_RUNBOOK_IDS:
                raise ValueError(
                    "available_recovery_runbook_ids에는 롤백 3종만 올 수 있습니다"
                    " (주 조치 계열 복구는 recommendations 경로)"
                )
        return self


class IncidentResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    incident_id: str = Field(min_length=1)
    title: Optional[str] = Field(None, min_length=1)
    subject_arn: str = Field(min_length=1)
    category: IncidentCategory
    status: IncidentStatus
    initial_risk_level: Optional[RiskLevel] = None
    reviewed_risk_level: Optional[RiskLevel] = None
    response_mode: Optional[ResponseMode] = None
    summary_lines: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    recommendations: list[RecommendationItem] = Field(default_factory=list)
    executions: list[ExecutionSummaryItem] = Field(default_factory=list)
    created_at: UtcDateTime
    updated_at: UtcDateTime

    @model_validator(mode="after")
    def _enforce_contract(self):
        # FINOPS에는 위험 대응 축이 없다
        if self.category == IncidentCategory.FINOPS and (
            self.initial_risk_level is not None
            or self.reviewed_risk_level is not None
            or self.response_mode is not None
        ):
            raise ValueError(
                "FINOPS는 initial_risk_level·reviewed_risk_level·response_mode가 null이어야 합니다"
            )

        # 분석 완료 = 정확히 3줄, 그 외 = 빈 배열
        if len(self.summary_lines) not in (0, 3):
            raise ValueError("summary_lines는 빈 배열 또는 정확히 3개여야 합니다")

        # Incident당 같은 runbook_id 제안은 최대 1개
        rec_ids = [r.runbook_id.value for r in self.recommendations]
        if len(rec_ids) != len(set(rec_ids)):
            raise ValueError("recommendations에 같은 runbook_id가 중복될 수 없습니다")

        # 상태 ↔ 목록 정합 (설계 4.3 상태 정의에서 직접 도출되는 것만 강제)
        in_progress = any(
            e.status in (ExecutionStatus.IN_PROGRESS, ExecutionStatus.ROLLBACK_INITIATED)
            for e in self.executions
        )
        if self.status == IncidentStatus.AWAITING_APPROVAL:
            if not self.recommendations:
                raise ValueError("AWAITING_APPROVAL이면 실행 가능한 제안이 1개 이상이어야 합니다")
            if in_progress:
                raise ValueError("AWAITING_APPROVAL이면 진행 중인 실행이 없어야 합니다")
        if self.status == IncidentStatus.ACTION_IN_PROGRESS and not in_progress:
            raise ValueError("ACTION_IN_PROGRESS이면 진행 중인 실행이 1개 이상이어야 합니다")
        if (
            self.status in (IncidentStatus.ANALYZING, IncidentStatus.RESOLVED, IncidentStatus.FAILED)
            and self.recommendations
        ):
            raise ValueError(f"{self.status.value}이면 recommendations는 빈 배열이어야 합니다")
        if self.status == IncidentStatus.ANALYZING and self.summary_lines:
            raise ValueError("ANALYZING이면 summary_lines는 빈 배열이어야 합니다")
        if self.status == IncidentStatus.RESOLVED and in_progress:
            raise ValueError("RESOLVED이면 진행 중인 실행이 없어야 합니다")

        return self


class IncidentListItem(BaseModel):
    """목록 항목 — 상세(IncidentResponse)의 부분집합 10필드 (Issue #45 코멘트 확정)."""

    model_config = ConfigDict(extra="forbid")

    incident_id: str = Field(min_length=1)
    title: Optional[str] = Field(None, min_length=1)
    subject_arn: str = Field(min_length=1)
    category: IncidentCategory
    status: IncidentStatus
    initial_risk_level: Optional[RiskLevel] = None
    reviewed_risk_level: Optional[RiskLevel] = None
    response_mode: Optional[ResponseMode] = None
    created_at: UtcDateTime
    updated_at: UtcDateTime

    @model_validator(mode="after")
    def _enforce_contract(self):
        # 상세와 같은 불변식: FINOPS에는 위험 대응 축이 없다
        if self.category == IncidentCategory.FINOPS and (
            self.initial_risk_level is not None
            or self.reviewed_risk_level is not None
            or self.response_mode is not None
        ):
            raise ValueError(
                "FINOPS는 initial_risk_level·reviewed_risk_level·response_mode가 null이어야 합니다"
            )
        return self


class IncidentsResponse(BaseModel):
    """GET /api/v1/incidents 목록 봉투 — 페이지네이션 필드는 Post-MVP에 추가한다."""

    model_config = ConfigDict(extra="forbid")

    items: list[IncidentListItem] = Field(default_factory=list)
