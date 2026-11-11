"""GuardDuty 탐지 → 위협 이벤트 · 인시던트 (#588 C) — 실제 PostgreSQL + 상태를 드는 GuardDuty 대역.

지키는 것
  - 이벤트는 전부 남기고 인시던트는 MEDIUM 이상만(Q3). 심각도 구간(Q2)은 security/tests 가 본다.
  - 같은 탐지가 갱신돼 다시 와도 한 번만 남는다(탐지 ID).
  - 커서: 처음은 INITIAL_LOOKBACK 만큼, 다음 회차는 커서부터. 저장에 실패한 탐지에서 멈춘다.
  - 자동 선차단 · 격리로 가지 않는다(Q2) — 접수 때 부르지 않고, 다시 거는 자리는 위협 유형 관문이 막는다.
  - 읽기 실패는 그 리전의 FAILED 로만 남는다.
"""

from __future__ import annotations

import importlib.util
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

import guardduty_ingress
import workflows
from db import models
from db.repositories import guardduty as guardduty_repo
from db.repositories import incidents as incidents_repo
from schemas.api.incidents import IncidentStatus, ResponseMode, RiskLevel
from schemas.events import ThreatEventType
from schemas.runbooks import TriggerSource
from security import soar
from services import guardduty

_FAKE_PATH = Path(__file__).resolve().parents[1] / "services" / "tests" / "test_guardduty.py"
_spec = importlib.util.spec_from_file_location("_guardduty_fake", _FAKE_PATH)
_fake_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_fake_module)
FakeGuardDuty = _fake_module.FakeGuardDuty

REGION = "ap-northeast-2"  # make_tenant 의 관제 리전


@pytest.fixture()
def world(db, make_tenant, monkeypatch):
    tenant_id = make_tenant()
    account = db.get(models.Tenant, tenant_id).aws_account_id
    fake = FakeGuardDuty({"d-1": "ENABLED"})
    monkeypatch.setattr(guardduty, "aws_client", lambda service, region: fake)
    # 접수 경로는 자동 격리를 부르지 않는다 — 부르면 여기서 깨진다
    monkeypatch.setattr(soar, "trigger_isolation", _never("trigger_isolation"))

    class World:
        pass

    w = World()
    w.db, w.tenant_id, w.account, w.fake = db, tenant_id, account, fake
    w.now = datetime.now(UTC).replace(microsecond=0)
    return w


def _never(name):
    def _raise(*_a, **_kw):
        raise AssertionError(f"{name} 를 부르면 안 된다")
    return _raise


def _finding(w, fid=None, *, severity=5.0, updated=None, instance=None):
    updated = updated or w.now - timedelta(minutes=5)
    stamp = updated.isoformat().replace("+00:00", "Z")
    fid = fid or uuid.uuid4().hex
    return {
        "Id": fid, "AccountId": w.account, "Region": REGION, "Severity": severity,
        "Type": "UnauthorizedAccess:EC2/SSHBruteForce", "UpdatedAt": stamp, "CreatedAt": stamp,
        "Arn": f"arn:aws:guardduty:{REGION}:{w.account}:detector/d-1/finding/{fid}",
        "Resource": {"ResourceType": "Instance",
                     "InstanceDetails": {"InstanceId": instance or f"i-{fid[:12]}"}},
        "Service": {"Archived": False, "EventFirstSeen": stamp, "EventLastSeen": stamp},
    }


def _ingest(w, *, now=None, publish=None):
    return guardduty_ingress.ingest_region(
        lambda: w.db, tenant_id=w.tenant_id, account_id=w.account, region=REGION,
        publish=publish, now=now or w.now,
    )


def _events(w):
    return list(w.db.scalars(
        select(models.ThreatEvent).where(
            models.ThreatEvent.tenant_id == w.tenant_id,
            models.ThreatEvent.event_type == ThreatEventType.GUARDDUTY_FINDING,
        )
    ))


def _incident_of(w, event):
    return incidents_repo.get_incident_by_threat_event_id(w.db, event.threat_event_id)


