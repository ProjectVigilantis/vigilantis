# ==============================================================================
# [파일 설명]
# PostgreSQL ORM 13종 — 자산·인시던트 계열 9종 + 조치·복구 계열 4종. (Issue #60)
# 스키마 변경은 Alembic 마이그레이션 전용이다.
#
# 계층 원칙 (Issue #60)
#   - Enum·JSONB 값 어휘의 원천은 packages/schemas다 — 여기서 재정의하지 않는다.
#   - 상태 전이 검증 3층 분리: 허용 전이는 Workflow가 계산하고, 갱신은
#     Repository의 조건부 쿼리(expected→next)가 수행하며, DB는 Enum과
#     행 단위 불변식(CheckConstraint)만 강제한다.
#   - API 응답 직렬화는 packages/schemas의 Pydantic 계약을 쓴다(ORM 비노출).
# ==============================================================================

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import text

from schemas.api.actions import ExecutionStatus
from schemas.api.assets import AssetType, RelationType
from schemas.api.incidents import (
    IncidentCategory,
    IncidentStatus,
    ResolutionJudgement,
    ResponseMode,
    RiskLevel,
)
from schemas.candidates import CandidateStatus
from schemas.collections import CollectionRunStatus
from schemas.events import ThreatEventType
from schemas.evidence import EvidenceType
from schemas.executions import ExecutionEffect, ExecutionStepStatus
from schemas.guardrails import (
    GuardrailDecision,
    GuardrailStep,
    GuardrailValidationContext,
)
from schemas.incidents import AgentInvocationStatus
from schemas.runbooks import RunbookId, TriggerSource

from .session import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _new_id() -> str:
    return str(uuid.uuid4())


# 같은 이름의 PG ENUM 타입은 인스턴스 1개만 만든다 — 컬럼 2곳이 같은 타입을 쓸 때
# (risk_level·runbook_id) CREATE TYPE이 중복 발행되지 않게 한다
_ENUM_TYPES: dict[str, SAEnum] = {}


def _enum(enum_cls: type[Enum], name: str) -> SAEnum:
    """PG native ENUM — 저장 값은 Enum.value(이름이 아님)로 고정한다."""
    if name not in _ENUM_TYPES:
        _ENUM_TYPES[name] = SAEnum(
            enum_cls,
            name=name,
            values_callable=lambda e: [m.value for m in e],
            native_enum=True,
            create_constraint=False,
        )
    return _ENUM_TYPES[name]


_ID = Uuid(as_uuid=False)  # PG native UUID, 파이썬 값은 str — 내부 계약의 str ID와 정렬


# ==============================================================================
# 자산·인시던트 계열 9종
# ==============================================================================


class CollectionRun(Base):
    """수집 실행 1회. Rule 판정·Metric 요약이 이 단위에 묶인다."""

    __tablename__ = "collection_runs"

    collection_run_id: Mapped[str] = mapped_column(_ID, primary_key=True, default=_new_id)
    status: Mapped[CollectionRunStatus] = mapped_column(
        _enum(CollectionRunStatus, "collection_run_status"),
        default=CollectionRunStatus.IN_PROGRESS,
    )
    account_id: Mapped[str] = mapped_column(String(16))
    region: Mapped[str] = mapped_column(String(32))
    mode: Mapped[str] = mapped_column(String(16))  # localstack | aws
    lookback_days: Mapped[int] = mapped_column(Integer)
    period_seconds: Mapped[int] = mapped_column(Integer)
    error_summary: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    finished_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (Index("ix_collection_runs_started_at", "started_at"),)


