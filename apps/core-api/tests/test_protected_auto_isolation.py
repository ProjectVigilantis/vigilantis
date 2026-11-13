"""#598 보호 태그: 실제 PostgreSQL의 정책으로 사전·접수 관문을 함께 검증한다."""

from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import dispatcher
import pytest
import workflows
import agent_dispatcher
from db import models
from db.repositories import assets as assets_repo
from db.repositories import incidents as incidents_repo
from db.repositories import notification_events as events_repo
from db.repositories import tenant_settings as settings_repo
from db.repositories import tenants as tenants_repo
from schemas.api.actions import ExecuteActionRequest, ExecutionStatus
from schemas.api.assets import AssetType, RelationType
from schemas.guardrails import PrecheckReasonCode
from schemas.runbooks import RunbookId, TriggerSource
from security import soar
from services import guardduty
from sqlalchemy import delete, event, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from tests.test_auto_isolation_workflow import (
    INSTANCE_ID,
    ISOLATION_GROUP_ID,
    PRE,
    SUBJECT,
    TARGET_GROUP_ARN,
    TIMEOUT,
    _attempt,
    _auto_evaluations,
    _candidate_statuses,
    _committed,
    _executions,
    _seed,
)
from tests.test_tenant_auto_action import ACCOUNT, _attach, _tenant
from tests.test_agent_dispatcher import (
    EC2_ARN, _cycle, _failing_precheck, _passing_precheck, _pending_secops, _secops_client,
)
from tests.test_guardduty_ingress import FakeGuardDuty, _events, _finding, _incident_of, _ingest


class _PrecheckReached(Exception):
    pass


def _protect_asset_for_approval(db, target_arn=EC2_ARN):
    asset = assets_repo.get_asset_by_arn(db, target_arn)
    asset.spec = {
        **asset.spec, "tags": {"Environment": "production"}, "vpc_id": "vpc-0123456789abcdef0",
    }
    db.add(models.AssetRelationship(
        source_asset_id=asset.asset_id, relation_type=RelationType.REGISTERED_IN,
        target_arn=(
            f"arn:aws:elasticloadbalancing:{asset.region}:{asset.account_id}:targetgroup/vig/abc"
        ),
    ))
    db.add(models.Asset(
        arn=f"arn:aws:ec2:{asset.region}:{asset.account_id}:security-group/{ISOLATION_GROUP_ID}",
        asset_type=AssetType.SG, resource_id=ISOLATION_GROUP_ID,
        account_id=asset.account_id, region=asset.region, tenant_id=asset.tenant_id,
        spec={"vpc_id": "vpc-0123456789abcdef0", "tags": {"vigilantis:role": "isolation"}},
    ))
    db.commit()


