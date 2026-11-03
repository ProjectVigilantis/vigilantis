"""실행 루프의 테넌트 자격 문맥 (#533 E) — 실제 PostgreSQL 필요(미기동 시 skip).

지키는 것 — 실행 · 판정 · 자동 원복 · 가드레일 ④ 는 **기록의 소유 테넌트** 자격으로 나간다.
  - 스케줄러 잡(스레드 풀) 한 주기에 두 테넌트의 실행이 각자 자격 · 대상 ARN 의 리전으로 한 번씩 돈다.
  - 소속 없는 기록(NULL)은 「테넌트 없음」이다 — 바깥 문맥을 물려받지 않고 소유를 추론하지 않는다.
  - 문맥은 성공 · 예외 모두 되돌아간다.
  - 정지 뒤의 종료 판정 · 자동 원복도 그 자격으로 돈다(정지 관문 자체는 이 파일 몫이 아니다).
  - 가드레일 ③ 은 대상 ARN 의 계정이 소유 테넌트의 계정일 때만 통과한다(ADR-0012 §2).

AWS 는 aws_client 대역이다 — 클라이언트를 만든 순간의 문맥(aws_client 가 자격을 고르는 입력)을 적는다.
문맥 → 자격(AssumeRole · 서명)은 services/tests/test_aws_tenant_context.py 가 맡는다.
"""

import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError

CORE_API = Path(__file__).resolve().parents[1]
REPO_ROOT = Path(__file__).resolve().parents[3]
for p in (str(CORE_API), str(REPO_ROOT / "packages")):
    if p not in sys.path:
        sys.path.insert(0, p)

import dispatcher  # noqa: E402
import workflows  # noqa: E402
from db import models  # noqa: E402
from db.repositories import executions as exec_repo  # noqa: E402
from db.repositories import guardrails as guardrails_repo  # noqa: E402
from db.repositories import incidents as incidents_repo  # noqa: E402
from db.repositories import tenants as tenants_repo  # noqa: E402
from schemas.agents import AgentGraphOutput, RunbookCandidateDraft  # noqa: E402
from schemas.api.actions import ExecutionStatus  # noqa: E402
from schemas.api.assets import AssetType  # noqa: E402
from schemas.api.incidents import (  # noqa: E402
    IncidentCategory,
    IncidentStatus,
    ResponseMode,
    RiskLevel,
)
from schemas.backups import BackupType  # noqa: E402
from schemas.candidates import CandidateStatus  # noqa: E402
from schemas.evidence import EvidenceType  # noqa: E402
from schemas.guardrails import GuardrailDecision, GuardrailStep  # noqa: E402
from schemas.incidents import AgentInvocationStatus  # noqa: E402
from schemas.precheck import (  # noqa: E402
    PrecheckOutcome,
    VerificationMethod,
    build_verification_summary,
)
from schemas.runbooks import RunbookId, TriggerSource  # noqa: E402
from security import soar  # noqa: E402
from services.aws import backup as bk  # noqa: E402
from services.aws import client as client_module  # noqa: E402
from services.aws import executor as ex  # noqa: E402
from services.aws import rollback as rb  # noqa: E402
from services.aws.client import TenantAccess, tenant_scope  # noqa: E402

REGION = "ap-northeast-2"  # 테넌트 시드의 관제 리전(regions[0])
OTHER_REGION = "us-west-2"  # 테넌트 관제 리전과 다른 대상 리전 — 리전은 대상 ARN 에서 온다
INSTANCE_A = "i-0a1b2c3d4e5f6a7b8"
INSTANCE_B = "i-0b1b2c3d4e5f6a7b8"
LEGACY_INSTANCE = "i-0c1b2c3d4e5f6a7b8"
OTHER_GROUP = "sg-0d1b2c3d4e5f6a7b8"
LEGACY_ARN = f"arn:aws:ec2:{REGION}:123456789012:instance/{LEGACY_INSTANCE}"
BACKUP_TYPE = "t3.xlarge"  # 조치 이전 = 되돌릴 값
APPLIED_TYPE = "t3.medium"  # RIGHTSIZING 이 적용하는 값
SUMMARY = ["요약 1", "요약 2", "요약 3"]