def test_every_finding_is_an_event_and_only_medium_and_above_become_incidents(world):
    w = world
    low, medium, high = _finding(w, "low", severity=2.0), _finding(w, "medium", severity=5.5), \
        _finding(w, "high", severity=8.0)
    w.fake.findings = [low, medium, high]
    published = []

    summary = _ingest(w, publish=published.append)

    assert summary == {"status": "OK", "read": 3, "events_created": 3, "incidents_created": 2,
                       "duplicates": 0, "rejected": 0, "failed": 0}
    by_id = {e.source_event_id: e for e in _events(w)}
    assert set(by_id) == {"low", "medium", "high"}
    assert all(e.tenant_id == w.tenant_id for e in by_id.values())
    assert _incident_of(w, by_id["low"]) is None  # LOW 는 이벤트만 — 인시던트를 만들지 않는다(Q3)

    m, h = _incident_of(w, by_id["medium"]), _incident_of(w, by_id["high"])
    assert (m.initial_risk_level, m.response_mode) == (RiskLevel.MEDIUM, ResponseMode.AGENT_WAIT)
    assert h.initial_risk_level is RiskLevel.HIGH
    for incident in (m, h):
        assert incident.tenant_id == w.tenant_id
        assert incident.title == "GuardDuty 탐지 · UnauthorizedAccess:EC2/SSHBruteForce"
        assert incident.subject_arn.startswith(f"arn:aws:ec2:{REGION}:{w.account}:instance/i-")
    # HIGH 알림은 다른 유형과 같이 남는다 — 알림은 자동 대응이 아니다
    kinds = w.db.scalars(select(models.NotificationEvent.kind).where(
        models.NotificationEvent.incident_id == h.incident_id)).all()
    assert list(kinds) == ["HIGH_INCIDENT_CREATED"]
    assert {e.data.incident_id for e in published} == {m.incident_id, h.incident_id}


def test_an_updated_finding_is_not_stored_twice(world):
    w = world
    w.fake.findings = [_finding(w, "f", severity=5.0)]
    _ingest(w)
    # 같은 ID 가 갱신돼 돌아온다(심각도 · 최근 관측이 바뀌어도) — 새 이벤트 · 인시던트를 만들지 않는다
    w.fake.findings = [_finding(w, "f", severity=8.5, updated=w.now - timedelta(minutes=1))]

    summary = _ingest(w)

    assert (summary["events_created"], summary["duplicates"]) == (0, 1)
    (event,) = _events(w)
    assert event.payload["severity"] == 5.0  # 첫 관측이 남는다
    assert _incident_of(w, event).initial_risk_level is RiskLevel.MEDIUM


def test_a_low_finding_that_comes_back_higher_does_not_attach_an_incident(world):
    # 접수 Intake 는 「인시던트 없는 이벤트」를 끊긴 저장으로 보고 인시던트를 붙인다 — 그 경로로 들어가지 않는다
    w = world
    w.fake.findings = [_finding(w, "f", severity=2.0)]
    _ingest(w)
    w.fake.findings = [_finding(w, "f", severity=8.0, updated=w.now - timedelta(minutes=1))]
    _ingest(w)
    (event,) = _events(w)
    assert _incident_of(w, event) is None


def test_first_read_looks_back_one_day_and_the_cursor_moves_to_the_latest_update(world):
    w = world
    old = _finding(w, "old", updated=w.now - guardduty_ingress.INITIAL_LOOKBACK - timedelta(minutes=1))
    recent = _finding(w, "recent", updated=w.now - timedelta(hours=1))
    latest = _finding(w, "latest", updated=w.now - timedelta(minutes=2))
    w.fake.findings = [latest, old, recent]

    summary = _ingest(w)

    assert summary["read"] == 2  # 하루보다 오래된 탐지는 읽지 않는다
    assert {e.source_event_id for e in _events(w)} == {"recent", "latest"}
    assert guardduty_repo.get_cursor(w.db, tenant_id=w.tenant_id, region=REGION) == w.now - timedelta(minutes=2)

    newer = _finding(w, "newer", updated=w.now + timedelta(minutes=3))
    w.fake.findings.append(newer)
    summary = _ingest(w, now=w.now + timedelta(minutes=5))
    # 다음 회차는 커서부터다 — 커서와 같은 시각의 탐지는 다시 읽고 중복으로 접는다
    assert (summary["read"], summary["events_created"], summary["duplicates"]) == (2, 1, 1)
    assert guardduty_repo.get_cursor(w.db, tenant_id=w.tenant_id, region=REGION) == w.now + timedelta(minutes=3)


