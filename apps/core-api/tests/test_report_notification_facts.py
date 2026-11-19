"""#667 계약으로 만든 기록을 #623이 인시던트·고객사 범위 안에서만 읽는다."""

from datetime import UTC, datetime, timedelta

import pytest
from ai.model_client import FakeAIModelClient
from ai.report_draft import get_report_model_client
from db import models
from db.repositories import platform_events
from schemas.api.incidents import RiskLevel
from schemas.api.report_draft import ReportNarrative, ReportSentence
from schemas.platform_events import PlatformEventAction, PlatformEventOutcome


def _notification(db, incident):
    row = models.NotificationEvent(
        incident_id=incident.incident_id, kind="HIGH_INCIDENT_CREATED", phase="CREATED",
        target_arn=incident.subject_arn, risk_level=RiskLevel.HIGH,
        occurred_at=datetime.now(UTC) - timedelta(minutes=10),
    )
    db.add(row)
    db.flush()
    return row


def _record(
    db, notification, tenant_id, *, outcome=PlatformEventOutcome.SUCCEEDED,
    at=None, reason_code="SLACK:CHANNEL_UNAVAILABLE",
):
    return platform_events.record_platform_event(
        db, PlatformEventAction.NOTIFICATION_SEND, platform_events.SYSTEM,
        tenant_id=tenant_id, target_id=str(notification.event_id), outcome=outcome,
        occurred_at=at or datetime.now(UTC) - timedelta(minutes=1),
        details={"reason_code": reason_code} if outcome is PlatformEventOutcome.FAILED else {},
    )


def _download(client_pg, incident_id):
    fake = FakeAIModelClient([ReportNarrative(sentences=[ReportSentence(
        section="occurrence", text="저장된 기록이 있습니다.", citations=[f"{incident_id}:F1"],
    )])])
    client_pg.app.dependency_overrides[get_report_model_client] = lambda: fake
    response = client_pg.post(
        f"/api/v1/incidents/{incident_id}/report-draft", params={"format": "json"},
    )
    assert response.status_code == 200, response.text
    return response, fake


@pytest.mark.parametrize("outcome", [PlatformEventOutcome.SUCCEEDED, PlatformEventOutcome.FAILED])
def test_notification_facts_keep_aggregate_result_provenance_and_scope(
    db, client_pg, make_incident, make_tenant, outcome,
):
    tenant, other_tenant = make_tenant(), make_tenant()
    incident, other = make_incident(db), make_incident(db)
    incident.tenant_id = other.tenant_id = tenant
    db.flush()
    notification, unrelated = _notification(db, incident), _notification(db, other)
    accepted = _record(db, notification, tenant, outcome=outcome)
    hidden = [
        _record(db, notification, other_tenant),
        _record(db, notification, None),
        _record(db, unrelated, tenant),
        _record(db, notification, tenant, at=datetime.now(UTC) + timedelta(days=1)),
    ]
    expected_id, notification_id, recorded_at = accepted.event_id, notification.event_id, accepted.occurred_at
    hidden_ids = {row.event_id for row in hidden}
    incident_id = incident.incident_id
    db.commit()

    response, fake = _download(client_pg, incident_id)

    facts = [fact for fact in response.json()["server_facts"]["facts"] if fact["source_kind"] == "platform_event"]
    assert {fact["record_id"] for fact in facts} == {expected_id}
    assert not hidden_ids.intersection(fact["record_id"] for fact in facts)
    indexed = {fact["json_path"]: fact for fact in facts}
    assert indexed["/target_id"]["value"] == str(notification_id)
    assert indexed["/outcome"]["value"] == outcome.value
    assert datetime.fromisoformat(indexed["/outcome"]["recorded_at"]) == recorded_at.replace(microsecond=0)
    if outcome is PlatformEventOutcome.FAILED:
        assert indexed["/details/reason_code"]["value"] == "SLACK:CHANNEL_UNAVAILABLE"
    else:
        assert "/details/reason_code" not in indexed
    sent_facts = fake.sent[0]["user_payload"]["facts"]
    assert any(fact["record_id"] == expected_id for fact in sent_facts)
    assert all(fact["record_id"] not in hidden_ids for fact in sent_facts)
    assert "수신자의 열람" in response.json()["notice"]


def test_unknown_delivery_result_does_not_claim_non_delivery(
    db, client_pg, make_incident, make_tenant,
):
    incident = make_incident(db)
    incident.tenant_id = make_tenant()
    db.flush()
    row = _record(
        db, _notification(db, incident), incident.tenant_id,
        outcome=PlatformEventOutcome.FAILED,
        reason_code="SNS:EXHAUSTED:UNKNOWN",
    )
    record_id, incident_id = row.event_id, incident.incident_id
    db.commit()

    response, fake = _download(client_pg, incident_id)

    for facts in (
        fake.sent[0]["user_payload"]["facts"],
        response.json()["server_facts"]["facts"],
    ):
        indexed = {fact["json_path"]: fact for fact in facts if fact["record_id"] == record_id}
        assert indexed["/outcome"]["value"] == "FAILED"
        assert indexed["/details/reason_code"]["value"] == "SNS:EXHAUSTED:UNKNOWN"
        meaning = indexed["/outcome"]["interpretation"]
        assert "공급자 접수 미확인" in meaning
        assert "미전달을 뜻하지 않음" in meaning

    saved = client_pg.get(
        f"/api/v1/incidents/{incident_id}/report-draft", params={"format": "md"},
    )
    assert saved.status_code == 200
    assert "SNS:EXHAUSTED:UNKNOWN" in saved.text
    assert "공급자 접수 미확인" in saved.text
    assert "미전달을 뜻하지 않음" in saved.text


@pytest.mark.parametrize("quarantined", [False, True])
def test_missing_visible_notification_log_remains_unknown(
    db, client_pg, make_incident, make_tenant, quarantined,
):
    incident = make_incident(db)
    incident.tenant_id = make_tenant()
    db.flush()
    notification = _notification(db, incident)
    if quarantined:
        _record(db, notification, None)
    incident_id = incident.incident_id
    db.commit()

    response, _ = _download(client_pg, incident_id)

    facts = [fact for fact in response.json()["server_facts"]["facts"] if fact["source_kind"] == "platform_event"]
    assert len(facts) == 1
    assert facts[0]["status"] == "unknown" and facts[0]["value"] is None
    assert facts[0]["reason"] == "not_recorded"