RETRY_NOW = workflows.VerificationRetryPolicy(max_attempts=3, interval_seconds=0)


def cycle(db):
    return dispatcher.dispatch_pending(db, None, RETRY_NOW)


def instance_arn(region: str, account: str, instance_id: str) -> str:
    return f"arn:aws:ec2:{region}:{account}:instance/{instance_id}"


@dataclass(frozen=True)
class Owner:
    tenant_id: str
    account: str
    access: TenantAccess  # DB 행에서 옮긴 기대값 — 이 자격으로 나가야 한다


@pytest.fixture()
def owners(db, make_tenant):
    def _make(status=None) -> Owner:
        tenant_id = make_tenant(status)
        row = db.get(models.Tenant, tenant_id)
        return Owner(
            tenant_id, row.aws_account_id,
            TenantAccess(tenant_id, row.access_role_arn, row.external_id),
        )

    return _make


def suspend(db, owner: Owner) -> None:
    assert tenants_repo.suspend_tenant(db, owner.tenant_id, reason="테스트 정지", now=datetime.now(UTC))
    db.commit()


# --- AWS 대역 --------------------------------------------------------------------


@dataclass(frozen=True)
class Call:
    operation: str
    instance_id: str | None
    region: str
    owner: TenantAccess | None  # 클라이언트를 만든 순간의 문맥
    dry_run: bool


class _Ec2:
    def __init__(self, aws, region, owner):
        self._aws, self._region, self._owner = aws, region, owner

    def _record(self, operation, kwargs) -> str | None:
        instance_id = (kwargs.get("InstanceIds") or [kwargs.get("InstanceId")])[0]
        self._aws.calls.append(
            Call(operation, instance_id, self._region, self._owner, bool(kwargs.get("DryRun")))
        )
        return instance_id

    def get_waiter(self, name):
        return SimpleNamespace(wait=lambda **kwargs: self._record(f"wait:{name}", kwargs))

    def __getattr__(self, operation):
        def call(**kwargs):
            instance_id = self._record(operation, kwargs)
            if kwargs.get("DryRun"):
                raise ClientError({"Error": {"Code": "DryRunOperation"}}, operation)
            if operation == "describe_instances":
                return {"Reservations": [{"Instances": [{
                    "InstanceId": instance_id, "InstanceType": self._aws.instance_type,
                    "State": {"Name": "running"},
                }]}]}
            if operation == "stop_instances":
                return {"StoppingInstances": [
                    {"InstanceId": instance_id, "PreviousState": {"Name": "running"}}
                ]}
            if operation == "describe_instance_status":
                return {"InstanceStatuses": [{
                    "InstanceId": instance_id, "InstanceState": {"Name": "running"},
                    "SystemStatus": {"Status": "ok"}, "InstanceStatus": {"Status": "ok"},
                }]}
            return {}

        return call


class FakeAws:
    def __init__(self):
        self.calls: list[Call] = []
        self.instance_type = BACKUP_TYPE

    def factory(self, service, region=None, **_):
        return _Ec2(self, region, client_module.current_tenant())

    def contexts(self) -> dict[tuple[str | None, str], set]:
        """(대상 인스턴스, 리전) → 그 대상에 나간 호출들의 문맥."""
        out: dict[tuple[str | None, str], set] = {}
        for call in self.calls:
            out.setdefault((call.instance_id, call.region), set()).add(call.owner)
        return out

    def count(self, operation: str) -> dict[str | None, int]:
        out: dict[str | None, int] = {}
        for call in self.calls:
            if call.operation == operation and not call.dry_run:
                out[call.instance_id] = out.get(call.instance_id, 0) + 1
        return out