def test_a_failed_finding_holds_the_cursor_and_is_retried_next_round(world, monkeypatch):
    w = world
    first = _finding(w, "first", severity=5.0, updated=w.now - timedelta(minutes=30))
    broken = _finding(w, "broken", severity=5.0, updated=w.now - timedelta(minutes=20))
    after = _finding(w, "after", severity=5.0, updated=w.now - timedelta(minutes=10))
    w.fake.findings = [first, broken, after]
    real = guardduty_ingress.create_incident_from_intake

    def flaky(db, intake):
        if intake.threat_event.source_event_id == "broken":
            raise RuntimeError("저장 실패")
        return real(db, intake)

    monkeypatch.setattr(guardduty_ingress, "create_incident_from_intake", flaky)
    summary = _ingest(w)

    assert (summary["incidents_created"], summary["failed"]) == (2, 1)  # 한 건의 실패가 뒤의 탐지를 막지 않는다
    assert guardduty_repo.get_cursor(w.db, tenant_id=w.tenant_id, region=REGION) == w.now - timedelta(minutes=20)

    monkeypatch.setattr(guardduty_ingress, "create_incident_from_intake", real)
    summary = _ingest(w)
    assert (summary["incidents_created"], summary["duplicates"], summary["failed"]) == (1, 1, 0)
    assert {e.source_event_id for e in _events(w)} == {"first", "broken", "after"}
    assert guardduty_repo.get_cursor(w.db, tenant_id=w.tenant_id, region=REGION) == w.now - timedelta(minutes=10)


def test_a_rejected_finding_does_not_hold_the_cursor(world):
    w = world
    bad = _finding(w, "bad", updated=w.now - timedelta(minutes=10))
    bad["Severity"] = None
    w.fake.findings = [bad, _finding(w, "good", updated=w.now - timedelta(minutes=5))]
    summary = _ingest(w)
    assert (summary["rejected"], summary["events_created"]) == (1, 1)
    # 다시 읽어도 같은 이유로 거절된다 — 그 자리에 멈추면 뒤의 탐지를 영영 못 읽는다
    assert guardduty_repo.get_cursor(w.db, tenant_id=w.tenant_id, region=REGION) == w.now - timedelta(minutes=5)


def test_no_detector_and_refusals_leave_no_cursor(world):
    w = world
    w.fake.detectors = {}
    assert _ingest(w) == {"status": "NO_DETECTOR"}
    w.fake.detectors = {"d-1": "ENABLED"}
    w.fake.deny["ListFindings"] = "AccessDeniedException"  # 템플릿을 갱신하지 않은 고객 역할
    assert _ingest(w) == {"status": "FAILED", "error": "AccessDeniedException"}
    assert guardduty_repo.get_cursor(w.db, tenant_id=w.tenant_id, region=REGION) is None


# --- 자동 격리 관문(#588 Q2) ----------------------------------------------------------------------------------
def _guardduty_incident(w, severity):
    w.fake.findings = [_finding(w, severity=severity, instance=f"i-{uuid.uuid4().hex[:12]}")]
    _ingest(w)
    (event,) = [e for e in _events(w) if e.payload["severity"] == severity]
    return _incident_of(w, event)


