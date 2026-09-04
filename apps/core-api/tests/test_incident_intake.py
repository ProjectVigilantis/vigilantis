# ==============================================================================
# [파일 설명]
# create_incident_from_intake 통합 검증(PostgreSQL). Detection 판정/위협 → Incident 1건.
# 저장 순서·중복 억제·트랜잭션(commit)·근거 유형을 실제 저장소로 확인한다. (Issue #254)
# ==============================================================================

from __future__ import annotations

import uuid

from schemas.api.incidents import IncidentCategory, IncidentStatus
from schemas.evidence import EvidenceType
from schemas.intake import FinOpsIncidentIntake, SecOpsIncidentIntake

from db.repositories import incidents as incidents_repo
from incident_intake import create_incident_from_intake

EC2_ARN = "arn:aws:ec2:ap-northeast-2:123456789012:instance/i-0a1b2c3d4e5f00001"


def _ec2_asset(arn: str = EC2_ARN) -> dict:
    return {
        "arn": arn,
        "resource_id": "i-0a1b2c3d4e5f00001",
        "asset_type": "EC2",
        "resource_role": "PRIMARY",
        "name": "batch-dev",
        "account_id": "123456789012",
        "region": "ap-northeast-2",
        "state": "running",
        "spec": {"instance_type": "t3.xlarge", "availability_zone": "ap-northeast-2a"},
        "relationships": [],
        "evaluation_status": "COMPLETED",
        "health_score": 4,
        "verdict": "COST_CANDIDATE",
        "skip_reason_code": None,
        "collected_at": "2026-09-02T09:00:00Z",
    }


def _finops(subject_arn: str = EC2_ARN) -> FinOpsIncidentIntake:
    return FinOpsIncidentIntake.model_validate(
        {
            "asset_snapshot": {"collection_run_id": "run-001", "asset": _ec2_asset(subject_arn)},
            "rule_evaluation": {
                "asset_arn": subject_arn,
                "collection_run_id": "run-001",
                "evaluation_status": "COMPLETED",
                "verdict": "COST_CANDIDATE",
                "health_score": 4,
                "skip_reason_code": None,
                "reason": "2일 평균 CPU 4.9% — 다운사이징 후보",
                "evaluated_at": "2026-09-02T09:00:05Z",
            },
        }
    )


def _secops(*, threat_event_id: str | None = None, deduplication_key: str | None = None) -> SecOpsIncidentIntake:
    tid = threat_event_id or str(uuid.uuid4())
    dedup = deduplication_key or f"SSH_BRUTE_FORCE:i-0a1b2c3d4e5f00001:{tid[:8]}"
    return SecOpsIncidentIntake.model_validate(
        {
            "title": "SSH 브루트포스 시도",
            "threat_event": {
                "threat_event_id": tid,
                "source_event_id": "evt-mock-001",
                "event_type": "SSH_BRUTE_FORCE",
                "target_arn": EC2_ARN,
                "occurred_at": "2026-09-02T09:00:00Z",
                "payload": {"source_ip": "203.0.113.10", "failed_attempt_count": 120, "window_seconds": 300},
                "deduplication_key": dedup,
                "collected_at": "2026-09-02T09:00:01Z",
            },
            "initial_risk": {
                "threat_event_id": tid,
                "initial_risk_level": "HIGH",
                "response_mode": "PRE_MITIGATION_0_5S",
                "reason_codes": ["RISK_SSH_BRUTEFORCE"],
            },
        }
    )


# --- FINOPS -------------------------------------------------------------------


def test_finops_creates_incident_with_rule_evidence(db):
    out = create_incident_from_intake(db, _finops())
    assert out.created is True

    inc = incidents_repo.get_incident(db, out.incident_id)
    assert inc.category == IncidentCategory.FINOPS
    assert inc.subject_arn == EC2_ARN
    # 위험 대응 축은 전부 null(계약 category_risk_shape)
    assert inc.title is None
    assert inc.initial_risk_level is None
    assert inc.response_mode is None
    assert list(inc.initial_risk_reason_codes) == []

    ev = incidents_repo.list_evidence(db, out.incident_id)
    assert len(ev) == 1
    assert ev[0].evidence_type == EvidenceType.RULE
    assert ev[0].content["evaluation"]["asset_arn"] == EC2_ARN


def test_finops_dedup_returns_existing_open_incident(db):
    first = create_incident_from_intake(db, _finops())
    second = create_incident_from_intake(db, _finops())

    assert second.created is False
    assert second.incident_id == first.incident_id
    # 카드가 주기 수만큼 쌓이지 않는다 — 1건만
    same_arn = [
        i for i in incidents_repo.list_incidents(db, category=IncidentCategory.FINOPS)
        if i.subject_arn == EC2_ARN
    ]
    assert len(same_arn) == 1


def test_finops_recreates_after_terminal(db):
    first = create_incident_from_intake(db, _finops())
    inc = incidents_repo.get_incident(db, first.incident_id)
    inc.status = IncidentStatus.RESOLVED  # 종료 처리
    db.flush()

    second = create_incident_from_intake(db, _finops())
    assert second.created is True
    assert second.incident_id != first.incident_id


# --- SECOPS -------------------------------------------------------------------


def test_secops_creates_threat_incident_and_evidence(db):
    intake = _secops()
    out = create_incident_from_intake(db, intake)
    assert out.created is True

    inc = incidents_repo.get_incident(db, out.incident_id)
    assert inc.category == IncidentCategory.SECOPS
    assert inc.title == "SSH 브루트포스 시도"
    assert inc.initial_risk_level.value == "HIGH"
    assert inc.response_mode.value == "PRE_MITIGATION_0_5S"
    assert list(inc.initial_risk_reason_codes) == ["RISK_SSH_BRUTEFORCE"]
    assert inc.threat_event_id == intake.threat_event.threat_event_id

    te = incidents_repo.get_threat_event_by_dedup_key(db, intake.threat_event.deduplication_key)
    assert te is not None

    ev = incidents_repo.list_evidence(db, out.incident_id)
    assert len(ev) == 1
    assert ev[0].evidence_type == EvidenceType.THREAT


def test_secops_dedup_by_deduplication_key(db):
    intake = _secops()
    first = create_incident_from_intake(db, intake)
    # 같은 deduplication_key, 다른 threat_event_id — 재배달로 본다
    second = create_incident_from_intake(
        db, _secops(deduplication_key=intake.threat_event.deduplication_key)
    )

    assert second.created is False
    assert second.incident_id == first.incident_id
