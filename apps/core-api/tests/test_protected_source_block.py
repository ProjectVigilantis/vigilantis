"""#598 2단계: 보호 High SSH_BRUTE_FORCE의 서버 자동 출발지 차단 통합 회귀 — 실제 PostgreSQL 필요.

격리는 생략·에스컬레이션으로 기록된 뒤, 서버만 공급한 /32 tcp 인바운드 NACL deny 1건이 실제
가드레일 4단계·접수·dispatcher(runner+judge)를 지나는지 상태 가진 가짜 AWS로 본다. 성공 기준은
계약(규칙 번호·CIDR·프로토콜·EC2 무변경·1회성·귀속)에서 가져오며 생산 코드 출력을 복사하지 않는다.
"""

import copy
import logging
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import dispatcher
import incident_intake
import pytest
import workflows
from botocore.exceptions import ClientError
from db import models
from db.repositories import assets as assets_repo
from db.repositories import executions as exec_repo
from db.repositories import incidents as incidents_repo
from db.repositories import notification_events as events_repo
from schemas.api.actions import ExecuteActionRequest, ExecutionStatus
from schemas.api.assets import AssetType, RelationType
from schemas.runbooks import RunbookId, TriggerSource
from security import soar
from services import guardduty
from services.aws import executor as ex
from services.aws.client import _current_tenant
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from schemas.platform_events import PlatformEventAction
from schemas.guardrails import PrecheckReasonCode
from tests.platform_event_faults import failing_platform_event_write
from tests.test_agent_dispatcher import (
    EC2_ARN, INSTANCE_ID, NACL_ARN, _failing_precheck, _passing_precheck, _pending_secops,
    _secops_intake, _seed_asset_row,
)
from tests.test_guardduty_ingress import FakeGuardDuty, _events, _finding, _incident_of, _ingest
from tests.test_nacl_release_offer import aws  # noqa: F401  (fixture)
from tests.test_nacl_release_offer import release_candidates
from tests.test_tenant_auto_action import _tenant, _toggle

PRE = TriggerSource.PRE_MITIGATION_0_5S
ADD_DENY = RunbookId.RUNBOOK_NACL_ADD_DENY
RESTORE = RunbookId.RUNBOOK_NACL_RESTORE
ISOLATE = RunbookId.RUNBOOK_EC2_ISOLATE
SOURCE_CIDR = "203.0.113.10/32"  # _pending_secops의 SSH 출발지
ALLOW_200 = {
    "RuleNumber": 200, "Egress": False, "CidrBlock": "0.0.0.0/0", "Protocol": "-1",
    "RuleAction": "allow",
}


def _expected_entry(rule_number=1):
    return {
        "RuleNumber": rule_number, "Egress": False, "CidrBlock": SOURCE_CIDR,
        "Protocol": "6", "RuleAction": "deny",
    }


def _protected_incident(db, aws, *, entries=(ALLOW_200,), **kwargs):
    aws["entries"][:] = [dict(entry) for entry in entries]
    incident_id = _pending_secops(db, **kwargs)
    asset = assets_repo.get_asset_by_arn(db, EC2_ARN)
    asset.spec = {**asset.spec, "tags": {"Environment": "production"}}
    db.commit()
    return incident_id


def _trigger(db, incident_id, publish=None):
    return soar.trigger_isolation(db, incident_id, trigger_source=PRE, publish=publish)


def _independent_incident(db, *, source_ip="203.0.113.10"):
    intake = _secops_intake()
    event_id = str(uuid.uuid4())
    intake.threat_event = intake.threat_event.model_copy(update={
        "threat_event_id": event_id,
        "source_event_id": event_id,
        "deduplication_key": event_id,
        "occurred_at": datetime.now(UTC),
        "payload": intake.threat_event.payload.model_copy(update={"source_ip": source_ip}),
    })
    intake.initial_risk = intake.initial_risk.model_copy(update={"threat_event_id": event_id})
    return incident_intake.create_incident_from_intake(db, intake).incident_id


def _blocks(db, incident_id):
    return [r for r in exec_repo.list_by_incident(db, incident_id) if r.runbook_id is ADD_DENY]