@pytest.fixture()
def aws(monkeypatch):
    """백업 캡처 · 실행 · 판정이 같은 대역을 본다 — 실제 런북 · 가드레일 경로가 돈다."""
    fake = FakeAws()
    for module in (bk, ex, rb):
        monkeypatch.setattr(module, "aws_client", fake.factory)
    return fake


# --- 시드 -----------------------------------------------------------------------


def _own(db, incident, tenant_id):
    incident.tenant_id = tenant_id
    db.flush()  # create_execution 이 인시던트의 소유를 SELECT 로 물려받는다


def _collected(
    db, arn: str, *, account: str, region: str, instance_id: str, tenant_id,
    asset_type: AssetType = AssetType.EC2,
):
    """가드레일 ③ 은 수집된 자산만 통과시킨다."""
    db.add(models.Asset(
        arn=arn, tenant_id=tenant_id, asset_type=asset_type, resource_id=instance_id,
        account_id=account, region=region, spec={"instance_type": APPLIED_TYPE},
    ))
    db.flush()


@pytest.fixture()
def reserve(db, make_incident, make_candidate):
    """관제자 승인으로 접수된 RIGHTSIZING 실행 1건 — 실행 행의 소유는 인시던트에서 물려받는다."""

    def _make(arn: str, tenant_id: str | None) -> str:
        incident = make_incident(
            db, category=IncidentCategory.FINOPS, subject_arn=arn, status=IncidentStatus.ANALYZING
        )
        _own(db, incident, tenant_id)
        candidate = make_candidate(
            db, incident, runbook_id=RunbookId.RUNBOOK_EC2_RIGHTSIZING, target_arn=arn,
            parameters={"target_instance_type": APPLIED_TYPE}, status=CandidateStatus.CLAIMED,
        )
        incidents_repo.update_incident_status(
            db, incident.incident_id, expected=IncidentStatus.ANALYZING,
            next_status=IncidentStatus.ACTION_IN_PROGRESS,
        )
        execution = exec_repo.create_execution(
            db, incident_id=incident.incident_id, runbook_id=RunbookId.RUNBOOK_EC2_RIGHTSIZING,
            target_arn=arn, trigger_source=TriggerSource.USER_APPROVAL,
            candidate_id=candidate.candidate_id,
        )
        db.commit()
        return execution.execution_id

    return _make


@pytest.fixture()
def rolled_back(db, make_incident, make_candidate):
    """2/2 실패로 「되돌려야 한다」고 남은 RIGHTSIZING 원본 — 백업이 결속돼 있다."""

    def _make(arn: str, instance_id: str, tenant_id: str) -> str:
        incident = make_incident(
            db, category=IncidentCategory.FINOPS, subject_arn=arn, status=IncidentStatus.ANALYZING
        )
        _own(db, incident, tenant_id)
        candidate = make_candidate(
            db, incident, runbook_id=RunbookId.RUNBOOK_EC2_RIGHTSIZING, target_arn=arn,
            parameters={"target_instance_type": APPLIED_TYPE}, status=CandidateStatus.CLAIMED,
        )
        incidents_repo.update_incident_status(
            db, incident.incident_id, expected=IncidentStatus.ANALYZING,
            next_status=IncidentStatus.ACTION_IN_PROGRESS,
        )
        origin = exec_repo.create_execution(
            db, incident_id=incident.incident_id, runbook_id=RunbookId.RUNBOOK_EC2_RIGHTSIZING,
            target_arn=arn, trigger_source=TriggerSource.USER_APPROVAL,
            candidate_id=candidate.candidate_id,
        )
        record = exec_repo.create_backup_record(
            db, execution_id=origin.execution_id, target_arn=arn,
            backup_type=BackupType.SAVE_INSTANCE_SPEC_JSON.value,
            payload={"instance_id": instance_id, "instance_type": BACKUP_TYPE, "state": "running"},
        )
        exec_repo.bind_backup_record(db, origin.execution_id, record.backup_record_id)
        exec_repo.update_execution_status(
            db, origin.execution_id, expected=ExecutionStatus.IN_PROGRESS,
            next_status=ExecutionStatus.ROLLBACK_INITIATED,
            error_summary="FAILED: 기동 실패 — 인스턴스 상태가 stopped입니다",
        )
        db.commit()
        return origin.execution_id

    return _make