def test_protected_isolation_is_offered_by_server_without_rewriting_ai_input(
    db, client_pg, monkeypatch, audit_actor,
):
    incident_id = _pending_secops(db, medium=True)
    before = agent_dispatcher.build_graph_input(db, incident_id)
    _protect_asset_for_approval(db)
    protected = agent_dispatcher.build_graph_input(db, incident_id)
    assert protected == before
    checked = []

    def precheck(runbook_id, target_arn, parameters, **kwargs):
        assert not db.in_transaction()
        if runbook_id is RunbookId.RUNBOOK_EC2_ISOLATE:
            assert target_arn == EC2_ARN
            checked.append(parameters)
        return _passing_precheck(None)

    monkeypatch.setattr(workflows.executor, "precheck", precheck)
    monkeypatch.setattr(
        agent_dispatcher, "resolve_nacl_add_deny_rule_number", lambda *args, **kwargs: (1, None),
    )
    report = _cycle(db, _secops_client(db, incident_id))
    assert (report.succeeded, report.failed, report.errored) == (1, 0, 0)
    response = client_pg.get(f"/api/v1/incidents/{incident_id}")
    assert response.status_code == 200
    detail = response.json()
    assert {item["runbook_id"] for item in detail["recommendations"]} == {
        "RUNBOOK_NACL_ADD_DENY", "RUNBOOK_EC2_ISOLATE",
    }
    assert detail["analysis_result"]["status"] == "PROPOSALS_GENERATED"
    assert db.get(models.Incident, incident_id).response_deadline_at is not None
    isolation = next(
        row for row in incidents_repo.list_candidates(db, incident_id)
        if row.runbook_id is RunbookId.RUNBOOK_EC2_ISOLATE
    )
    assert isolation.parameters == {}
    assert isolation.status.value == "EXECUTABLE"
    # 모의 SSH 위협은 규칙 v1의 탐지가 아니다 — 판정은 관제자 판단 필요로 남고 근거 투영은 없다
    assert (
        isolation.recommendation_basis["classification"], isolation.recommendation_basis["reason_code"],
    ) == ("OPERATOR_JUDGEMENT_REQUIRED", "FINDING_TYPE_NOT_COVERED")
    by_runbook = {item["runbook_id"]: item for item in detail["recommendations"]}
    assert by_runbook["RUNBOOK_EC2_ISOLATE"]["recommendation_basis"] == isolation.recommendation_basis
    assert by_runbook["RUNBOOK_EC2_ISOLATE"]["evidence_ids"] == isolation.evidence_ids
    assert by_runbook["RUNBOOK_EC2_ISOLATE"]["recommendation_evidence"] is None
    assert by_runbook["RUNBOOK_NACL_ADD_DENY"]["recommendation_basis"] is None
    assert len(checked) == 1
    assert checked[0].isolation_group_id == ISOLATION_GROUP_ID
    assert checked[0].target_group_arn == TARGET_GROUP_ARN
    assert _executions(db, incident_id) == []
    workflows.reserve_execution(
        db,
        ExecuteActionRequest(
            incident_id=incident_id, runbook_id=RunbookId.RUNBOOK_EC2_ISOLATE,
            idempotency_key="protected-approval",
        ),
        audit_actor=audit_actor,
    )
    [execution] = _executions(db, incident_id)
    assert execution.trigger_source is TriggerSource.USER_APPROVAL

    def run_isolation(session, execution_id):
        session.commit()
        return SimpleNamespace(succeeded=True)

    monkeypatch.setitem(dispatcher._RUNNERS, RunbookId.RUNBOOK_EC2_ISOLATE, run_isolation)
    monkeypatch.setitem(
        dispatcher._JUDGES, RunbookId.RUNBOOK_EC2_ISOLATE,
        lambda *args: workflows.ExecutionJudgement(
            next_status=ExecutionStatus.FAILED, error_summary="격리 상태가 확인되지 않음",
        ),
    )
    dispatcher.dispatch_now(db, execution.execution_id)
    db.refresh(execution)
    assert execution.status is ExecutionStatus.FAILED
    assert all(row.kind != "AUTO_ISOLATION" for row in events_repo.list_by_incident(db, incident_id))


@pytest.mark.parametrize("analysis_case, expected_analysis", [
    ("no_proposal", "NO_PROPOSAL"),
    ("ipv6", "NO_ACTION_AVAILABLE"),
    ("nacl_unavailable", "FAILED"),
    ("ai_rejected", "GUARDRAIL_REJECTED"),
])
def test_server_isolation_does_not_turn_missing_ai_action_into_ai_success(
    db, client_pg, monkeypatch, analysis_case, expected_analysis,
):
    incident_id = _pending_secops(
        db, medium=True, source_ip="2001:db8::1" if analysis_case == "ipv6" else None,
    )
    _protect_asset_for_approval(db)

    def precheck(runbook_id, *args, **kwargs):
        assert not db.in_transaction()
        if analysis_case == "ai_rejected" and runbook_id is RunbookId.RUNBOOK_NACL_ADD_DENY:
            return _failing_precheck(None)
        return _passing_precheck(None)

    monkeypatch.setattr(workflows.executor, "precheck", precheck)
    rule_result = (
        (None, PrecheckReasonCode.PRECHECK_INVALID_STATE)
        if analysis_case == "nacl_unavailable" else (1, None)
    )
    monkeypatch.setattr(
        agent_dispatcher, "resolve_nacl_add_deny_rule_number", lambda *args, **kwargs: rule_result,
    )
    client = _secops_client(db, incident_id, candidates=analysis_case != "no_proposal")
    report = _cycle(db, client)
    assert report.errored == 0
    response = client_pg.get(f"/api/v1/incidents/{incident_id}")
    assert response.status_code == 200
    detail = response.json()
    assert detail["status"] == "AWAITING_APPROVAL"
    assert [item["runbook_id"] for item in detail["recommendations"]] == ["RUNBOOK_EC2_ISOLATE"]
    assert detail["analysis_result"]["status"] == expected_analysis
    if analysis_case in {"ipv6", "nacl_unavailable"}:
        assert client.sent == []
    incident = db.get(models.Incident, incident_id)
    assert incident.agent_wait_started_at is None
    assert incident.response_deadline_at is None
    assert detail["auto_isolation_block"] == "PROTECTED_TAG"
    asset = assets_repo.get_asset_by_arn(db, EC2_ARN)
    asset.spec = {**asset.spec, "tags": {"Environment": "dev"}}
    db.commit()
    response = client_pg.get(f"/api/v1/incidents/{incident_id}")
    assert response.status_code == 200
    assert response.json()["auto_isolation_block"] == "WAIT_NOT_STARTED"
    assert [item["runbook_id"] for item in response.json()["recommendations"]] == [
        "RUNBOOK_EC2_ISOLATE",
    ]
    assert soar.run_timeout_cycle(lambda: nullcontext(db)).scanned == 0
    assert _executions(db, incident_id) == []
    assert not any(
        row.kind == "PROTECTED_TARGET_ESCALATION"
        for row in events_repo.list_by_incident(db, incident_id)
    )


