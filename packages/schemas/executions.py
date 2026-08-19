# ==============================================================================
# [파일 설명]  담당: 안성일 (AI/Guardrail · Architect)
# 실행 단계(ExecutionStep)의 상태·적용 결과 내부 계약입니다. (Issue #55)
# Executor가 단계별 typed 결과를 반환하면 Action Workflow가 저장하고, 부분 적용·
# 상태 불명(PARTIAL·UNKNOWN)은 원본 Execution의 Rollback 판단으로 전달된다.
#
# 계약 원칙
#   - status↔effect 짝 고정: IN_PROGRESS→effect 없음, SUCCESS→APPLIED|NOT_APPLIED,
#     FAILED→NOT_APPLIED|PARTIAL|UNKNOWN. 그 외 조합은 계약 위반.
#   - step_type 값 어휘·Runbook별 결과 모델은 세부 계약 확정 후 추가한다.
#   - Execution 자체의 상태 6종은 공개 계약(api/actions.py ExecutionStatus)이 원천.
# ==============================================================================

from __future__ import annotations

from enum import Enum, unique
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .api.actions import ExecutionStatus
from .api.assets import UtcDateTime


# ExecutionStatus(공개 계약 api/actions.py)에서 파생한 진행/종료 집합.
# Candidate 예약 검사·Dispatcher 회수 인덱스·Incident 전이 조건이 모두 이 집합을
# 사용하고 각 모듈에서 상태를 다시 열거하지 않는다.
EXECUTION_NON_TERMINAL_STATUSES: frozenset[ExecutionStatus] = frozenset(
    {ExecutionStatus.IN_PROGRESS, ExecutionStatus.ROLLBACK_INITIATED}
)

EXECUTION_TERMINAL_STATUSES: frozenset[ExecutionStatus] = frozenset(
    {
        ExecutionStatus.SUCCESS,
        ExecutionStatus.FAILED,
        ExecutionStatus.ROLLED_BACK,
        ExecutionStatus.ROLLBACK_FAILED,
    }
)


@unique
class ExecutionStepStatus(str, Enum):
    IN_PROGRESS = "IN_PROGRESS"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"


@unique
class ExecutionEffect(str, Enum):
    """실행 단계가 실제 AWS 상태에 미친 결과."""

    NOT_APPLIED = "NOT_APPLIED"
    APPLIED = "APPLIED"
    PARTIAL = "PARTIAL"
    UNKNOWN = "UNKNOWN"


_ALLOWED_EFFECTS_BY_STATUS: dict[ExecutionStepStatus, frozenset[ExecutionEffect]] = {
    ExecutionStepStatus.SUCCESS: frozenset(
        {ExecutionEffect.APPLIED, ExecutionEffect.NOT_APPLIED}
    ),
    ExecutionStepStatus.FAILED: frozenset(
        {ExecutionEffect.NOT_APPLIED, ExecutionEffect.PARTIAL, ExecutionEffect.UNKNOWN}
    ),
}


class ExecutionStepResult(BaseModel):
    """실행 1단계의 typed 결과 — Executor 반환값이자 ExecutionStep 저장 계약."""

    model_config = ConfigDict(extra="forbid")

    sequence: int = Field(ge=1)
    affected_arn: str = Field(min_length=1)
    # 단계 유형 값 어휘는 Runbook별 세부 계약 확정 시 Enum으로 교체한다
    step_type: str = Field(min_length=1)
    aws_operation: str = Field(min_length=1)
    status: ExecutionStepStatus
    effect: Optional[ExecutionEffect] = None
    aws_request_id: Optional[str] = Field(None, min_length=1)
    result_summary: Optional[str] = Field(None, min_length=1)
    error_summary: Optional[str] = Field(None, min_length=1)
    occurred_at: UtcDateTime

    @model_validator(mode="after")
    def _effect_matches_status(self):
        if self.status == ExecutionStepStatus.IN_PROGRESS:
            if self.effect is not None:
                raise ValueError("IN_PROGRESS 단계의 effect는 null이어야 합니다")
            return self
        allowed = _ALLOWED_EFFECTS_BY_STATUS[self.status]
        if self.effect not in allowed:
            raise ValueError(
                f"{self.status.value} 단계의 effect는 "
                f"{'|'.join(sorted(e.value for e in allowed))}만 가능합니다"
            )
        return self
