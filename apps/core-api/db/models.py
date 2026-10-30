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
from datetime import UTC, date, datetime
from enum import Enum

from schemas.api.actions import ExecutionStatus
from schemas.api.assets import AssetType, RelationType
from schemas.api.audit import AuditActorKind, AuditEventAction
from schemas.api.auth import AccountType, PlatformRole, UserRole
from schemas.api.incidents import (
    IncidentCategory,
    IncidentStatus,
    ResolutionJudgement,
    ResponseMode,
    RiskLevel,
)
from schemas.api.support import (
    InquiryCategory,
    InquiryEventAction,
    InquiryStatus,
    NotificationKind,
    SignupRequestStatus,
)
from schemas.api.tenants import TenantStatus, TenantVerificationResult
from schemas.api.users import UserEventAction
from schemas.candidates import CandidateStatus
from schemas.collections import CollectionRunStatus
from schemas.events import ThreatEventType
from schemas.evidence import EvidenceType
from schemas.executions import ExecutionEffect, ExecutionStepStatus
from schemas.guardrails import (
    GuardrailDecision,
    GuardrailStep,
    GuardrailValidationContext,
    PrecheckReasonCode,
)
from schemas.incidents import AgentInvocationStatus
from schemas.runbooks import RunbookId, TriggerSource
from sqlalchemy import (
    CHAR,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
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

from .session import Base


def _utcnow() -> datetime:
    return datetime.now(UTC)


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
# 테넌트 — 고객 계정 1개 (Post-MVP T-4)
# ==============================================================================


class Tenant(Base):
    """고객 계정 1개. 플랫폼이 이 계정의 역할에 AssumeRole + ExternalId 로 들어간다(#438 §C-1).

    공개 계약은 `schemas.api.tenants`(#485)이고 아래 CHECK 는 그 불변식의 DB 거울이다 —
    API 를 거치지 않는 쓰기(마이그레이션 · 운영 스크립트)도 같은 모양을 지키게 한다.
    연결 확인 결과는 관측값이라 「그 시각에 확인한 결과」로 저장한다(상태를 자동으로 바꾸지 않는다).
    온보딩 링크는 저장하지 않는다 — `external_id` 와 플랫폼 주체에서 만드는 파생값이다.
    """

    __tablename__ = "tenants"

    tenant_id: Mapped[str] = mapped_column(_ID, primary_key=True, default=_new_id)
    name: Mapped[str] = mapped_column(String(100))
    status: Mapped[TenantStatus] = mapped_column(
        _enum(TenantStatus, "tenant_status"), default=TenantStatus.PENDING_CONNECTION
    )
    # 한 계정은 한 테넌트다 — 같은 계정을 두 테넌트로 두면 같은 자산을 두 번 수집 · 조치한다
    aws_account_id: Mapped[str] = mapped_column(String(12), unique=True)
    access_role_arn: Mapped[str] = mapped_column(String(2048))
    external_id: Mapped[str] = mapped_column(String(128), unique=True)
    regions: Mapped[list] = mapped_column(JSONB)
    collect_vpc_ids: Mapped[list] = mapped_column(JSONB, default=list)
    # T-6 — 끄면 자동 발동(선차단 · 1분 자동 격리)만 하지 않는다
    auto_action_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # T-6 — 마지막으로 다시 켠 시각(내부 칸 · 계약에 내보내지 않는다). 이 시각 이전에 지난 Medium 응답
    # 기한은 다시 켜도 발동하지 않는다 — 꺼진 동안의 오래된 판단으로 사람 없이 격리하지 않게(#514 Q2).
    # 한 번도 끈 적이 없으면 NULL 이다
    auto_action_resumed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_verification_result: Mapped[TenantVerificationResult | None] = mapped_column(
        _enum(TenantVerificationResult, "tenant_verification_result"), nullable=True
    )
    last_verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_verification_reason_code: Mapped[str | None] = mapped_column(
        String(200), nullable=True
    )
    # T-16 구독 현황(모의) — 원천은 운영자 입력
    plan: Mapped[str | None] = mapped_column(String(50), nullable=True)
    period_start: Mapped[date | None] = mapped_column(Date, nullable=True)
    period_end: Mapped[date | None] = mapped_column(Date, nullable=True)
    suspended_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    suspended_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    __table_args__ = (
        CheckConstraint("aws_account_id ~ '^[0-9]{12}$'", name="account_id_format"),
        # 역할은 그 테넌트의 계정에 있다 — arn:aws:iam::<계정>:role/<이름> 의 5번째 칸
        CheckConstraint(
            "split_part(access_role_arn, ':', 5) = aws_account_id", name="role_in_tenant_account"
        ),
        CheckConstraint("length(external_id) >= 16", name="external_id_min_length"),
        CheckConstraint(
            "jsonb_typeof(regions) = 'array' AND jsonb_array_length(regions) > 0",
            name="regions_non_empty_array",
        ),
        CheckConstraint("jsonb_typeof(collect_vpc_ids) = 'array'", name="collect_vpc_ids_is_array"),
        # 확인 결과와 시각은 함께 있고, 사유는 실패일 때만 있다
        CheckConstraint(
            "((last_verification_result IS NULL) = (last_verified_at IS NULL))"
            " AND ((last_verification_result IS NULL OR last_verification_result = 'OK')"
            " = (last_verification_reason_code IS NULL))",
            name="verification_shape",
        ),
        # ACTIVE 는 연결 확인을 거쳐서만 된다 — 그 뒤의 실패는 상태를 내리지 않는다
        CheckConstraint(
            "status <> 'ACTIVE' OR last_verified_at IS NOT NULL", name="active_was_verified"
        ),
        CheckConstraint(
            "((status = 'SUSPENDED') = (suspended_at IS NOT NULL))"
            " AND ((status = 'SUSPENDED') = (suspended_reason IS NOT NULL))",
            name="suspension_with_status",
        ),
        CheckConstraint(
            "period_start IS NULL OR period_end IS NULL OR period_end >= period_start",
            name="period_ordered",
        ),
    )


# 뿌리 테이블 5개(collection_runs · assets · threat_events · incidents · action_executions)의
# 테넌트 칸. 나머지 테이블은 FK 로 따라온다(#485 설계 초안 Q1).
# **NULL = MVP 단일 계정 기록이다** — 테넌트가 생기기 전의 행이고, 그 계정의 테넌트가 실제로
# 연결된 뒤에 채운다. 마이그레이션이 계정 ID · 역할 · ExternalId 를 지어내 「기본 테넌트」를
# 만들지 않는다 — 연결 확인 없는 ACTIVE 가 되어 계약(active_was_verified)과 어긋난다.
def _tenant_fk():
    return mapped_column(_ID, ForeignKey("tenants.tenant_id"), nullable=True, index=True)


# ==============================================================================
# 자산·인시던트 계열 9종
# ==============================================================================


class CollectionRun(Base):
    """수집 실행 1회. Rule 판정·Metric 요약이 이 단위에 묶인다."""

    __tablename__ = "collection_runs"

    collection_run_id: Mapped[str] = mapped_column(
        _ID, primary_key=True, default=_new_id
    )
    tenant_id: Mapped[str | None] = _tenant_fk()
    status: Mapped[CollectionRunStatus] = mapped_column(
        _enum(CollectionRunStatus, "collection_run_status"),
        default=CollectionRunStatus.IN_PROGRESS,
    )
    account_id: Mapped[str] = mapped_column(String(16))
    region: Mapped[str] = mapped_column(String(32))
    mode: Mapped[str] = mapped_column(String(16))  # localstack | aws
    lookback_days: Mapped[int] = mapped_column(Integer)
    period_seconds: Mapped[int] = mapped_column(Integer)
    error_summary: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        # 리전별 최신 run 조회(latest_collection_run_per_region)가 5분 스캔으로 쌓이는
        # 행을 전부 정렬하지 않게 — (region, started_at DESC, id DESC) 순서로 LIMIT 1 을
        # 찍는다. 세 번째 키 id 는 동시각 tie-break 다.
        # 종전의 started_at 단독 인덱스(ix_collection_runs_started_at)는 #343 에서 지웠다 —
        # 플래너가 그쪽을 최신순으로 훑다가 리전 필터로 수천 행을 버리는 경쟁 경로였고
        # (PR #344 리뷰), 시작 시각 단독으로 정렬·범위 조회하는 문장이 운영 코드에 없다.
        Index(
            "ix_collection_runs_region_started_at",
            "region",
            text("started_at DESC"),
            text("collection_run_id DESC"),
        ),
    )


