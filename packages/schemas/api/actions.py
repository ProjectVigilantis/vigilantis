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
#   - idempotency_key 상한 128자 = 저장 컬럼 폭(action_executions.idempotency_key).
#     계약에 상한이 없으면 검증을 통과한 값이 저장 시점에 깨져 500으로 샌다 (Issue #116).
#   - runbook_id는 schemas/runbooks.py 확정 10종 원천(RunbookId)으로만 검증한다.
#     목록 복사 금지. 등록 ID여도 현재 실행 가능한 제안·복구 조치가 아니면
#     실행부가 409 PROPOSAL_NOT_EXECUTABLE로 거절한다.
#   - 실행 상태 7종 = SSOT 4종 + 복구 최종 결과 2종(ROLLED_BACK·ROLLBACK_FAILED)
#     + 결과 확인 불가 1종(UNVERIFIED, Issue #249).
#     복구 최종 결과 2종은 원본 Execution에만 기록하며(Rollback 자식은 IN_PROGRESS→SUCCESS|FAILED),
#     FE 합의 대상 확장으로 이 PR에서 SSOT 표와 함께 확정한다.
#   - UNVERIFIED도 원본 전용이다. AWS에 물어보지 못해 결과를 확정하지 못한 채 재시도를
#     소진한 실행이며 자동 원복의 입력이 아니다 — 판정은 관제자에게 넘어간다.
# ==============================================================================

from __future__ import annotations

from enum import Enum, unique

from pydantic import BaseModel, ConfigDict, Field

from ..runbooks import RunbookId
from .assets import UtcDateTime


@unique
class ExecutionStatus(str, Enum):
    """실행 상태 7종 = SSOT 4종 + 복구 최종 결과 2종 + 결과 확인 불가 1종(FE 합의 확장)."""

    IN_PROGRESS = "IN_PROGRESS"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    ROLLBACK_INITIATED = "ROLLBACK_INITIATED"
    ROLLED_BACK = "ROLLED_BACK"
    ROLLBACK_FAILED = "ROLLBACK_FAILED"
    # 결과 확인 불가 — AWS 조회 실패로 종료 판정을 내리지 못한 채 재시도를 소진했다.
    # 자산이 바뀌었을 수 있지만 성공으로도 실패로도 확정하지 않고, 자동 원복하지 않고
    # 관제자 확인으로 넘긴다(종료 상태 · 관제자 복구 가능). 원본 실행 전용이다. (Issue #249)
    UNVERIFIED = "UNVERIFIED"


class ExecuteActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    incident_id: str = Field(min_length=1)
    runbook_id: RunbookId
    idempotency_key: str = Field(min_length=1, max_length=128)


class ExecuteActionResponse(BaseModel):
    """202 Accepted(신규 예약) / 200 OK(같은 Key 재요청) 공통 응답 본문."""

    model_config = ConfigDict(extra="forbid")

    execution_id: str = Field(min_length=1)
    status: ExecutionStatus
    updated_at: UtcDateTime