@pytest.mark.parametrize("suspended", [False, True])
def test_late_protection_offer_survives_escalation_and_policy_change(
    db, client_pg, monkeypatch, suspended,
):
    incident_id = _pending_secops(db, medium=True)
    checked = []

    def precheck(runbook_id, target_arn, parameters, **kwargs):
        assert not db.in_transaction()
        if runbook_id is RunbookId.RUNBOOK_EC2_ISOLATE:
            checked.append(parameters)
        return _passing_precheck(None)

    monkeypatch.setattr(workflows.executor, "precheck", precheck)
    monkeypatch.setattr(
        agent_dispatcher, "resolve_nacl_add_deny_rule_number", lambda *args, **kwargs: (1, None),
    )
    assert _cycle(db, _secops_client(db, incident_id)).succeeded == 1
    _protect_asset_for_approval(db)
    incident = db.get(models.Incident, incident_id)
    incident.response_deadline_at = datetime.now(UTC) - timedelta(seconds=1)
    incident.agent_wait_started_at = incident.response_deadline_at - timedelta(seconds=60)
    db.commit()
    wait = (incident.agent_wait_started_at, incident.response_deadline_at)
    assert soar.prepare_isolation(db, incident_id, TIMEOUT) is None
    assert checked == []

    # 생략 기록 뒤 프로세스가 멈추고 정책이 바뀌어도, 다음 감시는 승인 후보 공급을 이어 간다.
    asset = assets_repo.get_asset_by_arn(db, EC2_ARN)
    asset.spec = {**asset.spec, "tags": {"Environment": "dev"}}
    if suspended:
        tenant = _tenant(db, account=asset.account_id)
        assert tenants_repo.suspend_tenant(
            db, tenant.tenant_id, reason="원격 회수", now=datetime.now(UTC),
        )
        incident.tenant_id = tenant.tenant_id
    db.commit()
    published = []
    first = soar.run_timeout_cycle(lambda: nullcontext(db), publish=published.append)
    assert first.scanned == 0
    if suspended:
        assert [
            row.runbook_id for row in incidents_repo.list_candidates(db, incident_id)
        ] == [RunbookId.RUNBOOK_NACL_ADD_DENY]
        assert workflows.offer_protected_isolation(db, incident_id).candidate_status is None
        assert checked == published == []
        assert _executions(db, incident_id) == []
        return
    response = client_pg.get(f"/api/v1/incidents/{incident_id}")
    assert response.status_code == 200
    assert {item["runbook_id"] for item in response.json()["recommendations"]} == {
        "RUNBOOK_NACL_ADD_DENY", "RUNBOOK_EC2_ISOLATE",
    }
    # 뒤늦은 공급도 같은 생성 경로에서 판정을 남긴다
    by_runbook = {item["runbook_id"]: item for item in response.json()["recommendations"]}
    assert by_runbook["RUNBOOK_EC2_ISOLATE"]["recommendation_basis"]["reason_code"] == "FINDING_TYPE_NOT_COVERED"
    assert by_runbook["RUNBOOK_NACL_ADD_DENY"]["recommendation_basis"] is None
    assert len(checked) == len(published) == 1
    assert checked[0].isolation_group_id == ISOLATION_GROUP_ID
    assert checked[0].target_group_arn == TARGET_GROUP_ARN
    incident = db.get(models.Incident, incident_id)
    assert (incident.agent_wait_started_at, incident.response_deadline_at) == wait
    assert _executions(db, incident_id) == []
    db.rollback()
    second = soar.run_timeout_cycle(lambda: nullcontext(db), publish=published.append)
    assert second.scanned == 0
    assert len(checked) == len(published) == 1


