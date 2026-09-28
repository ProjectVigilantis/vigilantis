"""#362 공개 문맥의 DB 연결·조회 비용·누락 진단. 형식 전수는 schemas 테스트가 소유한다."""

from __future__ import annotations

import logging
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime

import pytest
from db import models
from db.repositories import incidents as incidents_repo
from logging_config import JsonLineFormatter
from schemas.api.incidents import IncidentCategory, IncidentStatus
from schemas.events import ThreatEventType
from schemas.incidents import AgentInvocationStatus
from sqlalchemy import event as sa_event
from sqlalchemy.exc import OperationalError


@pytest.fixture
def linked_incident(db, make_incident):
    def make(*, open_ip=False, source="203.0.113.10"):
        kind = ThreatEventType.OPEN_IP if open_ip else ThreatEventType.SSH_BRUTE_FORCE
        resource = "security-group/sg-0aaa" if open_ip else "instance/i-0aaa"
        # 같은 대상에 여러 관측이 생겨도 문맥은 threat_event_id로만 연결되어야 한다.
        target = f"arn:aws:ec2:ap-northeast-2:123456789012:{resource}"
        payload = (
            {"protocol": "tcp", "from_port": 22, "to_port": 22, "source_cidr": "0.0.0.0/0"}
            if open_ip else {"source_ip": source, "failed_attempt_count": 120, "window_seconds": 300}
        )
        threat = models.ThreatEvent(
            source_event_id=str(uuid.uuid4()), event_type=kind, target_arn=target,
            payload=payload, deduplication_key=str(uuid.uuid4()),
            occurred_at=datetime.now(UTC),
        )
        db.add(threat)
        db.flush()
        incident = make_incident(db, subject_arn=target, status=IncidentStatus.ANALYZING)
        incident.threat_event_id = threat.threat_event_id
        db.flush()
        context = (
            {"event_type": "OPEN_IP", "exposed_cidr": payload["source_cidr"]}
            if open_ip else {"event_type": "SSH_BRUTE_FORCE", "source_ip": source}
        )
        return incident, threat, context

    return make


@contextmanager
def capture_selects(db):
    statements = []
    connection = db.connection()

    def capture(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement)

    sa_event.listen(connection, "before_cursor_execute", capture)
    try:
        yield statements
    finally:
        sa_event.remove(connection, "before_cursor_execute", capture)


@pytest.mark.parametrize("count", [1, 8])
def test_list_batches_events_and_keeps_each_observation_context(db, client_pg, linked_incident, count):
    expected = {}
    for index in range(count):
        incident, _, context = linked_incident(open_ip=index % 3 == 0, source=f"203.0.113.{index + 1}")
        expected[incident.incident_id] = context
    db.expire_all()

    with capture_selects(db) as statements:
        response = client_pg.get("/api/v1/incidents")
    assert response.status_code == 200
    assert len(statements) == 2  # Incident 1회 + 해당 ThreatEvent 전체 1회, 건수와 무관
    assert sum("FROM threat_events" in sql for sql in statements) == 1
    items = response.json()["items"]
    assert {item["incident_id"]: item["threat_context"] for item in items} == expected
    for item in items:
        detail = client_pg.get(f"/api/v1/incidents/{item['incident_id']}")
        assert detail.status_code == 200
        assert detail.json()["threat_context"] == item["threat_context"]


@pytest.mark.parametrize("open_ip", [False, True])
def test_ai_failure_and_resolution_preserve_the_observation(db, client_pg, linked_incident, open_ip):
    incident, threat, context = linked_incident(open_ip=open_ip)
    original_payload = dict(threat.payload)
    incident.status = IncidentStatus.FAILED
    incident.agent_invocation_status = AgentInvocationStatus.FAILED
    db.commit()
    url = f"/api/v1/incidents/{incident.incident_id}"

    failed = client_pg.get(url)
    assert failed.status_code == 200
    assert failed.json()["threat_context"] == context
    assert failed.json()["summary_lines"] == failed.json()["recommendations"] == []

    resolved = client_pg.post(f"{url}/resolve", json={"resolution": "NO_FURTHER_ACTION"})
    assert resolved.status_code == 200
    assert resolved.json()["status"] == "RESOLVED"
    assert resolved.json()["threat_context"] == context
    assert client_pg.get(url).json()["threat_context"] == context
    assert client_pg.get("/api/v1/incidents").json()["items"][0]["threat_context"] == context
    db.refresh(threat)
    assert threat.payload == original_payload  # 내부 source_cidr는 공개 이름으로 바꾸지 않는다.