class Asset(Base):
    """관제 대상 AWS 자산 1건 — arn이 안정 키. 판정·수집값은 별도 테이블."""

    __tablename__ = "assets"

    asset_id: Mapped[str] = mapped_column(_ID, primary_key=True, default=_new_id)
    arn: Mapped[str] = mapped_column(String(512), unique=True)
    tenant_id: Mapped[str | None] = _tenant_fk()
    asset_type: Mapped[AssetType] = mapped_column(_enum(AssetType, "asset_type"))
    resource_id: Mapped[str] = mapped_column(String(128))
    name: Mapped[str | None] = mapped_column(String(256), nullable=True)
    account_id: Mapped[str] = mapped_column(String(16))
    region: Mapped[str] = mapped_column(String(32))
    state: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # 유형별 세부 스펙(api/assets.py AssetSpec 7종과 대응) — 유형별 테이블을 두지 않는다
    spec: Mapped[dict] = mapped_column(JSONB, default=dict)
    last_collection_run_id: Mapped[str | None] = mapped_column(
        _ID, ForeignKey("collection_runs.collection_run_id"), nullable=True
    )
    collected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )
    # AWS 에서 사라진 자산의 표시. 하드 삭제하지 않는 이유는 MetricSummary·RuleEvaluation·
    # AssetRelationship 이 asset_id 를 참조해 이력이 함께 끊기기 때문이다. 값은 그 유형을
    # 실제로 관측한 수집 회차가 그 리전에서 이 자산을 보지 못한 시각이고, 다시 관측되면
    # upsert_asset 이 None 으로 되돌린다. (Issue #332)
    absent_since: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    __table_args__ = (
        Index("ix_assets_asset_type", "asset_type"),
        Index("ix_assets_resource_id", "resource_id"),
        Index("ix_assets_region", "region"),
    )


class AssetInventoryCount(Base):
    """수집 회차 1회가 **관측한** 유형별 자산 수 — 시계열 축 5(자산 수 추이)의 원천.

    `assets` 행은 회차마다 덮어써 이력이 없고, 판정 이력(`rule_evaluations`)은 판정 대상
    3종(EC2·SG·EBS)만 남긴다. 그래서 "내 자산이 늘었나 줄었나"를 전 유형으로 복원할 자리가
    없다 — 회차를 마감할 때 유형별 건수를 여기 남긴다(2026-09-29).

    **그 회차가 못 본 유형은 행을 남기지 않는다**(PARTIAL 회차의 degrade 유형). 0건 행과
    "모름"을 가르기 위해서다 — 0을 남기면 추이가 자산이 사라진 것처럼 떨어진다.
    """

    __tablename__ = "asset_inventory_counts"

    collection_run_id: Mapped[str] = mapped_column(
        _ID, ForeignKey("collection_runs.collection_run_id"), primary_key=True
    )
    asset_type: Mapped[AssetType] = mapped_column(
        _enum(AssetType, "asset_type"), primary_key=True
    )
    count: Mapped[int] = mapped_column(Integer)

    __table_args__ = (CheckConstraint("count >= 0", name="count_non_negative"),)


class AssetRelationship(Base):
    """자산 간 방향 연결(source → target). 토폴로지 맵의 역방향 조회를 지원한다."""

    __tablename__ = "asset_relationships"

    relationship_id: Mapped[str] = mapped_column(_ID, primary_key=True, default=_new_id)
    source_asset_id: Mapped[str] = mapped_column(_ID, ForeignKey("assets.asset_id"))
    relation_type: Mapped[RelationType] = mapped_column(
        _enum(RelationType, "relation_type")
    )
    target_arn: Mapped[str] = mapped_column(String(512))
    collection_run_id: Mapped[str | None] = mapped_column(
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

    metric_summary_id: Mapped[str] = mapped_column(
        _ID, primary_key=True, default=_new_id
    )
    asset_id: Mapped[str] = mapped_column(_ID, ForeignKey("assets.asset_id"))
    collection_run_id: Mapped[str] = mapped_column(
        _ID, ForeignKey("collection_runs.collection_run_id")
    )
    cpu_datapoints: Mapped[int] = mapped_column(Integer, default=0)
    cpu_avg: Mapped[float | None] = mapped_column(Float, nullable=True)
    cpu_max: Mapped[float | None] = mapped_column(Float, nullable=True)
    net_in_avg: Mapped[float | None] = mapped_column(Float, nullable=True)
    net_out_avg: Mapped[float | None] = mapped_column(Float, nullable=True)
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    window_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    collected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )

    __table_args__ = (
        UniqueConstraint("asset_id", "collection_run_id"),
        CheckConstraint("window_end >= window_start", name="window_ordered"),
        # 신선한 메트릭 재사용(fresh_ec2_metric_summaries)의 window_end >= 기준 시각 —
        # 회차마다 쌓이는 요약을 전부 훑지 않고 최근 구간만 읽는다.
        Index("ix_metric_summaries_window_end", text("window_end DESC")),
    )


class RuleEvaluation(Base):
    """Rule Evaluator의 결정적 판정 1건 — rules.RuleEvaluationResult와 대응."""

    __tablename__ = "rule_evaluations"

    rule_evaluation_id: Mapped[str] = mapped_column(
        _ID, primary_key=True, default=_new_id
    )
    asset_id: Mapped[str] = mapped_column(_ID, ForeignKey("assets.asset_id"))
    collection_run_id: Mapped[str] = mapped_column(
        _ID, ForeignKey("collection_runs.collection_run_id")
    )
    # 공개 Enum(EvaluationStatus·Verdict·SkipReasonCode) 값을 담는다 — 교차 불변식
    # (COMPLETED↔verdict, SKIP↔사유)은 Pydantic 계약(rules.py)과 Workflow가 검증한다
    evaluation_status: Mapped[str] = mapped_column(String(32))
    verdict: Mapped[str | None] = mapped_column(String(32), nullable=True)
    health_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    skip_reason_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reason: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    evaluated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint("asset_id", "collection_run_id"),
        CheckConstraint(
            "health_score IS NULL OR (health_score >= 0 AND health_score <= 100)",
            name="health_score_range",
        ),
        Index("ix_rule_evaluations_verdict", "verdict"),
        # 자산별 최신 판정(latest_rule_evaluation / latest_rule_evaluation_by_asset) —
        # 정렬 키와 같은 순서라 자산마다 인덱스 첫 항목 1건으로 끝난다.
        Index(
            "ix_rule_evaluations_asset_evaluated_at",
            "asset_id",
            text("evaluated_at DESC"),
            text("rule_evaluation_id DESC"),
        ),
    )


class ThreatEvent(Base):
    """정규화된 위협 이벤트 — events.NormalizedThreatEvent와 대응. 생성 후 불변."""

    __tablename__ = "threat_events"

    threat_event_id: Mapped[str] = mapped_column(_ID, primary_key=True, default=_new_id)
    tenant_id: Mapped[str | None] = _tenant_fk()
    source_event_id: Mapped[str] = mapped_column(String(256))
    event_type: Mapped[ThreatEventType] = mapped_column(
        _enum(ThreatEventType, "threat_event_type")
    )
    target_arn: Mapped[str] = mapped_column(String(512))
    payload: Mapped[dict] = mapped_column(JSONB)
    deduplication_key: Mapped[str] = mapped_column(String(512), unique=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    collected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )

    __table_args__ = (
        Index("ix_threat_events_source_event_id", "source_event_id"),
        Index("ix_threat_events_target_arn", "target_arn"),
    )