@pytest.mark.parametrize("severity,suspended_before_analysis", [
    (5.0, False), (8.0, False), (5.0, True),
])
def test_protected_guardduty_gets_approval_only_without_claiming_an_automatic_skip(
    db, client_pg, make_tenant, monkeypatch, severity, suspended_before_analysis,
):
    tenant_id = make_tenant()
    tenant = db.get(models.Tenant, tenant_id)
    target = f"arn:aws:ec2:ap-northeast-2:{tenant.aws_account_id}:instance/{INSTANCE_ID}"
    run = assets_repo.start_collection_run(
        db, account_id=tenant.aws_account_id, region="ap-northeast-2",
        mode="localstack", lookback_days=3, period_seconds=3600,
    )
    assets_repo.upsert_asset(
        db,
        arn=target, asset_type=AssetType.EC2, resource_id=INSTANCE_ID,
        account_id=tenant.aws_account_id,
        region="ap-northeast-2", state="running", spec={},
        collection_run_id=run.collection_run_id, collected_at=datetime.now(UTC),
    )
    assets_repo.get_asset_by_arn(db, target).tenant_id = tenant_id
    db.commit()
    _protect_asset_for_approval(db, target)
    fake = FakeGuardDuty({"d-1": "ENABLED"})
    world = SimpleNamespace(
        db=db, tenant_id=tenant_id, account=tenant.aws_account_id, fake=fake,
        now=datetime.now(UTC).replace(microsecond=0),
    )
    monkeypatch.setattr(guardduty, "aws_client", lambda *args: fake)
    fake.findings = [_finding(world, "protected", severity=severity, instance=INSTANCE_ID)]
    assert _ingest(world)["incidents_created"] == 1
    incident = _incident_of(world, _events(world)[0])
    if suspended_before_analysis:
        assert tenants_repo.suspend_tenant(
            db, tenant_id, reason="원격 회수", now=datetime.now(UTC),
        )
        db.commit()
    checked = []

    def precheck(runbook_id, *args, **kwargs):
        assert not db.in_transaction()
        checked.append(runbook_id)
        return _passing_precheck(None)

    monkeypatch.setattr(workflows.executor, "precheck", precheck)
    client = _secops_client(db, incident.incident_id)
    assert _cycle(db, client).errored == 0
    assert client.sent == []
    response = client_pg.get(f"/api/v1/incidents/{incident.incident_id}")
    assert response.status_code == 200
    detail = response.json()
    if suspended_before_analysis:
        assert detail["status"] == "FAILED"
        assert detail["recommendations"] == []
        assert detail["analysis_result"]["status"] == "NO_ACTION_AVAILABLE"
        assert checked == []
        assert _executions(db, incident.incident_id) == []
        assert events_repo.list_by_incident(db, incident.incident_id) == []
        return
    assert detail["status"] == "AWAITING_APPROVAL"
    assert [item["runbook_id"] for item in detail["recommendations"]] == ["RUNBOOK_EC2_ISOLATE"]
    assert detail["analysis_result"]["status"] == "NO_ACTION_AVAILABLE"
    if severity == 5.0:
        assert detail["auto_isolation_block"] == "THREAT_TYPE_NOT_ELIGIBLE"
        assert db.get(models.Incident, incident.incident_id).response_deadline_at is None
    assert checked == [RunbookId.RUNBOOK_EC2_ISOLATE]
    assert _executions(db, incident.incident_id) == []
    assert [row.kind for row in events_repo.list_by_incident(db, incident.incident_id)] == (
        ["HIGH_INCIDENT_CREATED"] if severity == 8.0 else []
    )
    if severity == 5.0:
        assert tenants_repo.suspend_tenant(
            db, tenant_id, reason="원격 회수", now=datetime.now(UTC),
        )
        db.commit()
        response = client_pg.get(f"/api/v1/incidents/{incident.incident_id}")
        assert response.status_code == 200
        assert response.json()["auto_isolation_block"] == "TENANT_SUSPENDED"