class Asset(Base):
    """관제 대상 AWS 자산 1건 — arn이 안정 키. 판정·수집값은 별도 테이블."""

    __tablename__ = "assets"

    asset_id: Mapped[str] = mapped_column(_ID, primary_key=True, default=_new_id)
    arn: Mapped[str] = mapped_column(String(512), unique=True)
    asset_type: Mapped[AssetType] = mapped_column(_enum(AssetType, "asset_type"))
    resource_id: Mapped[str] = mapped_column(String(128))
    name: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    account_id: Mapped[str] = mapped_column(String(16))
    region: Mapped[str] = mapped_column(String(32))
    state: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    # 유형별 세부 스펙(api/assets.py AssetSpec 7종과 대응) — 유형별 테이블을 두지 않는다
    spec: Mapped[dict] = mapped_column(JSONB, default=dict)
    last_collection_run_id: Mapped[Optional[str]] = mapped_column(
        _ID, ForeignKey("collection_runs.collection_run_id"), nullable=True
    )
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    __table_args__ = (
        Index("ix_assets_asset_type", "asset_type"),
        Index("ix_assets_resource_id", "resource_id"),
        Index("ix_assets_region", "region"),
    )


class AssetRelationship(Base):
    """자산 간 방향 연결(source → target). 토폴로지 맵의 역방향 조회를 지원한다."""

    __tablename__ = "asset_relationships"

    relationship_id: Mapped[str] = mapped_column(_ID, primary_key=True, default=_new_id)
    source_asset_id: Mapped[str] = mapped_column(_ID, ForeignKey("assets.asset_id"))
    relation_type: Mapped[RelationType] = mapped_column(_enum(RelationType, "relation_type"))
    target_arn: Mapped[str] = mapped_column(String(512))
    collection_run_id: Mapped[Optional[str]] = mapped_column(
        _ID, ForeignKey("collection_runs.collection_run_id"), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    __table_args__ = (
        UniqueConstraint("source_asset_id", "relation_type", "target_arn"),
        Index("ix_asset_relationships_target_arn", "target_arn"),
    )


class MetricSummary(Base):
    """수집 회차별 메트릭 요약 — Asset 컬럼으로 두면 회차마다 덮어써 이력이 사라진다."""

    __tablename__ = "metric_summaries"

    metric_summary_id: Mapped[str] = mapped_column(_ID, primary_key=True, default=_new_id)
    asset_id: Mapped[str] = mapped_column(_ID, ForeignKey("assets.asset_id"))
    collection_run_id: Mapped[str] = mapped_column(
        _ID, ForeignKey("collection_runs.collection_run_id")
    )
    cpu_datapoints: Mapped[int] = mapped_column(Integer, default=0)
    cpu_avg: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    cpu_max: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    net_in_avg: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    net_out_avg: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    window_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    __table_args__ = (
        UniqueConstraint("asset_id", "collection_run_id"),
        CheckConstraint("window_end >= window_start", name="window_ordered"),
    )


class RuleEvaluation(Base):
    """Rule Evaluator의 결정적 판정 1건 — rules.RuleEvaluationResult와 대응."""

    __tablename__ = "rule_evaluations"

    rule_evaluation_id: Mapped[str] = mapped_column(_ID, primary_key=True, default=_new_id)
    asset_id: Mapped[str] = mapped_column(_ID, ForeignKey("assets.asset_id"))
    collection_run_id: Mapped[str] = mapped_column(
        _ID, ForeignKey("collection_runs.collection_run_id")
    )
    # 공개 Enum(EvaluationStatus·Verdict·SkipReasonCode) 값을 담는다 — 교차 불변식
    # (COMPLETED↔verdict, SKIP↔사유)은 Pydantic 계약(rules.py)과 Workflow가 검증한다
    evaluation_status: Mapped[str] = mapped_column(String(32))
    verdict: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    health_score: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    skip_reason_code: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    reason: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)
    evaluated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint("asset_id", "collection_run_id"),
        CheckConstraint(
            "health_score IS NULL OR (health_score >= 0 AND health_score <= 100)",
            name="health_score_range",
        ),
        Index("ix_rule_evaluations_verdict", "verdict"),
    )


