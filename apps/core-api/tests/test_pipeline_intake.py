"""#306: 실제 판정→Intake→DB·조회·AI 입력 경계를 검증한다.

AWS 수집만 골든 인벤토리로 대체한다. 판정 규칙의 경계값은 기존 테스트가
소유하며, 여기서는 선택·회차·멱등·실패 후 재시도·생성 이벤트를 확인한다.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

import agent_dispatcher
import db.session as db_session
import incident_intake
from db import models
from db.repositories import assets as assets_repo
from db.repositories import incidents as incidents_repo
from schemas.api.incidents import IncidentStatus
from schemas.api.ws import WsEventType
from schemas.assets import AssetInventory
from schemas.evidence import EvidenceType
from services import collector, rule_engine, scheduler

GOLDEN = Path(__file__).resolve().parents[3] / "datasets/golden/finops"


@pytest.fixture()
def pipeline(db, pg_engine, monkeypatch):
    # 건별 Session.close/rollback도 실제로 수행하되 데이터는 테스트 외부
    # 트랜잭션에 가둔다. 실제 커넥션 간 commit 가시성은 별도 smoke에서 확인한다.
    factory = sessionmaker(
        bind=db.bind, autoflush=False, expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )
    monkeypatch.setattr(db_session, "get_engine", lambda: pg_engine)
    monkeypatch.setattr(db_session, "get_session_factory", lambda: factory)

    def collect():
        with factory() as session:
            result = []
            for path in sorted((GOLDEN / "input").glob("*.json")):
                inventory = AssetInventory.model_validate_json(path.read_text(encoding="utf-8"))
                result.append(collector.persist_inventory(inventory, session))
            session.commit()
            return result

    monkeypatch.setattr(collector, "collect_and_store", collect)
    return factory


def _expected_incident_arns():
    # 구현의 선택 상수로 기대값을 만들지 않는다. 합의된 골든 판정과
    # FINOPS 계약(COST_CANDIDATE·UNUSED)에서 독립적으로 도출한다.
    return {
        row["asset_arn"]
        for path in (GOLDEN / "expected").glob("*.json")
        for row in json.loads(path.read_text(encoding="utf-8"))["evaluations"]
        if row["verdict"] in {"COST_CANDIDATE", "UNUSED"}
    }


def test_pipeline_creates_only_finops_and_preserves_first_detection_on_rescan(
    pipeline, db, client_pg,
):
    events = []
    first = scheduler.run_pipeline(events.append)
    expected = _expected_incident_arns()
    incidents = list(db.scalars(select(models.Incident)))
    assert expected
    assert {item.subject_arn for item in incidents} == expected
    assert first["incidents"] == {"created": len(expected), "existing": 0, "failed": 0}
    assert len(events) == len(expected)
    assert all(e.event_type is WsEventType.INCIDENT_CREATED for e in events)
    assert {e.data.incident_id for e in events} == {item.incident_id for item in incidents}

    first_inputs = {}
    for item in incidents:
        assert item.status is IncidentStatus.ANALYZING
        response = client_pg.get(f"/api/v1/incidents/{item.incident_id}")
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "ANALYZING"
        evidence = incidents_repo.list_evidence(db, item.incident_id)
        rule = next(e.content for e in evidence if e.evidence_type is EvidenceType.RULE)
        snapshot = next(e.content for e in evidence if e.evidence_type is EvidenceType.ASSET)
        asset = assets_repo.get_asset_by_arn(db, item.subject_arn)
        assert snapshot["collection_run_id"] == asset.last_collection_run_id
        assert rule["evaluation"]["collection_run_id"] == snapshot["collection_run_id"]
        assert snapshot["asset"]["arn"] == item.subject_arn
        assert snapshot["asset"]["verdict"] == rule["evaluation"]["verdict"]
        graph_input = agent_dispatcher.build_graph_input(db, item.incident_id)
        if snapshot["asset"]["asset_type"] == "EC2":
            metric = next(e for e in graph_input.evidences if e.evidence_type is EvidenceType.METRIC)
            assert metric.content.summary.cpu_datapoints >= 48
        first_inputs[item.incident_id] = graph_input

    second = scheduler.run_pipeline(events.append)
    assert second["incidents"] == {"created": 0, "existing": len(expected), "failed": 0}
    assert len(list(db.scalars(select(models.Incident)))) == len(expected)
    assert len(events) == len(expected)
    db.expire_all()
    for incident_id, original in first_inputs.items():
        # 새 스캔이 자산/판정 행을 바꿔도 기존 Incident 근거는 최초 회차 그대로다.
        current_asset = assets_repo.get_asset_by_arn(db, original.asset_context.arn)
        assert current_asset.last_collection_run_id != original.rule_evaluation.collection_run_id
        assert agent_dispatcher.build_graph_input(db, incident_id) == original


def test_persistent_intake_failure_does_not_starve_later_assets_and_recovers(
    pipeline, db, monkeypatch, caplog,
):
    events = []
    add_evidence = incident_intake._add_evidence
    expected = _expected_incident_arns()
    failing_arn = sorted(expected)[1]  # 앞뒤에 정상 대상이 있는 같은 자산을 매번 실패시킨다.
    failed_ids = []

    def fail_same_asset(session, **kwargs):
        result = add_evidence(session, **kwargs)
        if kwargs["evidence_type"] is EvidenceType.ASSET and kwargs["source_id"] == failing_arn:
            # Incident·RULE·ASSET을 쓴 뒤 실패시켜 부분 저장의 rollback도 확인한다.
            failed_ids.append(kwargs["incident_id"])
            raise RuntimeError("intake evidence failure")
        return result

    monkeypatch.setattr(incident_intake, "_add_evidence", fail_same_asset)
    with caplog.at_level(logging.ERROR, logger="vigilantis.scheduler"):
        for tick in range(2):
            result = scheduler.run_pipeline(events.append)
            assert result["incidents"] == {
                "created": len(expected) - 1 if tick == 0 else 0,
                "existing": 0 if tick == 0 else len(expected) - 1,
                "failed": 1,
            }
            db.expire_all()
            incidents = list(db.scalars(select(models.Incident)))
            assert {item.subject_arn for item in incidents} == expected - {failing_arn}
            assert {event.data.incident_id for event in events} == {
                item.incident_id for item in incidents
            }
            assert len(events) == len(expected) - 1
            assert not list(db.scalars(select(models.Evidence).where(
                models.Evidence.incident_id.in_(failed_ids),
            )))

    failures = [record for record in caplog.records if record.getMessage() == "scan_intake_failed"]
    assert len(failed_ids) == len(failures) == 2
    assert all(record.subject_arn == failing_arn for record in failures)
    assert all(record.exc_info and isinstance(record.exc_info[1], RuntimeError) for record in failures)
    assert list(db.scalars(select(models.RuleEvaluation)))  # 판정은 이미 저장됨.
    monkeypatch.setattr(incident_intake, "_add_evidence", add_evidence)
    retried = scheduler.run_pipeline(events.append)  # 실패 후 advisory lock도 해제되어야 한다.
    assert retried["incidents"] == {"created": 1, "existing": len(expected) - 1, "failed": 0}
    assert len(events) == len(expected)
    assert {item.subject_arn for item in db.scalars(select(models.Incident))} == expected


def test_publish_failure_does_not_rollback_or_count_as_intake_failure(pipeline, db, caplog):
    def fail_publish(event):
        raise RuntimeError("publish failure")

    with caplog.at_level(logging.ERROR, logger="vigilantis.scheduler"):
        with pytest.raises(RuntimeError, match="publish failure"):
            scheduler.run_pipeline(fail_publish)

    assert len(list(db.scalars(select(models.Incident)))) == 1  # 발행 전에 저장됨.
    assert not any(record.getMessage() == "scan_intake_failed" for record in caplog.records)
    expected = _expected_incident_arns()
    retried = scheduler.run_pipeline()
    assert retried["incidents"] == {"created": len(expected) - 1, "existing": 1, "failed": 0}


def test_pipeline_does_not_disguise_another_collection_as_the_detection(
    pipeline, db, monkeypatch,
):
    real_rule_engine = rule_engine.run_rule_engine
    judged_arns = set()

    def judge_with_wrong_run(session):
        result = real_rule_engine(session)
        for evaluation in result["evaluations"]:
            judged_arns.add(evaluation.asset_arn)
            evaluation.collection_run_id = "00000000-0000-0000-0000-000000000001"
        return result

    monkeypatch.setattr(rule_engine, "run_rule_engine", judge_with_wrong_run)
    events = []
    with pytest.raises(ValidationError, match="collection_run_id"):
        scheduler.run_pipeline(events.append)
    # 조립 오류는 Incident와 이벤트만 막는다. 그 전에 정상 적재한 판정은
    # 같은 tick의 다른 독립 결과이므로 rollback으로 잃지 않아야 한다.
    assert judged_arns
    stored_arns = set(db.scalars(
        select(models.Asset.arn).join(
            models.RuleEvaluation, models.RuleEvaluation.asset_id == models.Asset.asset_id,
        )
    ))
    assert stored_arns == judged_arns
    assert not list(db.scalars(select(models.Incident)))
    assert not events


def test_registered_scan_job_passes_publish_to_pipeline(pipeline):
    events = []
    scan_scheduler = scheduler.build_scheduler(events.append)
    job = scan_scheduler.get_job(scheduler.JOB_ID)
    result = job.func(*job.args, **job.kwargs)
    assert result["incidents"]["created"] == len(events) > 0
