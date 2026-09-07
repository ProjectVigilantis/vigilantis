# ==============================================================================
# [파일 설명]
# GET /api/v1/incidents(목록·필터·정렬)·/{id}(상세·404) 통합 검증(PostgreSQL).
# (Issue #68)
# ==============================================================================

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from schemas.api.actions import ExecutionStatus
from schemas.api.incidents import (
    IncidentCategory,
    IncidentStatus,
    RiskLevel,
)
from schemas.candidates import CandidateStatus
from schemas.evidence import EvidenceType
from schemas.executions import EXECUTION_NON_TERMINAL_STATUSES
from schemas.runbooks import RunbookId, TriggerSource

from db import models

T0 = datetime(2026, 8, 19, 3, 0, 0, tzinfo=timezone.utc)
SUBJECT_EC2 = "arn:aws:ec2:ap-northeast-2:123456789012:instance/i-0aaa"
SUBJECT_EBS = "arn:aws:ec2:ap-northeast-2:123456789012:volume/vol-0bbb"


@pytest.fixture()
def two_incidents(db, make_incident, seed_summary_lines):
    """목록 정렬·필터 검증용 SECOPS+FINOPS 쌍.

    사본이 아니라 조합이라 이 파일에 남긴다. 위험 축 모양(SECOPS는 필수 필드,
    FINOPS는 전부 NULL)은 팩토리가 category에서 파생시키므로 여기서 적지 않는다
    — 예전 이 함수는 두 모양을 나란히 손으로 박고 있었다 (Issue #233).
    """

    def _make() -> tuple[models.Incident, models.Incident]:
        secops = make_incident(
            db,
            subject_arn=SUBJECT_EC2,
            initial_risk_level=RiskLevel.HIGH,
            summary_lines=seed_summary_lines,
            created_at=T0,
            updated_at=T0,
        )
        finops = make_incident(
            db,
            category=IncidentCategory.FINOPS,
            subject_arn=SUBJECT_EBS,
            status=IncidentStatus.ANALYZING,
            created_at=T0 + timedelta(minutes=10),
            updated_at=T0 + timedelta(minutes=10),
        )
        return secops, finops

    return _make


def test_list_orders_by_created_at_desc_and_filters(client_pg, db, two_incidents):
    secops, finops = two_incidents()

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


def test_detail_assembles_evidence_recommendations_executions(
    client_pg, db, two_incidents, make_candidate
):
    secops, _ = two_incidents()
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
    # display_parameters는 팩토리가 계약(#154)으로 파생시킨다 — 시드가 그 값을 손으로
    # 적으면 아래 단언이 자기가 심은 값을 되읽어, 파생 규칙의 회귀를 못 잡는다.
    make_candidate(db, secops)
    make_candidate(
        db,
        secops,
        runbook_id=RunbookId.RUNBOOK_SG_DELETE_ISOLATED,
        status=CandidateStatus.REJECTED,
    )
    db.add_all([older, newer])
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
        "RUNBOOK_NACL_ADD_DENY"
    ]
    assert body["recommendations"][0]["display_parameters"] == {
        "rule_number": "100", "cidr_block": "203.0.113.5/32", "protocol": "-1",
    }
    assert body["executions"] == []
    assert body["created_at"] == "2026-08-19T03:00:00Z"


def test_detail_assembles_execution_summaries(client_pg, db, two_incidents, make_candidate):
    secops, _ = two_incidents()
    make_candidate(db, secops)
    execution = models.ActionExecution(
        incident_id=secops.incident_id,
        runbook_id=RunbookId.RUNBOOK_EC2_ISOLATE,
        target_arn=SUBJECT_EC2,
        status=ExecutionStatus.SUCCESS,
        trigger_source=TriggerSource.PRE_MITIGATION_0_5S,
        updated_at=T0 + timedelta(minutes=2),
    )
    db.add(execution)
    db.flush()

    body = client_pg.get(f"/api/v1/incidents/{secops.incident_id}").json()
    assert body["executions"] == [
        {
            "execution_id": execution.execution_id,
            "runbook_id": "RUNBOOK_EC2_ISOLATE",
            "status": "SUCCESS",
            "available_recovery_runbook_ids": ["RUNBOOK_EC2_UNISOLATE"],
            "updated_at": "2026-08-19T03:02:00Z",
        }
    ]


