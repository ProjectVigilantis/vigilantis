"""#598 조건부 격리 추천 — 후보 생성 시 저장한 판정과 그 후보의 근거 사본이 상세 API에 그대로 닿는가.

실제 PostgreSQL · GuardDuty 정규화 · 분석 주기를 거친다. AWS 사전 검사만 대역이다.
  - 판정은 생성 때 한 번 저장되고 조회 · 이후 위협 변경 · 규칙 변경이 다시 쓰지 않는다.
  - 근거 투영은 이 인시던트의 근거 행에서만 찾는다 — 다른 인시던트의 ID는 따라가지 않는다.
  - 판정은 실행 권한 · 대기 타이머 · 파라미터를 바꾸지 않는다. 이전 후보(판정 없음)는 null 그대로다.
"""

from __future__ import annotations

import copy
import uuid
from contextlib import nullcontext
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from sqlalchemy import null, select, update

import workflows
from db import models
from db.repositories import assets as assets_repo
from db.repositories import incidents as incidents_repo
from schemas.api.actions import ExecuteActionRequest
from schemas.api.assets import AssetType
from schemas.api.auth import UserRole
from schemas.api.incidents import IncidentStatus, RecommendationItem
from schemas.candidates import CandidateStatus
from schemas.evidence import EvidenceType
from schemas.runbooks import RunbookId, TriggerSource
from security import isolation_recommendation, soar
from services import guardduty
from tests.test_agent_dispatcher import _cycle, _passing_precheck, _secops_client
from tests.test_auto_isolation_workflow import INSTANCE_ID, _executions
from tests.test_guardduty_ingress import FakeGuardDuty, _events, _finding, _incident_of, _ingest
from tests.test_protected_auto_isolation import _protect_asset_for_approval

REGION = "ap-northeast-2"
REMOTE_IP = "198.51.100.7"
ACTOR_OUTBOUND = {
    "ResourceRole": "ACTOR",
    "Action": {
        "ActionType": "NETWORK_CONNECTION",
        "NetworkConnectionAction": {
            "ConnectionDirection": "OUTBOUND",
            "RemoteIpDetails": {"IpAddressV4": REMOTE_IP},
            "Blocked": True,
        },
    },
}
TARGET_ROLE = {**copy.deepcopy(ACTOR_OUTBOUND), "ResourceRole": "TARGET"}
PROJECTION_FIELDS = {
    "evidence_id", "target_arn", "finding_id", "finding_type", "resource_type",
    "first_seen_at", "last_seen_at", "activity",
}


def _never(*_args, **_kwargs):
    raise AssertionError("저장된 판정을 다시 계산하면 안 된다")


def _protected_guardduty(db, make_tenant, monkeypatch, service):
    """보호 태그 자산의 GuardDuty 탐지 1건 → 접수 → 분석 주기(서버 격리 후보 공급)."""
    tenant_id = make_tenant()
    account = db.get(models.Tenant, tenant_id).aws_account_id
    target = f"arn:aws:ec2:{REGION}:{account}:instance/{INSTANCE_ID}"
    run = assets_repo.start_collection_run(
        db, account_id=account, region=REGION, mode="localstack", lookback_days=3, period_seconds=3600,
    )
    assets_repo.upsert_asset(
        db, arn=target, asset_type=AssetType.EC2, resource_id=INSTANCE_ID, account_id=account,
        region=REGION, state="running", spec={}, collection_run_id=run.collection_run_id,
        collected_at=datetime.now(UTC),
    )
    assets_repo.get_asset_by_arn(db, target).tenant_id = tenant_id
    db.commit()
    _protect_asset_for_approval(db, target)
    fake = FakeGuardDuty({"d-1": "ENABLED"})
    world = SimpleNamespace(
        db=db, tenant_id=tenant_id, account=account, fake=fake,
        now=datetime.now(UTC).replace(microsecond=0),
    )
    monkeypatch.setattr(guardduty, "aws_client", lambda *args: fake)
    finding = _finding(world, uuid.uuid4().hex, instance=INSTANCE_ID)
    finding["Service"].update(copy.deepcopy(service))
    fake.findings = [finding]
    assert _ingest(world)["incidents_created"] == 1
    incident = _incident_of(world, _events(world)[0])
    monkeypatch.setattr(workflows.executor, "precheck", lambda *args, **kwargs: _passing_precheck(None))
    assert _cycle(db, _secops_client(db, incident.incident_id)).errored == 0
    return SimpleNamespace(
        incident_id=incident.incident_id, tenant_id=tenant_id, target=target, finding=finding,
    )