def _assert_protected_facts(db, incident_id):
    notifications = events_repo.list_by_incident(db, incident_id)
    assert [(row.kind, row.phase, row.reason_code, row.execution_id) for row in notifications] == [
        ("PROTECTED_TARGET_ESCALATION", "CREATED", "PROTECTED_TAG", None),
    ]
    facts = list(db.scalars(
        select(models.PlatformEvent).where(models.PlatformEvent.target_id == incident_id)
        .order_by(models.PlatformEvent.action),
    ))
    assert [(row.action, row.outcome.value, row.details) for row in facts] == [
        ("ACTION_BLOCK", "DENIED", {"reason_code": "PROTECTED_TAG"}),
        ("INCIDENT_ESCALATE", "SUCCEEDED", {"trigger_reason": "PROTECTED_TAG"}),
    ]


@pytest.mark.parametrize("kind, trigger", [("high", PRE), ("medium", TIMEOUT)])
@pytest.mark.parametrize("policy,tags,protected", [
    (None, {"Environment": " production "}, True),
    (None, {"Environment": "production-copy"}, False),
    ([], {"Environment": "production"}, False),
    ([{"key": "Critical", "values": [" ERP "]}], {"CRITICAL": "erp"}, True),
    ([{"key": "Critical", "values": ["ERP"]}], {"Environment": "production"}, False),
])
def test_protected_policy_blocks_both_gates_without_consuming_approval(
    db, make_incident, make_candidate, monkeypatch, kind, trigger, policy, tags, protected,
):
    tenant = _tenant(db)
    seeded = _seed(db, make_incident, make_candidate, kind)
    _attach(db, seeded, tenant)
    setting = settings_repo.new_record(tenant.tenant_id, updated_at=datetime.now(UTC))
    setting.protected_tags = policy
    db.add(setting)
    asset = models.Asset(
        arn=SUBJECT, asset_type=AssetType.EC2, resource_id=INSTANCE_ID,
        tenant_id=tenant.tenant_id, account_id=ACCOUNT, region="ap-northeast-2",
        state="running", spec={"tags": tags, "vpc_id": "vpc-0123456789abcdef0"},
    )
    db.add(asset)
    db.flush()
    db.add(models.AssetRelationship(
        source_asset_id=asset.asset_id, relation_type=RelationType.REGISTERED_IN,
        target_arn=TARGET_GROUP_ARN,
    ))
    db.add(models.Asset(
        arn=f"arn:aws:ec2:ap-northeast-2:{ACCOUNT}:security-group/{ISOLATION_GROUP_ID}",
        asset_type=AssetType.SG, resource_id=ISOLATION_GROUP_ID,
        tenant_id=tenant.tenant_id, account_id=ACCOUNT, region="ap-northeast-2",
        spec={"vpc_id": "vpc-0123456789abcdef0", "tags": {"vigilantis:role": "isolation"}},
    ))
    db.commit()
    before = _candidate_statuses(db, seeded.incident_id)
    incident = db.get(models.Incident, seeded.incident_id)
    state_before = (incident.status, incident.response_mode, incident.response_deadline_at)
    db.rollback()

    def precheck_spy(*args, **kwargs):
        if protected:
            pytest.fail("보호 자산이 AWS 사전 검사에 도달했습니다")
        raise _PrecheckReached

    monkeypatch.setattr(soar.executor, "precheck", precheck_spy)
    if protected:
        assert soar.prepare_isolation(db, seeded.incident_id, trigger) is None
    else:
        with pytest.raises(_PrecheckReached):
            soar.prepare_isolation(db, seeded.incident_id, trigger)

    result = _attempt(db, seeded, trigger)
    if not protected:
        assert result.created
        assert result.execution_id is not None
        return

    assert result.execution_id is None
    assert result.skipped_reason.value == "PROTECTED_TAG"
    assert _executions(db, seeded.incident_id) == []
    assert _auto_evaluations(db, seeded.incident_id) == []
    assert _candidate_statuses(db, seeded.incident_id) == before
    incident = db.get(models.Incident, seeded.incident_id)
    assert (incident.status, incident.response_mode, incident.response_deadline_at) == state_before
    _assert_protected_facts(db, seeded.incident_id)
    assert seeded.incident_id not in incidents_repo.list_expired_agent_waits(
        db, now=datetime.now(UTC), exclude_notification_kind="PROTECTED_TARGET_ESCALATION",
    )


