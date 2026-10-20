"""#447 자동 격리 기능 통합 테스트 — 실제 PostgreSQL, AWS 클라이언트 경계만 대역.

실제로 지나는 것: receive_threat → 위험 판정 → prepare_isolation(실제 4단계 가드레일, 실제
workflows.resolve_isolation_parameters) → workflows.reserve_auto_isolation(원자 예약·평가 저장)
→ dispatcher → workflows.run_ec2_isolate_execution(store_sg_and_tg_mapping_backup,
executor.execute_ec2_isolate) → workflows.judge_ec2_isolate → close_execution.

대역은 `services.aws.executor.aws_client` / `services.aws.backup.aws_client`가 돌려주는 boto3
클라이언트 하나뿐이다(상태를 가진 가짜 EC2·ELBv2). 이 파일은 기능·순서·원자성을 본다.
30회 계측·500ms 목표·성능은 이 파일의 범위가 아니다. 실제 AWS가 격리된다는 증거도 아니다.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
import threat_ingress
import workflows
from botocore.exceptions import ClientError, EndpointConnectionError
from db import models
from db.repositories import executions as executions_repo
from db.repositories import incidents as incidents_repo
from db.repositories import notification_events as events_repo
from isolation_timing import begin_trace
from mock_threat_source import parse_observation
from schemas.api.actions import ExecutionStatus
from schemas.api.assets import AssetType, RelationType
from schemas.api.incidents import IncidentStatus, ResponseMode
from schemas.asset_roles import ROLE_ISOLATION, ROLE_TAG_KEY
from schemas.executions import ExecutionEffect
from schemas.guardrails import (
    GuardrailDecision,
    GuardrailStep,
    GuardrailStepStatus,
    GuardrailValidationContext,
)
from schemas.incidents import AgentInvocationStatus, AgentWaitSchedule
from schemas.runbooks import RunbookId, TriggerSource
from security import soar
from sqlalchemy import delete, select, update
from sqlalchemy.orm import sessionmaker

ROOT = Path(__file__).resolve().parents[3]
GOLDEN = ROOT / "datasets/golden/secops"
ISOLATE = RunbookId.RUNBOOK_EC2_ISOLATE
SOURCE_PREFIX = "test447it-"
ACCOUNT = "123456789012"
REGION = "ap-northeast-2"
PORT = 8080
PRE = TriggerSource.PRE_MITIGATION_0_5S
TIMEOUT = TriggerSource.TIMEOUT_ISOLATION_1M
MUTATIONS = {"elbv2.deregister_targets", "ec2.modify_network_interface_attribute"}
FLOW_PHASES = [
    "risk_evaluated", "incident_committed", "guardrail_passed", "execution_reserved",
    "backup_committed", "execution_started", "isolation_confirmed",
]


def _hex(n: int = 17) -> str:
    return uuid.uuid4().hex[:n]


def _raw(name: str, arn: str) -> dict:
    raw = json.loads((GOLDEN / "input" / f"{name}.json").read_text(encoding="utf-8"))
    raw["event_id"] = f"{SOURCE_PREFIX}{uuid.uuid4().hex}"
    raw["occurred_at"] = datetime.now(UTC).isoformat()
    raw["target_arn"] = arn
    return raw


# ---------------------------------------------------------------- 자산 시드 (실제 EC2/TG/SG + 관계)

@pytest.fixture
def committed_sessions(pg_engine):
    sessions = sessionmaker(bind=pg_engine, autoflush=False, expire_on_commit=False)
    yield sessions
    with sessions() as db:
        threats = list(db.scalars(select(models.ThreatEvent.threat_event_id).where(
            models.ThreatEvent.source_event_id.like(f"{SOURCE_PREFIX}%"))))
        incident_ids = list(db.scalars(select(models.Incident.incident_id).where(
            models.Incident.threat_event_id.in_(threats))))
        db.commit()
        db.execute(delete(models.Evidence).where(models.Evidence.incident_id.in_(incident_ids)))
        db.execute(delete(models.Incident).where(models.Incident.incident_id.in_(incident_ids)))
        db.execute(delete(models.ThreatEvent).where(models.ThreatEvent.threat_event_id.in_(threats)))
        db.commit()


@pytest.fixture
def world(committed_sessions):
    """커밋된 EC2(두 ENI)·TG·격리 SG 자산과 REGISTERED_IN 관계. 정리까지 소유한다."""
    instance_id = f"i-{_hex()}"
    vpc = f"vpc-{_hex()}"
    tg_name = f"tg-{_hex(8)}"
    w = SimpleNamespace(
        instance_id=instance_id,
        arn=f"arn:aws:ec2:{REGION}:{ACCOUNT}:instance/{instance_id}",
        tg_arn=f"arn:aws:elasticloadbalancing:{REGION}:{ACCOUNT}:targetgroup/{tg_name}/0123456789abcdef",
        isolation_sg=f"sg-{_hex()}", web_sg=f"sg-{_hex()}", ssh_sg=f"sg-{_hex()}", db_sg=f"sg-{_hex()}",
        eni1=f"eni-{_hex()}", eni2=f"eni-{_hex()}", port=PORT,
    )
    sg_arn = f"arn:aws:ec2:{REGION}:{ACCOUNT}:security-group/{w.isolation_sg}"
    with committed_sessions() as s:
        ec2 = models.Asset(
            arn=w.arn, asset_type=AssetType.EC2, resource_id=instance_id, account_id=ACCOUNT,
            region=REGION, spec={"vpc_id": vpc},
        )
        s.add_all([
            ec2,
            models.Asset(arn=w.tg_arn, asset_type=AssetType.ALB_TARGET_GROUP, resource_id=tg_name,
                         account_id=ACCOUNT, region=REGION, spec={}),
            models.Asset(arn=sg_arn, asset_type=AssetType.SG, resource_id=w.isolation_sg,
                         account_id=ACCOUNT, region=REGION,
                         spec={"vpc_id": vpc, "tags": {ROLE_TAG_KEY: ROLE_ISOLATION}}),
        ])
        s.flush()
        s.add(models.AssetRelationship(
            source_asset_id=ec2.asset_id, relation_type=RelationType.REGISTERED_IN, target_arn=w.tg_arn))
        s.commit()
    yield w
    with committed_sessions() as s:
        ids = list(s.scalars(select(models.ActionExecution.execution_id).where(
            models.ActionExecution.target_arn == w.arn)))
        s.execute(update(models.ActionExecution).where(
            models.ActionExecution.execution_id.in_(ids)).values(backup_record_id=None))
        for table in (models.ExecutionStep, models.BackupRecord, models.GuardrailEvaluation):
            s.execute(delete(table).where(table.execution_id.in_(ids)))
        s.execute(delete(models.ActionExecution).where(models.ActionExecution.execution_id.in_(ids)))
        s.commit()
        # 인시던트는 committed_sessions가 지우지만 실행이 먼저 사라져야 한다 — 자산은 마지막에
        ec2_id = s.scalar(select(models.Asset.asset_id).where(models.Asset.arn == w.arn))
        s.execute(delete(models.AssetRelationship).where(
            models.AssetRelationship.source_asset_id == ec2_id))
        s.execute(delete(models.Asset).where(models.Asset.arn.in_([w.arn, w.tg_arn, sg_arn])))
        s.commit()


# ---------------------------------------------------------------- 상태를 가진 가짜 AWS (경계 대역)

def _client_error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": ""},
                        "ResponseMetadata": {"RequestId": "req", "HTTPStatusCode": 412}}, "Op")


class FakeAwsWorld:
    """ec2·elbv2를 한 기록에 모은다. 변경 호출은 실제로 상태를 바꿔 판정이 같은 상태를 읽는다."""

    def __init__(self, w, sessions):
        self.w, self.sessions = w, sessions
        self.enis = {w.eni1: [w.web_sg, w.ssh_sg], w.eni2: [w.db_sg]}
        self.registered = True
        self.calls: list[SimpleNamespace] = []
        self.overrides: dict[str, list] = {}
        self.watch = None  # 호출 순간 열린 트랜잭션이 없어야 하는 세션
        self.probe = None  # 첫 변경 호출 순간 별도 세션이 본 값

    def factory(self, service, region=None, **_):
        return _Client(self, service)

    def set(self, key, *outcomes):
        self.overrides[key] = list(outcomes)

    @property
    def mutations(self):
        return [c for c in self.calls if c.mutation]

    def health(self):
        if self.registered:
            return {"TargetHealthDescriptions": [{"Target": {"Id": self.w.instance_id, "Port": PORT},
                                                  "TargetHealth": {"State": "healthy"}}]}
        return {"TargetHealthDescriptions": [{"Target": {"Id": self.w.instance_id, "Port": 80},
                                              "TargetHealth": {"State": "unused",
                                                               "Reason": "Target.NotRegistered"}}]}

    def instance(self):
        return {"Reservations": [{"Instances": [{
            "InstanceId": self.w.instance_id,
            "NetworkInterfaces": [
                {"NetworkInterfaceId": eni, "Attachment": {"DeviceIndex": index},
                 "Groups": [{"GroupId": g} for g in groups]}
                for index, (eni, groups) in enumerate(self.enis.items())],
        }]}]}

    def probe_first_mutation(self):
        with self.sessions() as s:
            row = s.execute(select(models.ActionExecution).where(
                models.ActionExecution.target_arn == self.w.arn)).scalar_one()
            record = (s.get(models.BackupRecord, row.backup_record_id)
                      if row.backup_record_id else None)
            self.probe = SimpleNamespace(
                execution_id=row.execution_id, status=row.status,
                backup_record_id=row.backup_record_id,
                backup=None if record is None else SimpleNamespace(
                    execution_id=record.execution_id, backup_type=record.backup_type,
                    payload=record.payload),
            )


class _Client:
    def __init__(self, world: FakeAwsWorld, service: str):
        self._world, self._service = world, service

    def __getattr__(self, operation):
        world = self._world
        key = f"{self._service}.{operation}"

        def call(**kwargs):
            dry = bool(kwargs.get("DryRun"))
            mutation = key in MUTATIONS and not dry
            if mutation and world.probe is None:
                world.probe_first_mutation()
            world.calls.append(SimpleNamespace(
                key=key, kwargs=kwargs, mutation=mutation, dry_run=dry,
                in_transaction=None if world.watch is None else world.watch.in_transaction()))
            queue = world.overrides.get(key)
            outcome = queue.pop(0) if queue else None
            if isinstance(outcome, BaseException):
                raise outcome
            if outcome is not None:
                return outcome
            if key == "ec2.describe_instances":
                return world.instance()
            if key == "elbv2.describe_target_health":
                return world.health()
            if key == "ec2.modify_network_interface_attribute":
                if dry:
                    raise _client_error("DryRunOperation")
                world.enis[kwargs["NetworkInterfaceId"]] = list(kwargs["Groups"])
            if key == "elbv2.deregister_targets":
                world.registered = False
            return {"ResponseMetadata": {"RequestId": "req-ok"}}

        return call


@pytest.fixture
def aws(monkeypatch, world, committed_sessions):
    fake = FakeAwsWorld(world, committed_sessions)
    from services.aws import backup as backup_module
    from services.aws import executor as executor_module
    monkeypatch.setattr(executor_module, "aws_client", fake.factory)
    monkeypatch.setattr(backup_module, "aws_client", fake.factory)
    return fake


# ---------------------------------------------------------------- 조회 도우미

def _receive(committed_sessions, aws, world, name):
    with committed_sessions() as db:
        aws.watch = db
        with begin_trace() as trace:
            outcome = threat_ingress.receive_threat(db, parse_observation(_raw(name, world.arn)))
    return outcome, trace.finish("failed")  # 이미 종료된 trace는 첫 결과를 돌려준다


def _snapshot(committed_sessions, incident_id):
    with committed_sessions() as s:
        executions = executions_repo.list_by_incident(s, incident_id)
        (execution,) = executions
        return SimpleNamespace(
            incident=incidents_repo.get_incident(s, incident_id),
            execution=execution,
            steps=executions_repo.list_steps(s, execution.execution_id),
            evidence_ids=[e.evidence_id for e in incidents_repo.list_evidence(s, incident_id)],
            evaluations=list(s.scalars(select(models.GuardrailEvaluation).where(
                models.GuardrailEvaluation.execution_id == execution.execution_id))),
            backups=list(s.scalars(select(models.BackupRecord).where(
                models.BackupRecord.execution_id == execution.execution_id))),
        )


def _expected_command(world, evidence_ids):
    return {
        "runbook_id": ISOLATE.value,
        "target_arn": world.arn,
        "parameters": {
            "instance_id": world.instance_id,
            "target_group_arn": world.tg_arn,
            "isolation_group_id": world.isolation_sg,
            "evidence_id": evidence_ids[0],
        },
        "evidence_ids": evidence_ids,
    }


def _assert_full_trace(record, *, incident_id, execution_id, phases):
    """phase는 이름·순서·식별자만 본다 — 시간 목표는 단언하지 않는다."""
    assert record["outcome"] == "success" and record["confirmed"] is True
    assert record["incident_id"] == incident_id and record["execution_id"] == execution_id
    elapsed = [record["phases"][name]["elapsed_ms"] for name in phases]
    assert all(value is not None for value in elapsed), record["phases"]
    assert elapsed == sorted(elapsed)  # 단조 시계라 도착 순서 = 값 순서


# ==============================================================================
# High 선차단 — 전체 흐름
# ==============================================================================

def test_high_full_flow_isolates_and_confirms(committed_sessions, aws, world):
    outcome, record = _receive(committed_sessions, aws, world, "evt_ssh_bruteforce_001")
    assert outcome.created
    snap = _snapshot(committed_sessions, outcome.incident_id)
    execution = snap.execution

    # 접수 → 위험 판정(High) → 실제 4단계 가드레일 PASS가 명령·평가로 원자 저장됐다
    assert snap.incident.response_mode is ResponseMode.PRE_MITIGATION_0_5S
    assert execution.trigger_source is PRE and execution.runbook_id is ISOLATE
    assert execution.validated_command == _expected_command(world, snap.evidence_ids)
    (evaluation,) = snap.evaluations
    assert evaluation.validation_context is GuardrailValidationContext.AUTO_ISOLATION
    assert evaluation.result is GuardrailDecision.PASS
    assert [s["result"] for s in evaluation.steps] == [GuardrailStepStatus.PASS.value] * 4
    assert evaluation.validated_command == execution.validated_command

    # 백업은 첫 AWS 변경 전에 커밋됐고 실행에 결속돼 별도 세션이 본다
    assert aws.probe is not None and aws.probe.execution_id == execution.execution_id
    assert aws.probe.backup_record_id is not None
    assert aws.probe.backup.execution_id == execution.execution_id
    assert aws.probe.backup.payload == {
        "security_group_ids": [world.web_sg, world.ssh_sg],
        "target_group_arn": world.tg_arn, "target_port": PORT,
        "network_interfaces": [
            {"network_interface_id": world.eni1, "security_group_ids": [world.web_sg, world.ssh_sg]},
            {"network_interface_id": world.eni2, "security_group_ids": [world.db_sg]},
        ],
        # ASG 태그가 없는 대상 — 보호 관측은 ASG를 조회하지 않고 NOT_MANAGED로 남는다(#446)
        "scale_in_protection": {
            "status": "NOT_MANAGED",
            "asg_name": None,
            "previous_protected": None,
            "autoscaling_execution_id": None,
            "reason": "NOT_ASG_MEMBER",
        },
    }
    (backup,) = snap.backups
    assert execution.backup_record_id == backup.backup_record_id == aws.probe.backup_record_id

    # AWS 변경은 TG 해제 → ENI별 SG 교체 순이고 대상·값이 백업·명령 그대로다
    assert [(c.key, c.kwargs) for c in aws.mutations] == [
        ("elbv2.deregister_targets",
         {"TargetGroupArn": world.tg_arn, "Targets": [{"Id": world.instance_id, "Port": PORT}]}),
        ("ec2.modify_network_interface_attribute",
         {"NetworkInterfaceId": world.eni1, "Groups": [world.isolation_sg]}),
        ("ec2.modify_network_interface_attribute",
         {"NetworkInterfaceId": world.eni2, "Groups": [world.isolation_sg]}),
    ]
    dry_runs = [c for c in aws.calls if c.dry_run]
    assert [(c.kwargs["NetworkInterfaceId"], c.kwargs["Groups"]) for c in dry_runs] == [
        (world.eni1, [world.isolation_sg])]
    assert aws.calls.index(dry_runs[0]) < aws.calls.index(aws.mutations[0])
    assert [s.sequence for s in snap.steps] == [1, 2]
    assert [s.effect for s in snap.steps] == [ExecutionEffect.APPLIED] * 2

    # 판정은 변경 뒤의 실제 상태를 AWS에서 다시 읽었고, 그 뒤에야 SUCCESS로 닫혔다
    last_change = max(i for i, c in enumerate(aws.calls) if c.mutation)
    assert any(c.key.startswith(("ec2.describe", "elbv2.describe"))
               for c in aws.calls[last_change + 1:])
    assert execution.status is ExecutionStatus.SUCCESS
    assert snap.incident.status is not IncidentStatus.ACTION_IN_PROGRESS

    # 외부 호출 순간 열린 트랜잭션이 없고, 추적은 모든 단계와 식별자를 갖는다
    assert [c.in_transaction for c in aws.calls] == [False] * len(aws.calls)
    _assert_full_trace(record, incident_id=outcome.incident_id,
                       execution_id=execution.execution_id, phases=FLOW_PHASES)
    with committed_sessions() as reopened:
        facts = events_repo.list_by_incident(reopened, outcome.incident_id)
        assert [(row.kind, row.phase, row.outcome) for row in facts] == [
            ("HIGH_INCIDENT_CREATED", "CREATED", None),
            ("AUTO_ISOLATION", "REQUESTED", None),
            ("AUTO_ISOLATION", "RESULT", "SUCCESS"),
        ]
        assert all(row.aws_account_id == ACCOUNT for row in facts)


def test_guardrail_denial_persists_failed_attempt_and_never_touches_aws(
    committed_sessions, aws, world,
):
    aws.set("elbv2.describe_target_health", aws.health() | {"TargetHealthDescriptions": []})
    outcome, record = _receive(committed_sessions, aws, world, "evt_ssh_bruteforce_001")
    snap = _snapshot(committed_sessions, outcome.incident_id)
    execution = snap.execution

    assert execution.status is ExecutionStatus.FAILED and execution.validated_command is None
    (evaluation,) = snap.evaluations
    assert evaluation.result is GuardrailDecision.FAIL
    assert evaluation.failed_step is GuardrailStep.AWS_DRY_RUN
    assert aws.mutations == [] and aws.probe is None
    assert snap.backups == [] and snap.steps == []
    assert [c.in_transaction for c in aws.calls] == [False] * len(aws.calls)
    assert record["outcome"] == "failed" and record["confirmed"] is False
    assert record["end_to_end_ms"] is None
    for name in ("backup_committed", "execution_started", "isolation_confirmed", "guardrail_passed"):
        assert record["phases"][name]["elapsed_ms"] is None

    # 재전달은 같은 거절을 되풀이하지 않는다 — 새 AWS 호출도 새 실행도 없다
    calls_before = len(aws.calls)
    with committed_sessions() as db, begin_trace():
        assert soar.trigger_isolation(db, outcome.incident_id, trigger_source=PRE) == "skipped"
    assert len(aws.calls) == calls_before
    assert _snapshot(committed_sessions, outcome.incident_id).execution.status is ExecutionStatus.FAILED


def test_backup_failure_prevents_every_aws_mutation(committed_sessions, aws, world):
    # 가드레일 ④의 조회는 통과하고, 백업 캡처의 두 번째 조회만 실패한다
    aws.set("ec2.describe_instances", None, EndpointConnectionError(endpoint_url="x"))
    outcome, record = _receive(committed_sessions, aws, world, "evt_ssh_bruteforce_001")
    snap = _snapshot(committed_sessions, outcome.incident_id)

    (evaluation,) = snap.evaluations
    assert evaluation.result is GuardrailDecision.PASS  # 검증은 통과했고 실행 준비에서 멈췄다
    assert snap.execution.status is ExecutionStatus.FAILED
    assert snap.execution.backup_record_id is None and snap.backups == []
    assert aws.mutations == [] and aws.probe is None
    assert snap.steps == []
    assert [c.in_transaction for c in aws.calls] == [False] * len(aws.calls)
    assert aws.registered and aws.enis[world.eni1] == [world.web_sg, world.ssh_sg]  # 자산 그대로
    assert record["outcome"] == "failed" and record["confirmed"] is False
    assert record["phases"]["execution_reserved"]["elapsed_ms"] is not None
    assert record["phases"]["backup_committed"]["elapsed_ms"] is None
    assert record["phases"]["execution_started"]["elapsed_ms"] is None
    assert record["phases"]["isolation_confirmed"]["elapsed_ms"] is None


def test_loser_of_reservation_race_waits_for_winner_execution(
    committed_sessions, aws, world, monkeypatch,
):
    """분석 쪽 준비 중 접수 쪽이 먼저 예약했다 — 패자는 승자를 끝까지 통과시킨 뒤 skipped로 돌아온다."""
    real_prepare = soar.prepare_isolation
    state = SimpleNamespace(incident_id=None, raced=False)

    def prepare_then_lose(db, incident_id, trigger_source):
        prepared = real_prepare(db, incident_id, trigger_source)
        if not state.raced:
            state.raced = True
            # 같은 시점에 승자가 별도 세션에서 준비·예약만 끝냈다(아직 실행 전)
            with committed_sessions() as winner:
                execution_id, command, result = real_prepare(winner, incident_id, trigger_source)
                reserved = workflows.reserve_auto_isolation(
                    winner, incident_id, trigger_source=trigger_source,
                    now=datetime.now(UTC), execution_id=execution_id,
                    validated_command=command, guardrail_result=result)
                assert reserved.created and reserved.execution_status is ExecutionStatus.IN_PROGRESS
                state.winner_id = execution_id
        return prepared

    # 접수 단계의 자동 발동을 막고(접수만 만든다) 분석 쪽 호출을 패자로 직접 실행한다
    monkeypatch.setattr(threat_ingress, "trigger_isolation", lambda *a, **k: "deferred")
    outcome, _ = _receive(committed_sessions, aws, world, "evt_ssh_bruteforce_001")
    monkeypatch.setattr(soar, "prepare_isolation", prepare_then_lose)
    with committed_sessions() as db, begin_trace() as trace:
        aws.watch = db
        assert soar.trigger_isolation(db, outcome.incident_id, trigger_source=PRE) == "skipped"
        assert trace.execution_id is None  # 패자는 승자의 실행을 자기 응답 시간으로 묶지 않는다

    snap = _snapshot(committed_sessions, outcome.incident_id)  # 실행은 정확히 1건
    assert snap.execution.execution_id == state.winner_id
    assert snap.execution.status is ExecutionStatus.SUCCESS  # 돌아온 시점에 승자는 이미 종료됐다
    assert len(aws.mutations) == 3 and len(snap.evaluations) == 1
    assert [c.in_transaction for c in aws.calls] == [False] * len(aws.calls)


# ==============================================================================
# Medium 기한 감시 — DB에 저장된 기한만 본다 (재시작 포함)
# ==============================================================================

def _start_wait(committed_sessions, incident_id, *, started_ago: int):
    """분석 완료 직후의 승인 대기를 record_agent_analysis와 같은 저장소 호출로 만든다."""
    started = datetime.now(UTC) - timedelta(seconds=started_ago)
    with committed_sessions() as s:
        assert incidents_repo.claim_agent_invocation(s, incident_id, started_at=started)
        assert incidents_repo.finish_agent_invocation(s, incident_id, AgentInvocationStatus.SUCCEEDED)
        assert incidents_repo.update_incident_status(
            s, incident_id, expected=IncidentStatus.ANALYZING, next_status=IncidentStatus.AWAITING_APPROVAL)
        assert incidents_repo.set_agent_wait(s, AgentWaitSchedule(
            incident_id=incident_id, started_at=started,
            response_deadline_at=started + timedelta(seconds=60)))
        s.commit()


def test_medium_expired_wait_is_isolated_by_first_cycle_after_restart(
    committed_sessions, aws, world, caplog,
):
    # 접수·타임아웃 로그가 모두 켜진 환경에서도 해당 실행의 계측만 고른다.
    caplog.set_level(logging.INFO, logger="vigilantis.isolation_timing")
    outcome, _ = _receive(committed_sessions, aws, world, "evt_ssh_bruteforce_006")  # MEDIUM
    assert aws.calls == []  # 접수만으로는 아무 AWS 호출도 없다
    with committed_sessions() as s:
        assert incidents_repo.get_incident(s, outcome.incident_id).response_mode is ResponseMode.AGENT_WAIT

    # 기한 전: 감시 주기는 대상을 잡지 않는다
    _start_wait(committed_sessions, outcome.incident_id, started_ago=10)
    assert soar.run_timeout_cycle(committed_sessions).scanned == 0
    assert aws.calls == []

    # 기한이 지났다. 프로세스 재시작 = 새 세션 팩토리가 같은 DB 기한만으로 집는다
    long_ago = datetime.now(UTC) - timedelta(seconds=120)
    with committed_sessions() as s:
        s.execute(update(models.Incident).where(
            models.Incident.incident_id == outcome.incident_id
        ).values(agent_wait_started_at=long_ago,
                 response_deadline_at=long_ago + timedelta(seconds=60)))
        s.commit()
    base = sessionmaker(bind=committed_sessions.kw["bind"], autoflush=False, expire_on_commit=False)

    def restarted():
        session = base()
        aws.watch = session  # 감시 주기가 쓰는 바로 그 세션의 트랜잭션 상태를 외부 호출마다 본다
        return session

    with caplog.at_level(logging.INFO, logger="vigilantis.isolation_timing"):
        report = soar.run_timeout_cycle(restarted)
    assert (report.scanned, report.success, report.errored) == (1, 1, 0)

    snap = _snapshot(committed_sessions, outcome.incident_id)
    assert snap.execution.trigger_source is TIMEOUT
    assert snap.execution.status is ExecutionStatus.SUCCESS
    assert snap.incident.response_mode is ResponseMode.TIMEOUT_ISOLATION_1M
    with committed_sessions() as reopened:
        facts = events_repo.list_by_incident(reopened, outcome.incident_id)
        assert [(row.kind, row.phase, row.outcome) for row in facts] == [
            ("AUTO_ISOLATION", "REQUESTED", None),
            ("AUTO_ISOLATION", "RESULT", "SUCCESS"),
        ]
        assert all(row.trigger_source is TIMEOUT for row in facts)
    assert snap.execution.validated_command == _expected_command(world, snap.evidence_ids)
    assert aws.probe.backup_record_id == snap.execution.backup_record_id is not None
    assert len(aws.mutations) == 3
    assert [c.in_transaction for c in aws.calls] == [False] * len(aws.calls)

    (record,) = [
        r for r in caplog.records
        if r.getMessage() == "isolation_latency"
        and r.trigger_source == TIMEOUT.value
        and r.execution_id == snap.execution.execution_id
    ]
    assert record.outcome == "success" and record.confirmed is True
    assert record.start_boundary == "timeout_scan" and record.trigger_source == TIMEOUT.value
    assert record.execution_id == snap.execution.execution_id
    assert record.scan_to_confirmation_ms is not None and record.end_to_end_ms is None
    scan_phases = ["guardrail_passed", "execution_reserved", "backup_committed",
                   "execution_started", "isolation_confirmed"]
    elapsed = [record.phases[name]["elapsed_ms"] for name in scan_phases]
    assert all(v is not None for v in elapsed) and elapsed == sorted(elapsed)

    # 다음 주기는 같은 건을 다시 집지 않는다 — 두 번째 격리·AWS 변경 없음
    assert soar.run_timeout_cycle(restarted).scanned == 0
    assert len(aws.mutations) == 3
