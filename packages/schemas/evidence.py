# ==============================================================================
# [파일 설명]  담당: 안성일 (AI/Guardrail · Architect)
# AI 판단 근거(Evidence)의 유형·내용 계약입니다. (Issue #49)
# ThreatEvent·RuleEvaluation·Metric·Execution 근거를 Incident에 고정해 AI 입력과
# 감사에 사용한다. MVP 외부 API에는 evidence_ids(ID 목록)만 공개한다.
#
# 계약 원칙 (#49 확정)
#   - content는 새 구조를 발명하지 않고 기존 확정 계약을 재사용한다:
#     RULE→RuleEvaluationResult, THREAT→NormalizedThreatEvent,
#     METRIC→관측 구간+MetricSummary(수집 요약), EXECUTION→실행 요약 최소 필드.
#   - evidence_type과 content 모델은 반드시 일치한다(JSON 저장·조회 양쪽 검증).
# ==============================================================================

from __future__ import annotations

from enum import Enum, unique
from typing import Optional, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .api.actions import ExecutionStatus
from .api.assets import UtcDateTime
from .assets import MetricName, MetricSummary
from .events import NormalizedThreatEvent
from .rules import RuleEvaluationResult
from .runbooks import RunbookId


@unique
class EvidenceType(str, Enum):
    METRIC = "METRIC"
    RULE = "RULE"
    THREAT = "THREAT"
    EXECUTION = "EXECUTION"


class MetricEvidence(BaseModel):
    """CPU·Network 관측 근거 — 수집 요약(MetricSummary)을 그대로 보존한다."""

    model_config = ConfigDict(extra="forbid")

    metric_name: MetricName
    window_start: UtcDateTime
    window_end: UtcDateTime
    summary: MetricSummary

    @model_validator(mode="after")
    def _window_ordered(self):
        if self.window_end < self.window_start:
            raise ValueError("window_end는 window_start보다 빠를 수 없습니다")
        return self


class RuleEvidence(BaseModel):
    """Rule 판정 근거 — 판정 결과 계약을 그대로 보존한다."""

    model_config = ConfigDict(extra="forbid")

    evaluation: RuleEvaluationResult


class ThreatEvidence(BaseModel):
    """위협 이벤트 근거 — 정규화된 이벤트 계약을 그대로 보존한다."""

    model_config = ConfigDict(extra="forbid")

    event: NormalizedThreatEvent


class ExecutionEvidence(BaseModel):
    """실행 결과 근거(예: 사전 격리) — 최소 요약만 보존한다."""

    model_config = ConfigDict(extra="forbid")

    execution_id: str = Field(min_length=1)
    runbook_id: RunbookId
    status: ExecutionStatus
    summary: Optional[str] = Field(None, min_length=1)


EvidenceContent = Union[MetricEvidence, RuleEvidence, ThreatEvidence, ExecutionEvidence]

# evidence_type → content 모델 매핑의 단일 원천 (AgentEvidenceInput도 이 매핑을 쓴다)
EVIDENCE_CONTENT_MODELS: dict[EvidenceType, type[BaseModel]] = {
    EvidenceType.METRIC: MetricEvidence,
    EvidenceType.RULE: RuleEvidence,
    EvidenceType.THREAT: ThreatEvidence,
    EvidenceType.EXECUTION: ExecutionEvidence,
}


def bind_evidence_content(data: dict) -> dict:
    """dict 입력의 content를 evidence_type이 지정한 모델로만 검증한다(smart-union 오매칭 방지)."""
    if isinstance(data, dict) and isinstance(data.get("content"), dict):
        try:
            evidence_type = EvidenceType(data.get("evidence_type"))
        except (ValueError, TypeError):
            return data  # evidence_type 오류는 필드 검증이 보고한다
        data = dict(data)
        data["content"] = EVIDENCE_CONTENT_MODELS[evidence_type].model_validate(data["content"])
    return data


class EvidenceItem(BaseModel):
    """Incident에 고정되는 근거 1건 — DB Evidence 엔티티와 대응하는 내부 계약."""

    model_config = ConfigDict(extra="forbid")

    evidence_id: str = Field(min_length=1)
    incident_id: str = Field(min_length=1)
    evidence_type: EvidenceType
    source_type: str = Field(min_length=1)  # 원천 종류(예: threat_event·rule_evaluation)
    source_id: str = Field(min_length=1)
    content: EvidenceContent
    occurred_at: UtcDateTime
    collected_at: UtcDateTime

    @model_validator(mode="before")
    @classmethod
    def _bind_content(cls, data):
        return bind_evidence_content(data)

    @model_validator(mode="after")
    def _enforce_content_shape(self):
        expected = EVIDENCE_CONTENT_MODELS[self.evidence_type]
        if not isinstance(self.content, expected):
            raise ValueError(
                f"{self.evidence_type.value} 근거의 content는 {expected.__name__}이어야 합니다"
            )
        return self
