"""#322의 소유 경계: 실제 접수·DB/API, 파일 배달·재시도, 앱 수명주기.

Risk Evaluator 전 분기와 중복 키 계산은 해당 모듈 테스트가 소유한다.
여기서는 골든 대표 2건으로 그 판정이 저장·조회·이벤트까지 보존되는지 확인한다.
"""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import sys
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from sqlalchemy import delete, select
from sqlalchemy.orm import sessionmaker

import incident_intake
import mock_threat_source
import threat_ingress
from db import mappers, models
from db.repositories import incidents as incidents_repo
from incident_intake import IntakeOutcome
from mock_threat_source import (
    MAX_OBSERVATION_BYTES,
    MockThreatConsumer,
    parse_observation,
    prepare_observation,
)
from schemas.api.incidents import IncidentResponse, IncidentStatus, RiskLevel
from schemas.api.ws import WsEventType
from schemas.events import ThreatEventType
from schemas.incidents import AgentInvocationStatus

ROOT = Path(__file__).resolve().parents[3]
GOLDEN = ROOT / "datasets/golden/secops"
SCRIPT = ROOT / "scripts/inject_mock_threat.py"


def _raw(name="evt_ssh_bruteforce_001"):
    raw = json.loads((GOLDEN / "input" / f"{name}.json").read_text(encoding="utf-8"))
    raw["event_id"] = f"test322-{uuid.uuid4().hex}"
    # 독립 관측으로 만들어 다른 테스트의 commit과 중복되지 않게 한다.
    raw["occurred_at"] = datetime.now(timezone.utc).isoformat()
    return raw


@pytest.mark.parametrize(
    ("name", "title"),
    [
        ("evt_open_ip_001", "보안 그룹 인그레스 전체 개방"),
        ("evt_ssh_bruteforce_001", "SSH 브루트포스 시도"),
    ],
)
def test_real_workflow_preserves_judgement_evidence_and_read_api(db, client_pg, name, title):
    raw = _raw(name)
    expected = json.loads((GOLDEN / "expected" / f"{name}.json").read_text(encoding="utf-8"))
    events = []
    result = threat_ingress.receive_threat(db, parse_observation(raw), events.append)

    assert result.created
    row = incidents_repo.get_incident(db, result.incident_id)
    assert row.title == title
    assert row.initial_risk_level.value == expected["initial_risk_level"]
    assert row.response_mode.value == expected["response_mode"]
    assert sorted(row.initial_risk_reason_codes) == sorted(expected["reason_codes"])
    assert row.status == IncidentStatus.ANALYZING
    assert row.agent_invocation_status == AgentInvocationStatus.PENDING
    assert row.reviewed_risk_level is None
    assert (result.incident_id, row.category) in incidents_repo.list_pending_agent_analysis(db)

    evidence = mappers.to_evidence_item(incidents_repo.list_evidence(db, result.incident_id)[0])
    stored_event = evidence.content.event
    assert stored_event.source_event_id == raw["event_id"]
    assert stored_event.target_arn == raw["target_arn"]
    assert stored_event.threat_event_id == row.threat_event_id
    assert stored_event.event_type == ThreatEventType(raw["event_type"])
    assert uuid.UUID(row.threat_event_id)

    response = client_pg.get(f"/api/v1/incidents/{result.incident_id}")
    assert response.status_code == 200
    dto = IncidentResponse.model_validate(response.json())
    assert dto.title == title
    assert dto.initial_risk_level == row.initial_risk_level
    assert dto.response_mode == row.response_mode
    assert dto.evidence_ids == [evidence.evidence_id]
    assert dto.summary_lines == dto.recommendations == dto.executions == []
    assert len(events) == 1
    assert events[0].event_type == WsEventType.INCIDENT_CREATED
    assert events[0].data.incident_id == result.incident_id
    assert events[0].occurred_at == result.occurred_at


def test_redelivery_preserves_analysis_but_new_observation_creates_incident(db, client_pg):
    raw = _raw()
    events = []
    first = threat_ingress.receive_threat(db, parse_observation(raw), events.append)
    row = incidents_repo.get_incident(db, first.incident_id)
    row.status = IncidentStatus.FAILED
    row.agent_invocation_status = AgentInvocationStatus.NO_PROPOSAL
    row.summary_lines = ["관측", "진단", "제안 없음"]
    row.reviewed_risk_level = RiskLevel.LOW
    db.commit()
    original = client_pg.get(f"/api/v1/incidents/{first.incident_id}").json()

    raw["event_id"] = f"test322-{uuid.uuid4().hex}"  # 배달 식별자가 바뀌어도 동일 관측이다.
    duplicate = threat_ingress.receive_threat(db, parse_observation(raw), events.append)
    assert not duplicate.created
    assert duplicate.incident_id == first.incident_id
    assert client_pg.get(f"/api/v1/incidents/{first.incident_id}").json() == original
    assert len(events) == 1

    observed = datetime.fromisoformat(raw["occurred_at"]) + timedelta(minutes=1)
    raw["occurred_at"] = observed.isoformat()
    newer = threat_ingress.receive_threat(db, parse_observation(raw), events.append)
    assert newer.created and newer.incident_id != first.incident_id
    assert len(events) == 2