def test_high_guardduty_incident_is_excluded_before_any_aws_call(world, monkeypatch):
    w = world
    incident = _guardduty_incident(w, 8.0)
    assert (incident.initial_risk_level, incident.response_mode, incident.status) == (
        RiskLevel.HIGH, ResponseMode.PRE_MITIGATION_0_5S, IncidentStatus.ANALYZING)  # 선차단 조건은 모두 맞는다
    assert workflows.auto_isolation_excluded(w.db, incident)
    monkeypatch.setattr(soar.guardrails, "run_guardrail_validation", _never("가드레일(④ AWS Dry-Run)"))
    monkeypatch.setattr(soar.workflows, "resolve_isolation_parameters", _never("격리 파라미터 공급"))
    assert soar.prepare_isolation(w.db, incident.incident_id, TriggerSource.PRE_MITIGATION_0_5S) is None


def test_the_locked_gate_refuses_by_threat_type(world):
    w = world
    incident = _guardduty_incident(w, 8.0)
    reason = workflows._auto_isolation_skip_reason(
        w.db, incident, TriggerSource.PRE_MITIGATION_0_5S, datetime.now(UTC))
    assert reason is workflows.AutoIsolationSkipReason.THREAT_TYPE_NOT_ELIGIBLE


def test_the_screen_shows_no_pending_automatic_block(world):
    w = world
    incident = _guardduty_incident(w, 8.0)
    tenant = w.db.get(models.Tenant, w.tenant_id)
    tenant.auto_action_enabled = False  # 다른 유형이면 「자동 조치 꺼짐으로 막힘」이 보인다
    w.db.flush()
    assert workflows.pending_automatic_block(w.db, incident) is None  # 걸릴 자동 발동 자체가 없다


def test_expired_medium_guardduty_wait_is_not_scanned(world):
    w = world
    incident = _guardduty_incident(w, 5.0)
    incident.status = IncidentStatus.AWAITING_APPROVAL
    incident.agent_wait_started_at = datetime.now(UTC) - timedelta(minutes=2)
    incident.response_deadline_at = incident.agent_wait_started_at + timedelta(seconds=60)
    w.db.flush()
    now = datetime.now(UTC)
    assert incident.incident_id in incidents_repo.list_expired_agent_waits(w.db, now=now)  # 관문 없이는 잡힌다
    assert incident.incident_id not in incidents_repo.list_expired_agent_waits(
        w.db, now=now, exclude_event_types=workflows.AUTO_ISOLATION_EXCLUDED_EVENT_TYPES)
    reason = workflows._auto_isolation_skip_reason(w.db, incident, TriggerSource.TIMEOUT_ISOLATION_1M, now)
    assert reason is workflows.AutoIsolationSkipReason.THREAT_TYPE_NOT_ELIGIBLE


def test_other_threat_types_are_not_excluded(world, db):
    assert ThreatEventType.SSH_BRUTE_FORCE not in workflows.AUTO_ISOLATION_EXCLUDED_EVENT_TYPES
    assert ThreatEventType.OPEN_IP not in workflows.AUTO_ISOLATION_EXCLUDED_EVENT_TYPES


# --- 조회 계약 ----------------------------------------------------------------------------------------------
def test_threat_events_and_incident_detail_carry_the_finding(world, client_pg, monkeypatch):
    w = world
    monkeypatch.setattr("routers.assets._configured_regions", lambda: [REGION])
    monkeypatch.setattr("routers.threat_events._now", lambda: w.now)
    w.fake.findings = [_finding(w, "low", severity=2.0), _finding(w, "high", severity=8.0)]
    _ingest(w)

    body = client_pg.get("/api/v1/threat-events", params={"days": 1}).json()
    mine = {i["payload"]["finding_id"]: i for i in body["items"] if i["event_type"] == "GUARDDUTY_FINDING"}
    assert {"low", "high"} <= set(mine)  # LOW 도 목록에 있다(Q3)
    assert mine["high"]["payload"]["severity"] == 8.0

    (event,) = [e for e in _events(w) if e.source_event_id == "high"]
    incident = _incident_of(w, event)
    detail = client_pg.get(f"/api/v1/incidents/{incident.incident_id}")
    assert detail.status_code == 200, detail.text
    assert detail.json()["threat_context"] is None  # GuardDuty 탐지에는 출발지 · 노출 대역 칸이 없다
    assert detail.json()["auto_isolation_block"] is None