class ThreatEvent(Base):
    """정규화된 위협 이벤트 — events.NormalizedThreatEvent와 대응. 생성 후 불변."""

    __tablename__ = "threat_events"

    threat_event_id: Mapped[str] = mapped_column(_ID, primary_key=True, default=_new_id)
    source_event_id: Mapped[str] = mapped_column(String(256))
    event_type: Mapped[ThreatEventType] = mapped_column(
        _enum(ThreatEventType, "threat_event_type")
    )
    target_arn: Mapped[str] = mapped_column(String(512))
    payload: Mapped[dict] = mapped_column(JSONB)
    deduplication_key: Mapped[str] = mapped_column(String(512), unique=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    __table_args__ = (
        Index("ix_threat_events_source_event_id", "source_event_id"),
        Index("ix_threat_events_target_arn", "target_arn"),
    )


class Incident(Base):
    """업무 흐름의 중심 엔티티. 초기 판정(불변)과 AI 사후 평가를 분리 보관한다."""

    __tablename__ = "incidents"

    incident_id: Mapped[str] = mapped_column(_ID, primary_key=True, default=_new_id)
    title: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    subject_arn: Mapped[str] = mapped_column(String(512))
    category: Mapped[IncidentCategory] = mapped_column(
        _enum(IncidentCategory, "incident_category")
    )
    status: Mapped[IncidentStatus] = mapped_column(
        _enum(IncidentStatus, "incident_status"), default=IncidentStatus.ANALYZING
    )
    threat_event_id: Mapped[Optional[str]] = mapped_column(
        _ID, ForeignKey("threat_events.threat_event_id"), nullable=True
    )
    # --- 위험 대응 축(SECOPS 전용, FINOPS는 전부 null — api/incidents.py 불변식) ---
    initial_risk_level: Mapped[Optional[RiskLevel]] = mapped_column(
        _enum(RiskLevel, "risk_level"), nullable=True
    )
    reviewed_risk_level: Mapped[Optional[RiskLevel]] = mapped_column(
        _enum(RiskLevel, "risk_level"), nullable=True
    )
    response_mode: Mapped[Optional[ResponseMode]] = mapped_column(
        _enum(ResponseMode, "response_mode"), nullable=True
    )
    initial_risk_reason_codes: Mapped[list] = mapped_column(JSONB, default=list)
    # --- AI 분석 산출(ADR-0005 결정 4항의 보존 대상) ---
    summary_lines: Mapped[list] = mapped_column(JSONB, default=list)
    # --- AI 호출 상태(원자 Claim 대상)와 승인 대기 일정(incidents.py 계약) ---
    agent_invocation_status: Mapped[AgentInvocationStatus] = mapped_column(
        _enum(AgentInvocationStatus, "agent_invocation_status"),
        default=AgentInvocationStatus.PENDING,
    )
    agent_invocation_started_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    agent_wait_started_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    response_deadline_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # --- 관제자 종료 판단(Issue #199) — updated_at은 자식 상태 변경으로도 올라가므로
    #     종료 시각은 따로 남긴다 ---
    resolution: Mapped[Optional[ResolutionJudgement]] = mapped_column(
        _enum(ResolutionJudgement, "resolution_judgement"), nullable=True
    )
    resolved_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    __table_args__ = (
        # reason_codes는 항상 JSON 배열 — 비배열 값은 형태 검사 전에 거절한다
        CheckConstraint(
            "jsonb_typeof(initial_risk_reason_codes) = 'array'",
            name="reason_codes_is_array",
        ),
        # SECOPS: 위협 이름(title)·초기 위험도 필수 + 사유 코드 1개 이상 /
        # FINOPS: 위험 대응 축 전부 null + 사유 코드 빈 배열 (api/incidents.py 불변식)
        # CASE로 배열 확인을 먼저 강제한다 — AND 단락 평가는 SQL이 보장하지 않아
        # 비배열 값에서 jsonb_array_length가 제약 위반 대신 런타임 오류를 낸다
        CheckConstraint(
            "CASE jsonb_typeof(initial_risk_reason_codes) WHEN 'array' THEN"
            " (category = 'SECOPS' AND title IS NOT NULL"
            " AND initial_risk_level IS NOT NULL"
            " AND jsonb_array_length(initial_risk_reason_codes) >= 1)"
            " OR "
            "(category = 'FINOPS' AND initial_risk_level IS NULL"
            " AND reviewed_risk_level IS NULL AND response_mode IS NULL"
            " AND jsonb_array_length(initial_risk_reason_codes) = 0)"
            " ELSE false END",
            name="category_risk_shape",
        ),
        # 요약은 빈 배열 또는 정확히 3줄 (api/incidents.py summary_lines 계약)
        CheckConstraint(
            "CASE jsonb_typeof(summary_lines) WHEN 'array' THEN"
            " jsonb_array_length(summary_lines) IN (0, 3)"
            " ELSE false END",
            name="summary_lines_len",
        ),
        # 응답 기한은 항상 시작 + 60초 (incidents.AgentWaitSchedule 계약)
        # 한쪽만 NULL이면 산술 결과가 NULL이 되어 CHECK를 통과하므로 명시적으로 막는다
        CheckConstraint(
            "(agent_wait_started_at IS NULL AND response_deadline_at IS NULL)"
            " OR (agent_wait_started_at IS NOT NULL AND response_deadline_at IS NOT NULL"
            " AND response_deadline_at = agent_wait_started_at + INTERVAL '60 seconds')",
            name="wait_deadline_60s",
        ),
        # 종료 판단은 판단·시각이 함께 있고, RESOLVED에서만 채워진다
        # (api/incidents.py 불변식과 같은 것). RESOLVED인데 판단이 없는 것은
        # 허용한다 — 관제자 판단 없이 종료되는 경로가 뒤에 생길 수 있다.
        # RESOLVED에서 나가는 전이는 판단을 함께 지운다(workflows의 복구 재개)
        CheckConstraint(
            "((resolution IS NULL) = (resolved_at IS NULL))"
            " AND (resolution IS NULL OR status = 'RESOLVED')",
            name="resolution_with_resolved_status",
        ),
        Index("ix_incidents_status", "status"),
        Index("ix_incidents_category", "category"),
        Index("ix_incidents_created_at", "created_at"),
        Index("ix_incidents_subject_arn", "subject_arn"),
        Index("ix_incidents_agent_invocation_status", "agent_invocation_status"),
    )


class Evidence(Base):
    """Incident에 고정된 판단 근거 — evidence.EvidenceItem과 대응. 생성 후 불변(ADR-0005)."""

    __tablename__ = "evidence_items"

    evidence_id: Mapped[str] = mapped_column(_ID, primary_key=True, default=_new_id)
    incident_id: Mapped[str] = mapped_column(_ID, ForeignKey("incidents.incident_id"))
    evidence_type: Mapped[EvidenceType] = mapped_column(_enum(EvidenceType, "evidence_type"))
    source_type: Mapped[str] = mapped_column(String(64))
    source_id: Mapped[str] = mapped_column(String(256))
    content: Mapped[dict] = mapped_column(JSONB)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    __table_args__ = (Index("ix_evidence_items_incident_id", "incident_id"),)


class RunbookCandidate(Base):
    """AI 제안 후보 — candidates.RunbookCandidateData와 대응."""

    __tablename__ = "runbook_candidates"

    candidate_id: Mapped[str] = mapped_column(_ID, primary_key=True, default=_new_id)
    incident_id: Mapped[str] = mapped_column(_ID, ForeignKey("incidents.incident_id"))
    runbook_id: Mapped[RunbookId] = mapped_column(_enum(RunbookId, "runbook_id"))
    target_arn: Mapped[str] = mapped_column(String(512))
    # Runbook별 typed 파라미터의 저장본 — AI가 정한 값만 들어온다(#154).
    # display_parameters는 그것에서 서버가 만든 화면 표시본이다.
    # ORM 기본값을 두지 않는다 — 기본값이 있으면 계약(RunbookCandidateData)을
    # 우회한 삽입이 빈 파라미터로 조용히 저장된다. 쓰는 쪽이 반드시 값을 낸다.
    parameters: Mapped[dict] = mapped_column(JSONB)
    display_parameters: Mapped[dict] = mapped_column(JSONB, default=dict)
    evidence_ids: Mapped[list] = mapped_column(JSONB, default=list)
    status: Mapped[CandidateStatus] = mapped_column(
        _enum(CandidateStatus, "candidate_status"),
        default=CandidateStatus.PENDING_VALIDATION,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    __table_args__ = (
        # 활성 상태에서는 (incident, runbook)당 후보 1개 — 공개 계약의 외부 식별자.
        # REJECTED·INVALIDATED는 이력으로 남으므로 부분 유니크 인덱스로 건다
        Index(
            "uq_runbook_candidates_active",
            "incident_id",
            "runbook_id",
            unique=True,
            postgresql_where=text(
                "status IN ('PENDING_VALIDATION', 'EXECUTABLE', 'CLAIMED')"
            ),
        ),
        Index("ix_runbook_candidates_incident_id", "incident_id"),
    )


# ==============================================================================
# 조치·복구 계열 4종
# ==============================================================================


class GuardrailEvaluation(Base):
    """4단계 Guardrail 검증 결과 — guardrails.GuardrailValidationResult와 대응."""

    __tablename__ = "guardrail_evaluations"

    guardrail_evaluation_id: Mapped[str] = mapped_column(
        _ID, primary_key=True, default=_new_id
    )
    validation_context: Mapped[GuardrailValidationContext] = mapped_column(
        _enum(GuardrailValidationContext, "guardrail_validation_context")
    )
    candidate_id: Mapped[Optional[str]] = mapped_column(
        _ID, ForeignKey("runbook_candidates.candidate_id"), nullable=True
    )
    execution_id: Mapped[Optional[str]] = mapped_column(
        _ID, ForeignKey("action_executions.execution_id"), nullable=True
    )
    result: Mapped[GuardrailDecision] = mapped_column(
        _enum(GuardrailDecision, "guardrail_decision")
    )
    failed_step: Mapped[Optional[GuardrailStep]] = mapped_column(
        _enum(GuardrailStep, "guardrail_step"), nullable=True
    )
    # 고정 순서 4단계 결과(guardrails.GuardrailStepResult 목록) — 구조 검증은 Pydantic
    steps: Mapped[list] = mapped_column(JSONB)
    # PASS 시 확정되는 불변 실행 명령 — typed RunbookCommand 계약 확정 전까지 JSONB
    validated_command: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    validated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        # 검증 대상은 candidate 또는 execution 중 정확히 하나 (guardrails.py 계약)
        CheckConstraint(
            "(candidate_id IS NULL) != (execution_id IS NULL)",
            name="exactly_one_reference",
        ),
        Index("ix_guardrail_evaluations_candidate_id", "candidate_id"),
        Index("ix_guardrail_evaluations_execution_id", "execution_id"),
    )


class ActionExecution(Base):
    """런북 실행 1건. 상태 6종은 공개 계약(api/actions.py)이 원천."""

    __tablename__ = "action_executions"

    execution_id: Mapped[str] = mapped_column(_ID, primary_key=True, default=_new_id)
    incident_id: Mapped[str] = mapped_column(_ID, ForeignKey("incidents.incident_id"))
    candidate_id: Mapped[Optional[str]] = mapped_column(
        _ID, ForeignKey("runbook_candidates.candidate_id"), nullable=True
    )
    runbook_id: Mapped[RunbookId] = mapped_column(_enum(RunbookId, "runbook_id"))
    target_arn: Mapped[str] = mapped_column(String(512))
    status: Mapped[ExecutionStatus] = mapped_column(
        _enum(ExecutionStatus, "execution_status"), default=ExecutionStatus.IN_PROGRESS
    )
    trigger_source: Mapped[TriggerSource] = mapped_column(
        _enum(TriggerSource, "trigger_source")
    )
    # 관제자 승인 실행 중복 방지 키 / 시스템 실행(선차단·타임아웃·자동 원복) 중복 방지 키
    idempotency_key: Mapped[Optional[str]] = mapped_column(
        String(128), unique=True, nullable=True
    )
    deduplication_key: Mapped[Optional[str]] = mapped_column(
        String(512), unique=True, nullable=True
    )
    # 롤백 자식 → 원본 실행. 원복 파라미터는 backup_record에서만 로드(ADR-0004 정책 ③)
    parent_execution_id: Mapped[Optional[str]] = mapped_column(
        _ID, ForeignKey("action_executions.execution_id"), nullable=True
    )
    backup_record_id: Mapped[Optional[str]] = mapped_column(
        _ID,
        ForeignKey("backup_records.backup_record_id", use_alter=True),
        nullable=True,
    )
    # Guardrail PASS에서 복사되는 불변 실행 명령
    validated_command: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    error_summary: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )
    finished_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        # ROLLBACK_INITIATED·ROLLED_BACK·ROLLBACK_FAILED는 원본 Execution 전용 —
        # 롤백 자식은 진행·성공·실패만 갖는다 (SSOT §API 계약)
        CheckConstraint(
            "parent_execution_id IS NULL"
            " OR status IN ('IN_PROGRESS', 'SUCCESS', 'FAILED')",
            name="rollback_child_status",
        ),
        Index("ix_action_executions_incident_id", "incident_id"),
        # Dispatcher 회수 스캔용 — 진행 중 상태만 부분 인덱스로 좁힌다
        Index(
            "ix_action_executions_non_terminal",
            "status",
            postgresql_where=text("status IN ('IN_PROGRESS', 'ROLLBACK_INITIATED')"),
        ),
    )