class Incident(Base):
    """업무 흐름의 중심 엔티티. 초기 판정(불변)과 AI 사후 평가를 분리 보관한다."""

    __tablename__ = "incidents"

    incident_id: Mapped[str] = mapped_column(_ID, primary_key=True, default=_new_id)
    tenant_id: Mapped[str | None] = _tenant_fk()
    title: Mapped[str | None] = mapped_column(String(256), nullable=True)
    subject_arn: Mapped[str] = mapped_column(String(512))
    category: Mapped[IncidentCategory] = mapped_column(
        _enum(IncidentCategory, "incident_category")
    )
    status: Mapped[IncidentStatus] = mapped_column(
        _enum(IncidentStatus, "incident_status"), default=IncidentStatus.ANALYZING
    )
    threat_event_id: Mapped[str | None] = mapped_column(
        _ID, ForeignKey("threat_events.threat_event_id"), nullable=True
    )
    # --- 위험 대응 축(SECOPS 전용, FINOPS는 전부 null — api/incidents.py 불변식) ---
    initial_risk_level: Mapped[RiskLevel | None] = mapped_column(
        _enum(RiskLevel, "risk_level"), nullable=True
    )
    reviewed_risk_level: Mapped[RiskLevel | None] = mapped_column(
        _enum(RiskLevel, "risk_level"), nullable=True
    )
    response_mode: Mapped[ResponseMode | None] = mapped_column(
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
    agent_invocation_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    agent_wait_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    response_deadline_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # --- 관제자 종료 판단(Issue #199) — updated_at은 자식 상태 변경으로도 올라가므로
    #     종료 시각은 따로 남긴다 ---
    resolution: Mapped[ResolutionJudgement | None] = mapped_column(
        _enum(ResolutionJudgement, "resolution_judgement"), nullable=True
    )
    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    resolution_note: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )
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
        CheckConstraint(
            "resolution_note IS NULL OR"
            " (resolution IS NOT NULL AND length(btrim(resolution_note)) > 0)",
            name="resolution_note_with_judgement",
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
    evidence_type: Mapped[EvidenceType] = mapped_column(
        _enum(EvidenceType, "evidence_type")
    )
    source_type: Mapped[str] = mapped_column(String(64))
    source_id: Mapped[str] = mapped_column(String(256))
    content: Mapped[dict] = mapped_column(JSONB)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    collected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )

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
    ai_savings_estimate: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    evidence_ids: Mapped[list] = mapped_column(JSONB, default=list)
    status: Mapped[CandidateStatus] = mapped_column(
        _enum(CandidateStatus, "candidate_status"),
        default=CandidateStatus.PENDING_VALIDATION,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )
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
    candidate_id: Mapped[str | None] = mapped_column(
        _ID, ForeignKey("runbook_candidates.candidate_id"), nullable=True
    )
    execution_id: Mapped[str | None] = mapped_column(
        _ID, ForeignKey("action_executions.execution_id"), nullable=True
    )
    result: Mapped[GuardrailDecision] = mapped_column(
        _enum(GuardrailDecision, "guardrail_decision")
    )
    failed_step: Mapped[GuardrailStep | None] = mapped_column(
        _enum(GuardrailStep, "guardrail_step"), nullable=True
    )
    # 고정 순서 4단계 결과(guardrails.GuardrailStepResult 목록) — 구조 검증은 Pydantic
    steps: Mapped[list] = mapped_column(JSONB)
    # PASS 시 확정되는 불변 실행 명령 — typed RunbookCommand 계약 확정 전까지 JSONB
    validated_command: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
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
    """런북 실행 1건. 상태 7종은 공개 계약(api/actions.py)이 원천."""

    __tablename__ = "action_executions"

    execution_id: Mapped[str] = mapped_column(_ID, primary_key=True, default=_new_id)
    incident_id: Mapped[str] = mapped_column(_ID, ForeignKey("incidents.incident_id"))
    # 인시던트로도 따라오지만 디스패처가 정지(SUSPENDED) 테넌트를 실행 행에서 바로 거르도록 둔다.
    # 인시던트의 테넌트와 같아야 한다 — 트리거 없이 CHECK 로 못 걸어 쓰는 자리에서 맞춘다
    tenant_id: Mapped[str | None] = _tenant_fk()
    candidate_id: Mapped[str | None] = mapped_column(
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
    idempotency_key: Mapped[str | None] = mapped_column(
        String(128), unique=True, nullable=True
    )
    deduplication_key: Mapped[str | None] = mapped_column(
        String(512), unique=True, nullable=True
    )
    # 롤백 자식 → 원본 실행. 원복 파라미터는 backup_record에서만 로드(ADR-0004 정책 ③)
    parent_execution_id: Mapped[str | None] = mapped_column(
        _ID, ForeignKey("action_executions.execution_id"), nullable=True
    )
    backup_record_id: Mapped[str | None] = mapped_column(
        _ID,
        ForeignKey("backup_records.backup_record_id", use_alter=True),
        nullable=True,
    )
    # Guardrail PASS에서 복사되는 불변 실행 명령
    validated_command: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # EC2_ENABLE_AUTOSCALING 전용 — 첫 AWS 변경 전에 commit되는 고정 실행 계획
    # (schemas.autoscaling.AutoscalingExecutionPlan). validated_command의 의미는 바꾸지
    # 않는다. 한 번 채우면 덮어쓰지 않는다(executions_repo.bind_autoscaling_plan).
    # none_as_null: 계획 없음은 SQL NULL이어야 한다 — JSON null이면 bind의 IS NULL 조건을 비껴간다.
    autoscaling_plan: Mapped[dict | None] = mapped_column(
        JSONB(none_as_null=True), nullable=True
    )
    # EC2_ISOLATE(PROTECT)·EC2_UNISOLATE(RESTORE)의 ASG scale-in 보호 결과
    # (schemas.backups.ScaleInProtectionResult). 실행 상태·원복 근거 백업과 별개의 보조 관측이다.
    # none_as_null: 결과 없음은 SQL NULL이다.
    scale_in_protection_result: Mapped[dict | None] = mapped_column(
        JSONB(none_as_null=True), nullable=True
    )
    error_summary: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # 판정 불가 보류 기록 (Issue #249) — AWS에 물어보지 못해 결과를 확정하지 못한 이력.
    # 재시도 여부와 소진 판단의 입력이라 사유는 typed 코드로만 두고, 사람이 읽는
    # error_summary를 근거로 쓰지 않는다. 판정이 내려지면 지워지고(workflows.close_execution)
    # 소진으로 확정한 기록만 남는다.
    verification_reason_code: Mapped[PrecheckReasonCode | None] = mapped_column(
        _enum(PrecheckReasonCode, "precheck_reason_code"), nullable=True
    )
    verification_attempts: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0")
    )
    verification_first_failed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    verification_last_failed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        # ROLLBACK_INITIATED·ROLLED_BACK·ROLLBACK_FAILED·UNVERIFIED는 원본 Execution 전용 —
        # 롤백 자식은 진행·성공·실패만 갖는다 (SSOT §API 계약)
        CheckConstraint(
            "parent_execution_id IS NULL"
            " OR status IN ('IN_PROGRESS', 'SUCCESS', 'FAILED')",
            name="rollback_child_status",
        ),
        # 보류 기록 네 칸은 함께 채워지거나 함께 비어 있다 (Issue #249). 마지막 실패
        # 시각의 IS NOT NULL을 명시한다 — 없으면 NULL 비교가 NULL이 되어 CHECK를 통과한다
        CheckConstraint(
            "(verification_attempts = 0 AND verification_reason_code IS NULL"
            " AND verification_first_failed_at IS NULL"
            " AND verification_last_failed_at IS NULL)"
            " OR (verification_attempts >= 1 AND verification_reason_code IS NOT NULL"
            " AND verification_first_failed_at IS NOT NULL"
            " AND verification_last_failed_at IS NOT NULL"
            " AND verification_last_failed_at >= verification_first_failed_at)",
            name="verification_hold_shape",
        ),
        # UNVERIFIED는 무엇을 확인하지 못했는지가 남아야 한다. text로 비교하는 것은 새
        # enum 값을 추가한 트랜잭션에서 그 값을 쓰지 않기 위해서다(20260914 마이그레이션)
        CheckConstraint(
            "status::text <> 'UNVERIFIED' OR verification_attempts >= 1",
            name="unverified_has_hold",
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

    execution_step_id: Mapped[str] = mapped_column(
        _ID, primary_key=True, default=_new_id
    )
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
    effect: Mapped[ExecutionEffect | None] = mapped_column(
        _enum(ExecutionEffect, "execution_effect"), nullable=True
    )
    aws_request_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    result_summary: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    error_summary: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )

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

    backup_record_id: Mapped[str] = mapped_column(
        _ID, primary_key=True, default=_new_id
    )
    # 백업을 생성한 실행(주 조치·자동 격리)
    execution_id: Mapped[str] = mapped_column(
        _ID, ForeignKey("action_executions.execution_id")
    )
    target_arn: Mapped[str] = mapped_column(String(512))
    backup_type: Mapped[str] = mapped_column(String(64))  # 예: SAVE_INSTANCE_SPEC_JSON
    payload: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )

    __table_args__ = (Index("ix_backup_records_target_arn", "target_arn"),)


