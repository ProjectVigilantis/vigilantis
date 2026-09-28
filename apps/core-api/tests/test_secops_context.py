"""MVP 모의 근거 → PostgreSQL → 저장된 그래프 입력을 검증한다. 실제 모델·AWS 호출은 없다."""

from __future__ import annotations

import json
import sys
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from agent_dispatcher import GraphInputUnavailable, build_graph_input
from ai.agent import (
    CandidateProposalOutput,
    EvidenceSummaryOutput,
    RiskReassessmentOutput,
    _secops_payload,
    run_secops_graph,
)
from ai.model_client import FakeAIModelClient
from db import mappers, models
from db.repositories import assets as assets_repo
from db.repositories import incidents as incidents_repo
from logging_config import JsonLineFormatter
from mock_threat_source import MockThreatConsumer, parse_submission
from pydantic import ValidationError
from schemas.api.assets import AssetType, RelationType
from schemas.api.incidents import RiskLevel
from sqlalchemy import select
from threat_ingress import receive_threat

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))
from secops_log_corpus import prepare_case

ACCOUNT, REGION = "123456789012", "ap-northeast-2"
PREFIX = f"arn:aws:ec2:{REGION}:{ACCOUNT}:"
TARGET = PREFIX + "instance/i-0a1b2c3d4e5f00001"
NACL = PREFIX + "network-acl/acl-00000000000000001"
SG = PREFIX + "security-group/sg-00000000000000001"


def seed(db, *, relations=True):
    run = assets_repo.start_collection_run(db, account_id=ACCOUNT, region=REGION,
                                          mode="localstack", lookback_days=3, period_seconds=3600)
    for arn, kind, spec in (
        (TARGET, AssetType.EC2, {"instance_type": "t3.small", "subnet_id": "subnet-fixture"}),
        (NACL, AssetType.NACL, {"is_default": False, "associated_subnet_ids": ["subnet-fixture"]}),
        (SG, AssetType.SG, {"attached": True, "open_to_world": []}),
    ):
        assets_repo.upsert_asset(db, arn=arn, asset_type=kind, resource_id=arn.split("/")[-1],
                                 account_id=ACCOUNT, region=REGION, spec=spec,
                                 collection_run_id=run.collection_run_id,
                                 collected_at=datetime.now(UTC), state="running" if kind is AssetType.EC2 else None)
    target = assets_repo.get_asset_by_arn(db, TARGET)
    assets_repo.replace_relationships(db, target.asset_id,
                                     [(RelationType.PROTECTED_BY, NACL), (RelationType.SECURED_BY, SG)] if relations else [],
                                     collection_run_id=run.collection_run_id)
    db.commit()
    return run


def submission(tmp_path, case="C01"):
    path = prepare_case(case, tmp_path, TARGET)
    return path, parse_submission(json.loads(path.read_text(encoding="utf-8")))


def saved(db, incident_id):
    return mappers.to_evidence_item(incidents_repo.list_evidence(db, incident_id)[0]).content


def test_secops_context_inbox_freezes_logs_inventory_and_actual_model_payload(db, client_pg, tmp_path):
    seed(db)
    path, (observation, logs) = submission(tmp_path)
    published = []
    consumer = MockThreatConsumer(tmp_path, lambda: nullcontext(db), published.append, interval_seconds=1)
    assert consumer.consume_once() == {"created": 1, "existing": 0, "rejected": 0, "failed": 0}
    assert not path.exists()
    incident_id = published[0].data.incident_id
    evidence = saved(db, incident_id)
    assert evidence.context.log_evidence == logs
    assert len(logs.records) == 12 and logs.record_count == 200
    assert logs.failed_attempt_count == 120 and logs.accepted_count == 0
    assert len(evidence.context.related_assets) == 2
    before = build_graph_input(db, incident_id)
    payload = _secops_payload(before)
    assert payload["asset_context_at"] == "incident_intake"
    assert "target" not in payload["evidences"][0]["content"]["context"]
    assert evidence.context.target is not None  # 모델 입력 투영이 저장된 근거 객체를 바꾸지 않았다.

    row = assets_repo.get_asset_by_arn(db, TARGET)
    row.spec = {"instance_type": "t3.large"}
    row.collected_at = datetime.now(UTC) + timedelta(seconds=1)
    assets_repo.replace_relationships(db, row.asset_id, [], collection_run_id=row.last_collection_run_id)
    assets_repo.get_asset_by_arn(db, NACL).absent_since = datetime.now(UTC)
    db.commit()
    assert build_graph_input(db, incident_id) == before
    duplicate = receive_threat(db, observation, log_evidence=logs)
    assert not duplicate.created and duplicate.incident_id == incident_id
    assert saved(db, incident_id) == evidence

    client = FakeAIModelClient([
        RiskReassessmentOutput(reviewed_risk_level=RiskLevel.HIGH),
        CandidateProposalOutput(candidates=[]),
        EvidenceSummaryOutput(observation="합성 fixture 관측", diagnosis="SSH 실패 집계", rationale="평가용"),
    ])
    run_secops_graph(build_graph_input(db, incident_id), client=client)
    assert len(client.sent) == 3
    for request in client.sent:
        assert request["user_payload"]["asset"] == payload["asset"]
        assert request["user_payload"]["evidences"] == payload["evidences"]
    result = client_pg.get(f"/api/v1/incidents/{incident_id}")
    assert result.status_code == 200
    assert result.json()["initial_risk_level"] == "HIGH"
    assert len(result.json()["evidence_ids"]) == 1