class ExecutionStep(Base):
    """실행 1단계 — executions.ExecutionStepResult와 대응. AWS 호출 전 저장, 호출 후 갱신."""

    __tablename__ = "execution_steps"

    execution_step_id: Mapped[str] = mapped_column(_ID, primary_key=True, default=_new_id)
    execution_id: Mapped[str] = mapped_column(
        _ID, ForeignKey("action_executions.execution_id")
    )
    sequence: Mapped[int] = mapped_column(Integer)
    affected_arn: Mapped[str] = mapped_column(String(512))
    step_type: Mapped[str] = mapped_column(String(64))
    aws_operation: Mapped[str] = mapped_column(String(128))
    status: Mapped[ExecutionStepStatus] = mapped_column(
        _enum(ExecutionStepStatus, "execution_step_status"),
        default=ExecutionStepStatus.IN_PROGRESS,
    )
    effect: Mapped[Optional[ExecutionEffect]] = mapped_column(
        _enum(ExecutionEffect, "execution_effect"), nullable=True
    )
    aws_request_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    result_summary: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)
    error_summary: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    __table_args__ = (
        UniqueConstraint("execution_id", "sequence"),
        CheckConstraint("sequence >= 1", name="sequence_positive"),
        # status↔effect 짝 고정 (executions.py 계약)
        # effect가 NULL이면 IN 비교가 NULL이 되어 CHECK를 통과하므로 IS NOT NULL을 명시한다
        CheckConstraint(
            "(status = 'IN_PROGRESS' AND effect IS NULL)"
            " OR (status = 'SUCCESS' AND effect IS NOT NULL"
            " AND effect IN ('APPLIED', 'NOT_APPLIED'))"
            " OR (status = 'FAILED' AND effect IS NOT NULL"
            " AND effect IN ('NOT_APPLIED', 'PARTIAL', 'UNKNOWN'))",
            name="effect_matches_status",
        ),
    )


class BackupRecord(Base):
    """원복 파라미터의 유일 원천(ADR-0004 정책 ③) — 스펙 JSON 백업. 생성 후 불변."""

    __tablename__ = "backup_records"

    backup_record_id: Mapped[str] = mapped_column(_ID, primary_key=True, default=_new_id)
    # 백업을 생성한 실행(주 조치·자동 격리)
    execution_id: Mapped[str] = mapped_column(
        _ID, ForeignKey("action_executions.execution_id")
    )
    target_arn: Mapped[str] = mapped_column(String(512))
    backup_type: Mapped[str] = mapped_column(String(64))  # 예: SAVE_INSTANCE_SPEC_JSON
    payload: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    __table_args__ = (Index("ix_backup_records_target_arn", "target_arn"),)
