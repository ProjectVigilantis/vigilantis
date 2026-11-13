"""REST 공통 오류 봉투 계약 테스트: 기존 26종 + #552 6종 + T-10 1종, 봉투 형태 고정."""

import pytest
from pydantic import ValidationError

from schemas.api.errors import ErrorCode, ErrorResponse

# 기존 계약과 #552 오류 계약. 코드가 아니라 이 리터럴 집합이 기대값이다.
CONTRACT_ERROR_CODES = {
    "INCIDENT_NOT_FOUND",         # 404
    "IDEMPOTENCY_KEY_CONFLICT",   # 409
    "PROPOSAL_NOT_EXECUTABLE",    # 409
    "TARGET_EXECUTION_IN_PROGRESS",  # 409: 같은 대상에 끝나지 않은 실행 (T-10 · #595)
    "INCIDENT_NOT_RESOLVABLE",    # 409 — 종료 처리 거절 (Issue #199)
    "TENANT_NOT_FOUND",           # 404 — Post-MVP T-4
    "TENANT_ACCOUNT_CONFLICT",    # 409 — 같은 계정의 테넌트가 이미 있다
    "TENANT_ONBOARDING_UNAVAILABLE",  # 503 — 온보딩 링크 설정 없음
    "TENANT_STATE_CONFLICT",      # 409 — 테넌트 상태 전이 거절
    "TENANT_SUSPENDED",           # 409 — 정지된 테넌트 자산에 새 실행
    "GROUP_NOT_FOUND",            # 404 — 고객사 그룹 (#572 2번)
    "GROUP_NAME_CONFLICT",        # 409 — 같은 이름의 그룹
    "AUTH_REQUIRED",              # 401 — Post-MVP T-1 (#492)
    "AUTH_FAILED",                # 401
    "AUTH_CODE_INVALID",          # 401
    "AUTH_FLOW_EXPIRED",          # 401
    "AUTH_STEP_CONFLICT",         # 409
    "AUTH_PASSWORD_POLICY",       # 422
    "AUTH_ORIGIN_REJECTED",       # 403
    "AUTH_FORBIDDEN",             # 403 — 역할 불허 (#492 PR 2)
    "ACCOUNT_NOT_PROVISIONED",    # 403
    "ACCOUNT_DISABLED",           # 403
    "AUTH_EMAIL_DELIVERY_FAILED", # 503
    "AUTH_RATE_LIMITED",          # 429
    "AUTH_PROVIDER_UNAVAILABLE",  # 503
    "USER_NOT_FOUND",             # 404 — ADMIN 사용자 관리
    "USER_STATE_CONFLICT",        # 409 — 마지막 ADMIN · 중복 · 상태 충돌
    "SIGNUP_REQUEST_NOT_FOUND",   # 404
    "SIGNUP_REQUEST_STATE_CONFLICT",  # 409
    "SIGNUP_REQUEST_APPROVAL_REQUIRED",  # 409 — PUT status 로는 승인하지 않는다 (#572)
    "INQUIRY_NOT_FOUND",          # 404
    "INQUIRY_STATE_CONFLICT",     # 409
    "NOTIFICATION_NOT_FOUND",     # 404
    "REQUEST_BODY_TOO_LARGE",     # 413
    "EVIDENCE_EXPORT_TOO_LARGE",  # 413 — 근거 내보내기 파일 상한 초과 (#488)
    "REPORT_DRAFT_TOO_LARGE",     # 413 — 신고 초안 입력·파일 상한
    "REPORT_DRAFT_MODEL_UNAVAILABLE",  # 503 — 미연결·일시 장애
    "REPORT_DRAFT_GENERATION_FAILED",  # 502 — 생성·검증 실패
    "REQUEST_VALIDATION_FAILED",  # 422
    "INTERNAL_ERROR",             # 500
}


def make_envelope(**over):
    detail = {
        "code": "IDEMPOTENCY_KEY_CONFLICT",
        "message": "동일한 Idempotency Key가 다른 요청에 사용되었습니다.",
        "request_id": "req-20260812-001",
    }
    detail.update(over)
    return {"error": detail}


def test_error_codes_match_contract_exactly():
    assert {c.value for c in ErrorCode} == CONTRACT_ERROR_CODES


@pytest.mark.parametrize("code", sorted(CONTRACT_ERROR_CODES))
def test_envelope_roundtrip_all_codes(code):
    resp = ErrorResponse.model_validate(make_envelope(code=code))
    assert resp.error.code.value == code
    assert ErrorResponse.model_validate_json(resp.model_dump_json()) == resp


@pytest.mark.parametrize("data", [
    make_envelope(code="NOT_A_CODE"),                     # 미등록 코드
    make_envelope(code="internal_error"),                 # 대소문자 불일치
    make_envelope(message=""),                            # 빈 메시지
    make_envelope(request_id=""),                         # 빈 요청 ID
    {"error": {**make_envelope()["error"], "detail": "x"}},  # detail 층 extra 거부
    {**make_envelope(), "status": 409},                   # 봉투 층 extra 거부
    make_envelope()["error"],                             # 봉투 없이 detail만
])
def test_envelope_contract_violations(data):
    with pytest.raises(ValidationError):
        ErrorResponse.model_validate(data)
