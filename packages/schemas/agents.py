# ==============================================================================
# [파일 설명]  담당: 안성일 (AI/Guardrail · Architect)
# LangGraph 입출력 계약입니다. (Issue #49)
# 입력은 Workflow가 PostgreSQL에서 읽어 만든 typed snapshot이고(Graph Node는 DB를
# 직접 조회하지 않는다), 출력은 Workflow가 검증 후 Candidate로 저장한다.
# LangGraph 내부 State·Node·Checkpoint 모델은 이 계약과 분리하며 여기 두지 않는다.
#
# 계약 원칙
#   - domain은 Workflow가 Incident 분류에서 설정하는 첫 분기 기준 — Node가 바꿀 수 없다.
#   - 후보 Draft의 runbook_id는 AI 추천 가능 본편 7종만(ADR-0004 — 롤백 3종·임의
#     AWS 작업 출력 불가).
#   - 출력 invocation_status는 Terminal만: SUCCEEDED=요약 3줄+후보 1개 이상,
#     NO_PROPOSAL=요약 3줄+후보 0개, FAILED=빈 요약+후보 0개+reviewed null.
#   - FINOPS 출력의 reviewed_risk_level=null 규칙과 "후보 evidence_ids⊆입력 Evidence"
#     규칙은 입력·출력을 함께 아는 Workflow가 검증한다(출력 모델 단독으로는 불가).
#   - RULE 근거와 최상위 rule_evaluation이 같은 객체에서 나왔는지는 입력 계약이 본다
#     (Issue #265). 입력 안에서 닫히는 대조라 Workflow로 미루지 않는다.
#   - 후보 parameters는 Runbook별 typed 모델이다(#154, runbook_parameters.py).
#     AI가 정하는 값만 싣는다 — 자원 ID는 target_arn에서, 조회값과 evidence_id는
#     실행 접수 시점에 채운다. 화면 표시본(display_parameters)은 Draft에 없다.
#     서버가 parameters에서 생성하므로 LLM이 지을 자리를 두지 않는다.
#   - Capability의 파라미터 계약 메타데이터는 세부 계약 확정 후 추가한다(#49 확정 —
#     이번 범위에서 제외).
# ==============================================================================

from __future__ import annotations

from typing import Annotated, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator

from .api.actions import ExecutionStatus
from .api.assets import AssetItem, AssetType
from .api.incidents import IncidentCategory, RiskLevel
from .evidence import EVIDENCE_CONTENT_MODELS, EvidenceContent, EvidenceType, bind_evidence_content
from .events import InitialRiskEvaluationResult
from .incidents import AGENT_TERMINAL_STATUSES, AgentInvocationStatus
from .rules import RuleEvaluationResult
from .runbook_parameters import (
    CANDIDATE_PARAMETER_MODELS,
    CandidateParameters,
    bind_candidate_parameters,
)
from .runbooks import AI_RECOMMENDABLE_RUNBOOK_IDS, RunbookId

# 자산 문맥은 공개 AssetItem을 그대로 재사용한다 — 대상 ARN·유형·상태·Spec·관계를
# 이미 담고 있고, spec↔asset_type 정합 검증도 그쪽 계약이 강제한다. (#49 확정)
AgentAssetContext = AssetItem


class AgentEvidenceInput(BaseModel):
    """Graph에 전달하는 근거 1건 — EvidenceItem의 축약(식별자·유형·내용만).

    ASSET 근거는 여기 오지 않는다 — 자산은 asset_context로 이미 들어가므로 근거로도
    실으면 같은 값이 모델 입력에 두 번 간다(Issue #265, evidence.py 헤더). 후보의
    evidence_ids가 ASSET 근거를 가리키지 못하는 것은 이 거절 위에 "후보 evidence_ids ⊆
    입력 Evidence" 검증(Workflow 몫, 파일 헤더 계약 원칙)이 설 때 따라 나온다.
    """

    model_config = ConfigDict(extra="forbid")

    evidence_id: str = Field(min_length=1)
    evidence_type: EvidenceType
    content: EvidenceContent

    @model_validator(mode="before")
    @classmethod
    def _bind_content(cls, data):
        return bind_evidence_content(data)

    @model_validator(mode="after")
    def _enforce_content_shape(self):
        if self.evidence_type == EvidenceType.ASSET:
            raise ValueError(
                "ASSET 근거는 그래프 입력의 evidences에 실을 수 없습니다 "
                "(자산은 asset_context로 전달합니다)"
            )
        expected = EVIDENCE_CONTENT_MODELS[self.evidence_type]
        if not isinstance(self.content, expected):
            raise ValueError(
                f"{self.evidence_type.value} 근거의 content는 {expected.__name__}이어야 합니다"
            )
        return self


