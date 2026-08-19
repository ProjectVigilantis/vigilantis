"""Evidence 유형·내용 계약 테스트 (Issue #49).

content는 새 구조 발명 없이 기존 확정 계약 재사용 — RULE→RuleEvaluationResult,
THREAT→NormalizedThreatEvent, METRIC→MetricSummary, EXECUTION→최소 요약.
"""

import pytest
from pydantic import ValidationError

from schemas.evidence import (
    EVIDENCE_CONTENT_MODELS,
    EvidenceItem,
    EvidenceType,
    RuleEvidence,
)
from schemas.rules import RuleEvaluationResult

RULE_CONTENT = {
    "evaluation": {
        "asset_arn": "arn:aws:ec2:ap-northeast-2:123456789012:instance/i-0123",
        "collection_run_id": "run-20260814-001",
        "evaluation_status": "COMPLETED",
        "verdict": "COST_CANDIDATE",
        "health_score": 3,
        "skip_reason_code": None,
        "reason": "3일 평균 CPU 3%",
        "evaluated_at": "2026-08-14T09:00:00Z",
    }
}


def make_item(**over):
    base = {
        "evidence_id": "ev-001",
        "incident_id": "inc-20260814-001",
        "evidence_type": "RULE",
        "source_type": "rule_evaluation",
        "source_id": "run-20260814-001",
        "content": RULE_CONTENT,
        "occurred_at": "2026-08-14T09:00:00Z",
        "collected_at": "2026-08-14T09:00:01Z",
    }
    base.update(over)
    return base


def test_types_match_contract_exactly():
    assert {t.value for t in EvidenceType} == {"METRIC", "RULE", "THREAT", "EXECUTION"}
    assert set(EVIDENCE_CONTENT_MODELS) == set(EvidenceType)


def test_rule_content_reuses_rule_evaluation_result():
    item = EvidenceItem.model_validate(make_item())
    assert isinstance(item.content, RuleEvidence)
    assert isinstance(item.content.evaluation, RuleEvaluationResult)
    assert EvidenceItem.model_validate_json(item.model_dump_json()) == item


def test_rejects_mismatched_content():
    # THREAT 근거에 RULE content — evidence_type↔content 정합 위반
    with pytest.raises(ValidationError):
        EvidenceItem.model_validate(make_item(evidence_type="THREAT"))


def test_execution_content_valid():
    item = EvidenceItem.model_validate(make_item(
        evidence_type="EXECUTION",
        source_type="action_execution",
        source_id="exec-001",
        content={
            "execution_id": "exec-001",
            "runbook_id": "RUNBOOK_EC2_ISOLATE",
            "status": "SUCCESS",
            "summary": "사전 격리 완료",
        },
    ))
    assert item.content.runbook_id.value == "RUNBOOK_EC2_ISOLATE"


def test_metric_window_order_enforced():
    with pytest.raises(ValidationError):
        EvidenceItem.model_validate(make_item(
            evidence_type="METRIC",
            source_type="metric_summary",
            content={
                "metric_name": "CPUUtilization",
                "window_start": "2026-08-14T09:00:00Z",
                "window_end": "2026-08-13T09:00:00Z",
                "summary": {"cpu_datapoints": 72, "cpu_avg": 3.0},
            },
        ))
