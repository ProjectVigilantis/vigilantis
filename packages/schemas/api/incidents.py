# ==============================================================================
# [파일 설명]  담당: 안성일 (AI/Guardrail · Architect)
# GET /api/v1/incidents/{id}(상세)·GET /api/v1/incidents(목록) 외부 응답 DTO입니다.
# Dashboard(FE)와의 공개 계약입니다. (확정 설계 4.3 + PROJECT_STATUS API 계약)
#
# 계약 원칙
#   - title은 SECOPS 필수·FINOPS nullable — 카드 제목이 곧 위협 이름이라 SECOPS는
#     비면 제목이 자원 ID가 된다(Issue #200). 위협 이름은 만드는 시점에 이미 정해져
#     있어 AI를 기다리지 않는다. FINOPS는 진단명이라 분석 전 null이며, 그 경우 FE가
#     category+대상 ARN 축약으로 표시한다(Issue #45 코멘트).
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
#   - resolution·resolved_at은 관제자가 종료 처리하며 남긴 판단이다. status가
#     RESOLVED인 것과 동시에 채워지고, 그 전에는 둘 다 null이다 — 상태만 옮기고
#     판단을 빠뜨리면 왜 종료됐는지 남지 않는다. 관제자 복구 접수로 재개되면
#     (ADR-0004) 다시 null이 된다 — "지금 이 인시던트가 종료된 이유"를 말하는
#     값이라 재개된 뒤에는 거짓이 되기 때문이다. 목록에는 넣지 않는다(부분집합
#     10필드 유지). (Issue #199)
#   - executions의 verification_hold는 판정 불가 보류 기록이다 — AWS에 물어보지 못해
#     실행 결과를 확정하지 못했을 때의 사유 코드·횟수·시각. status가 UNVERIFIED면
#     반드시 있고, 판정이 내려진 SUCCESS·ROLLBACK_INITIATED에는 오지 않는다. (Issue #249)
# ==============================================================================

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum, unique
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..runbooks import AI_RECOMMENDABLE_RUNBOOK_IDS, ROLLBACK_RUNBOOK_IDS, RunbookId
from ..savings import AISavingsEstimate, validate_candidate_savings
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
    # 조치가 끝났고 관제자 종료 판단만 남음 — 남은 제안·진행 중 실행이 없고, 마지막
    # 으로 확정된 실행이 SUCCESS·ROLLED_BACK인 자리다. RESOLVED로 시스템이 먼저
    # 옮기지 않는 이유는 그러면 관제자 종료 API가 멱등 경로로 떨어져 종료 판단이
    # 영구히 비어 남기 때문이고(Issue #199), FAILED로 두지 않는 이유는 성공한 조치가
    # 화면에서 '진행 불가'로 읽히기 때문이다. (Issue #240)
    AWAITING_CLOSURE = "AWAITING_CLOSURE"
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


@unique
class ResolutionJudgement(str, Enum):
    """종료 처리 시 관제자가 남기는 판단 (2026-08-27 회의 결정 ④).

    JUSTIFIED 1종이다. 모달의 다른 선택지 `과잉이었다`는 종료 값이 아니라 해제
    실행으로 넘어가는 트리거라(#196 §D — 해제는 실행이라 결과를 보고 종료를
    판단한다) 이 API에 도달하지 않으며, 과잉 판단의 기록은 해제 실행 레코드가
    남긴다. 해제를 마친 뒤의 종료도 JUSTIFIED다 — 그 시점의 대응 상태(해제 완료)를
    정당하다고 보고 닫는 것이기 때문이다.
    """

    JUSTIFIED = "JUSTIFIED"    # 수행된 대응이 정당했다


class ResolveIncidentRequest(BaseModel):
    """POST /api/v1/incidents/{incident_id}/resolve 요청 본문.

    Idempotency Key를 받지 않는다 — 종료는 AWS를 바꾸지 않고 Incident 상태 하나만
    옮기므로 조건부 갱신 자체가 멱등이다. 이미 종료된 건의 재요청은 처음 저장된
    판단을 그대로 돌려준다(schemas/api/actions.py의 실행 접수와 다른 점).
    """

    model_config = ConfigDict(extra="forbid")

    resolution: ResolutionJudgement


class RecommendationItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    runbook_id: RunbookId
    target_arn: str = Field(min_length=1)
    display_parameters: dict[str, str] = Field(default_factory=dict)
    ai_savings_estimate: AISavingsEstimate | None = Field(
        default=None,
        description=(
            "RIGHTSIZING 조치의 AI 참고 추정. USD/월 730시간의 Linux 공유 온디맨드 "
            "컴퓨팅 비용만 비교하며 실제 청구액이 아니다. ESTIMATED만 금액·근거가 있고 "
            "UNAVAILABLE/INVALID는 amount=null이다. 기존 후보·비대상 조치는 필드가 null이다. "
            "단가는 AI 추정, 금액은 서버 계산이며 설명의 작성 주체는 "
            "basis.explanation_source(MODEL_GENERATED/SERVER_TEMPLATE)로 구분한다."
        ),
    )

    @model_validator(mode="after")
    def _ai_recommendable_only(self):
        # ADR-0004 정책 ②: AI 추천은 본편 7종만 — 롤백 3종은 recommendations에 못 온다
        if self.runbook_id.value not in AI_RECOMMENDABLE_RUNBOOK_IDS:
            raise ValueError("recommendations에는 AI 추천 가능 Runbook(본편 7종)만 올 수 있습니다")
        validate_candidate_savings(
            self.ai_savings_estimate, self.runbook_id, self.target_arn,
            self.display_parameters.get("target_instance_type"),
        )
        return self


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


class ExecutionVerificationHold(BaseModel):
    """판정 불가 보류 기록 — AWS에 물어보지 못해 실행 결과를 확정하지 못한 이력. (Issue #249)

    읽는 법은 실행 status가 정한다.
      - IN_PROGRESS: **재시도 중**이다. 서버가 간격을 두고 다시 묻는다.
      - UNVERIFIED: 재시도를 소진해 자동 판정을 멈췄다. 자동 원복은 하지 않았고,
        자산 상태를 관제자가 확인해야 한다(관제자 복구 경로는 열려 있다).
      - FAILED: 실행 전 상태 대조에서 멈춰 자산을 만지지 않았거나, 롤백 자식이 원복
        결과를 확인하지 못한 것이다.
      - ROLLED_BACK·ROLLBACK_FAILED: 확인 불가였던 원본을 관제자 복구가 이어받은 뒤다.
    판정이 내려지면 서버가 지운다 — 그래서 SUCCESS·ROLLBACK_INITIATED에는 오지 않는다.

    reason_code의 어휘는 가드레일 ④와 같은 사유 코드 표(PRECHECK_*)다. 실행·판정이 AWS
    오류를 분류하는 표가 하나이기 때문이다(services/aws/errors.py).
    """

    model_config = ConfigDict(extra="forbid")

    reason_code: str = Field(min_length=1)
    attempts: int = Field(ge=1)
    first_failed_at: UtcDateTime
    last_failed_at: UtcDateTime

    @field_validator("reason_code")
    @classmethod
    def _known_reason_code(cls, value: str) -> str:
        # 어휘의 원천은 schemas.guardrails.PrecheckReasonCode다. 모듈 상단에서 가져오지
        # 않는 것은 guardrails가 이 패키지(api.assets)를 import해 순환 import가 되기
        # 때문이다 — 목록을 여기 다시 적는 대신 검증 시점에 원천을 부른다.
        from ..guardrails import PrecheckReasonCode

        try:
            return PrecheckReasonCode(value).value
        except ValueError:
            raise ValueError(f"알 수 없는 판정 불가 사유 코드입니다: {value}") from None

    @model_validator(mode="after")
    def _failures_in_order(self):
        if _as_utc(self.last_failed_at) < _as_utc(self.first_failed_at):
            raise ValueError("last_failed_at은 first_failed_at보다 앞설 수 없습니다")
        return self


# 판정이 내려져 보류 기록이 지워진 뒤에만 오는 상태 (Issue #249)
_HOLD_CLEARED_STATUSES: frozenset[ExecutionStatus] = frozenset(
    {ExecutionStatus.SUCCESS, ExecutionStatus.ROLLBACK_INITIATED}
)


class ExecutionSummaryItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    execution_id: str = Field(min_length=1)
    runbook_id: RunbookId
    status: ExecutionStatus
    available_recovery_runbook_ids: list[RunbookId] = Field(default_factory=list)
    # 판정 불가 보류 기록 — 없으면 null (Issue #249)
    verification_hold: Optional[ExecutionVerificationHold] = None
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

    @model_validator(mode="after")
    def _hold_matches_status(self):
        # UNVERIFIED는 "무엇을 확인하지 못했는가"가 관제자 판단의 근거라 기록 없이 오지 않는다
        if self.status is ExecutionStatus.UNVERIFIED and self.verification_hold is None:
            raise ValueError("UNVERIFIED이면 verification_hold가 있어야 합니다")
        # 판정이 내려진 상태에 보류가 붙어 있으면 성공한 실행에 경고가 그려진다
        if self.status in _HOLD_CLEARED_STATUSES and self.verification_hold is not None:
            raise ValueError(f"{self.status.value}이면 verification_hold는 null이어야 합니다")
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
    resolution: Optional[ResolutionJudgement] = None
    resolved_at: Optional[UtcDateTime] = None
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

        # SECOPS 카드 제목은 위협 이름이다 — null이면 FE fallback이 자원 ID를 제목으로 쓴다
        if self.category == IncidentCategory.SECOPS and self.title is None:
            raise ValueError("SECOPS는 title이 필수입니다(위협 이름)")

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
        if self.status == IncidentStatus.AWAITING_CLOSURE:
            # "조치가 끝났고 종료 판단만 남았다"는 세 조건이 함께여야 성립한다.
            # 수행된 조치가 없으면 판단할 것이 없고(그건 ANALYZING·FAILED 자리다),
            # 남은 제안이 있으면 아직 승인 대기이며(v1.6 결정 ⑤ — 남은 제안이 있으면
            # 종료 불가), 진행 중 실행이 있으면 조치가 끝나지 않았다
            if not self.executions:
                raise ValueError("AWAITING_CLOSURE이면 수행된 실행이 1개 이상이어야 합니다")
            if in_progress:
                raise ValueError("AWAITING_CLOSURE이면 진행 중인 실행이 없어야 합니다")
        if (
            self.status
            in (
                IncidentStatus.ANALYZING,
                IncidentStatus.AWAITING_CLOSURE,
                IncidentStatus.RESOLVED,
                IncidentStatus.FAILED,
            )
            and self.recommendations
        ):
            raise ValueError(f"{self.status.value}이면 recommendations는 빈 배열이어야 합니다")
        if self.status == IncidentStatus.ANALYZING and self.summary_lines:
            raise ValueError("ANALYZING이면 summary_lines는 빈 배열이어야 합니다")
        if self.status == IncidentStatus.RESOLVED and in_progress:
            raise ValueError("RESOLVED이면 진행 중인 실행이 없어야 합니다")

        # 종료 판단은 RESOLVED와 함께 채워진다. 한쪽만 있으면 화면이 판단 없는
        # 종료나 종료되지 않은 판단을 그리게 된다 (Issue #199)
        if (self.resolution is None) != (self.resolved_at is None):
            raise ValueError("resolution과 resolved_at은 함께 채워지거나 함께 null이어야 합니다")
        if self.status != IncidentStatus.RESOLVED and self.resolution is not None:
            raise ValueError("RESOLVED가 아니면 resolution·resolved_at은 null이어야 합니다")

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

        # 상세와 같은 불변식: SECOPS 카드 제목은 위협 이름이다
        if self.category == IncidentCategory.SECOPS and self.title is None:
            raise ValueError("SECOPS는 title이 필수입니다(위협 이름)")
        return self


class IncidentsResponse(BaseModel):
    """GET /api/v1/incidents 목록 봉투 — 페이지네이션 필드는 Post-MVP에 추가한다."""

    model_config = ConfigDict(extra="forbid")

    items: list[IncidentListItem] = Field(default_factory=list)
