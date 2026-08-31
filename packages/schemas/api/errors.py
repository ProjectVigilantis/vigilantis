# ==============================================================================
# [파일 설명]  담당: 안성일 (AI/Guardrail · Architect)
# REST 공통 오류 응답 봉투입니다. Dashboard(FE)와의 공개 계약이며, 모든 REST
# 오류를 {"error": {code, message, request_id}} 한 가지 형태로 반환합니다.
# (확정 설계 4.6 — WebSocket 이벤트에는 적용하지 않는다)
#
# 계약 원칙
#   - 상세 예외·스택·자격증명은 노출하지 않는다. 예상 밖 오류는 INTERNAL_ERROR로 요약.
#   - Execution 예약 "전" 실패만 HTTP 오류로 반환한다. 예약 이후의 실패는
#     HTTP 오류가 아니라 Execution 상태(FAILED·ROLLBACK_*)로 저장해 REST·WS로 전달.
# ==============================================================================

from __future__ import annotations

from enum import Enum, unique

from pydantic import BaseModel, ConfigDict, Field


@unique
class ErrorCode(str, Enum):
    """REST 공통 오류 코드 6종. 괄호는 계약상 HTTP 상태."""

    INCIDENT_NOT_FOUND = "INCIDENT_NOT_FOUND"                # 404
    IDEMPOTENCY_KEY_CONFLICT = "IDEMPOTENCY_KEY_CONFLICT"    # 409
    PROPOSAL_NOT_EXECUTABLE = "PROPOSAL_NOT_EXECUTABLE"      # 409
    INCIDENT_NOT_RESOLVABLE = "INCIDENT_NOT_RESOLVABLE"      # 409
    REQUEST_VALIDATION_FAILED = "REQUEST_VALIDATION_FAILED"  # 422
    INTERNAL_ERROR = "INTERNAL_ERROR"                        # 500


class ErrorDetail(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: ErrorCode
    message: str = Field(min_length=1)
    request_id: str = Field(min_length=1)


class ErrorResponse(BaseModel):
    """오류 봉투 — 최상위 키는 "error" 하나뿐이다."""

    model_config = ConfigDict(extra="forbid")

    error: ErrorDetail
