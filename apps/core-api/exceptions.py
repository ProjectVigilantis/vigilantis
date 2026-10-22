# ==============================================================================
# [파일 설명]
# REST 공통 오류 봉투 처리기 — 모든 오류 응답을 확정 계약
# {"error": {code, message, request_id}}(schemas.api.errors)로 통일한다. (Issue #68)
#
#   - 내부 예외·ORM 원본·스택은 응답에 노출하지 않는다(스택은 로그로만).
#   - 409 2종(IDEMPOTENCY_KEY_CONFLICT·PROPOSAL_NOT_EXECUTABLE)의 발생 경로는
#     조치 실행 API 작업에서 붙는다 — 여기서는 공통 봉투 변환만 둔다.
# ==============================================================================

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from schemas.api.errors import ErrorCode, ErrorDetail, ErrorResponse

from logging_config import request_id_var

logger = logging.getLogger("vigilantis.http")

# 코드 → HTTP 상태·표시 문구. Router는 코드만 던지고 문자열을 직접 만들지 않는다
_STATUS_BY_CODE: dict[ErrorCode, int] = {
    ErrorCode.INCIDENT_NOT_FOUND: 404,
    ErrorCode.IDEMPOTENCY_KEY_CONFLICT: 409,
    ErrorCode.PROPOSAL_NOT_EXECUTABLE: 409,
    ErrorCode.INCIDENT_NOT_RESOLVABLE: 409,
    ErrorCode.TENANT_NOT_FOUND: 404,
    ErrorCode.TENANT_STATE_CONFLICT: 409,
    ErrorCode.TENANT_SUSPENDED: 409,
    ErrorCode.AUTH_REQUIRED: 401,
    ErrorCode.AUTH_FAILED: 401,
    ErrorCode.AUTH_CODE_INVALID: 401,
    ErrorCode.AUTH_FLOW_EXPIRED: 401,
    ErrorCode.AUTH_STEP_CONFLICT: 409,
    ErrorCode.AUTH_PASSWORD_POLICY: 422,
    ErrorCode.AUTH_ORIGIN_REJECTED: 403,
    ErrorCode.AUTH_FORBIDDEN: 403,
    ErrorCode.ACCOUNT_NOT_PROVISIONED: 403,
    ErrorCode.ACCOUNT_DISABLED: 403,
    ErrorCode.AUTH_EMAIL_DELIVERY_FAILED: 503,
    ErrorCode.AUTH_RATE_LIMITED: 429,
    ErrorCode.AUTH_PROVIDER_UNAVAILABLE: 503,
    ErrorCode.USER_NOT_FOUND: 404,
    ErrorCode.USER_STATE_CONFLICT: 409,
    ErrorCode.REQUEST_VALIDATION_FAILED: 422,
    ErrorCode.INTERNAL_ERROR: 500,
}
_MESSAGE_BY_CODE: dict[ErrorCode, str] = {
    ErrorCode.INCIDENT_NOT_FOUND: "인시던트를 찾을 수 없습니다.",
    ErrorCode.IDEMPOTENCY_KEY_CONFLICT: "같은 멱등성 키의 실행이 이미 있습니다.",
    ErrorCode.PROPOSAL_NOT_EXECUTABLE: "실행 가능한 상태의 제안이 아닙니다.",
    ErrorCode.INCIDENT_NOT_RESOLVABLE: "종료 처리할 수 있는 상태의 인시던트가 아닙니다.",
    ErrorCode.TENANT_NOT_FOUND: "고객(테넌트)을 찾을 수 없습니다.",
    ErrorCode.TENANT_STATE_CONFLICT: "지금 고객 상태에서는 할 수 없는 요청입니다.",
    ErrorCode.TENANT_SUSPENDED: "정지된 고객의 자산에는 조치를 실행하지 않습니다.",
    ErrorCode.AUTH_REQUIRED: "로그인이 필요합니다.",
    ErrorCode.AUTH_FAILED: "이메일 또는 비밀번호가 올바르지 않습니다.",
    ErrorCode.AUTH_CODE_INVALID: "인증 코드가 올바르지 않습니다.",
    ErrorCode.AUTH_FLOW_EXPIRED: "로그인 진행이 만료되었습니다. 처음부터 다시 로그인하세요.",
    ErrorCode.AUTH_STEP_CONFLICT: "지금 단계에서 할 수 없는 요청입니다.",
    ErrorCode.AUTH_PASSWORD_POLICY: "새 비밀번호가 비밀번호 정책을 만족하지 않습니다.",
    ErrorCode.AUTH_ORIGIN_REJECTED: "허용되지 않은 출처의 요청입니다.",
    ErrorCode.AUTH_FORBIDDEN: "이 작업을 할 권한이 없습니다.",
    ErrorCode.ACCOUNT_NOT_PROVISIONED: "등록되지 않은 계정입니다. 관리자에게 문의하세요.",
    ErrorCode.ACCOUNT_DISABLED: "비활성화된 계정입니다.",
    ErrorCode.AUTH_EMAIL_DELIVERY_FAILED: "인증 이메일을 보내지 못했습니다.",
    ErrorCode.AUTH_RATE_LIMITED: "요청이 너무 많습니다. 잠시 후 다시 시도하세요.",
    ErrorCode.AUTH_PROVIDER_UNAVAILABLE: "인증 서비스를 사용할 수 없습니다.",
    ErrorCode.USER_NOT_FOUND: "사용자를 찾을 수 없습니다.",
    ErrorCode.USER_STATE_CONFLICT: "현재 사용자 상태에서는 처리할 수 없습니다. 마지막 활성 관리자와 중복 초대를 확인하세요.",
    ErrorCode.REQUEST_VALIDATION_FAILED: "요청 값이 계약과 다릅니다.",
    ErrorCode.INTERNAL_ERROR: "서버 내부 오류입니다.",
}


