"""#598 보호 태그: 실제 PostgreSQL의 정책으로 사전·접수 관문을 함께 검증한다."""

from datetime import UTC, datetime

import pytest
import workflows
from db import models
from db.repositories import incidents as incidents_repo
from db.repositories import notification_events as events_repo
from db.repositories import tenant_settings as settings_repo
from schemas.api.assets import AssetType, RelationType
from security import soar
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


class _PrecheckReached(Exception):
    pass


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