@pytest.mark.parametrize("corruption,reason", [
    ("missing_link", "missing_threat_event_id"),
    ("target_mismatch", "target_mismatch"),
    ("invalid_ip", "invalid_threat_event"),
    ("invalid_cidr", "invalid_threat_event"),
    ("wrong_payload", "invalid_threat_event"),
    ("non_object_payload", "invalid_threat_event"),
])
def test_unusable_context_is_null_with_safe_diagnostic(
    db, client_pg, linked_incident, caplog, corruption, reason,
):
    incident, threat, _ = linked_incident(open_ip=corruption == "invalid_cidr")
    good, _, expected = linked_incident(source="198.51.100.7")
    private_value = "private-malformed-input"
    if corruption == "missing_link":
        incident.threat_event_id = None
    elif corruption == "target_mismatch":
        threat.target_arn += "-different"
    elif corruption == "wrong_payload":
        threat.payload = {"source_cidr": "0.0.0.0/0", "protocol": "tcp"}
    elif corruption == "non_object_payload":
        threat.payload = [private_value]
    else:
        key = "source_cidr" if corruption == "invalid_cidr" else "source_ip"
        threat.payload = {**threat.payload, key: private_value}
    db.flush()
    db.expire_all()

    with caplog.at_level(logging.WARNING, logger="vigilantis.incidents"):
        listing = client_pg.get("/api/v1/incidents")
        detail = client_pg.get(f"/api/v1/incidents/{incident.incident_id}")
    assert listing.status_code == detail.status_code == 200
    contexts = {item["incident_id"]: item["threat_context"] for item in listing.json()["items"]}
    assert contexts == {incident.incident_id: None, good.incident_id: expected}
    assert detail.json()["threat_context"] is None
    records = [r for r in caplog.records if r.message == "incident_threat_context_unavailable"]
    assert len(records) == 2
    for record in records:
        assert record.reason == reason
        assert record.incident_id == incident.incident_id
        assert record.threat_event_id == incident.threat_event_id
        assert record.exc_info is None
        rendered = JsonLineFormatter().format(record)
        assert private_value not in rendered
        assert "payload" not in rendered and "SELECT" not in rendered


def test_finops_ignores_threat_link_and_does_not_query_events(
    db, client_pg, make_incident, linked_incident, caplog,
):
    _, threat, _ = linked_incident()
    finops = make_incident(db, category=IncidentCategory.FINOPS, status=IncidentStatus.ANALYZING)
    # 현재 DB 제약은 이 연결을 막지 않는다. 공개 계약이 FINOPS에 위협을 붙이면 안 된다.
    finops.threat_event_id = threat.threat_event_id
    db.flush()
    with capture_selects(db) as statements:
        response = client_pg.get("/api/v1/incidents", params={"category": "FINOPS"})
    assert response.status_code == 200
    assert response.json()["items"][0]["threat_context"] is None
    assert len(statements) == 1
    assert client_pg.get(f"/api/v1/incidents/{finops.incident_id}").json()["threat_context"] is None
    assert not [r for r in caplog.records if r.name == "vigilantis.incidents"]


def test_database_failure_is_not_reported_as_missing_context(client_pg, linked_incident, monkeypatch):
    linked_incident()

    def unavailable(*args, **kwargs):
        raise OperationalError("SELECT", {}, RuntimeError("test database unavailable"))

    monkeypatch.setattr(incidents_repo, "get_threat_events_by_ids", unavailable)
    response = client_pg.get("/api/v1/incidents")
    assert response.status_code == 500
    assert "items" not in response.json()