def test_storage_failure_rolls_back_partial_rows_and_emits_nothing(db, monkeypatch):
    raw = _raw()
    add = incident_intake._add_evidence

    def fail_evidence(*args, **kwargs):
        raise RuntimeError("evidence write failed")

    monkeypatch.setattr(incident_intake, "_add_evidence", fail_evidence)
    events = []
    with pytest.raises(RuntimeError, match="evidence write failed"):
        threat_ingress.receive_threat(db, parse_observation(raw), events.append)
    assert not list(db.scalars(select(models.ThreatEvent)))
    assert not list(db.scalars(select(models.Incident)))
    assert not list(db.scalars(select(models.Evidence)))
    assert events == []

    monkeypatch.setattr(incident_intake, "_add_evidence", add)
    assert threat_ingress.receive_threat(db, parse_observation(raw), events.append).created
    assert len(events) == 1


def test_publish_failure_leaves_committed_result_and_is_not_republished(db, caplog):
    raw = parse_observation(_raw())

    def fail(event):
        raise RuntimeError("notification failed")

    with caplog.at_level(logging.ERROR):
        result = threat_ingress.receive_threat(db, raw, fail)
    assert incidents_repo.get_incident(db, result.incident_id) is not None
    assert "threat_incident_publish_failed" in caplog.text
    events = []
    replay = threat_ingress.receive_threat(db, raw, events.append)
    assert not replay.created and replay.incident_id == result.incident_id
    assert not events


def test_non_world_open_ip_is_rejected_before_storage(db):
    raw = _raw("evt_open_ip_001")
    raw["source_cidr"] = "192.0.2.0/24"
    with pytest.raises(threat_ingress.ThreatInputRejected):
        threat_ingress.receive_threat(db, parse_observation(raw))
    assert not list(db.scalars(select(models.ThreatEvent)))


def test_inbox_retries_failed_delivery_and_continues_other_files(tmp_path, monkeypatch):
    first = prepare_observation(tmp_path, parse_observation(_raw()))
    second = prepare_observation(tmp_path, parse_observation(_raw()))
    (tmp_path / "invalid.json").write_text('{"initial_risk_level":"HIGH"}', encoding="utf-8")
    (tmp_path / ".incomplete.tmp").write_text("{", encoding="utf-8")
    failing = json.loads(first.read_text())["event_id"]
    calls = []

    def receive(db, observation, publish):
        calls.append(observation.event_id)
        if observation.event_id == failing:
            raise RuntimeError("storage unavailable")
        return IntakeOutcome("stored", True, datetime.now(timezone.utc))

    monkeypatch.setattr(mock_threat_source, "receive_threat", receive)
    consumer = MockThreatConsumer(tmp_path, MagicMock(), MagicMock(), interval_seconds=1)
    report = consumer.consume_once()
    assert report == {"created": 1, "existing": 0, "rejected": 1, "failed": 1}
    assert first.exists() and not second.exists()
    assert len(list((tmp_path / "done").glob("*.json"))) == 1
    assert len(list((tmp_path / "rejected").glob("*.json"))) == 1
    assert len(calls) == 2
    failing = None
    assert consumer.consume_once()["created"] == 1
    assert not first.exists()
    assert (tmp_path / ".incomplete.tmp").exists()


def test_oversize_input_is_rejected_without_opening_a_db_session(tmp_path):
    (tmp_path / "large.json").write_bytes(b" " * (MAX_OBSERVATION_BYTES + 1))
    sessions = MagicMock()
    consumer = MockThreatConsumer(tmp_path, sessions, MagicMock(), interval_seconds=1)
    assert consumer.consume_once()["rejected"] == 1
    sessions.assert_not_called()