def test_secops_context_missing_target_is_not_backfilled_on_later_collection(db, client_pg, tmp_path):
    _, (observation, logs) = submission(tmp_path)
    outcome = receive_threat(db, observation, log_evidence=logs)
    evidence = saved(db, outcome.incident_id)
    assert evidence.context.target_status == "not_collected"
    assert evidence.context.target is None and evidence.context.log_evidence is not None
    seed(db)
    with pytest.raises(GraphInputUnavailable, match="not_collected"):
        build_graph_input(db, outcome.incident_id)
    assert saved(db, outcome.incident_id) == evidence
    assert client_pg.get(f"/api/v1/incidents/{outcome.incident_id}").json()["initial_risk_level"] == "HIGH"


def test_secops_context_legacy_evidence_is_readable_but_not_rebuilt_from_latest_assets(db, client_pg, tmp_path):
    seed(db)
    _, (observation, _) = submission(tmp_path)
    result = receive_threat(db, observation)
    row = incidents_repo.list_evidence(db, result.incident_id)[0]
    row.content = {"event": row.content["event"]}
    db.commit()
    assert saved(db, result.incident_id).context is None
    assert client_pg.get(f"/api/v1/incidents/{result.incident_id}").status_code == 200
    with pytest.raises(GraphInputUnavailable, match="기존 Incident"):
        build_graph_input(db, result.incident_id)


@pytest.mark.parametrize("problem", ["not_collected", "absent", "missing_collection", "type_or_scope_mismatch", "relation_run_mismatch"])
def test_secops_context_invalid_related_observations_are_recorded_not_offered(db, tmp_path, problem):
    seed(db)
    row = assets_repo.get_asset_by_arn(db, NACL)
    target = assets_repo.get_asset_by_arn(db, TARGET)
    if problem == "not_collected":
        db.delete(row)
    elif problem == "absent":
        row.absent_since = datetime.now(UTC)
    elif problem == "missing_collection":
        row.last_collection_run_id = None
    elif problem == "type_or_scope_mismatch":
        row.region = "us-east-1"
    else:
        for relation in assets_repo.list_relationships_by_source(db, target.asset_id):
            if relation.target_arn == NACL:
                relation.collection_run_id = None
    db.commit()
    _, (observation, logs) = submission(tmp_path)
    result = receive_threat(db, observation, log_evidence=logs)
    context = saved(db, result.incident_id).context
    assert context.target_status == "available"
    assert [r.asset.arn for r in context.related_assets] == [SG]
    assert [(r.target_arn, r.reason) for r in context.relation_issues] == [(NACL, problem)]
    with pytest.raises(GraphInputUnavailable, match="조치"):
        build_graph_input(db, result.incident_id)
    assert incidents_repo.get_incident(db, result.incident_id).initial_risk_level is RiskLevel.HIGH


def test_secops_context_failure_rolls_back_event_and_incident(db, tmp_path, monkeypatch):
    import incident_intake

    _, (observation, logs) = submission(tmp_path)
    def fail(*args):
        raise RuntimeError("snapshot storage unavailable")
    monkeypatch.setattr(incident_intake, "capture_secops_context", fail)
    with pytest.raises(RuntimeError, match="snapshot storage unavailable"):
        receive_threat(db, observation, log_evidence=logs)
    assert not list(db.scalars(select(models.Incident)))
    assert not list(db.scalars(select(models.ThreatEvent)))
    assert not list(db.scalars(select(models.Evidence)))