# ==============================================================================
# MVP 서비스 연결·전환 3종 — ActionExecution·ExecutionStep과 의미를 섞지 않는다
# (계약: schemas.service_transition). 행은 삭제하지 않는다.
# ==============================================================================

_OPERATION_STATES = "'PENDING', 'APPLIED', 'NOT_APPLIED', 'UNCERTAIN'"


class ServiceTransition(Base):
    """#446 실행 1건의 서비스 시도 — 의도 + 원래 경로 스냅숏. 생성 후 불변(갱신 경로 없음).

    시도는 실행마다 한 줄로 잇는다. 첫 시도(root)는 실행당 하나, 각 시도는 같은 실행의 시도
    하나만 대체하며(fork 없음·자기 대체 없음), 현재 시도(leaf)는 아무도 대체하지 않은 시도로
    **유도한다** — 상태·시각을 따로 적지 않는다.
    """

    __tablename__ = "service_transitions"

    transition_id: Mapped[str] = mapped_column(_ID, primary_key=True)
    execution_id: Mapped[str] = mapped_column(
        _ID, ForeignKey("action_executions.execution_id")
    )
    supersedes_transition_id: Mapped[str | None] = mapped_column(_ID, nullable=True)
    plan: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )

    __table_args__ = (
        CheckConstraint("jsonb_typeof(plan) = 'object'", name="plan_is_object"),
        CheckConstraint(
            "supersedes_transition_id IS NULL OR supersedes_transition_id <> transition_id",
            name="not_self",
        ),
        # (시도, 실행) 짝 — 대체 대상·일지·관측이 같은 실행의 시도만 가리키게 하는 복합 FK의 대상
        UniqueConstraint(
            "transition_id", "execution_id", name="uq_service_transitions_id_execution"
        ),
        UniqueConstraint(
            "supersedes_transition_id", name="uq_service_transitions_supersedes"
        ),
        ForeignKeyConstraint(
            ["supersedes_transition_id", "execution_id"],
            ["service_transitions.transition_id", "service_transitions.execution_id"],
            name="fk_service_transitions_supersedes_same_execution",
        ),
        Index(
            "uq_service_transitions_execution_root",
            "execution_id",
            unique=True,
            postgresql_where=text("supersedes_transition_id IS NULL"),
        ),
    )


class ServiceTransitionOperation(Base):
    """AWS 변경 1건의 일지 — #446 실행 단위, 시도가 만든 서비스 자원이면 그 시도를 함께 가리킨다.

    호출 **전에** PENDING으로 commit하고 호출 뒤 결과로 닫는다. PENDING(끝나지 않음)·UNCERTAIN
    (응답 유실·5xx)은 재생하지 않는다 — 의도한 효과가 현물에서 확인될 때(EFFECT_OBSERVED)까지
    같은 실행의 변경 명령을 막는다. 효과가 안 보인다는 대조(EFFECT_NOT_OBSERVED)는 막음을 풀지 않는다.
    """

    __tablename__ = "service_transition_operations"

    operation_id: Mapped[str] = mapped_column(_ID, primary_key=True, default=_new_id)
    execution_id: Mapped[str] = mapped_column(
        _ID,
        ForeignKey(
            "action_executions.execution_id",
            name="fk_service_transition_operations_execution_id_action_executions",
        ),
    )
    # 시도 소유 서비스 자원 변경이면 그 시도, #446 인프라 정리면 NULL
    transition_id: Mapped[str | None] = mapped_column(_ID, nullable=True)
    sequence: Mapped[int] = mapped_column(Integer)
    command: Mapped[str] = mapped_column(String(32))
    aws_operation: Mapped[str] = mapped_column(String(128))
    resource_ref: Mapped[str] = mapped_column(String(512))
    request: Mapped[dict] = mapped_column(JSONB)
    state: Mapped[str] = mapped_column(String(16))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    response: Mapped[dict | None] = mapped_column(
        JSONB(none_as_null=True), nullable=True
    )
    error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    error_summary: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    reconciled_state: Mapped[str | None] = mapped_column(String(32), nullable=True)
    reconciled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        UniqueConstraint(
            "execution_id",
            "sequence",
            name="uq_service_transition_operations_execution_sequence",
        ),
        ForeignKeyConstraint(
            ["transition_id", "execution_id"],
            ["service_transitions.transition_id", "service_transitions.execution_id"],
            name="fk_service_transition_operations_attempt",
        ),
        CheckConstraint("sequence >= 1", name="sequence_positive"),
        CheckConstraint(f"state IN ({_OPERATION_STATES})", name="state_known"),
        CheckConstraint(
            "(state = 'PENDING') = (finished_at IS NULL)", name="pending_unfinished"
        ),
        CheckConstraint(
            "(reconciled_state IS NULL AND reconciled_at IS NULL)"
            " OR (reconciled_state IN ('EFFECT_OBSERVED', 'EFFECT_NOT_OBSERVED')"
            " AND reconciled_at IS NOT NULL AND state IN ('PENDING', 'UNCERTAIN'))",
            name="reconciled_shape",
        ),
    )


class NotificationEvent(Base):
    """#493: 사건 사실 자체가 outbox다. 발송 상태는 PR2의 별도 채널별 행에 둔다.

    업무 식별자는 스냅숏이다. 업무 행의 삭제가 미발송 의도까지 지우지 않도록
    FK cascade와 연결하지 않는다. 생성은 해당 업무 트랜잭션 안에서만 한다.
    """

    __tablename__ = "notification_events"

    event_id: Mapped[str] = mapped_column(_ID, primary_key=True, default=_new_id)
    incident_id: Mapped[str] = mapped_column(_ID)
    execution_id: Mapped[str | None] = mapped_column(_ID, nullable=True)
    related_execution_id: Mapped[str | None] = mapped_column(_ID, nullable=True)
    kind: Mapped[str] = mapped_column(String(32))
    phase: Mapped[str] = mapped_column(String(16))
    outcome: Mapped[str | None] = mapped_column(String(16), nullable=True)
    target_arn: Mapped[str] = mapped_column(String(512))
    aws_account_id: Mapped[str | None] = mapped_column(String(12), nullable=True)
    region: Mapped[str | None] = mapped_column(String(32), nullable=True)
    risk_level: Mapped[RiskLevel | None] = mapped_column(
        _enum(RiskLevel, "risk_level"), nullable=True
    )
    trigger_source: Mapped[TriggerSource | None] = mapped_column(
        _enum(TriggerSource, "trigger_source"), nullable=True
    )
    runbook_id: Mapped[RunbookId | None] = mapped_column(
        _enum(RunbookId, "runbook_id"), nullable=True
    )
    execution_status: Mapped[ExecutionStatus | None] = mapped_column(
        _enum(ExecutionStatus, "execution_status"), nullable=True
    )
    reason_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    __table_args__ = (
        UniqueConstraint(
            "kind", "incident_id", "execution_id", "phase",
            name="uq_notification_events_transition",
            postgresql_nulls_not_distinct=True,
        ),
        CheckConstraint(
            "(kind = 'HIGH_INCIDENT_CREATED' AND phase = 'CREATED'"
            " AND execution_id IS NULL AND related_execution_id IS NULL"
            " AND risk_level IS NOT NULL AND risk_level = 'HIGH'"
            " AND trigger_source IS NULL AND execution_status IS NULL AND runbook_id IS NULL)"
            " OR (kind IN ('AUTO_ISOLATION', 'AUTO_ROLLBACK')"
            " AND phase IN ('REQUESTED', 'RESULT') AND execution_id IS NOT NULL"
            " AND trigger_source IS NOT NULL AND execution_status IS NOT NULL"
            " AND runbook_id IS NOT NULL"
            " AND ((kind = 'AUTO_ISOLATION'"
            " AND trigger_source IN ('PRE_MITIGATION_0_5S', 'TIMEOUT_ISOLATION_1M')"
            " AND related_execution_id IS NULL)"
            " OR (kind = 'AUTO_ROLLBACK' AND trigger_source = 'AUTO_ON_FAILURE')))",
            name="event_shape",
        ),
        CheckConstraint(
            "(phase IN ('CREATED', 'REQUESTED') AND outcome IS NULL)"
            " OR (phase = 'RESULT' AND outcome IS NOT NULL"
            " AND outcome IN ('SUCCESS', 'FAILED', 'REJECTED', 'UNVERIFIED'))",
            name="outcome_shape",
        ),
        CheckConstraint(
            "aws_account_id IS NULL OR aws_account_id ~ '^[0-9]{12}$'",
            name="account_shape",
        ),
        Index("ix_notification_events_created_at", "created_at", "event_id"),
        Index("ix_notification_events_incident_id", "incident_id"),
    )