@pytest.mark.parametrize("changed", ["tags", "policy"])
def test_reservation_rereads_protection_committed_after_the_first_check(
    pg_engine, make_incident, make_candidate, changed,
):
    tenant_id = asset_id = None
    try:
        with _committed(pg_engine) as incident_ids:
            with Session(pg_engine) as setup:
                tenant = _tenant(setup)
                tenant_id = tenant.tenant_id
                seeded = _seed(setup, make_incident, make_candidate, "medium")
                incident_ids.append(seeded.incident_id)
                _attach(setup, seeded, tenant)
                setting = settings_repo.new_record(tenant_id, updated_at=datetime.now(UTC))
                setting.protected_tags = [] if changed == "policy" else None
                setup.add(setting)
                asset = models.Asset(
                    arn=SUBJECT, asset_type=AssetType.EC2, resource_id=INSTANCE_ID,
                    tenant_id=tenant_id, account_id=ACCOUNT, region="ap-northeast-2",
                    state="running",
                    spec={"tags": {"Environment": "production" if changed == "policy" else "dev"}},
                )
                setup.add(asset)
                setup.flush()
                asset_id = asset.asset_id
                setup.commit()

            with Session(pg_engine) as reader:
                before = _candidate_statuses(reader, seeded.incident_id)
                incident = reader.get(models.Incident, seeded.incident_id)
                state_before = (incident.status, incident.response_mode, incident.response_deadline_at)
                cached_asset = reader.get(models.Asset, asset_id)
                cached_policy = reader.get(models.TenantSettingsRecord, tenant_id)
                assert workflows.protected_auto_isolation_reason(reader, incident) is None
                with Session(pg_engine) as writer:
                    if changed == "tags":
                        writer.get(models.Asset, asset_id).spec = {"tags": {"Environment": "production"}}
                    else:
                        writer.get(models.TenantSettingsRecord, tenant_id).protected_tags = None
                    writer.commit()
                if changed == "tags":
                    assert cached_asset.spec["tags"]["Environment"] == "dev"
                else:
                    assert cached_policy.protected_tags == []

                result = _attempt(reader, seeded, TIMEOUT)
                assert result.execution_id is None
                assert result.skipped_reason.value == "PROTECTED_TAG"
                assert _executions(reader, seeded.incident_id) == []
                assert _candidate_statuses(reader, seeded.incident_id) == before
                incident = reader.get(models.Incident, seeded.incident_id)
                assert (incident.status, incident.response_mode, incident.response_deadline_at) == state_before
                _assert_protected_facts(reader, seeded.incident_id)
    finally:
        with Session(pg_engine) as cleanup:
            if asset_id is not None:
                cleanup.execute(delete(models.Asset).where(models.Asset.asset_id == asset_id))
            if tenant_id is not None:
                cleanup.execute(delete(models.TenantSettingsRecord).where(
                    models.TenantSettingsRecord.tenant_id == tenant_id,
                ))
                cleanup.execute(delete(models.Tenant).where(models.Tenant.tenant_id == tenant_id))
            cleanup.commit()


def test_failed_escalation_rolls_back_skip_record_and_leaves_approval_untouched(
    db, make_incident, make_candidate,
):
    seeded = _seed(db, make_incident, make_candidate, "medium")
    db.add(models.Asset(
        arn=SUBJECT, asset_type=AssetType.EC2, resource_id=INSTANCE_ID,
        account_id=ACCOUNT, region="ap-northeast-2", state="running",
        spec={"tags": {"Environment": "production"}},
    ))
    db.commit()
    before = _candidate_statuses(db, seeded.incident_id)

    def corrupt(_mapper, _connection, target):
        if target.kind == "PROTECTED_TARGET_ESCALATION":
            target.reason_code = "INVALID"

    event.listen(models.NotificationEvent, "before_insert", corrupt)
    try:
        with pytest.raises(IntegrityError, match="ck_notification_events_event_shape"):
            soar.prepare_isolation(db, seeded.incident_id, TIMEOUT)
    finally:
        event.remove(models.NotificationEvent, "before_insert", corrupt)

    assert events_repo.list_by_incident(db, seeded.incident_id) == []
    assert list(db.scalars(
        select(models.PlatformEvent).where(models.PlatformEvent.target_id == seeded.incident_id),
    )) == []
    assert _executions(db, seeded.incident_id) == []
    assert _candidate_statuses(db, seeded.incident_id) == before
    assert soar.prepare_isolation(db, seeded.incident_id, TIMEOUT) is None
    _assert_protected_facts(db, seeded.incident_id)
