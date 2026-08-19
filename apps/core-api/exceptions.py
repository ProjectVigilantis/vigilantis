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
    ErrorCode.REQUEST_VALIDATION_FAILED: 422,
    ErrorCode.INTERNAL_ERROR: 500,
}
_MESSAGE_BY_CODE: dict[ErrorCode, str] = {
    ErrorCode.INCIDENT_NOT_FOUND: "인시던트를 찾을 수 없습니다.",
    ErrorCode.IDEMPOTENCY_KEY_CONFLICT: "같은 멱등성 키의 실행이 이미 있습니다.",
    ErrorCode.PROPOSAL_NOT_EXECUTABLE: "실행 가능한 상태의 제안이 아닙니다.",
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


def unexpected_error_response(request: Request) -> JSONResponse:
    """처리되지 않은 예외 → 500 봉투. except 블록 안에서 불러야 스택이 로그에 남는다."""
    logger.exception("unhandled_exception")
    return _envelope(
        request, 500, ErrorCode.INTERNAL_ERROR, _MESSAGE_BY_CODE[ErrorCode.INTERNAL_ERROR]
    )


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def handle_api_error(request: Request, exc: ApiError) -> JSONResponse:
        return _envelope(
            request, _STATUS_BY_CODE[exc.code], exc.code, _MESSAGE_BY_CODE[exc.code]
        )

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