def _isolation(db, incident_id) -> models.RunbookCandidate:
    [row] = [
        row for row in incidents_repo.list_candidates(db, incident_id)
        if row.runbook_id is RunbookId.RUNBOOK_EC2_ISOLATE
    ]
    return row


def _threat(db, incident_id) -> models.Evidence:
    [row] = [
        row for row in incidents_repo.list_evidence(db, incident_id)
        if row.evidence_type is EvidenceType.THREAT
    ]
    return row


def _stored_basis(db, candidate_id):
    # 세션이 든 객체가 아니라 DB 칸을 읽는다
    return db.execute(
        select(models.RunbookCandidate.recommendation_basis)
        .where(models.RunbookCandidate.candidate_id == candidate_id)
    ).scalar_one()


def _detail(client_pg, incident_id) -> dict:
    response = client_pg.get(f"/api/v1/incidents/{incident_id}")
    assert response.status_code == 200
    return response.json()


def _isolation_item(detail) -> dict:
    [item] = [i for i in detail["recommendations"] if i["runbook_id"] == "RUNBOOK_EC2_ISOLATE"]
    return item


@pytest.mark.parametrize("service, classification, reason, role", [
    (ACTOR_OUTBOUND, "CONDITIONALLY_RECOMMENDED", "GUARDDUTY_SSH_BRUTE_FORCE_ACTOR", "ACTOR"),
    (TARGET_ROLE, "OPERATOR_JUDGEMENT_REQUIRED", "RESOURCE_ROLE_TARGET", "TARGET"),
], ids=["actor-outbound", "target-role"])
def test_basis_stored_at_creation_reaches_the_api_with_the_candidates_own_snapshot(
    db, client_pg, make_tenant, monkeypatch, audit_actor, service, classification, reason, role,
):
    case = _protected_guardduty(db, make_tenant, monkeypatch, service)
    candidate = _isolation(db, case.incident_id)
    threat = _threat(db, case.incident_id)
    stored = _stored_basis(db, candidate.candidate_id)
    assert (stored["origin"], stored["rule_id"], stored["rule_version"]) == (
        "SERVER_RULE", "PROTECTED_EC2_ISOLATION", 1,
    )
    assert (stored["classification"], stored["reason_code"]) == (classification, reason)
    assert candidate.evidence_ids == [threat.evidence_id]
    assert candidate.parameters == {}
    assert candidate.status is CandidateStatus.EXECUTABLE

    detail = _detail(client_pg, case.incident_id)
    item = _isolation_item(detail)
    assert item["evidence_ids"] == [threat.evidence_id]
    assert item["recommendation_basis"] == stored
    evidence = item["recommendation_evidence"]
    assert set(evidence) == PROJECTION_FIELDS
    assert {key: evidence[key] for key in PROJECTION_FIELDS - {"activity", "first_seen_at", "last_seen_at"}} == {
        "evidence_id": threat.evidence_id, "target_arn": case.target,
        "finding_id": case.finding["Id"], "finding_type": "UnauthorizedAccess:EC2/SSHBruteForce",
        "resource_type": "Instance",
    }
    assert evidence["activity"] == {
        "resource_role": {"status": "RECORDED", "value": role, "source_value": None},
        "action_type": {"status": "RECORDED", "value": "NETWORK_CONNECTION", "source_value": None},
        "connection_direction": {"status": "RECORDED", "value": "OUTBOUND", "source_value": None},
        "remote_ip": {"status": "RECORDED", "value": REMOTE_IP, "source_value": None},
        "blocked": {"status": "RECORDED", "value": True, "source_value": None},
    }

    # 판정은 실행 권한 · 타이머와 무관하다 — 양성도 자동 대응을 켜지 않고, 판단 필요도 승인을 막지 않는다
    assert detail["status"] == "AWAITING_APPROVAL"
    assert detail["auto_isolation_block"] == "THREAT_TYPE_NOT_ELIGIBLE"
    incident = db.get(models.Incident, case.incident_id)
    db.refresh(incident)
    assert (incident.agent_wait_started_at, incident.response_deadline_at) == (None, None)
    assert _executions(db, case.incident_id) == []
    workflows.reserve_execution(
        db,
        ExecuteActionRequest(
            incident_id=case.incident_id, runbook_id=RunbookId.RUNBOOK_EC2_ISOLATE,
            idempotency_key=f"approve-{reason}",
        ),
        audit_actor=audit_actor,
    )
    [execution] = _executions(db, case.incident_id)
    assert (execution.trigger_source, execution.candidate_id) == (
        TriggerSource.USER_APPROVAL, candidate.candidate_id,
    )