def _kinds(db, incident_id):
    return [row.kind for row in events_repo.list_by_incident(db, incident_id)]


def _edit_threat(db, mutate):
    [row] = [
        r for r in incidents_repo.list_evidence(db, db.info["incident_id"])
        if r.evidence_type.value == "THREAT"
    ]
    content = copy.deepcopy(row.content)
    mutate(content)
    row.content = content
    db.commit()


def _context_missing(content):
    content["context"] = None


def _event_id_mismatch(content):
    content["event"]["threat_event_id"] = "9a8b7c6d-5e4f-4a3b-8c2d-1e0f9a8b7caa"


def _second_nacl(content):
    target = content["context"]["target"]["asset"]
    related = copy.deepcopy(content["context"]["related_assets"][0])
    other = NACL_ARN.replace("acl-0abc123456789def0", "acl-0abc123456789def1")
    related["asset"]["arn"] = other
    related["asset"]["resource_id"] = "acl-0abc123456789def1"
    relation = copy.deepcopy(target["relationships"][0])
    relation["target_arn"] = other
    target["relationships"].append(relation)
    content["context"]["related_assets"].append(related)


def _assert_untouched(db, aws, incident_id):
    assert _blocks(db, incident_id) == []
    assert aws["entries"] == [ALLOW_200]
    assert "create_network_acl_entry" not in aws["calls"]
    assert not [r for r in exec_repo.list_by_incident(db, incident_id) if r.runbook_id is ISOLATE]


def _no_isolation_notice(db, incident_id):
    """접수 알림(HIGH_INCIDENT_CREATED)은 있을 수 있다 — 격리 확인·시간 알림만 없어야 한다."""
    kinds = _kinds(db, incident_id)
    return kinds.count("PROTECTED_TARGET_ESCALATION") == 1 and "AUTO_ISOLATION" not in kinds


def _protected_escalation_recorded(db, incident_id):
    return _kinds(db, incident_id).count("PROTECTED_TARGET_ESCALATION") == 1


# ------------------------------------------------------------------ 성공: 계약 그대로


def _latency_records(caplog):
    return [r for r in caplog.records if r.getMessage() == "protected_source_block_latency"]


@pytest.mark.parametrize("owner", ["no_tenant", "valid_tenant"])
def test_protected_high_ssh_gets_one_server_only_source_deny(db, aws, caplog, owner):
    incident_id = _protected_incident(db, aws)
    tenant = None
    if owner == "valid_tenant":
        tenant = _tenant(db)  # NACL·EC2와 같은 계정의 소유 테넌트
        db.get(models.Incident, incident_id).tenant_id = tenant.tenant_id
        db.commit()
    seen = []
    for operation in ("describe_network_acls", "create_network_acl_entry"):
        aws["hooks"][operation] = lambda _kwargs: seen.append(
            getattr(_current_tenant.get(), "tenant_id", None)
        )
    events = []

    with caplog.at_level(logging.INFO):
        assert _trigger(db, incident_id, events.append) == "skipped"  # 격리는 생략, 격리 확인을 주장하지 않는다
    db.expire_all()

    # AWS 호출(규칙 번호 조회·가드레일 ④·백업·삽입)은 소유 테넌트 문맥에서 나간다
    assert seen and set(seen) == {None if tenant is None else tenant.tenant_id}
    [latency] = _latency_records(caplog)
    assert latency.incident_id == incident_id
    assert latency.runbook_id == ADD_DENY.value
    assert latency.source_block_end_to_end_ms >= 0
    assert not [key for key in vars(latency) if "isolation" in key]
    assert not [r for r in caplog.records if "isolation" in r.getMessage() and "confirm" in r.getMessage()]

    assert aws["entries"] == [ALLOW_200, _expected_entry()]
    assert set(aws["calls"]) <= {"describe_network_acls", "create_network_acl_entry"}  # EC2 쓰기 0
    [block] = _blocks(db, incident_id)
    assert (block.status, block.trigger_source, block.target_arn) == (
        ExecutionStatus.SUCCESS, PRE, NACL_ARN,
    )
    assert block.candidate_id is None
    assert block.validated_command["parameters"] == {
        "network_acl_id": "acl-0abc123456789def0", "rule_number": 1,
        "cidr_block": SOURCE_CIDR, "protocol": "tcp", "evidence_id": block.validated_command["evidence_ids"][0],
    }
    assert [r.runbook_id for r in exec_repo.list_by_incident(db, incident_id)] == [ADD_DENY]
    # 격리 확인·시간으로 보이는 알림·기한이 없다
    assert _no_isolation_notice(db, incident_id)
    assert db.get(models.Incident, incident_id).response_deadline_at is None
    # 귀속: 서버(SYSTEM)가 시작했고 접수 감사·활동 로그가 남는다
    [audit] = db.scalars(
        select(models.AuditEvent).where(models.AuditEvent.execution_id == block.execution_id)
    )
    assert (audit.trigger_source, audit.actor_kind.value) == (PRE, "SYSTEM")
    facts = {
        (row.action, row.actor_kind.value, row.outcome.value)
        for row in db.scalars(select(models.PlatformEvent).where(
            models.PlatformEvent.target_id == block.execution_id))
    }
    assert ("ACTION_AUTO_TRIGGER", "SYSTEM", "SUCCEEDED") in facts
    assert {fact[1] for fact in facts} == {"SYSTEM"}
    assert events  # 실행 확정이 화면에 발행된다