class ApiError(Exception):
    """라우터가 던지는 계약 오류 — 코드만 지정하면 상태·문구는 여기 매핑이 정한다."""

    def __init__(self, code: ErrorCode) -> None:
        super().__init__(code.value)
        self.code = code


def _request_id(request: Request) -> str:
    rid = getattr(request.state, "request_id", None) or request_id_var.get()
    return rid or "-"


def _envelope(
    request: Request, status_code: int, code: ErrorCode, message: str
) -> JSONResponse:
    body = ErrorResponse(
        error=ErrorDetail(code=code, message=message, request_id=_request_id(request))
    )
    return JSONResponse(status_code=status_code, content=body.model_dump(mode="json"))


def api_error_response(request: Request, code: ErrorCode) -> JSONResponse:
    """계약 오류 코드 → 상태·문구가 매핑된 봉투 응답."""
    return _envelope(request, _STATUS_BY_CODE[code], code, _MESSAGE_BY_CODE[code])


def unexpected_error_response(request: Request) -> JSONResponse:
    """처리되지 않은 예외 → 500 봉투. except 블록 안에서 불러야 스택이 로그에 남는다."""
    logger.exception("unhandled_exception")
    return _envelope(
        request, 500, ErrorCode.INTERNAL_ERROR, _MESSAGE_BY_CODE[ErrorCode.INTERNAL_ERROR]
    )


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def handle_api_error(request: Request, exc: ApiError) -> JSONResponse:
        return api_error_response(request, exc.code)

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        # 필드 위치만 담는다 — 입력 값은 본문·자격증명일 수 있어 응답·로그에 싣지 않는다
        locations = sorted(
            {".".join(str(part) for part in err.get("loc", ())) for err in exc.errors()}
        )
        message = _MESSAGE_BY_CODE[ErrorCode.REQUEST_VALIDATION_FAILED]
        if locations:
            message = f"{message} 위치: {', '.join(locations)}"
        return _envelope(request, 422, ErrorCode.REQUEST_VALIDATION_FAILED, message)