def _evidence(db, incident_id: str) -> str:
    evidence = models.Evidence(
        incident_id=incident_id, evidence_type=EvidenceType.THREAT, source_type="threat_event",
        source_id="seed", content={}, occurred_at=datetime.now(UTC),
    )
    db.add(evidence)
    db.flush()
    return evidence.evidence_id


# --- 디스패처 — 실행 · 판정 ----------------------------------------------------------


def test_scheduled_cycle_runs_each_execution_once_under_its_owner(db, owners, reserve, aws):
    """스케줄러 잡처럼 문맥을 복사하지 않는 스레드에서 — 두 테넌트 · 소속 없는 기록이 각자 자격으로 한 번씩."""
    a, b = owners(), owners()
    reserve(instance_arn(REGION, a.account, INSTANCE_A), a.tenant_id)
    reserve(instance_arn(OTHER_REGION, b.account, INSTANCE_B), b.tenant_id)
    reserve(LEGACY_ARN, None)

    with ThreadPoolExecutor(max_workers=1) as worker:
        report = worker.submit(cycle, db).result()
        left_behind = worker.submit(client_module.current_tenant).result()

    assert (report.scanned, report.started, report.errored) == (3, 3, 0)
    assert aws.contexts() == {
        (INSTANCE_A, REGION): {a.access},
        (INSTANCE_B, OTHER_REGION): {b.access},
        (LEGACY_INSTANCE, REGION): {None},
    }
    # 테넌트마다 스캔을 되풀이하지 않는다 — 변경 호출은 대상마다 한 번이다
    assert aws.count("stop_instances") == {INSTANCE_A: 1, INSTANCE_B: 1, LEGACY_INSTANCE: 1}
    # 같은 작업 스레드의 다음 잡에 문맥이 남지 않는다
    assert left_behind is None


def test_ongoing_judgement_after_suspension_keeps_the_owner_credentials(
    db, owners, reserve, aws
):
    """단계를 남긴 실행의 종료 판정은 정지 뒤에도 그 테넌트 자격으로 돈다(ADR-0012 §3)."""
    b = owners()
    execution_id = reserve(instance_arn(OTHER_REGION, b.account, INSTANCE_B), b.tenant_id)
    assert cycle(db).awaiting_status_check == 1  # 실행 — 단계가 남았다
    suspend(db, b)
    aws.calls.clear()

    report = cycle(db)

    assert report.judged == 1
    assert aws.contexts() == {(INSTANCE_B, OTHER_REGION): {b.access}}
    assert exec_repo.get_execution(db, execution_id).status is ExecutionStatus.SUCCESS


def test_direct_dispatch_restores_the_caller_context_and_null_does_not_inherit_it(
    db, owners, reserve, aws, monkeypatch
):
    """즉시 실행(dispatch_now)도 실행 행의 소유로 돈다 — 예외로 끝나도 호출부 문맥이 되돌아오고,
    소속 없는 기록은 호출부의 테넌트 자격을 물려받지 않는다."""
    a, caller = owners(), owners()
    failing = reserve(instance_arn(REGION, a.account, INSTANCE_A), a.tenant_id)
    legacy = reserve(LEGACY_ARN, None)
    seen = {}
    real_runner = workflows.run_rightsizing_execution

    def runner(session, execution_id):
        seen[execution_id] = client_module.current_tenant()
        if execution_id == failing:
            raise RuntimeError("실행 결함 재현")
        return real_runner(session, execution_id)

    monkeypatch.setitem(dispatcher._RUNNERS, RunbookId.RUNBOOK_EC2_RIGHTSIZING, runner)

    with tenant_scope(caller.access):
        reports = [dispatcher.dispatch_now(db, failing), dispatcher.dispatch_now(db, legacy)]
        after = client_module.current_tenant()

    assert seen == {failing: a.access, legacy: None}
    assert [r.errored for r in reports] == [1, 0]
    assert aws.contexts() == {(LEGACY_INSTANCE, REGION): {None}}
    assert after == caller.access