class AgentExecutionContext(BaseModel):
    """이미 수행된 실행(예: High 사전 격리)의 요약 — SecOps Graph 입력용."""

    model_config = ConfigDict(extra="forbid")

    execution_id: str = Field(min_length=1)
    runbook_id: RunbookId
    status: ExecutionStatus
    affected_arns: list[Annotated[str, Field(min_length=1)]] = Field(default_factory=list)
    result_summary: Optional[str] = Field(None, min_length=1)


class RunbookCapability(BaseModel):
    """AI가 추천할 수 있는 Runbook 1종의 능력 설명 — Graph 입력용."""

    model_config = ConfigDict(extra="forbid")

    runbook_id: RunbookId
    purpose: str = Field(min_length=1)
    allowed_target_asset_types: list[AssetType] = Field(min_length=1)

    @model_validator(mode="after")
    def _ai_recommendable_only(self):
        if self.runbook_id.value not in AI_RECOMMENDABLE_RUNBOOK_IDS:
            raise ValueError("Capability에는 AI 추천 가능 Runbook(본편 7종)만 올 수 있습니다")
        return self


def _validate_capabilities(capabilities: list[RunbookCapability]) -> None:
    ids = [c.runbook_id.value for c in capabilities]
    if len(ids) != len(set(ids)):
        raise ValueError("capabilities에 같은 runbook_id가 중복될 수 없습니다")


def _validate_rule_evidence(
    evidences: list[AgentEvidenceInput], rule_evaluation: RuleEvaluationResult
) -> None:
    """RULE 근거와 최상위 rule_evaluation이 같은 객체에서 나왔는지 본다. (Issue #265)

    어긋나면 ai/agent.py의 _incident_payload가 중복 제거 조건(완전 일치)을 빗나가 같은
    판정이 모델 입력에 두 번 실린다. 그쪽은 로그도 예외도 남기지 않으므로 여기서 막는다.

    RULE 근거가 2건 이상이면 거절한다 — 최상위가 어느 쪽을 비추는지 정할 근거가 없다.
    인시던트 1건은 판정 1건에서 나오므로(apps/core-api/incident_intake.py 저장 순서)
    지금 그 조합이 생길 경로는 없고, 생긴다면 어느 것이 기준인지 먼저 정해야 한다.
    """
    rule_evidences = [e for e in evidences if e.evidence_type == EvidenceType.RULE]
    if not rule_evidences:
        return
    if len(rule_evidences) > 1:
        raise ValueError("RULE 근거는 최대 1건입니다 (최상위 rule_evaluation의 기준이 갈립니다)")
    if rule_evidences[0].content.evaluation != rule_evaluation:
        raise ValueError(
            "RULE 근거의 evaluation은 최상위 rule_evaluation과 같아야 합니다 "
            "(같은 객체에서 나오지 않았습니다)"
        )


class FinOpsGraphInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    domain: Literal[IncidentCategory.FINOPS] = IncidentCategory.FINOPS
    incident_id: str = Field(min_length=1)
    asset_context: AgentAssetContext
    rule_evaluation: RuleEvaluationResult
    evidences: list[AgentEvidenceInput] = Field(default_factory=list)
    capabilities: list[RunbookCapability] = Field(min_length=1)

    @model_validator(mode="after")
    def _enforce_contract(self):
        _validate_capabilities(self.capabilities)
        _validate_rule_evidence(self.evidences, self.rule_evaluation)
        return self


class SecOpsGraphInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    domain: Literal[IncidentCategory.SECOPS] = IncidentCategory.SECOPS
    incident_id: str = Field(min_length=1)
    asset_context: AgentAssetContext
    initial_risk: InitialRiskEvaluationResult
    evidences: list[AgentEvidenceInput] = Field(default_factory=list)
    # High 사전 격리가 있었던 경우에만 존재 — 격리 실패·부분 적용도 그대로 전달한다
    isolation_execution: Optional[AgentExecutionContext] = None
    capabilities: list[RunbookCapability] = Field(min_length=1)

    @model_validator(mode="after")
    def _enforce_contract(self):
        _validate_capabilities(self.capabilities)
        return self


AgentGraphInput = Annotated[
    Union[FinOpsGraphInput, SecOpsGraphInput],
    Field(discriminator="domain"),
]

# discriminated union 검증용 어댑터 — AgentService 진입점에서 사용한다
AGENT_GRAPH_INPUT_ADAPTER: TypeAdapter = TypeAdapter(AgentGraphInput)


class RunbookCandidateDraft(BaseModel):
    """Graph가 출력하는 후보 초안 — 서버가 ID·상태를 부여해 Candidate로 저장한다."""

    model_config = ConfigDict(extra="forbid")

    runbook_id: RunbookId
    target_arn: str = Field(min_length=1)
    parameters: CandidateParameters
    # evidence_id(단수)를 여기 첫 항목에서 뽑으므로 비어 있을 수 없다(#154)
    evidence_ids: list[Annotated[str, Field(min_length=1)]] = Field(min_length=1)

    @model_validator(mode="before")
    @classmethod
    def _bind_parameters(cls, data):
        return bind_candidate_parameters(data)

    @model_validator(mode="after")
    def _enforce_contract(self):
        if self.runbook_id.value not in AI_RECOMMENDABLE_RUNBOOK_IDS:
            raise ValueError("Draft에는 AI 추천 가능 Runbook(본편 7종)만 올 수 있습니다")
        expected = CANDIDATE_PARAMETER_MODELS[self.runbook_id]
        if not isinstance(self.parameters, expected):
            raise ValueError(
                f"{self.runbook_id.value}의 parameters는 {expected.__name__}이어야 합니다"
            )
        return self


class AgentGraphOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    invocation_status: AgentInvocationStatus
    summary_lines: list[Annotated[str, Field(min_length=1)]] = Field(default_factory=list)
    reviewed_risk_level: Optional[RiskLevel] = None
    candidates: list[RunbookCandidateDraft] = Field(default_factory=list)

    @model_validator(mode="after")
    def _enforce_contract(self):
        if self.invocation_status not in AGENT_TERMINAL_STATUSES:
            raise ValueError(
                "Graph 출력 invocation_status는 Terminal(SUCCEEDED·NO_PROPOSAL·FAILED)만 가능합니다"
            )

        if self.invocation_status == AgentInvocationStatus.SUCCEEDED:
            if len(self.summary_lines) != 3 or not self.candidates:
                raise ValueError("SUCCEEDED는 요약 3줄과 후보 1개 이상이어야 합니다")
        elif self.invocation_status == AgentInvocationStatus.NO_PROPOSAL:
            if len(self.summary_lines) != 3 or self.candidates:
                raise ValueError("NO_PROPOSAL은 요약 3줄과 빈 후보여야 합니다")
        else:  # FAILED
            if self.summary_lines or self.candidates or self.reviewed_risk_level is not None:
                raise ValueError("FAILED는 빈 요약·빈 후보·reviewed_risk_level=null이어야 합니다")

        rec_ids = [c.runbook_id.value for c in self.candidates]
        if len(rec_ids) != len(set(rec_ids)):
            raise ValueError("candidates에 같은 runbook_id가 중복될 수 없습니다")
        return self
