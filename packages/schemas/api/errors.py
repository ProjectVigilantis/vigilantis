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
    """REST 공통 오류 코드 24종. 괄호는 계약상 HTTP 상태.

    TENANT_* 3종은 /api/v1/tenants(Post-MVP T-4)와 테넌트 정지(원격 회수)의 거절이다.
    AUTH_* · ACCOUNT_* 13종은 로그인(/api/v1/auth)과 API 접근 보호(Post-MVP T-1)의 거절이다.
    미인증은 401, 인증은 됐지만 허용되지 않은 요청은 403 이다.
    """

    INCIDENT_NOT_FOUND = "INCIDENT_NOT_FOUND"                # 404 — 없음 · 요청의 테넌트 범위 밖(존재를 알리지 않는다 · api/tenants.py 머리말)
    IDEMPOTENCY_KEY_CONFLICT = "IDEMPOTENCY_KEY_CONFLICT"    # 409
    PROPOSAL_NOT_EXECUTABLE = "PROPOSAL_NOT_EXECUTABLE"      # 409
    INCIDENT_NOT_RESOLVABLE = "INCIDENT_NOT_RESOLVABLE"      # 409
    TENANT_NOT_FOUND = "TENANT_NOT_FOUND"                    # 404
    TENANT_STATE_CONFLICT = "TENANT_STATE_CONFLICT"          # 409 — 지금 상태에서 할 수 없는 전이(이미 정지된 테넌트 정지 등)
    TENANT_SUSPENDED = "TENANT_SUSPENDED"                    # 409 — 정지된 테넌트에 대한 새 실행 · 종료 요청(플랫폼 운영자의 종료는 받는다 · api/tenants.py 머리말)
    AUTH_REQUIRED = "AUTH_REQUIRED"                          # 401 — 세션 없음 · 만료 · 회수
    AUTH_FAILED = "AUTH_FAILED"                              # 401 — 이메일 · 비밀번호 불일치(사용자 존재 여부를 가리지 않는다)
    AUTH_CODE_INVALID = "AUTH_CODE_INVALID"                  # 401 — TOTP · 이메일 · 재설정 코드 불일치
    AUTH_FLOW_EXPIRED = "AUTH_FLOW_EXPIRED"                  # 401 — 로그인 진행이 없음 · 만료 · 시도 초과. 처음부터 다시 로그인
    AUTH_STEP_CONFLICT = "AUTH_STEP_CONFLICT"                # 409 — 지금 단계가 아닌 단계 API 호출
    AUTH_PASSWORD_POLICY = "AUTH_PASSWORD_POLICY"            # 422 — 새 비밀번호가 앱 또는 User Pool 정책 위반
    AUTH_ORIGIN_REJECTED = "AUTH_ORIGIN_REJECTED"            # 403 — 허용 Origin 이 아닌 상태 변경 요청
    AUTH_FORBIDDEN = "AUTH_FORBIDDEN"                        # 403 — 인증됐지만 역할이 허용하지 않음
    ACCOUNT_NOT_PROVISIONED = "ACCOUNT_NOT_PROVISIONED"      # 403 — Cognito 인증은 됐지만 앱 사용자가 없다
    ACCOUNT_DISABLED = "ACCOUNT_DISABLED"                    # 403 — 앱 사용자 비활성
    AUTH_EMAIL_DELIVERY_FAILED = "AUTH_EMAIL_DELIVERY_FAILED"  # 503 — 이메일 검증 코드 발송 실패
    AUTH_RATE_LIMITED = "AUTH_RATE_LIMITED"                  # 429 — Cognito 요청 · 실패 횟수 제한
    AUTH_PROVIDER_UNAVAILABLE = "AUTH_PROVIDER_UNAVAILABLE"  # 503 — Cognito 호출 실패(네트워크 · 5xx)
    USER_NOT_FOUND = "USER_NOT_FOUND"                        # 404 — ADMIN 사용자 관리 대상 없음
    USER_STATE_CONFLICT = "USER_STATE_CONFLICT"              # 409 — 마지막 ADMIN · 중복 초대 · 상태 충돌
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