def test_replaying_the_same_incident_does_not_block_twice(db, aws, caplog):
    incident_id = _protected_incident(db, aws)
    with caplog.at_level(logging.INFO):
        _trigger(db, incident_id)
        creates = aws["calls"].count("create_network_acl_entry")
        assert len(_latency_records(caplog)) == 1

        assert _trigger(db, incident_id) == "skipped"
        assert _trigger(db, incident_id) == "skipped"

    assert len(_latency_records(caplog)) == 1  # 재전달은 지연 시간을 새로 내지 않는다

    assert creates == 1
    assert aws["calls"].count("create_network_acl_entry") == 1
    assert len(_blocks(db, incident_id)) == 1
    assert aws["entries"] == [ALLOW_200, _expected_entry()]
    assert _protected_escalation_recorded(db, incident_id)


# ------------------------------------------------------------------ 제외: 자동 차단하지 않는다


@pytest.mark.parametrize("case", [
    "medium", "ipv6", "context_missing", "event_id_mismatch", "ambiguous_nacl",
])
def test_ineligible_evidence_is_never_auto_blocked(db, aws, case):
    incident_id = _protected_incident(
        db, aws, medium=case == "medium", source_ip="2001:db8::10" if case == "ipv6" else None,
    )
    db.info["incident_id"] = incident_id
    mutate = {
        "context_missing": _context_missing, "event_id_mismatch": _event_id_mismatch,
        "ambiguous_nacl": _second_nacl,
    }.get(case)
    if mutate is not None:
        _edit_threat(db, mutate)

    _trigger(db, incident_id)

    _assert_untouched(db, aws, incident_id)


@pytest.mark.parametrize("state", ["auto_off", "suspended"])
def test_tenant_policy_blocks_the_automatic_source_deny(db, aws, state):
    incident_id = _protected_incident(db, aws)
    tenant = _tenant(db)
    db.get(models.Incident, incident_id).tenant_id = tenant.tenant_id
    if state == "auto_off":
        _toggle(db, tenant, False)
    else:
        from db.repositories import tenants as tenants_repo
        assert tenants_repo.suspend_tenant(
            db, tenant.tenant_id, reason="회수", now=datetime.now(UTC),
        )
    db.commit()

    _trigger(db, incident_id)

    _assert_untouched(db, aws, incident_id)


