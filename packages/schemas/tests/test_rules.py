"""RuleEvaluationResult 내부 계약 테스트 (Issue #48).

공개 AssetItem과 같은 교차 불변식 + health_score 0~100 정수(소수 거부).
"""

import pytest
from pydantic import ValidationError

from schemas.api.assets import EvaluationStatus, SkipReasonCode, Verdict
from schemas.rules import RuleEvaluationResult


def make_result(**over):
    base = {
        "asset_arn": "arn:aws:ec2:ap-northeast-2:123456789012:instance/i-0123",
        "collection_run_id": "run-20260814-001",
        "evaluation_status": "COMPLETED",
        "verdict": "COST_CANDIDATE",
        "health_score": 3,
        "skip_reason_code": None,
        "reason": "3일 평균 CPU 3% — 다운사이징 후보",
        "evaluated_at": "2026-08-14T09:00:00Z",
    }
    base.update(over)
    return base


def test_reuses_public_enums():
    r = RuleEvaluationResult.model_validate(make_result())
    assert r.verdict is Verdict.COST_CANDIDATE
    assert r.evaluation_status is EvaluationStatus.COMPLETED


def test_roundtrip_z_serialization():
    r = RuleEvaluationResult.model_validate(make_result())
    dumped = r.model_dump_json()
    assert '"2026-08-14T09:00:00Z"' in dumped
    assert RuleEvaluationResult.model_validate_json(dumped) == r


def test_skip_with_code_valid():
    r = RuleEvaluationResult.model_validate(make_result(
        verdict="SKIP", skip_reason_code="SKIP_ACTIVE", health_score=62,
    ))
    assert r.skip_reason_code is SkipReasonCode.SKIP_ACTIVE


def test_failed_with_all_null_valid():
    r = RuleEvaluationResult.model_validate(make_result(
        evaluation_status="FAILED", verdict=None, health_score=None,
        skip_reason_code=None, reason=None,
    ))
    assert r.verdict is None and r.reason is None


@pytest.mark.parametrize("over", [
    {"health_score": 2.8},                                     # 소수 거부 — 0~100 정수만
    {"health_score": 101},
    {"health_score": -1},
    {"verdict": None},                                         # COMPLETED인데 verdict 없음
    {"evaluation_status": "PENDING"},                          # 판정 전인데 판정값 잔존
    {"verdict": "SKIP", "skip_reason_code": None},             # SKIP인데 사유 코드 없음
    {"verdict": "THREAT", "skip_reason_code": "SKIP_ACTIVE"},  # SKIP 아닌데 코드 잔존
    {"asset_arn": ""},
    {"unknown_field": 1},                                      # extra 거부
])
def test_contract_violations(over):
    with pytest.raises(ValidationError):
        RuleEvaluationResult.model_validate(make_result(**over))