@pytest.mark.parametrize("asset_arn", [TARGET, NACL])
def test_secops_context_invalid_stored_asset_retries_after_correction(db, client_pg, tmp_path, caplog, asset_arn):
    seed(db)
    asset = assets_repo.get_asset_by_arn(db, asset_arn)
    original_spec = dict(asset.spec)
    asset.spec = {**original_spec, "platform_details": "Linux/UNIX"}
    db.commit()
    path, _ = submission(tmp_path)
    original_input = path.read_bytes()
    published = []
    consumer = MockThreatConsumer(tmp_path, lambda: nullcontext(db), published.append, interval_seconds=1)

    assert consumer.consume_once() == {"created": 0, "existing": 0, "rejected": 0, "failed": 1}
    assert path.read_bytes() == original_input
    assert not (tmp_path / "rejected").exists()
    assert published == []
    failure = next(r for r in caplog.records if r.message == "mock_threat_delivery_failed")
    assert failure.reason == "stored_data_validation_failed"
    assert failure.error_type == "ValidationError"
    logged = json.loads(JsonLineFormatter().format(failure))
    assert "exc_info" not in logged
    assert "platform_details" not in json.dumps(logged) and "Linux/UNIX" not in json.dumps(logged)
    for model in (models.Incident, models.ThreatEvent, models.Evidence):
        assert not list(db.scalars(select(model)))

    assets_repo.get_asset_by_arn(db, asset_arn).spec = original_spec
    db.commit()
    assert consumer.consume_once() == {"created": 1, "existing": 0, "rejected": 0, "failed": 0}
    assert not path.exists()
    assert len(list((tmp_path / "done").glob("*.json"))) == 1
    incident_id = published[0].data.incident_id
    response = client_pg.get(f"/api/v1/incidents/{incident_id}")
    assert response.status_code == 200
    assert response.json()["initial_risk_level"] == "HIGH"
    assert len(response.json()["evidence_ids"]) == 1
    context = saved(db, incident_id).context
    assert context.target_status == "available" and len(context.related_assets) == 2


@pytest.mark.parametrize("relation_count", [64, 65])
def test_secops_context_relationship_limit_remains_an_explicit_rejection(db, tmp_path, caplog, relation_count):
    run = seed(db)
    target = assets_repo.get_asset_by_arn(db, TARGET)
    assets_repo.replace_relationships(
        db, target.asset_id,
        [(RelationType.SECURED_BY, PREFIX + f"security-group/sg-{i:017x}") for i in range(relation_count)],
        collection_run_id=run.collection_run_id,
    )
    db.commit()
    path, _ = submission(tmp_path)
    published = []
    consumer = MockThreatConsumer(tmp_path, lambda: nullcontext(db), published.append, interval_seconds=1)
    report = consumer.consume_once()
    assert not path.exists()
    if relation_count == 64:
        assert report == {"created": 1, "existing": 0, "rejected": 0, "failed": 0}
        context = saved(db, published[0].data.incident_id).context
        assert len(context.related_assets) + len(context.relation_issues) == 64
    else:
        assert report == {"created": 0, "existing": 0, "rejected": 1, "failed": 0}
        assert len(list((tmp_path / "rejected").glob("*.json"))) == 1
        assert published == []
        for model in (models.Incident, models.ThreatEvent, models.Evidence):
            assert not list(db.scalars(select(model)))
        rejection = next(r for r in caplog.records if r.message == "mock_threat_input_rejected")
        assert rejection.error_type == "SecOpsContextLimitExceeded"
        assert consumer.consume_once() == {"created": 0, "existing": 0, "rejected": 0, "failed": 0}


@pytest.mark.parametrize("case", [f"C{i:02}" for i in range(1, 8)])
def test_secops_context_each_corpus_submission_fits_ingress_and_round_trips(tmp_path, case):
    from mock_threat_source import MAX_OBSERVATION_BYTES

    path = prepare_case(case, tmp_path, TARGET, "2026-09-17T00:00:00Z")
    assert path.stat().st_size < MAX_OBSERVATION_BYTES
    observation, logs = parse_submission(json.loads(path.read_text(encoding="utf-8")))
    assert logs.matches_observation(observation)
    assert logs.window_end.isoformat() == "2026-09-17T00:00:00+00:00"
    assert logs.time_shift_seconds != 0


@pytest.mark.parametrize("field, value", [
    ("target_arn", PREFIX + "instance/i-00000000000000002"),
    ("source_ip", "198.51.100.1"), ("failed_attempt_count", 121),
    ("window_seconds", 301), ("occurred_at", "2026-09-17T00:00:00Z"),
])
def test_secops_context_attachment_cannot_be_paired_with_a_different_observation(tmp_path, field, value):
    path, _ = submission(tmp_path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["observation"][field] = value
    with pytest.raises(ValidationError, match="differs"):
        parse_submission(raw)


def test_secops_context_no_log_attachment_is_not_observed_zero(db, tmp_path):
    seed(db)
    _, (observation, _) = submission(tmp_path)
    result = receive_threat(db, observation)
    assert saved(db, result.incident_id).context.log_evidence is None
    assert build_graph_input(db, result.incident_id).asset_context.arn == TARGET
