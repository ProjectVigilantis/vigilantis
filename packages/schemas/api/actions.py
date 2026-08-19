# ==============================================================================
# [파일 설명]  담당: 안성일 (AI/Guardrail · Architect)
# POST /api/v1/actions/execute 요청·응답 DTO입니다. Dashboard(FE)와의 공개 계약이며,
# 자유 형식 실행 명령이 아니라 "저장된 제안·복구 조치에 대한 실행 요청"만 표현합니다.
# (확정 설계 4.4 + PROJECT_STATUS API 계약)
#
# 계약 원칙
#   - 요청은 SSOT 3필드만: incident_id · runbook_id · idempotency_key.
#     Target ARN·AWS 실행 파라미터는 받지 않는다 — 서버가 저장된 Guardrail PASS
#     제안(관제자 Rollback은 원본 Execution + DB backup_record_id)으로 명령을 재구성.
#   - 추가 필드는 거부한다(extra=forbid → 422 REQUEST_VALIDATION_FAILED).
#   - runbook_id는 schemas/runbooks.py 확정 10종 원천(RunbookId)으로만 검증한다.
#     목록 복사 금지. 등록 ID여도 현재 실행 가능한 제안·복구 조치가 아니면
#     실행부가 409 PROPOSAL_NOT_EXECUTABLE로 거절한다.
#   - 실행 상태 6종 = SSOT 4종 + 복구 최종 결과 2종(ROLLED_BACK·ROLLBACK_FAILED).
#     +2종은 원본 Execution에만 기록하며(Rollback 자식은 IN_PROGRESS→SUCCESS|FAILED),
#     FE 합의 대상 확장으로 이 PR에서 SSOT 표와 함께 확정한다.
# ==============================================================================

from __future__ import annotations

from enum import Enum, unique

from pydantic import BaseModel, ConfigDict, Field

from ..runbooks import RunbookId
from .assets import UtcDateTime


@unique
class ExecutionStatus(str, Enum):
    """실행 상태 6종 = SSOT 4종 + 복구 최종 결과 2종(FE 합의 확장)."""

    IN_PROGRESS = "IN_PROGRESS"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    ROLLBACK_INITIATED = "ROLLBACK_INITIATED"
    ROLLED_BACK = "ROLLED_BACK"
    ROLLBACK_FAILED = "ROLLBACK_FAILED"


class ExecuteActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    incident_id: str = Field(min_length=1)
    runbook_id: RunbookId
    idempotency_key: str = Field(min_length=1)


class ExecuteActionResponse(BaseModel):
    """202 Accepted(신규 예약) / 200 OK(같은 Key 재요청) 공통 응답 본문."""

    model_config = ConfigDict(extra="forbid")

    execution_id: str = Field(min_length=1)
    status: ExecutionStatus
    updated_at: UtcDateTime
