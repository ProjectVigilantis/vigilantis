# ==============================================================================
# [파일 설명]
# GET /api/v1/incidents(목록·필터·정렬)·/{id}(상세·404) 통합 검증(PostgreSQL).
# (Issue #68)
# ==============================================================================

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from schemas.api.actions import ExecutionStatus
from schemas.api.incidents import (
    IncidentCategory,
    IncidentStatus,
    ResponseMode,
    RiskLevel,
)
from schemas.candidates import CandidateStatus
from schemas.evidence import EvidenceType
from schemas.runbooks import RunbookId, TriggerSource

from db import models

T0 = datetime(2026, 8, 19, 3, 0, 0, tzinfo=timezone.utc)
SUBJECT_EC2 = "arn:aws:ec2:ap-northeast-2:123456789012:instance/i-0aaa"
SUBJECT_EBS = "arn:aws:ec2:ap-northeast-2:123456789012:volume/vol-0bbb"


def _seed_two_incidents(db) -> tuple[models.Incident, models.Incident]:
    secops = models.Incident(
        subject_arn=SUBJECT_EC2,
        category=IncidentCategory.SECOPS,
        status=IncidentStatus.AWAITING_APPROVAL,
        title="SSH 브루트포스 탐지",
        initial_risk_level=RiskLevel.HIGH,
        response_mode=ResponseMode.AGENT_WAIT,
        initial_risk_reason_codes=["SSH_BRUTE_FORCE"],
        summary_lines=["요약 1", "요약 2", "요약 3"],
        created_at=T0,
        updated_at=T0,
    )
    finops = models.Incident(
        subject_arn=SUBJECT_EBS,
        category=IncidentCategory.FINOPS,
        status=IncidentStatus.ANALYZING,
        created_at=T0 + timedelta(minutes=10),
        updated_at=T0 + timedelta(minutes=10),
    )
    db.add_all([secops, finops])
    db.flush()
    return secops, finops


def test_list_orders_by_created_at_desc_and_filters(client_pg, db):
    secops, finops = _seed_two_incidents(db)

    body = client_pg.get("/api/v1/incidents").json()
    assert [item["incident_id"] for item in body["items"]] == [
        finops.incident_id,
        secops.incident_id,
    ]

    filtered = client_pg.get(
        "/api/v1/incidents", params={"status": "AWAITING_APPROVAL"}
    ).json()
    assert [item["incident_id"] for item in filtered["items"]] == [secops.incident_id]

    filtered = client_pg.get("/api/v1/incidents", params={"category": "FINOPS"}).json()
    assert [item["incident_id"] for item in filtered["items"]] == [finops.incident_id]
    # FINOPS는 위험 대응 축이 전부 null(계약 불변식)
    assert filtered["items"][0]["initial_risk_level"] is None
    assert filtered["items"][0]["title"] is None


def test_detail_unknown_id_returns_404_envelope(client_pg):
    response = client_pg.get(f"/api/v1/incidents/{uuid.uuid4()}")
    assert response.status_code == 404
    body = response.json()
    assert body["error"]["code"] == "INCIDENT_NOT_FOUND"
    assert body["error"]["request_id"] == response.headers["X-Request-ID"]


def test_detail_assembles_evidence_recommendations_executions(client_pg, db):
    secops, _ = _seed_two_incidents(db)
    older = models.Evidence(
        incident_id=secops.incident_id,
        evidence_type=EvidenceType.THREAT,
        source_type="threat_event",
        source_id="te-1",
        content={"attempts": 120},
        occurred_at=T0 - timedelta(minutes=5),
        collected_at=T0,
    )
    newer = models.Evidence(
        incident_id=secops.incident_id,
        evidence_type=EvidenceType.METRIC,
        source_type="metric_summary",
        source_id="ms-1",
        content={"cpu_avg": 1.2},
        occurred_at=T0 - timedelta(minutes=1),
        collected_at=T0,
    )
    executable = models.RunbookCandidate(
        incident_id=secops.incident_id,
        runbook_id=RunbookId.RUNBOOK_EC2_ISOLATE,
        target_arn=SUBJECT_EC2,
        display_parameters={"port": "22"},
        evidence_ids=[],
        status=CandidateStatus.EXECUTABLE,
    )
    rejected = models.RunbookCandidate(
        incident_id=secops.incident_id,
        runbook_id=RunbookId.RUNBOOK_SG_DELETE_ISOLATED,
        target_arn=SUBJECT_EC2,
        status=CandidateStatus.REJECTED,
    )
    db.add_all([older, newer, executable, rejected])
    db.flush()

    response = client_pg.get(f"/api/v1/incidents/{secops.incident_id}")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "AWAITING_APPROVAL"
    assert body["summary_lines"] == ["요약 1", "요약 2", "요약 3"]
    # Evidence는 occurred_at 오름차순
    assert body["evidence_ids"] == [older.evidence_id, newer.evidence_id]
    # EXECUTABLE 후보만 recommendations에 온다(REJECTED 제외)
    assert [rec["runbook_id"] for rec in body["recommendations"]] == [
        "RUNBOOK_EC2_ISOLATE"
    ]
    assert body["recommendations"][0]["display_parameters"] == {"port": "22"}
    assert body["executions"] == []
    assert body["created_at"] == "2026-08-19T03:00:00Z"


def test_detail_assembles_execution_summaries(client_pg, db):
    secops, _ = _seed_two_incidents(db)
    candidate = models.RunbookCandidate(
        incident_id=secops.incident_id,
        runbook_id=RunbookId.RUNBOOK_NACL_ADD_DENY,
        target_arn=SUBJECT_EC2,
        status=CandidateStatus.EXECUTABLE,
    )
    execution = models.ActionExecution(
        incident_id=secops.incident_id,
        runbook_id=RunbookId.RUNBOOK_EC2_ISOLATE,
        target_arn=SUBJECT_EC2,
        status=ExecutionStatus.SUCCESS,
        trigger_source=TriggerSource.PRE_MITIGATION_0_5S,
        updated_at=T0 + timedelta(minutes=2),
    )
    db.add_all([candidate, execution])
    db.flush()

    body = client_pg.get(f"/api/v1/incidents/{secops.incident_id}").json()
    assert body["executions"] == [
        {
            "execution_id": execution.execution_id,
            "runbook_id": "RUNBOOK_EC2_ISOLATE",
            "status": "SUCCESS",
            # 복구 가능 목록 구성은 조치 실행 작업으로 유예 — 조회 단계는 빈 목록
            "available_recovery_runbook_ids": [],
            "updated_at": "2026-08-19T03:02:00Z",
        }
    ]