@pytest.mark.parametrize("role", ["ACTOR", "TARGET", None])
def test_guardduty_findings_stay_excluded_even_with_a_protected_nacl(
    db, aws, monkeypatch, make_tenant, role,
):
    aws["entries"][:] = [dict(ALLOW_200)]
    tenant_id = make_tenant()
    tenant = db.get(models.Tenant, tenant_id)
    account = tenant.aws_account_id
    arn = f"arn:aws:ec2:ap-northeast-2:{account}:instance/{INSTANCE_ID}"
    run = assets_repo.start_collection_run(
        db, account_id=account, region="ap-northeast-2", mode="localstack",
        lookback_days=3, period_seconds=3600,
    )
    for resource_arn, kind, rid in ((arn, AssetType.EC2, INSTANCE_ID),
                                    (NACL_ARN.replace("123456789012", account), AssetType.NACL, "acl-0abc123456789def0")):
        assets_repo.upsert_asset(
            db, arn=resource_arn, asset_type=kind, resource_id=rid, account_id=account,
            region="ap-northeast-2", state="running" if kind is AssetType.EC2 else None,
            spec={"tags": {"Environment": "production"}} if kind is AssetType.EC2 else {"is_default": False},
            collection_run_id=run.collection_run_id, collected_at=datetime.now(UTC),
        )
    asset = assets_repo.get_asset_by_arn(db, arn)
    asset.tenant_id = tenant_id
    assets_repo.replace_relationships(
        db, asset.asset_id,
        [(RelationType.PROTECTED_BY, NACL_ARN.replace("123456789012", account))],
        collection_run_id=run.collection_run_id,
    )
    db.commit()
    fake = FakeGuardDuty({"d-1": "ENABLED"})
    world = SimpleNamespace(
        db=db, tenant_id=tenant_id, account=account, fake=fake,
        now=datetime.now(UTC).replace(microsecond=0),
    )
    monkeypatch.setattr(guardduty, "aws_client", lambda *args: fake)
    finding = _finding(world, "protected", severity=8.0, instance=INSTANCE_ID)
    if role is not None:
        finding["Service"]["ResourceRole"] = role
    fake.findings = [finding]
    assert _ingest(world)["incidents_created"] == 1
    incident = _incident_of(world, _events(world)[0])

    _trigger(db, incident.incident_id)

    _assert_untouched(db, aws, incident.incident_id)


# ------------------------------------------------------------------ 실패: 에스컬레이션만 남는다


def _quota_failure(aws):
    def hook(_kwargs):
        # 확정된 4xx 거절 — 상태 코드가 있고 재전송이 없어야 NOT_APPLIED로 읽힌다(executor._effect_for)
        raise ClientError(
            {
                "Error": {"Code": "NetworkAclEntryLimitExceeded", "Message": "quota"},
                "ResponseMetadata": {"HTTPStatusCode": 400, "RetryAttempts": 0},
            },
            "CreateNetworkAclEntry",
        )

    aws["hooks"]["create_network_acl_entry"] = hook


@pytest.mark.parametrize("failure", ["precheck", "no_free_rule", "aws_quota"])
def test_unavailable_block_leaves_the_escalation_without_falling_back_to_isolation(
    db, aws, monkeypatch, failure,
):
    incident_id = _protected_incident(
        db, aws, entries=(
            {**ALLOW_200, "RuleNumber": 1} if failure == "no_free_rule" else ALLOW_200,
        ),
    )
    if failure == "precheck":
        monkeypatch.setattr(ex, "precheck", lambda *a, **k: _failing_precheck(None))
    if failure == "aws_quota":
        _quota_failure(aws)

    _trigger(db, incident_id)
    db.expire_all()

    assert _protected_escalation_recorded(db, incident_id)
    assert not [r for r in exec_repo.list_by_incident(db, incident_id) if r.runbook_id is ISOLATE]
    assert [e for e in aws["entries"] if e["CidrBlock"] == SOURCE_CIDR] == []
    blocks = _blocks(db, incident_id)
    if failure == "no_free_rule":
        assert blocks == []
    else:
        [block] = blocks
        assert block.status is ExecutionStatus.FAILED
    assert _no_isolation_notice(db, incident_id)
    assert db.get(models.Incident, incident_id).response_deadline_at is None
    if failure == "precheck":
        denied = list(db.scalars(select(models.PlatformEvent).where(
            models.PlatformEvent.target_id == incident_id,
            models.PlatformEvent.action == "ACTION_BLOCK",
        )))
        assert {row.actor_kind.value for row in denied} == {"SYSTEM"}
        assert any(row.details["reason_code"] == "PRECHECK_TARGET_NOT_FOUND" for row in denied)