def test_later_threat_asset_and_rule_changes_do_not_rewrite_the_stored_judgement(
    db, client_pg, make_tenant, monkeypatch,
):
    case = _protected_guardduty(db, make_tenant, monkeypatch, ACTOR_OUTBOUND)
    before = _isolation_item(_detail(client_pg, case.incident_id))
    assert before["recommendation_basis"]["reason_code"] == "GUARDDUTY_SSH_BRUTE_FORCE_ACTOR"

    # 위협 행 · 요약 · 보호 태그가 나중에 바뀌고 규칙도 바뀐다 — 저장된 판정과 근거 사본은 그대로여야 한다
    incident = db.get(models.Incident, case.incident_id)
    event = db.get(models.ThreatEvent, incident.threat_event_id)
    event.payload = {
        **event.payload,
        "activity": {
            **event.payload["activity"],
            "resource_role": {"status": "RECORDED", "value": "TARGET", "source_value": None},
        },
    }
    incident.summary_lines = ["역할 변경 1", "역할 변경 2", "역할 변경 3"]
    asset = assets_repo.get_asset_by_arn(db, case.target)
    asset.spec = {**asset.spec, "tags": {"Environment": "dev"}}
    db.commit()
    monkeypatch.setattr(isolation_recommendation, "judge_protected_isolation", _never)
    monkeypatch.setattr(isolation_recommendation, "COVERED_FINDING_TYPE", "Recon:EC2/Portscan")

    assert _isolation_item(_detail(client_pg, case.incident_id)) == before
    report = soar.run_timeout_cycle(lambda: nullcontext(db))
    assert (report.errored, report.protected_offered) == (0, 0)
    assert _isolation_item(_detail(client_pg, case.incident_id)) == before


def test_legacy_null_basis_is_not_backfilled_and_foreign_refs_are_not_followed(
    db, client_pg, make_tenant, monkeypatch, make_incident, make_candidate,
):
    case = _protected_guardduty(db, make_tenant, monkeypatch, ACTOR_OUTBOUND)
    candidate = _isolation(db, case.incident_id)
    threat = _threat(db, case.incident_id)
    # 판정 칸이 생기기 전의 후보 — 근거 사실은 보여도 판정은 없다(읽기가 채우지 않는다)
    db.execute(
        update(models.RunbookCandidate)
        .where(models.RunbookCandidate.candidate_id == candidate.candidate_id)
        .values(recommendation_basis=null())
    )
    db.commit()
    db.expire_all()
    monkeypatch.setattr(isolation_recommendation, "judge_protected_isolation", _never)
    legacy = _isolation_item(_detail(client_pg, case.incident_id))
    assert legacy["recommendation_basis"] is None
    assert legacy["recommendation_evidence"]["evidence_id"] == threat.evidence_id
    assert _stored_basis(db, candidate.candidate_id) is None

    # 다른 인시던트의 후보가 이 근거 ID를 가리켜도 그 사본을 읽어 오지 않는다
    other = make_incident(db, status=IncidentStatus.AWAITING_APPROVAL)
    make_candidate(
        db, other, runbook_id=RunbookId.RUNBOOK_EC2_ISOLATE, parameters={},
        evidence_ids=[threat.evidence_id],
    )
    db.commit()
    response = client_pg.get(f"/api/v1/incidents/{other.incident_id}")
    assert response.status_code == 200
    [foreign] = response.json()["recommendations"]
    assert foreign["evidence_ids"] == [threat.evidence_id]
    assert (foreign["recommendation_basis"], foreign["recommendation_evidence"]) == (None, None)
    assert case.finding["Id"] not in response.text