# --- 자동 원복 ---------------------------------------------------------------------


def test_auto_rollback_child_runs_under_the_origin_owner_after_suspension(
    db, owners, rolled_back, aws
):
    """AUTO_ON_FAILURE 자식은 원본 인시던트의 소유를 물려받아 그 자격으로 가드레일 ④ · 원복을 돈다 —
    정지 뒤에 생겨도 끝까지 돈다(ADR-0012 §3)."""
    b = owners()
    arn = instance_arn(OTHER_REGION, b.account, INSTANCE_B)
    _collected(db, arn, account=b.account, region=OTHER_REGION, instance_id=INSTANCE_B,
               tenant_id=b.tenant_id)
    origin_id = rolled_back(arn, INSTANCE_B, b.tenant_id)
    aws.instance_type = APPLIED_TYPE
    suspend(db, b)

    assert cycle(db).rollback_started == 1
    [child] = exec_repo.list_rollback_children(db, origin_id)
    assert (child.tenant_id, child.trigger_source) == (b.tenant_id, TriggerSource.AUTO_ON_FAILURE)
    assert aws.calls == []  # 접수만 했다

    cycle(db)

    assert aws.contexts() == {(INSTANCE_B, OTHER_REGION): {b.access}}
    assert any(call.dry_run for call in aws.calls)  # 가드레일 ④
    assert aws.count("modify_instance_attribute") == {INSTANCE_B: 1}
    assert exec_repo.get_execution(db, child.execution_id).status is ExecutionStatus.SUCCESS


def test_rollback_guardrail_rejects_a_target_outside_the_owner_account(
    db, owners, rolled_back, aws
):
    """원복 대상 ARN 의 계정이 소유 테넌트의 계정이 아니면 ③ 에서 멈춘다 — AWS 를 부르지 않는다."""
    owner, other = owners(), owners()
    arn = instance_arn(REGION, other.account, INSTANCE_A)
    _collected(db, arn, account=other.account, region=REGION, instance_id=INSTANCE_A,
               tenant_id=other.tenant_id)
    origin_id = rolled_back(arn, INSTANCE_A, owner.tenant_id)
    cycle(db)
    [child] = exec_repo.list_rollback_children(db, origin_id)

    cycle(db)

    evaluation = guardrails_repo.latest_for_execution(db, child.execution_id)
    assert (evaluation.result, evaluation.failed_step) == (
        GuardrailDecision.FAIL, GuardrailStep.ARN_MATCH,
    )
    assert aws.calls == []
    assert exec_repo.get_execution(db, child.execution_id).status is ExecutionStatus.FAILED


# --- 가드레일 ④ 를 직접 부르는 워크플로 ----------------------------------------------------