# ==============================================================================
# #493 PR2 채널별 발송 상태 3종 — notification_events(사건 사실)는 고치지 않는다.
# 시각 비교(활성화·임대·재시도·관문)는 모두 DB 시각이다. 수신처 비밀값은 두지 않는다.
# ==============================================================================

_DELIVERY_CHANNELS = "'SLACK', 'SNS'"


class NotificationRoute(Base):
    """현재 활성 수신 바인딩(계정·채널당 1행). 수신처는 fingerprint로만 남긴다.

    같은 fingerprint 재등록은 epoch·activated_at을 유지한다. 바인딩 변경과 제거 후
    재등록은 새 epoch와 DB 시각 activated_at을 받는다 — 대기 행은 자기 epoch가 여기
    현재 값과 같을 때만 보낸다.
    """

    __tablename__ = "notification_routes"

    aws_account_id: Mapped[str] = mapped_column(String(12), primary_key=True)
    channel: Mapped[str] = mapped_column(String(8), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128))
    fingerprint: Mapped[str] = mapped_column(String(64))
    route_epoch: Mapped[str] = mapped_column(_ID)
    activated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(f"channel IN ({_DELIVERY_CHANNELS})", name="channel_known"),
        CheckConstraint("aws_account_id ~ '^[0-9]{12}$'", name="account_shape"),
    )


class NotificationLane(Base):
    """채널 공통 호출 관문. Slack은 모든 처리 프로세스가 이 1행으로 간격·429 대기를 공유한다.

    available_at은 보유자가 여닫는 간격 관문(호출 중 임대 길이, 결과 뒤 1초)이고, cooldown_until은
    공급자가 요구한 대기(Retry-After)다. 대기는 누가 해제하든 최댓값으로만 늘고 보유자 해제로
    줄지 않는다. claim은 두 시각이 모두 지나야 관문을 잡는다.
    """

    __tablename__ = "notification_lanes"

    channel: Mapped[str] = mapped_column(String(8), primary_key=True)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    holder_token: Mapped[str | None] = mapped_column(_ID, nullable=True)
    cooldown_until: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(f"channel IN ({_DELIVERY_CHANNELS})", name="channel_known"),
    )