def test_platform_event_failure_rolls_the_reservation_back(db, aws):
    incident_id = _protected_incident(db, aws)

    with failing_platform_event_write(PlatformEventAction.ACTION_AUTO_TRIGGER) as failed:
        with pytest.raises(IntegrityError):
            _trigger(db, incident_id)
    db.rollback()

    assert failed.count == 1
    assert _blocks(db, incident_id) == []
    assert "create_network_acl_entry" not in aws["calls"]
    assert _protected_escalation_recorded(db, incident_id)
    assert db.get(models.Incident, incident_id).status.value == "ANALYZING"


# ------------------------------------------------------------------ 사전 판정과 접수 사이 변경


def _tenant_auto_off(db, incident_id):
    tenant = _tenant(db)
    db.get(models.Incident, incident_id).tenant_id = tenant.tenant_id
    _toggle(db, tenant, False)
    db.commit()


def _tenant_suspended(db, incident_id):
    from db.repositories import tenants as tenants_repo
    tenant = _tenant(db)
    db.get(models.Incident, incident_id).tenant_id = tenant.tenant_id
    assert tenants_repo.suspend_tenant(db, tenant.tenant_id, reason="회수", now=datetime.now(UTC))
    db.commit()


def _evidence_changed(db, incident_id):
    db.info["incident_id"] = incident_id
    _edit_threat(db, _event_id_mismatch)


@pytest.mark.parametrize("mutation", [_tenant_auto_off, _tenant_suspended, _evidence_changed])
def test_change_between_precheck_and_reservation_is_not_executed(db, aws, monkeypatch, mutation):
    incident_id = _protected_incident(db, aws)

    def precheck_then_change(*args, **kwargs):
        mutation(db, incident_id)
        return _passing_precheck(None)

    monkeypatch.setattr(ex, "precheck", precheck_then_change)

    _trigger(db, incident_id)

    _assert_untouched(db, aws, incident_id)


# ------------------------------------------------------------------ 해제: 후보 없는 자동 차단


def _blocked_incident(db, aws):
    incident_id = _protected_incident(db, aws)
    _trigger(db, incident_id)
    [block] = _blocks(db, incident_id)
    assert block.status is ExecutionStatus.SUCCESS
    return incident_id, block


def test_manual_release_before_ttl_uses_the_original_backup(db, aws, audit_actor):
    incident_id, block = _blocked_incident(db, aws)
    dispatcher.dispatch_pending(db)  # 해제 제안 주기
    [offer] = release_candidates(db, incident_id)
    assert offer.parameters == {"rule_number": 1, "egress": False}  # 백업이 가리키는 우리가 넣은 규칙

    assert workflows.reserve_execution(
        db,
        ExecuteActionRequest(incident_id=incident_id, runbook_id=RESTORE, idempotency_key="release-598"),
        audit_actor=audit_actor,
    ).created
    dispatcher.dispatch_pending(db)

    [restore] = [r for r in exec_repo.list_by_incident(db, incident_id) if r.runbook_id is RESTORE]
    assert restore.status is ExecutionStatus.SUCCESS
    assert restore.trigger_source is TriggerSource.USER_APPROVAL
    assert restore.backup_record_id == exec_repo.get_execution(db, block.execution_id).backup_record_id
    assert aws["entries"] == [ALLOW_200]


def test_untouched_block_expires_after_24_hours_with_its_original_backup(db, aws):
    incident_id, block = _blocked_incident(db, aws)
    exec_repo.get_execution(db, block.execution_id).finished_at = (
        datetime.now(UTC) - timedelta(hours=25)
    )
    db.commit()

    report = dispatcher.dispatch_pending(db)

    assert report.expiry_reserved == 1
    [restore] = [r for r in exec_repo.list_by_incident(db, incident_id) if r.runbook_id is RESTORE]
    assert restore.trigger_source is TriggerSource.NACL_DENY_EXPIRED_24H
    assert restore.status is ExecutionStatus.SUCCESS
    assert restore.backup_record_id == exec_repo.get_execution(db, block.execution_id).backup_record_id
    assert aws["entries"] == [ALLOW_200]