# --- 복구 가능 목록 파생 (Issue #126) --------------------------------------------


def _add_execution(
    db,
    incident: models.Incident,
    runbook_id: RunbookId,
    status: ExecutionStatus,
    parent: models.ActionExecution | None = None,
) -> models.ActionExecution:
    execution = models.ActionExecution(
        incident_id=incident.incident_id,
        runbook_id=runbook_id,
        target_arn=SUBJECT_EC2,
        status=status,
        trigger_source=TriggerSource.USER_APPROVAL,
        parent_execution_id=None if parent is None else parent.execution_id,
    )
    db.add(execution)
    db.flush()
    return execution


@pytest.mark.parametrize(
    "runbook_id, status, expected",
    [
        # 짝이 있고 원본이 복구 가능 상태 — 노출한다
        (RunbookId.RUNBOOK_EC2_ISOLATE, ExecutionStatus.SUCCESS, ["RUNBOOK_EC2_UNISOLATE"]),
        (RunbookId.RUNBOOK_SG_DELETE_ISOLATED, ExecutionStatus.SUCCESS, ["RUNBOOK_SG_RECREATE"]),
        (
            RunbookId.RUNBOOK_EC2_RIGHTSIZING,
            ExecutionStatus.ROLLBACK_INITIATED,
            ["RUNBOOK_EC2_REVERT_SIZE"],
        ),
        # AWS가 아직 안 바뀌었거나(IN_PROGRESS) 바뀌지 않은 채 실패(FAILED) — 되돌릴 것이 없다
        (RunbookId.RUNBOOK_EC2_ISOLATE, ExecutionStatus.IN_PROGRESS, []),
        (RunbookId.RUNBOOK_EC2_ISOLATE, ExecutionStatus.FAILED, []),
        # 복구가 이미 끝난 원본은 다시 열지 않는다
        (RunbookId.RUNBOOK_EC2_ISOLATE, ExecutionStatus.ROLLED_BACK, []),
        # 짝이 없는 본편 조치 — NACL 차단 해제는 recommendations 경로다
        (RunbookId.RUNBOOK_NACL_ADD_DENY, ExecutionStatus.SUCCESS, []),
        (RunbookId.RUNBOOK_EBS_DELETE_UNATTACHED, ExecutionStatus.SUCCESS, []),
    ],
)
def test_detail_derives_available_recovery(client_pg, db, two_incidents, runbook_id, status, expected):
    """FE mock(route.ts RECOVERY_BY_RUNBOOK·RECOVERABLE_ORIGIN_STATUSES)과 같은 규칙."""
    secops, _ = two_incidents()
    secops.status = (
        IncidentStatus.ACTION_IN_PROGRESS
        if status in EXECUTION_NON_TERMINAL_STATUSES
        else IncidentStatus.RESOLVED
    )
    _add_execution(db, secops, runbook_id, status)

    body = client_pg.get(f"/api/v1/incidents/{secops.incident_id}").json()

    assert body["executions"][0]["available_recovery_runbook_ids"] == expected


def test_detail_hides_recovery_once_child_execution_exists(client_pg, db, two_incidents):
    """복구가 접수된 원본은 목록에서 빠진다 — 이중 롤백을 화면에서부터 막는다."""
    secops, _ = two_incidents()
    secops.status = IncidentStatus.ACTION_IN_PROGRESS
    origin = _add_execution(db, secops, RunbookId.RUNBOOK_EC2_ISOLATE, ExecutionStatus.SUCCESS)
    _add_execution(
        db,
        secops,
        RunbookId.RUNBOOK_EC2_UNISOLATE,
        ExecutionStatus.IN_PROGRESS,
        parent=origin,
    )

    body = client_pg.get(f"/api/v1/incidents/{secops.incident_id}").json()

    summaries = {e["execution_id"]: e for e in body["executions"]}
    assert summaries[origin.execution_id]["available_recovery_runbook_ids"] == []
    # 자식(롤백) 자신은 짝이 없어 아무것도 열지 않는다
    assert all(e["available_recovery_runbook_ids"] == [] for e in body["executions"])
