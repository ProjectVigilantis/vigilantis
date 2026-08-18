"""실행 단계 상태·적용 결과 계약 테스트 (Issue #55) — status↔effect 짝 고정."""

import pytest
from pydantic import ValidationError

from schemas.api.actions import ExecutionStatus
from schemas.executions import (
    EXECUTION_NON_TERMINAL_STATUSES,
    EXECUTION_TERMINAL_STATUSES,
    ExecutionEffect,
    ExecutionStepResult,
    ExecutionStepStatus,
)


def test_enums_match_contract_exactly():
    assert {s.value for s in ExecutionStepStatus} == {"IN_PROGRESS", "SUCCESS", "FAILED"}
    assert {e.value for e in ExecutionEffect} == {
        "NOT_APPLIED", "APPLIED", "PARTIAL", "UNKNOWN",
    }


def make_step(**over):
    base = {
        "sequence": 1,
        "affected_arn": "arn:aws:ec2:ap-northeast-2:123456789012:instance/i-0123",
        "step_type": "APPLY_CHANGE",
        "aws_operation": "ec2:ModifyInstanceAttribute",
        "status": "SUCCESS",
        "effect": "APPLIED",
        "aws_request_id": "req-aws-001",
        "result_summary": "인스턴스 타입 변경 적용",
        "error_summary": None,
        "occurred_at": "2026-08-14T09:00:00Z",
    }
    base.update(over)
    return base


def test_roundtrip():
    s = ExecutionStepResult.model_validate(make_step())
    assert ExecutionStepResult.model_validate_json(s.model_dump_json()) == s


@pytest.mark.parametrize("status,effect,ok", [
    ("IN_PROGRESS", None, True),
    ("SUCCESS", "APPLIED", True),
    ("SUCCESS", "NOT_APPLIED", True),
    ("FAILED", "NOT_APPLIED", True),
    ("FAILED", "PARTIAL", True),
    ("FAILED", "UNKNOWN", True),
    ("IN_PROGRESS", "APPLIED", False),  # 진행 중엔 effect 없음
    ("SUCCESS", "PARTIAL", False),      # 성공이 부분 적용일 수 없음
    ("SUCCESS", "UNKNOWN", False),
    ("SUCCESS", None, False),           # 종료 상태는 effect 필수
    ("FAILED", "APPLIED", False),       # 실패인데 전체 적용이면 상태 모순
    ("FAILED", None, False),
])
def test_status_effect_pairs(status, effect, ok):
    data = make_step(status=status, effect=effect)
    if ok:
        assert ExecutionStepResult.model_validate(data).status.value == status
    else:
        with pytest.raises(ValidationError):
            ExecutionStepResult.model_validate(data)


@pytest.mark.parametrize("over", [
    {"sequence": 0},
    {"affected_arn": ""},
    {"unknown_field": 1},
])
def test_contract_violations(over):
    with pytest.raises(ValidationError):
        ExecutionStepResult.model_validate(make_step(**over))


def test_status_sets_partition_execution_status():
    """두 집합이 ExecutionStatus 6종을 겹침 없이 정확히 나눈다."""
    assert not EXECUTION_NON_TERMINAL_STATUSES & EXECUTION_TERMINAL_STATUSES
    assert EXECUTION_NON_TERMINAL_STATUSES | EXECUTION_TERMINAL_STATUSES == set(ExecutionStatus)


def test_rollback_initiated_is_non_terminal():
    """Rollback 자식이 도는 동안 원본은 진행 중이다.
    이 값이 종료 집합에 들어가면 Dispatcher 회수와 Incident RESOLVED 전이가 어긋난다."""
    assert ExecutionStatus.ROLLBACK_INITIATED in EXECUTION_NON_TERMINAL_STATUSES
    assert ExecutionStatus.ROLLBACK_FAILED in EXECUTION_TERMINAL_STATUSES