def test_other_tenant_cannot_read_the_judgement_or_its_evidence(
    db, client_pg, make_tenant, monkeypatch, login_as,
):
    case = _protected_guardduty(db, make_tenant, monkeypatch, ACTOR_OUTBOUND)
    login_as(UserRole.VIEWER, tenant_id=make_tenant())
    refused = client_pg.get(f"/api/v1/incidents/{case.incident_id}")
    assert (refused.status_code, refused.json()["error"]["code"]) == (404, "INCIDENT_NOT_FOUND")
    assert case.finding["Id"] not in refused.text

    login_as(UserRole.VIEWER, tenant_id=case.tenant_id)
    item = _isolation_item(_detail(client_pg, case.incident_id))
    assert item["recommendation_basis"]["classification"] == "CONDITIONALLY_RECOMMENDED"
    assert item["recommendation_evidence"]["finding_id"] == case.finding["Id"]


def test_unreadable_snapshot_leaves_no_judgement_instead_of_inventing_a_reason():
    now = datetime.now(UTC)
    incident = models.Incident(
        incident_id=str(uuid.uuid4()), subject_arn="arn:aws:ec2:ap-northeast-2:123456789012:instance/i-1",
        threat_event_id=str(uuid.uuid4()),
    )
    threat = models.Evidence(
        evidence_id=str(uuid.uuid4()), incident_id=incident.incident_id, evidence_type=EvidenceType.THREAT,
        source_type="threat_event", source_id=incident.threat_event_id, content={"event": {}},
        occurred_at=now, collected_at=now,
    )
    assert workflows._protected_isolation_basis(incident, threat) is None


def test_recommendation_item_binds_basis_and_evidence_to_its_single_reference():
    basis = {
        "classification": "OPERATOR_JUDGEMENT_REQUIRED", "reason_code": "RESOURCE_ROLE_TARGET",
        "evaluated_at": "2026-11-13T04:05:06.789012Z",
    }
    evidence = {
        "evidence_id": "e-1", "target_arn": "arn:aws:ec2:ap-northeast-2:123456789012:instance/i-1",
        "finding_id": "f-1", "finding_type": "UnauthorizedAccess:EC2/SSHBruteForce",
        "resource_type": "Instance", "first_seen_at": "2026-11-10T01:02:03Z",
        "last_seen_at": "2026-11-10T01:02:03Z", "activity": None,
    }
    base = {"runbook_id": "RUNBOOK_EC2_ISOLATE", "target_arn": evidence["target_arn"], "evidence_ids": ["e-1"]}
    item = RecommendationItem.model_validate(
        {**base, "recommendation_basis": basis, "recommendation_evidence": evidence},
    )
    assert RecommendationItem.model_validate(item.model_dump(mode="json")) == item
    assert RecommendationItem.model_validate({**base, "recommendation_evidence": evidence}).recommendation_basis is None
    legacy = RecommendationItem.model_validate({"runbook_id": "RUNBOOK_EC2_ISOLATE", "target_arn": "t"})
    assert (legacy.evidence_ids, legacy.recommendation_basis, legacy.recommendation_evidence) == ([], None, None)
    for bad in (
        {**base, "evidence_ids": [], "recommendation_basis": basis},
        {**base, "evidence_ids": ["e-1", "e-2"], "recommendation_basis": basis},
        {**base, "runbook_id": "RUNBOOK_NACL_ADD_DENY", "recommendation_basis": basis},
        {**base, "evidence_ids": ["e-2"], "recommendation_evidence": evidence},
        {**base, "evidence_ids": ["e-1", "e-2"], "recommendation_evidence": evidence},
        {**base, "recommendation_evidence": {**evidence, "payload": {}}},
    ):
        with pytest.raises(ValidationError):
            RecommendationItem.model_validate(bad)