@pytest.mark.parametrize("release", [
    "manual", "expiry", "failed", "pending", "unverified_restore", "unverified_block", "external_delete",
])
def test_independent_incidents_share_one_block_until_successful_release(db, aws, audit_actor, release):
    incident_id, block = _blocked_incident(db, aws)
    finished_at, backup_id = block.finished_at, block.backup_record_id
    second = _independent_incident(db)
    assert second != incident_id
    calls = list(aws["calls"])

    _trigger(db, second)

    assert _blocks(db, second) == []
    assert aws["calls"] == calls  # 사전 중복 판정은 규칙 번호 조회·가드레일 AWS 호출보다 앞선다
    assert aws["entries"] == [ALLOW_200, _expected_entry()]
    assert _no_isolation_notice(db, second)
    assert db.get(models.Incident, second).status.value == "ANALYZING"
    assert release_candidates(db, second) == []
    assert (block.finished_at, block.backup_record_id) == (finished_at, backup_id)

    if release in {"unverified_block", "external_delete"}:
        if release == "external_delete":
            aws["entries"][:] = [dict(ALLOW_200)]
        else:
            block.status = ExecutionStatus.UNVERIFIED
            block.verification_attempts = 1
            block.verification_reason_code = PrecheckReasonCode.PRECHECK_INVALID_STATE
            block.verification_first_failed_at = block.verification_last_failed_at = datetime.now(UTC)
            db.commit()
    else:
        if release == "expiry":
            block.finished_at = datetime.now(UTC) - timedelta(hours=25)
            db.commit()
            assert dispatcher.dispatch_pending(db).expiry_reserved == 1
        else:
            dispatcher.dispatch_pending(db)
            assert workflows.reserve_execution(
                db, ExecuteActionRequest(
                    incident_id=incident_id, runbook_id=RESTORE,
                    idempotency_key=f"release-cross-incident-{release}",
                ), audit_actor=audit_actor,
            ).created
            if release in {"failed", "unverified_restore"}:
                def refuse_delete(_kwargs):
                    raise ClientError({
                        "Error": {"Code": "UnauthorizedOperation", "Message": "denied"},
                        "ResponseMetadata": {"HTTPStatusCode": 403, "RetryAttempts": 0},
                    }, "DeleteNetworkAclEntry")
                aws["hooks"]["delete_network_acl_entry"] = refuse_delete
            if release != "pending":
                dispatcher.dispatch_pending(db)
        [restore] = [
            r for r in exec_repo.list_by_incident(db, incident_id) if r.runbook_id is RESTORE
        ]
        if release == "unverified_restore":
            assert restore.status is ExecutionStatus.FAILED
            restore.status = ExecutionStatus.UNVERIFIED
            restore.verification_attempts = 1
            restore.verification_reason_code = PrecheckReasonCode.PRECHECK_INVALID_STATE
            restore.verification_first_failed_at = restore.verification_last_failed_at = datetime.now(UTC)
            db.commit()
        else:
            expected = (
                ExecutionStatus.IN_PROGRESS if release == "pending" else
                ExecutionStatus.FAILED if release == "failed" else ExecutionStatus.SUCCESS
            )
            assert restore.status is expected

    third = _independent_incident(db)
    _trigger(db, third)

    if release in {"manual", "expiry"}:
        [next_block] = _blocks(db, third)
        assert next_block.status is ExecutionStatus.SUCCESS
        assert next_block.backup_record_id != backup_id
        assert next_block.finished_at > finished_at
        assert aws["entries"] == [ALLOW_200, _expected_entry()]
        assert aws["calls"].count("create_network_acl_entry") == 2
    else:
        assert _blocks(db, third) == []
        assert aws["calls"].count("create_network_acl_entry") == 1
        assert _no_isolation_notice(db, third)
    assert release_candidates(db, second) == []
    assert exec_repo.get_execution(db, block.execution_id).backup_record_id == backup_id


