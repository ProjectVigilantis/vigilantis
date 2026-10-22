"""REST 공통 오류 봉투 계약 테스트 (확정 설계 4.6) — 코드 6종, 봉투 형태 고정."""

import pytest
from pydantic import ValidationError

from schemas.api.errors import ErrorCode, ErrorResponse

# 계약 원문 24종. 코드가 아니라 이 리터럴 집합이 기대값이다.
CONTRACT_ERROR_CODES = {
    "INCIDENT_NOT_FOUND",         # 404
    "IDEMPOTENCY_KEY_CONFLICT",   # 409
    "PROPOSAL_NOT_EXECUTABLE",    # 409
    "INCIDENT_NOT_RESOLVABLE",    # 409 — 종료 처리 거절 (Issue #199)
    "TENANT_NOT_FOUND",           # 404 — Post-MVP T-4
    "TENANT_ACCOUNT_CONFLICT",    # 409 — 같은 계정의 테넌트가 이미 있다
    "TENANT_ONBOARDING_UNAVAILABLE",  # 503 — 온보딩 링크 설정 없음
    "TENANT_STATE_CONFLICT",      # 409 — 테넌트 상태 전이 거절
    "TENANT_SUSPENDED",           # 409 — 정지된 테넌트 자산에 새 실행
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