def test_ai_candidates_are_guarded_under_the_incident_owner(db, owners, make_incident, aws):
    """AI 후보의 ④ 는 인시던트 소유 테넌트 자격으로 — 다른 테넌트 계정의 대상은 ③ 에서 거절한다."""
    a, b = owners(), owners()
    own_arn = instance_arn(REGION, a.account, INSTANCE_A)
    other_arn = f"arn:aws:ec2:{REGION}:{b.account}:security-group/{OTHER_GROUP}"
    _collected(db, own_arn, account=a.account, region=REGION, instance_id=INSTANCE_A,
               tenant_id=a.tenant_id)
    _collected(db, other_arn, account=b.account, region=REGION, instance_id=OTHER_GROUP,
               tenant_id=b.tenant_id, asset_type=AssetType.SG)
    incident = make_incident(
        db, category=IncidentCategory.FINOPS, subject_arn=own_arn, status=IncidentStatus.ANALYZING
    )
    _own(db, incident, a.tenant_id)
    evidence_id = _evidence(db, incident.incident_id)
    incidents_repo.claim_agent_invocation(db, incident.incident_id, started_at=datetime.now(UTC))
    db.commit()
    output = AgentGraphOutput(
        invocation_status=AgentInvocationStatus.SUCCEEDED, summary_lines=SUMMARY,
        candidates=[
            RunbookCandidateDraft(
                runbook_id=RunbookId.RUNBOOK_EC2_RIGHTSIZING, target_arn=own_arn,
                parameters={"target_instance_type": APPLIED_TYPE}, evidence_ids=[evidence_id],
            ),
            RunbookCandidateDraft(
                runbook_id=RunbookId.RUNBOOK_SG_DELETE_ISOLATED, target_arn=other_arn,
                parameters={}, evidence_ids=[evidence_id],
            ),
        ],
    )

    outcome = workflows.record_agent_analysis(db, incident.incident_id, output)

    assert (outcome.executable, outcome.rejected) == (1, 1)
    assert aws.contexts() == {(INSTANCE_A, REGION): {a.access}}
    rejected = next(
        c for c in incidents_repo.list_candidates(db, incident.incident_id)
        if c.status is CandidateStatus.REJECTED
    )
    assert rejected.target_arn == other_arn
    assert guardrails_repo.latest_for_candidate(db, rejected.candidate_id).failed_step is (
        GuardrailStep.ARN_MATCH
    )


@pytest.mark.parametrize("same_account", [True, False], ids=["owner-account", "other-tenant-account"])
def test_automatic_isolation_is_guarded_under_the_incident_owner(
    db, owners, make_incident, monkeypatch, same_account
):
    """High 선차단의 ④ 는 인시던트 소유 테넌트 자격으로 — 다른 테넌트 계정의 대상은 ③ 에서 멈춘다."""
    owner, other = owners(), owners()
    account = owner.account if same_account else other.account
    arn = instance_arn(REGION, account, INSTANCE_A)
    _collected(db, arn, account=account, region=REGION, instance_id=INSTANCE_A,
               tenant_id=owner.tenant_id if same_account else other.tenant_id)
    incident = make_incident(
        db, subject_arn=arn, status=IncidentStatus.ANALYZING,
        initial_risk_level=RiskLevel.HIGH, response_mode=ResponseMode.PRE_MITIGATION_0_5S,
    )
    _own(db, incident, owner.tenant_id)
    _evidence(db, incident.incident_id)
    db.commit()
    monkeypatch.setattr(workflows, "resolve_isolation_parameters", lambda _db, _arn: (
        workflows.IsolationParameters(
            target_group_arn=f"arn:aws:elasticloadbalancing:{REGION}:{account}:targetgroup/vig/abc",
            isolation_group_id="sg-0abc123456789def0",
        )
    ))
    prechecked = []

    def precheck(runbook_id, target_arn, parameters, **_):
        prechecked.append(client_module.current_tenant())
        return PrecheckOutcome(passed=True, verification_summary=build_verification_summary(
            VerificationMethod.DRY_RUN, verified=["AWS 대상 상태"], unverified=["IAM 권한(대역)"],
        ))

    monkeypatch.setattr(soar.executor, "precheck", precheck)

    _execution_id, _command, result = soar.prepare_isolation(
        db, incident.incident_id, TriggerSource.PRE_MITIGATION_0_5S
    )

    if same_account:
        assert (result.result, prechecked) == (GuardrailDecision.PASS, [owner.access])
    else:
        assert (result.result, result.failed_step, prechecked) == (
            GuardrailDecision.FAIL, GuardrailStep.ARN_MATCH, [],
        )
    assert client_module.current_tenant() is None
