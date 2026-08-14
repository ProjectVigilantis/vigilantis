"""POST /api/v1/actions/execute 외부 DTO 계약 테스트.

확정 계약(4.4): 요청은 SSOT 3필드만, 추가 필드 거부, runbook_id는 확정 10종 원천,
실행 상태 6종(SSOT 4종 + 복구 최종 결과 2종), "Z" 시각 직렬화.
"""

import pytest
from pydantic import ValidationError

from schemas.api.actions import (
    ExecuteActionRequest,
    ExecuteActionResponse,
    ExecutionStatus,
)
from schemas.runbooks import ALLOWED_RUNBOOK_IDS

# SSOT 4종 + FE 합의 확장 2종. 코드가 아니라 이 리터럴 집합이 기대값이다.
CONTRACT_EXECUTION_STATUSES = {
    "IN_PROGRESS",
    "SUCCESS",
    "FAILED",
    "ROLLBACK_INITIATED",
    "ROLLED_BACK",
    "ROLLBACK_FAILED",
}


def make_request(**over):
    base = {
        "incident_id": "inc-20260812-001",
        "runbook_id": "RUNBOOK_NACL_ADD_DENY",
        "idempotency_key": "6dbfe076-1da1-4d35-88f8-b869dce44e61",
    }
    base.update(over)
    return base


def test_execution_status_matches_contract_exactly():
    assert {s.value for s in ExecutionStatus} == CONTRACT_EXECUTION_STATUSES


def test_request_roundtrip():
    req = ExecuteActionRequest.model_validate(make_request())
    assert req.runbook_id.value == "RUNBOOK_NACL_ADD_DENY"
    assert ExecuteActionRequest.model_validate_json(req.model_dump_json()) == req


@pytest.mark.parametrize("runbook_id", sorted(ALLOWED_RUNBOOK_IDS))
def test_request_accepts_all_confirmed_runbooks(runbook_id):
    req = ExecuteActionRequest.model_validate(make_request(runbook_id=runbook_id))
    assert req.runbook_id.value == runbook_id


@pytest.mark.parametrize("runbook_id", [
    "RUNBOOK_EC2_DOWNSIZE",       # 폐기 2종
    "RUNBOOK_IP_BLOCK",
    "runbook_nacl_add_deny",      # 대소문자 불일치
    "RUNBOOK_NACL_ADD_DENY ",     # 공백 포함
    "",
])
def test_request_rejects_unlisted_runbooks(runbook_id):
    with pytest.raises(ValidationError):
        ExecuteActionRequest.model_validate(make_request(runbook_id=runbook_id))


@pytest.mark.parametrize("missing", ["incident_id", "runbook_id", "idempotency_key"])
def test_request_requires_all_three_fields(missing):
    data = make_request()
    del data[missing]
    with pytest.raises(ValidationError):
        ExecuteActionRequest.model_validate(data)


@pytest.mark.parametrize("extra", [
    {"target_arn": "arn:aws:ec2:ap-northeast-2:123456789012:instance/i-0123"},
    {"parameters": {"instance_type": "t3.small"}},
    {"dry_run": True},
])
def test_request_rejects_extra_fields(extra):
    # 계약: Target ARN·AWS 파라미터를 받지 않는다 — 서버가 저장된 제안으로 재구성
    with pytest.raises(ValidationError):
        ExecuteActionRequest.model_validate(make_request(**extra))


@pytest.mark.parametrize("over", [
    {"incident_id": ""},
    {"idempotency_key": ""},
])
def test_request_rejects_empty_identifiers(over):
    with pytest.raises(ValidationError):
        ExecuteActionRequest.model_validate(make_request(**over))


@pytest.mark.parametrize("status", sorted(CONTRACT_EXECUTION_STATUSES))
def test_response_roundtrip_all_statuses(status):
    resp = ExecuteActionResponse.model_validate({
        "execution_id": "exec-20260812-001",
        "status": status,
        "updated_at": "2026-08-12T09:02:00Z",
    })
    assert '"2026-08-12T09:02:00Z"' in resp.model_dump_json()


@pytest.mark.parametrize("over", [
    {"status": "ROLLED-BACK"},        # 미등록 상태 표기
    {"status": "in_progress"},        # 대소문자 불일치
    {"execution_id": ""},
    {"unknown_field": 1},             # extra 거부
])
def test_response_contract_violations(over):
    base = {
        "execution_id": "exec-20260812-001",
        "status": "IN_PROGRESS",
        "updated_at": "2026-08-12T09:02:00Z",
    }
    base.update(over)
    with pytest.raises(ValidationError):
        ExecuteActionResponse.model_validate(base)