@pytest.mark.parametrize("completion", ["precheck", "target_lookup"])
def test_source_block_completed_between_precheck_and_reservation_is_not_duplicated(
    db, aws, monkeypatch, completion,
):
    _, block = _blocked_incident(db, aws)
    block.status = ExecutionStatus.IN_PROGRESS
    block.finished_at = None
    db.commit()
    second = _independent_incident(db)
    reads = []

    def finish_during_precheck(_kwargs):
        reads.append(True)
        if completion == "precheck" and len(reads) == 2:
            block.status = ExecutionStatus.SUCCESS
            block.finished_at = datetime.now(UTC)
            db.commit()

    active_query = exec_repo.list_active_on_target

    def finish_before_active_read(session, target_arn):
        # 완료 이력을 먼저 읽는 잘못된 순서라면 IN_PROGRESS를 보고 지나간 뒤,
        # 이 조회에서도 SUCCESS를 놓쳐 중복을 접수한다.
        block.status = ExecutionStatus.SUCCESS
        block.finished_at = datetime.now(UTC)
        session.flush()
        return active_query(session, target_arn)

    if completion == "target_lookup":
        monkeypatch.setattr(exec_repo, "list_active_on_target", finish_before_active_read)
    aws["hooks"]["describe_network_acls"] = finish_during_precheck
    _trigger(db, second)

    assert len(reads) == 2
    assert _blocks(db, second) == []
    assert aws["entries"] == [ALLOW_200, _expected_entry()]
    assert aws["calls"].count("create_network_acl_entry") == 1
    assert _no_isolation_notice(db, second)


@pytest.mark.parametrize("different", ["source", "nacl", "tenant"])
def test_source_block_dedup_does_not_suppress_other_owners(db, aws, audit_actor, different):
    _, original = _blocked_incident(db, aws)
    source_ip = "203.0.113.11" if different == "source" else "203.0.113.10"
    if different == "nacl":
        other_nacl = NACL_ARN.replace("acl-0abc123456789def0", "acl-0abc123456789def1")
        _seed_asset_row(
            db, arn=other_nacl, asset_type=AssetType.NACL,
            resource_id="acl-0abc123456789def1", spec={"is_default": False}, state=None,
        )
        asset = assets_repo.get_asset_by_arn(db, EC2_ARN)
        assets_repo.replace_relationships(
            db, asset.asset_id, [(RelationType.PROTECTED_BY, other_nacl)],
            collection_run_id=asset.last_collection_run_id,
        )
        db.commit()
        aws["entries"][:] = [dict(ALLOW_200)]
    second = _independent_incident(db, source_ip=source_ip)
    if different == "tenant":
        tenant = _tenant(db)
        db.get(models.Incident, second).tenant_id = tenant.tenant_id
        db.commit()

    _trigger(db, second)

    [block] = _blocks(db, second)
    assert block.status is ExecutionStatus.SUCCESS
    assert block.validated_command["parameters"]["cidr_block"] == f"{source_ip}/32"
    assert block.target_arn == (other_nacl if different == "nacl" else NACL_ARN)
    assert aws["calls"].count("create_network_acl_entry") == 2

    if different == "source":
        dispatcher.dispatch_pending(db)
        assert workflows.reserve_execution(
            db, ExecuteActionRequest(
                incident_id=second, runbook_id=RESTORE, idempotency_key="release-other-source",
            ), audit_actor=audit_actor,
        ).created
        dispatcher.dispatch_pending(db)
        [restore] = [r for r in exec_repo.list_by_incident(db, second) if r.runbook_id is RESTORE]
        assert restore.status is ExecutionStatus.SUCCESS
        assert restore.backup_record_id == block.backup_record_id != original.backup_record_id

        third = _independent_incident(db)
        _trigger(db, third)

        assert _blocks(db, third) == []
        assert aws["entries"] == [ALLOW_200, _expected_entry()]
        assert aws["calls"].count("create_network_acl_entry") == 2