def test_stop_waits_for_inflight_delivery_and_leaves_queued_file(tmp_path, monkeypatch):
    for _ in range(2):
        prepare_observation(tmp_path, parse_observation(_raw()))
    entered = threading.Event()
    release = threading.Event()

    def receive(db, observation, publish):
        entered.set()
        assert release.wait(5)
        return IntakeOutcome("stored", True, datetime.now(timezone.utc))

    monkeypatch.setattr(mock_threat_source, "receive_threat", receive)

    async def scenario():
        consumer = MockThreatConsumer(tmp_path, MagicMock(), MagicMock(), interval_seconds=60)
        consumer.start()
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            stopping = asyncio.create_task(consumer.stop())
            await asyncio.sleep(0)  # 종료 요청을 실행시키고 진행 중인 작업을 관찰한다.
            assert not stopping.done()
            release.set()
            await asyncio.wait_for(stopping, timeout=5)
            assert len(list(tmp_path.glob("*.json"))) == 1
            assert len(list((tmp_path / "done").glob("*.json"))) == 1
        finally:
            release.set()
            await consumer.stop()

    asyncio.run(scenario())


def test_prepare_cli_preserves_golden_and_only_prepares_observation(tmp_path):
    source = GOLDEN / "input/evt_ssh_bruteforce_001.json"
    before = source.read_bytes()
    target = "arn:aws:ec2:ap-northeast-2:000000000000:instance/i-0123456789abcdef0"
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "evt_ssh_bruteforce_001",
         "--prepare-inbox", str(tmp_path), "--target-arn", target,
         "--occurred-at", "2026-09-11T00:00:00Z"],
        capture_output=True, text=True, encoding="utf-8", timeout=20,
    )
    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stdout)
    assert result["status"] == "PREPARED"
    event = parse_observation(json.loads(Path(result["path"]).read_text()))
    assert event.target_arn == target
    assert event.failed_attempt_count == 120 and event.window_seconds == 300
    assert event.occurred_at == datetime(2026, 9, 11, tzinfo=timezone.utc)
    assert not list(tmp_path.glob("*.tmp"))
    assert source.read_bytes() == before


@pytest.fixture
def committed_sessions(pg_engine):
    sessions = sessionmaker(bind=pg_engine, autoflush=False, expire_on_commit=False)
    yield sessions
    # pg_engine은 Alembic으로 생성한 일회용 테스트 DB다. 독립 commit 검증분만 정리한다.
    with sessions() as db:
        threats = list(db.scalars(select(models.ThreatEvent.threat_event_id).where(
            models.ThreatEvent.source_event_id.like("test322-%"),
        )))
        ids = select(models.Incident.incident_id).where(models.Incident.threat_event_id.in_(threats))
        db.execute(delete(models.Evidence).where(models.Evidence.incident_id.in_(ids)))
        db.execute(delete(models.Incident).where(models.Incident.threat_event_id.in_(threats)))
        db.execute(delete(models.ThreatEvent).where(models.ThreatEvent.threat_event_id.in_(threats)))
        db.commit()


def test_app_consumes_s3_and_publishes_websocket_after_commit(
    committed_sessions, tmp_path, monkeypatch,
):
    from fastapi.testclient import TestClient

    import main
    from config import get_settings
    from db.session import get_db
    from realtime import RealtimeManager

    monkeypatch.setenv("MOCK_THREAT_INBOX_DIR", str(tmp_path))
    monkeypatch.setenv("MOCK_THREAT_POLL_SECONDS", "0.01")
    get_settings.cache_clear()
    monkeypatch.setattr(main, "get_session_factory", lambda: committed_sessions)
    published = threading.Event()
    original_publish = RealtimeManager.publish
    observed = []

    def publish_after_commit(manager, event):
        # 소비 스레드와 다른 세션에서 이미 조회되어야 진짜 commit 이후다.
        with committed_sessions() as db:
            row = incidents_repo.get_incident(db, event.data.incident_id)
            assert row is not None
            assert row.agent_invocation_status == AgentInvocationStatus.PENDING
        original_publish(manager, event)
        observed.append(event)
        published.set()

    monkeypatch.setattr(RealtimeManager, "publish", publish_after_commit)
    app = main.create_app()

    def get_test_db():
        with committed_sessions() as db:
            yield db

    app.dependency_overrides[get_db] = get_test_db
    try:
        with TestClient(app) as client:
            with client.websocket_connect("/api/v1/ws") as ws:
                prepare_observation(tmp_path, parse_observation(_raw()))
                assert published.wait(5), "앱 소비·commit·발행이 실행되지 않았다"
                # 발행 확인 후에도 실제 WebSocket 전송과 조회 계약을 대조한다.
                message = ws.receive_json()
                assert message["event_type"] == "INCIDENT_CREATED"
                incident_id = message["data"]["incident_id"]
                response = client.get(f"/api/v1/incidents/{incident_id}")
                assert response.status_code == 200
                dto = IncidentResponse.model_validate(response.json())
                assert dto.status == IncidentStatus.ANALYZING
                assert dto.initial_risk_level == RiskLevel.HIGH
                assert dto.title == "SSH 브루트포스 시도"
                assert observed[0].data.incident_id == dto.incident_id
    finally:
        get_settings.cache_clear()