class NotificationDelivery(Base):
    """(사건, 채널)당 발송 상태 1행. 종결(SUCCEEDED·FAILED·BLOCKED)은 되돌리지 않는다.

    PENDING → IN_FLIGHT(claim, 시도 수 증가) → SUCCEEDED | PENDING(재시도) | FAILED.
    BLOCKED는 라우트 없음·충돌·바인딩 변경·활성화 전 사건이다. last_category는
    마지막 실패 범주이고 final_reason은 FAILED의 종결 사유(PERMANENT·EXHAUSTED)다.
    """

    __tablename__ = "notification_deliveries"

    delivery_id: Mapped[str] = mapped_column(_ID, primary_key=True, default=_new_id)
    event_id: Mapped[str] = mapped_column(
        _ID, ForeignKey("notification_events.event_id")
    )
    channel: Mapped[str] = mapped_column(String(8))
    state: Mapped[str] = mapped_column(String(16))
    # 생성 때 묶은 바인딩 스냅숏 — 보내기 전 DB 현재 바인딩·설정과 대조한다
    tenant_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    aws_account_id: Mapped[str | None] = mapped_column(String(12), nullable=True)
    route_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    route_epoch: Mapped[str | None] = mapped_column(_ID, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    next_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    claim_token: Mapped[str | None] = mapped_column(_ID, nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_category: Mapped[str | None] = mapped_column(String(16), nullable=True)
    last_error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    blocked_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    final_reason: Mapped[str | None] = mapped_column(String(16), nullable=True)
    receipt_kind: Mapped[str | None] = mapped_column(String(16), nullable=True)
    receipt_ref: Mapped[str | None] = mapped_column(String(256), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        UniqueConstraint(
            "event_id", "channel", name="uq_notification_deliveries_event_channel"
        ),
        CheckConstraint(f"channel IN ({_DELIVERY_CHANNELS})", name="channel_known"),
        CheckConstraint("attempts >= 0", name="attempts_non_negative"),
        CheckConstraint(
            "last_category IS NULL"
            " OR last_category IN ('TRANSIENT', 'PERMANENT', 'UNKNOWN')",
            name="category_known",
        ),
        # IN 비교는 NULL이면 NULL이 되어 CHECK를 통과하므로 IS NOT NULL을 함께 쓴다
        CheckConstraint(
            "(state IN ('PENDING', 'IN_FLIGHT', 'SUCCEEDED', 'FAILED')"
            " AND tenant_id IS NOT NULL AND aws_account_id IS NOT NULL"
            " AND route_fingerprint IS NOT NULL AND route_epoch IS NOT NULL"
            " AND blocked_reason IS NULL"
            " AND ((state = 'PENDING' AND next_attempt_at IS NOT NULL"
            " AND claim_token IS NULL AND lease_expires_at IS NULL"
            " AND finished_at IS NULL AND final_reason IS NULL AND receipt_kind IS NULL)"
            " OR (state = 'IN_FLIGHT' AND attempts >= 1 AND next_attempt_at IS NULL"
            " AND claim_token IS NOT NULL AND lease_expires_at IS NOT NULL"
            " AND finished_at IS NULL AND final_reason IS NULL AND receipt_kind IS NULL)"
            " OR (state = 'SUCCEEDED' AND attempts >= 1 AND next_attempt_at IS NULL"
            " AND claim_token IS NULL AND lease_expires_at IS NULL"
            " AND finished_at IS NOT NULL AND final_reason IS NULL"
            " AND receipt_kind IS NOT NULL"
            " AND receipt_kind IN ('SLACK_POSTED', 'SNS_ACCEPTED'))"
            " OR (state = 'FAILED' AND attempts >= 1 AND next_attempt_at IS NULL"
            " AND claim_token IS NULL AND lease_expires_at IS NULL"
            " AND finished_at IS NOT NULL AND receipt_kind IS NULL"
            " AND last_category IS NOT NULL AND final_reason IS NOT NULL"
            " AND final_reason IN ('PERMANENT', 'EXHAUSTED'))))"
            " OR (state = 'BLOCKED' AND blocked_reason IS NOT NULL"
            " AND next_attempt_at IS NULL AND claim_token IS NULL"
            " AND lease_expires_at IS NULL AND finished_at IS NOT NULL"
            " AND final_reason IS NULL AND receipt_kind IS NULL)",
            name="state_shape",
        ),
        Index(
            "ix_notification_deliveries_pending_due",
            "channel",
            "next_attempt_at",
            postgresql_where=text("state = 'PENDING'"),
        ),
        Index(
            "ix_notification_deliveries_in_flight_lease",
            "channel",
            "lease_expires_at",
            postgresql_where=text("state = 'IN_FLIGHT'"),
        ),
    )


class ServiceTransitionObservation(Base):
    """typed 관측 1건(schemas.service_transition.ServiceObservation). 생성 후 불변.

    #446 실행 단위로 남고, 시도의 관측이면 그 시도를 함께 가리킨다(실행 단위 정리 재고는 NULL).
    """

    __tablename__ = "service_transition_observations"

    observation_id: Mapped[str] = mapped_column(_ID, primary_key=True, default=_new_id)
    execution_id: Mapped[str] = mapped_column(
        _ID,
        ForeignKey(
            "action_executions.execution_id",
            name="fk_service_transition_observations_execution",
        ),
    )
    transition_id: Mapped[str | None] = mapped_column(_ID, nullable=True)
    kind: Mapped[str] = mapped_column(String(16))
    verdict: Mapped[str] = mapped_column(String(16))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    evidence: Mapped[dict] = mapped_column(JSONB)

    __table_args__ = (
        ForeignKeyConstraint(
            ["transition_id", "execution_id"],
            ["service_transitions.transition_id", "service_transitions.execution_id"],
            name="fk_service_transition_observations_attempt",
        ),
        CheckConstraint(
            "kind IN ('PLAN', 'VERIFY', 'CUTOVER', 'RETURN', 'RECONCILE', 'CLEANUP')",
            name="kind_known",
        ),
        CheckConstraint(
            "verdict IN ('PASSED', 'FAILED', 'UNKNOWN')", name="verdict_known"
        ),
        Index(
            "ix_service_transition_observations_execution_observed",
            "execution_id",
            text("observed_at DESC"),
        ),
    )


# ==============================================================================
# 인증 계열 — Cognito 로그인 + 서버 세션 (#492 PR 1) · 담당 고객사 배정 (#492 PR 5)
# ==============================================================================


class AppUserStatus(str, Enum):
    ACTIVE = "ACTIVE"
    DISABLED = "DISABLED"


class AuthSessionMfaStep(str, Enum):
    """이번 로그인에서 TOTP를 증명한 Cognito 챌린지 단계."""

    SOFTWARE_TOKEN_MFA = "SOFTWARE_TOKEN_MFA"
    MFA_SETUP = "MFA_SETUP"


class AuthSessionRevokeReason(str, Enum):
    LOGOUT = "LOGOUT"
    #: 같은 사용자가 새로 로그인해 이전 세션 전부(이전 쿠키 세션 포함)를 새 세션으로 교체했다.
    SUPERSEDED = "SUPERSEDED"
    ADMIN_REVOKED = "ADMIN_REVOKED"
    ACCOUNT_DISABLED = "ACCOUNT_DISABLED"
    PASSWORD_RESET = "PASSWORD_RESET"
    #: TOTP 분실 복구(#515)의 제공자 호출 전 차단 — 대상의 살아 있는 세션 전부.
    TOTP_RESET = "TOTP_RESET"


class MfaRecoveryState(str, Enum):
    """TOTP 분실 복구(#515)가 사용자 로그인에 거는 차단. 전이는 그 신원의 변경 울타리를 쥔 쪽의 조건부 갱신이다."""

    #: 차단 없음 — 현재 세대의 TOTP 등록으로 로그인한다(이메일 증명은 별도 요건).
    NONE = "NONE"
    #: 차단 기록 뒤 제공자 삭제가 확인되지 않았다(진행 · 결과 불명 · 거절 · 중단). 앱 인증 전부 거절.
    RESET_PENDING = "RESET_PENDING"
    #: 제공자에 등록이 없다고 확인됐다 — 이 세대의 새 MFA_SETUP 성공만 NONE 으로 되돌린다.
    ENROLLMENT_REQUIRED = "ENROLLMENT_REQUIRED"


class TotpRecoveryStatus(str, Enum):
    """복구 작업 1건의 확정 상태. 성공은 COMPLETED 하나뿐이다."""

    #: 차단은 확정됐고 제공자 호출이 일어났을 수 있다 — 결과를 기록하지 못했다(진행 중 · 중단 · 기록 실패).
    PROVIDER_PENDING = "PROVIDER_PENDING"
    #: AdminDeleteSoftwareToken 성공 응답을 받아 기록했다.
    COMPLETED = "COMPLETED"
    #: ResourceNotFoundException — 등록 없음 또는 리소스 없음. 이 작업이 삭제했다는 증명이 아니다.
    PROVIDER_NOT_FOUND = "PROVIDER_NOT_FOUND"
    #: 그 밖의 4xx — 제공자가 거절했다(삭제되지 않았다). 차단은 남는다.
    PROVIDER_REJECTED = "PROVIDER_REJECTED"
    #: 5xx · 전송 실패 · 시간 초과 — 삭제 여부를 모른다. 차단 · 신원 변경 울타리가 남는다.
    PROVIDER_UNKNOWN = "PROVIDER_UNKNOWN"


class TotpProviderMutationKind(str, Enum):
    """신원 변경 울타리(#515)를 연 Cognito TOTP 변경 구간의 종류."""

    #: 복구 작업의 AdminDeleteSoftwareToken
    RESET = "RESET"
    #: 로그인 흐름의 TOTP 등록 변경(AssociateSoftwareToken · VerifySoftwareToken · MFA_SETUP 응답 · 선호 설정)
    ENROLLMENT = "ENROLLMENT"


class TotpProviderMutationResolution(str, Enum):
    """울타리가 풀린 근거. 풀리지 않은(NULL) 울타리는 그 신원의 다른 TOTP 변경 · 복구를 모두 막는다."""

    #: 그 구간의 제공자 호출이 확정 응답(성공 · 4xx)으로 끝났다고 작업자가 기록했다.
    PROVIDER_RETURNED = "PROVIDER_RETURNED"
    #: 운영자가 그 호출이 더는 적용될 수 없음을 외부에서 확인했다고 진술했다 — 앱의 증명이 아니다.
    OPERATOR_RECONCILED = "OPERATOR_RECONCILED"


class AppUser(Base):
    """앱 사용자. 신원은 (issuer, cognito_sub)이다 — email은 표시·연락용이며 계정 연결에 쓰지 않는다."""

    __tablename__ = "app_users"

    user_id: Mapped[str] = mapped_column(_ID, primary_key=True, default=_new_id)
    issuer: Mapped[str] = mapped_column(String)
    cognito_sub: Mapped[str] = mapped_column(String)
    email: Mapped[str] = mapped_column(String)
    email_verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    role: Mapped[UserRole] = mapped_column(_enum(UserRole, "app_user_role"))
    # 계정 종류 — 플랫폼 계정인지의 유일한 원천이다(role 은 계정 안의 역할일 뿐). ORM·서버 기본값이 없다:
    # 호출부가 반드시 정해야 하고, 빠뜨린 쓰기는 NOT NULL 이 거절한다. PLATFORM 의 role=ADMIN 은 호환 값이며
    # 권한 판정에 쓰지 않는다.
    account_type: Mapped[AccountType] = mapped_column(
        _enum(AccountType, "app_user_account_type")
    )
    # 플랫폼 등급(#492) — 플랫폼 관리 권한의 유일한 원천이다. PLATFORM 만 값을 갖고(CHECK)
    # TENANT · UNASSIGNED 는 NULL 이다. 서버 기본값이 없다 — 플랫폼 계정을 만드는 쪽이 정한다.
    # CENTRAL_ADMIN 은 모든 고객사, TENANT_MANAGER 는 app_user_tenant_assignments 에 배정된 고객사만이다.
    platform_role: Mapped[PlatformRole | None] = mapped_column(
        _enum(PlatformRole, "app_user_platform_role"), nullable=True
    )
    # 소속 고객사(Post-MVP T-4 · #495). TENANT 계정만 값을 갖고(CHECK), PLATFORM·UNASSIGNED 는 NULL 이다.
    # NULL 자체는 권한이 아니다 — 권한은 account_type 이 정한다. 소속을 API 로 옮기지 않는다.
    tenant_id: Mapped[str | None] = _tenant_fk()
    status: Mapped[AppUserStatus] = mapped_column(
        _enum(AppUserStatus, "app_user_status"), default=AppUserStatus.ACTIVE
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )
    # TOTP 분실 복구(#515) — 인증 세대는 복구 차단마다 1 오른다. 로그인 흐름은 TOTP 증명 전에 세대를
    # 포착하고, 완료 시 users 잠금 안에서 같은 세대 · 차단 없음(NONE)이어야 세션을 받는다.
    # 서버 기본값은 이 칸을 모르는 쓰기(raw INSERT)용이다 — 기존 사용자는 세대 0 · NONE.
    auth_generation: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0")
    )
    mfa_recovery_state: Mapped[MfaRecoveryState] = mapped_column(
        _enum(MfaRecoveryState, "app_user_mfa_recovery_state"),
        default=MfaRecoveryState.NONE,
        server_default=MfaRecoveryState.NONE.value,
    )

    __table_args__ = (
        UniqueConstraint("issuer", "cognito_sub"),
        CheckConstraint(
            "(account_type = 'TENANT') = (tenant_id IS NOT NULL)",
            name="tenant_binding_by_account_type",
        ),
        # 호환 제약 — 플랫폼 권한의 근거가 아니다(권한은 platform_role)
        CheckConstraint(
            "account_type <> 'PLATFORM' OR role = 'ADMIN'",
            name="platform_role_is_admin",
        ),
        CheckConstraint(
            "(account_type = 'PLATFORM') = (platform_role IS NOT NULL)",
            name="platform_role_by_account_type",
        ),
    )


class AppUserTenantAssignment(Base):
    """TENANT_MANAGER 의 담당 고객사 1건(#492). 명시 배정 행만 범위가 된다 — 암묵 부여는 없다.

    소속(app_users.tenant_id) · 계정 종류는 배정이 아니다. 범위 판정은 등급이 TENANT_MANAGER 일 때만 이 행을 읽는다.
    고객사가 지워지면 배정도 함께 지워진다(CASCADE) — 없는 고객사를 가리키는 배정이 남지 않는다.
    """

    __tablename__ = "app_user_tenant_assignments"

    user_id: Mapped[str] = mapped_column(
        _ID, ForeignKey("app_users.user_id", ondelete="CASCADE"), primary_key=True
    )
    tenant_id: Mapped[str] = mapped_column(
        _ID, ForeignKey("tenants.tenant_id", ondelete="CASCADE"), primary_key=True, index=True
    )


class AuthSession(Base):
    """서버 세션 1건. 원본 토큰은 저장하지 않고 SHA-256 hex만 둔다."""

    __tablename__ = "auth_sessions"

    session_id: Mapped[str] = mapped_column(_ID, primary_key=True, default=_new_id)
    token_hash: Mapped[str] = mapped_column(CHAR(64), unique=True)
    user_id: Mapped[str] = mapped_column(
        _ID, ForeignKey("app_users.user_id", ondelete="RESTRICT"), index=True
    )
    mfa_step: Mapped[AuthSessionMfaStep] = mapped_column(
        _enum(AuthSessionMfaStep, "auth_session_mfa_step")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )
    last_activity_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )
    absolute_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    revoke_reason: Mapped[AuthSessionRevokeReason | None] = mapped_column(
        _enum(AuthSessionRevokeReason, "auth_session_revoke_reason"), nullable=True
    )

    __table_args__ = (
        CheckConstraint(
            "(revoked_at IS NULL) = (revoke_reason IS NULL)", name="revoke_shape"
        ),
        CheckConstraint("absolute_expires_at > created_at", name="absolute_after_created"),
        CheckConstraint(
            "last_activity_at >= created_at AND last_activity_at <= absolute_expires_at",
            name="activity_window",
        ),
    )


