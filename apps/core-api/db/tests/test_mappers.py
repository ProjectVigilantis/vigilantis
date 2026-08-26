"""Mapper 왕복 테스트 — DB 없이 항상 실행된다. (Issue #60)

계약 → ORM(new_*) → 계약(to_*) 왕복에서 값이 보존되는지 확인한다.
검증 규칙 자체는 packages/schemas 테스트가 담당한다 — 여기서는 필드 이동만 본다.
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

CORE_API = Path(__file__).resolve().parents[2]
REPO_ROOT = Path(__file__).resolve().parents[4]
for p in (str(CORE_API), str(REPO_ROOT / "packages")):
    if p not in sys.path:
        sys.path.insert(0, p)

from db import mappers, models  # noqa: E402
from schemas.candidates import CandidateStatus, RunbookCandidateData  # noqa: E402
from schemas.events import NormalizedThreatEvent, ThreatEventType  # noqa: E402
from schemas.evidence import EvidenceItem, EvidenceType  # noqa: E402
from schemas.executions import (  # noqa: E402
    ExecutionEffect,
    ExecutionStepResult,
    ExecutionStepStatus,
)
from schemas.rules import RuleEvaluationResult  # noqa: E402

NOW = datetime(2026, 8, 18, 12, 0, 0, tzinfo=timezone.utc)


def test_threat_event_round_trip():
    contract = NormalizedThreatEvent.model_validate(
        {
            "threat_event_id": "te-1",
            "source_event_id": "src-1",
            "event_type": ThreatEventType.SSH_BRUTE_FORCE,
            "target_arn": "arn:aws:ec2:ap-northeast-2:123456789012:instance/i-1",
            "occurred_at": NOW,
            "payload": {
                "source_ip": "203.0.113.9",
                "failed_attempt_count": 42,
                "window_seconds": 60,
            },
            "deduplication_key": "dk-1",
            "collected_at": NOW,
        }
    )
    row = mappers.new_threat_event(contract)
    assert mappers.to_threat_event(row) == contract


def test_evidence_round_trip_binds_content_by_type():
    contract = EvidenceItem.model_validate(
        {
            "evidence_id": "ev-1",
            "incident_id": "in-1",
            "evidence_type": EvidenceType.RULE,
            "source_type": "rule_evaluation",
            "source_id": "re-1",
            "content": {
                "evaluation": {
                    "asset_arn": "arn:x",
                    "collection_run_id": "run-1",
                    "evaluation_status": "COMPLETED",
                    "verdict": "COST_CANDIDATE",
                    "health_score": 17,
                    "evaluated_at": NOW.isoformat(),
                }
            },
            "occurred_at": NOW,
            "collected_at": NOW,
        }
    )
    row = mappers.new_evidence(contract)
    assert mappers.to_evidence_item(row) == contract


def test_candidate_round_trip():
    contract = RunbookCandidateData(
        candidate_id="cand-1",
        incident_id="in-1",
        runbook_id="RUNBOOK_EC2_RIGHTSIZING",
        target_arn="arn:x",
        # display_parameters는 넘기지 않는다 — 계약이 parameters에서 만든다(#154)
        parameters={"target_instance_type": "t3.medium"},
        evidence_ids=["ev-1", "ev-2"],
        status=CandidateStatus.PENDING_VALIDATION,
    )
    row = mappers.new_candidate(contract)
    assert mappers.to_candidate_data(row) == contract


def test_rule_evaluation_round_trip_resolves_arn_externally():
    contract = RuleEvaluationResult.model_validate(
        {
            "asset_arn": "arn:x",
            "collection_run_id": "run-1",
            "evaluation_status": "COMPLETED",
            "verdict": "SKIP",
            "skip_reason_code": "SKIP_PROD_PROTECTED",
            "reason": "prod 태그 보호 대상",
            "evaluated_at": NOW,
        }
    )
    row = mappers.new_rule_evaluation(contract, asset_id="a-1")
    row.collection_run_id = "run-1"  # DB에서는 FK로 채워지는 값
    restored = mappers.to_rule_evaluation_result(
        row, asset_arn="arn:x", collection_run_id="run-1"
    )
    assert restored == contract


def test_step_round_trip():
    contract = ExecutionStepResult(
        sequence=2,
        affected_arn="arn:x",
        step_type="modify_instance",
        aws_operation="ModifyInstanceAttribute",
        status=ExecutionStepStatus.SUCCESS,
        effect=ExecutionEffect.APPLIED,
        aws_request_id="req-1",
        result_summary="t3.large -> t3.medium",
        occurred_at=NOW,
    )
    row = mappers.new_execution_step(contract, execution_id="ex-1")
    assert mappers.to_step_result(row) == contract


def test_agent_wait_schedule_requires_both_fields():
    incident = models.Incident(
        incident_id="in-1",
        subject_arn="arn:x",
        category="SECOPS",
        agent_wait_started_at=None,
        response_deadline_at=None,
    )
    assert mappers.to_agent_wait_schedule(incident) is None

    incident.agent_wait_started_at = NOW
    incident.response_deadline_at = NOW + timedelta(seconds=60)
    schedule = mappers.to_agent_wait_schedule(incident)
    assert schedule is not None
    assert schedule.response_deadline_at - schedule.started_at == timedelta(seconds=60)
