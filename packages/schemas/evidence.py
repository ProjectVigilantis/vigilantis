# ==============================================================================
# [파일 설명]  담당: 안성일 (AI/Guardrail · Architect)
# AI 판단 근거(Evidence)의 유형·내용 계약입니다. (Issue #49)
# ThreatEvent·RuleEvaluation·Metric·Execution·Asset 근거를 Incident에 고정해 AI 입력과
# 감사에 사용한다. MVP 외부 API에는 evidence_ids(ID 목록)만 공개한다.
#
# 계약 원칙 (#49 확정)
#   - content는 새 구조를 발명하지 않고 기존 확정 계약을 재사용한다:
#     RULE→RuleEvaluationResult, THREAT→NormalizedThreatEvent,
#     METRIC→관측 구간+MetricSummary(수집 요약), EXECUTION→실행 요약 최소 필드,
#     ASSET→판정 회차+공개 AssetItem.
#   - evidence_type과 content 모델은 반드시 일치한다(JSON 저장·조회 양쪽 검증).
#
# ASSET 근거는 나머지 넷과 쓰임이 다르다 (Issue #265)
#   - 다른 근거는 "무엇을 보고 판단했나"를 남기고 그래프 입력의 evidences로도 나가지만,
#     ASSET은 **판정 시점의 자산 상태를 되살리기 위한 것**이라 그래프에는 asset_context로
#     들어간다. 그래서 AgentEvidenceInput은 이 유형을 거절한다(agents.py) — 근거로도
#     실으면 같은 값이 모델 입력에 두 번 간다.
#   - 후보의 evidence_ids가 ASSET 근거를 가리키지 못하는 것은 "후보 evidence_ids ⊆
#     그래프 입력 Evidence" 검증이 설 때 따라 나온다. 그 검증은 Workflow 몫이고
#     (agents.py 계약 원칙 · apps/core-api/agent_dispatcher.py 5번) 아직 없다 —
#     그래프 자체는 모델이 돌려준 evidence_ids를 입력과 대조하지 않는다.
#   - 자산 행은 수집 회차마다 덮어써지므로(db/repositories/assets.py upsert_asset)
#     이 근거가 그 회차 자산의 유일한 사본이다.
# ==============================================================================

from __future__ import annotations

from enum import Enum, unique
from typing import Optional, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .api.actions import ExecutionStatus
from .api.assets import AssetItem, UtcDateTime
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
    ASSET = "ASSET"


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


class DetectionAssetSnapshot(BaseModel):
    """판정이 내려진 그 회차의 자산 상태 — Intake가 나르고 ASSET 근거로 보존된다.

    자산 표현은 공개 AssetItem을 그대로 쓴다. 대상 ARN·유형·상태·Spec·관계를 이미
    담고 있고 spec↔asset_type 정합도 그쪽 계약이 강제하며, 그래프 입력의 자산 문맥과
    같은 타입이라(agents.py AgentAssetContext) 되살릴 때 변환이 필요 없다.

    collection_run_id를 따로 받는 것은 공개 AssetItem이 그 값을 담지 않기 때문이다
    (FE 계약이라 여기 필요한 필드를 늘리지 않는다). 자산 행의 last_collection_run_id에서
    채우며, 판정의 collection_run_id와 대조하는 것이 이 필드의 쓸모다(intake.py).
    """

    model_config = ConfigDict(extra="forbid")

    collection_run_id: str = Field(min_length=1)
    asset: AssetItem


EvidenceContent = Union[
    MetricEvidence, RuleEvidence, ThreatEvidence, ExecutionEvidence, DetectionAssetSnapshot
]

# evidence_type → content 모델 매핑의 단일 원천 (AgentEvidenceInput도 이 매핑을 쓴다)
EVIDENCE_CONTENT_MODELS: dict[EvidenceType, type[BaseModel]] = {
    EvidenceType.METRIC: MetricEvidence,
    EvidenceType.RULE: RuleEvidence,
    EvidenceType.THREAT: ThreatEvidence,
    EvidenceType.EXECUTION: ExecutionEvidence,
    # ASSET만 *Evidence 이름이 아닌 것은, 같은 객체를 Intake도 나르기 때문이다
    # (packages/schemas/intake.py). 이름을 갈면 같은 값이 두 이름을 갖는다.
    EvidenceType.ASSET: DetectionAssetSnapshot,
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