class UserAdminEvent(Base):
    """사용자 관리 이력 — append-only(DB 트리거가 UPDATE·DELETE를 거절한다).

    상태 JSON은 저장소가 schemas.api.users.UserEventState로 검증한 값만 들어간다.
    """

    __tablename__ = "user_admin_events"

    event_id: Mapped[str] = mapped_column(_ID, primary_key=True, default=_new_id)
    actor_user_id: Mapped[str] = mapped_column(
        _ID, ForeignKey("app_users.user_id", ondelete="RESTRICT"), index=True
    )
    target_user_id: Mapped[str] = mapped_column(
        _ID, ForeignKey("app_users.user_id", ondelete="RESTRICT"), index=True
    )
    action: Mapped[UserEventAction] = mapped_column(
        _enum(UserEventAction, "user_event_action")
    )
    before_state: Mapped[dict] = mapped_column(JSONB)
    after_state: Mapped[dict] = mapped_column(JSONB)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )


class AuditEvent(Base):
    """조치 승인 · 종료 판단 감사 기록 — append-only(DB 트리거가 UPDATE·DELETE를 거절한다). (T-2 · #544)

    공개 계약은 schemas.api.audit 다. 실행 행 · 종료 판단과 **같은 트랜잭션**에서 한 행씩 남고
    (db/repositories/audit.py), 재요청(200 재생) · 거절은 행을 만들지 않는다.
    행위자 종류는 쓰는 시점의 계정 종류를 옮겨 둔다 — 뒤에 계정 종류가 바뀌어도 그때 누가 했는지는
    그대로다. 이메일을 보일지는 조회가 보는 사람으로 정한다(여기에 이메일을 복사하지 않는다).
    실행의 Runbook · 대상 ARN 은 실행 행에서 읽는다(바뀌지 않는 칸이라 복사하지 않는다).
    """

    __tablename__ = "audit_events"

    event_id: Mapped[str] = mapped_column(_ID, primary_key=True, default=_new_id)
    action: Mapped[AuditEventAction] = mapped_column(
        _enum(AuditEventAction, "audit_event_action")
    )
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )
    actor_kind: Mapped[AuditActorKind] = mapped_column(
        _enum(AuditActorKind, "audit_actor_kind")
    )
    # 사람만 값이 있다(SYSTEM 은 NULL). 기록이 남은 사용자는 지울 수 없다
    actor_user_id: Mapped[str | None] = mapped_column(
        _ID, ForeignKey("app_users.user_id", ondelete="RESTRICT"), nullable=True, index=True
    )
    incident_id: Mapped[str] = mapped_column(
        _ID, ForeignKey("incidents.incident_id"), index=True
    )
    # 인시던트의 테넌트를 옮겨 싣는다 — 조회 범위가 이 칸을 본다
    tenant_id: Mapped[str | None] = _tenant_fk()
    # ACTION_EXECUTION_STARTED 만 값이 있다. 실행 하나에 시작 기록은 하나다
    execution_id: Mapped[str | None] = mapped_column(
        _ID, ForeignKey("action_executions.execution_id"), nullable=True, unique=True
    )
    trigger_source: Mapped[TriggerSource | None] = mapped_column(
        _enum(TriggerSource, "trigger_source"), nullable=True
    )
    # INCIDENT_RESOLVED 만 값이 있다
    resolution: Mapped[ResolutionJudgement | None] = mapped_column(
        _enum(ResolutionJudgement, "resolution_judgement"), nullable=True
    )

    __table_args__ = (
        CheckConstraint(
            "(action = 'ACTION_EXECUTION_STARTED' AND execution_id IS NOT NULL"
            " AND trigger_source IS NOT NULL AND resolution IS NULL)"
            " OR (action = 'INCIDENT_RESOLVED' AND resolution IS NOT NULL"
            " AND execution_id IS NULL AND trigger_source IS NULL)",
            name="action_shape",
        ),
        CheckConstraint(
            "(actor_kind = 'SYSTEM') = (actor_user_id IS NULL)", name="actor_shape"
        ),
        # 사람 승인 실행만 사람이 시작하고, 자동 발동은 SYSTEM 이 시작한다. 종료 판단은 사람만 한다
        CheckConstraint(
            "CASE action WHEN 'ACTION_EXECUTION_STARTED'"
            " THEN (trigger_source = 'USER_APPROVAL') = (actor_kind <> 'SYSTEM')"
            " ELSE actor_kind <> 'SYSTEM' END",
            name="actor_matches_trigger",
        ),
        # 목록은 최신순 · 같은 시각은 event_id 순이다(api/audit.py)
        Index("ix_audit_events_occurred_at_event_id", "occurred_at", "event_id"),
    )


class SignupRequest(Base):
    """익명 도입 신청. 키는 접수 재시도용이며 이메일 소유·남용 방지의 근거가 아니다."""

    __tablename__ = "signup_requests"

    request_id: Mapped[str] = mapped_column(_ID, primary_key=True, default=_new_id)
    idempotency_key: Mapped[str] = mapped_column(_ID)
    company_name: Mapped[str] = mapped_column(String(100))
    contact_name: Mapped[str] = mapped_column(String(100))
    email: Mapped[str] = mapped_column(String(254))
    phone: Mapped[str | None] = mapped_column(String(100), nullable=True)
    note: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    status: Mapped[SignupRequestStatus] = mapped_column(
        _enum(SignupRequestStatus, "signup_request_status"), default=SignupRequestStatus.NEW
    )
    rejected_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_signup_requests_idempotency_key"),
        CheckConstraint(
            "(status = 'REJECTED' AND rejected_reason IS NOT NULL "
            "AND length(btrim(rejected_reason)) > 0) OR "
            "(status <> 'REJECTED' AND rejected_reason IS NULL)",
            name="rejection_reason_by_status",
        ),
        Index("ix_signup_requests_created_at_request_id", "created_at", "request_id"),
    )


class Inquiry(Base):
    """고객 문의. 작성자 표시 정보는 조회 시 현재 사용자·고객사와 조인한다."""

    __tablename__ = "inquiries"

    inquiry_id: Mapped[str] = mapped_column(_ID, primary_key=True, default=_new_id)
    tenant_id: Mapped[str] = mapped_column(
        _ID, ForeignKey("tenants.tenant_id", ondelete="RESTRICT"), index=True
    )
    author_user_id: Mapped[str] = mapped_column(
        _ID, ForeignKey("app_users.user_id", ondelete="RESTRICT"), index=True
    )
    category: Mapped[InquiryCategory] = mapped_column(_enum(InquiryCategory, "inquiry_category"))
    title: Mapped[str] = mapped_column(String(100))
    body: Mapped[str] = mapped_column(String(1000))
    occurred_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    page_path: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    status: Mapped[InquiryStatus] = mapped_column(
        _enum(InquiryStatus, "inquiry_status"), default=InquiryStatus.OPEN
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    __table_args__ = (
        Index("ix_inquiries_created_at_inquiry_id", "created_at", "inquiry_id"),
    )


class InquiryEvent(Base):
    """문의 처리 기록 — migration의 트리거가 일반 UPDATE·DELETE를 거절한다."""

    __tablename__ = "inquiry_events"

    event_id: Mapped[str] = mapped_column(_ID, primary_key=True, default=_new_id)
    inquiry_id: Mapped[str] = mapped_column(
        _ID, ForeignKey("inquiries.inquiry_id", ondelete="RESTRICT"), index=True
    )
    actor_user_id: Mapped[str] = mapped_column(
        _ID, ForeignKey("app_users.user_id", ondelete="RESTRICT"), index=True
    )
    action: Mapped[InquiryEventAction] = mapped_column(
        _enum(InquiryEventAction, "inquiry_event_action")
    )
    before_status: Mapped[InquiryStatus | None] = mapped_column(
        _enum(InquiryStatus, "inquiry_status"), nullable=True
    )
    after_status: Mapped[InquiryStatus | None] = mapped_column(
        _enum(InquiryStatus, "inquiry_status"), nullable=True
    )
    note: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class OperatorNotification(Base):
    """#552 내부 수신함. 기존 사건 발송 outbox(NotificationEvent)와 별개다."""

    __tablename__ = "notifications"

    notification_id: Mapped[str] = mapped_column(_ID, primary_key=True, default=_new_id)
    kind: Mapped[NotificationKind] = mapped_column(
        _enum(NotificationKind, "operator_notification_kind")
    )
    target_id: Mapped[str] = mapped_column(_ID)
    tenant_id: Mapped[str | None] = mapped_column(
        _ID, ForeignKey("tenants.tenant_id", ondelete="RESTRICT"), nullable=True, index=True
    )
    subject: Mapped[str] = mapped_column(String(100))
    summary: Mapped[str] = mapped_column(String(100))
    inquiry_category: Mapped[InquiryCategory | None] = mapped_column(
        _enum(InquiryCategory, "inquiry_category"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    __table_args__ = (
        UniqueConstraint("kind", "target_id"),
        CheckConstraint(
            "(kind = 'SIGNUP_REQUEST' AND tenant_id IS NULL AND inquiry_category IS NULL)"
            " OR (kind = 'INQUIRY' AND tenant_id IS NOT NULL AND inquiry_category IS NOT NULL)",
            name="notification_target_shape",
        ),
        Index("ix_notifications_created_at_notification_id", "created_at", "notification_id"),
    )


class OperatorNotificationRead(Base):
    __tablename__ = "notification_reads"

    notification_id: Mapped[str] = mapped_column(
        _ID, ForeignKey("notifications.notification_id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[str] = mapped_column(
        _ID, ForeignKey("app_users.user_id", ondelete="CASCADE"), primary_key=True
    )
    read_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class TotpRecoveryOperation(Base):
    """TOTP 분실 복구 작업 1건 (#515). operation_id 는 호출자가 고르고, 같은 값의 재요청은 이 행을 돌려준다
    (제공자를 다시 부르지 않는다). 행은 차단(세대 증가 · 이메일 증명 제거 · 세션 폐기)과 같은 commit 에 생긴다.

    시각은 기록용이다 — 판정(잠금 · 재개)에 쓰지 않는다.
    """

    __tablename__ = "totp_recovery_operations"

    operation_id: Mapped[str] = mapped_column(_ID, primary_key=True)
    target_user_id: Mapped[str] = mapped_column(
        _ID, ForeignKey("app_users.user_id", ondelete="RESTRICT")
    )
    # 앱 계정 없는 운영 CLI 행위자는 NULL 이다
    actor_user_id: Mapped[str | None] = mapped_column(
        _ID, ForeignKey("app_users.user_id", ondelete="RESTRICT"), nullable=True
    )
    # 행위자가 외부 절차로 사람의 신원을 확인했다는 진술 — 증명이 아니다. 진술 없는 작업은 DB 가 거절한다.
    identity_verified: Mapped[bool] = mapped_column(Boolean)
    # 이 작업의 차단이 만든 대상의 인증 세대
    generation: Mapped[int] = mapped_column(Integer)
    status: Mapped[TotpRecoveryStatus] = mapped_column(
        _enum(TotpRecoveryStatus, "totp_recovery_status")
    )
    # 차단 commit 이 폐기한 대상 세션 수 — 재요청에도 처음 값을 돌려준다
    revoked_session_count: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finalized_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        UniqueConstraint("target_user_id", "generation"),
        CheckConstraint("identity_verified", name="identity_verified"),
        CheckConstraint("generation > 0", name="generation_positive"),
        CheckConstraint("revoked_session_count >= 0", name="revoked_count_nonnegative"),
        CheckConstraint(
            "(status = 'PROVIDER_PENDING') = (finalized_at IS NULL)", name="finalized_shape"
        ),
    )


class TotpProviderMutation(Base):
    """신원(issuer, sub)별 Cognito TOTP 변경 울타리 (#515). 제공자 변경 호출 전에 commit 하고, 풀리지 않은 행은
    신원당 1개뿐이다(부분 유일 인덱스) — 연결 · 작업자가 사라져도 남는다.

    풀림은 두 가지뿐이다: 작업자가 확정 응답을 기록(PROVIDER_RETURNED)하거나, 운영자가 외부 확인을
    진술(OPERATOR_RECONCILED)한다. 시간 임대 · 자동 만료가 없다. 시각은 기록용이다.
    """

    __tablename__ = "totp_provider_mutations"

    mutation_id: Mapped[str] = mapped_column(_ID, primary_key=True)
    issuer: Mapped[str] = mapped_column(String)
    cognito_sub: Mapped[str] = mapped_column(String)
    kind: Mapped[TotpProviderMutationKind] = mapped_column(
        _enum(TotpProviderMutationKind, "totp_provider_mutation_kind")
    )
    # RESET 이면 그 복구 작업 — 같은 commit 에 생긴다
    operation_id: Mapped[str | None] = mapped_column(
        _ID,
        ForeignKey("totp_recovery_operations.operation_id", ondelete="RESTRICT"),
        nullable=True,
        unique=True,
    )
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    resolution: Mapped[TotpProviderMutationResolution | None] = mapped_column(
        _enum(TotpProviderMutationResolution, "totp_provider_mutation_resolution"), nullable=True
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # 정리를 진술한 행위자 — 앱 계정 없는 운영 CLI 행위자는 NULL 이다
    reconciled_by: Mapped[str | None] = mapped_column(
        _ID, ForeignKey("app_users.user_id", ondelete="RESTRICT"), nullable=True
    )

    __table_args__ = (
        Index(
            "uq_totp_provider_mutations_unresolved_identity",
            "issuer",
            "cognito_sub",
            unique=True,
            postgresql_where=text("resolution IS NULL"),
        ),
        CheckConstraint("(kind = 'RESET') = (operation_id IS NOT NULL)", name="operation_by_kind"),
        CheckConstraint("(resolution IS NULL) = (resolved_at IS NULL)", name="resolution_shape"),
        # NULL 안전 비교 — 풀리지 않은(NULL) 울타리에 정리 행위자가 붙지 않게 한다
        CheckConstraint(
            "reconciled_by IS NULL OR resolution IS NOT DISTINCT FROM 'OPERATOR_RECONCILED'",
            name="reconciler_shape",
        ),
    )
