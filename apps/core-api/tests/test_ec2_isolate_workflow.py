"""EC2 격리·해제 워크플로 단위 테스트 — **DB·AWS 불필요**. (MVP 체크리스트 §2-1 B1–B3)

AWS 호출 분기는 services/tests/test_execute_ec2_isolate.py가 맡고, 여기서는 workflows의
**순서·원천·판정**을 본다. 저장소 계층(executions_repo)을 가짜로 갈아 끼우고 db.commit을
AWS 호출과 **한 기록**에 남겨, 서로의 앞뒤를 직접 대조한다.

  - 격리: 백업이 **첫 AWS 변경 이전에** commit되고, 캡처가 실패하면 변경 호출이 0건이다
    (ADR-0008 §1 ①②)
  - 해제: 복원 값은 **결속된 백업 레코드에서만** 오고, 가드레일 4단계가 AWS 변경보다
    먼저다(ADR-0004 정책 ①③). 백업이 없거나 종류가 다르면 시작하지 않는다
  - 판정: 격리 완료·미적용·부분 적용, 해제 완료·미완을 실자산으로 가른다

PostgreSQL이 필요한 통합 경로(실제 커밋·Incident 전이)는 같은 모양의 SG 2종 통합 테스트
(test_sg_delete_workflow.py)가 이미 지키는 공통 코드(close_execution·_step_recorder)를 탄다.
"""

import contextlib
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from botocore.exceptions import EndpointConnectionError

CORE_API = Path(__file__).resolve().parents[1]
REPO_ROOT = Path(__file__).resolve().parents[3]
for p in (str(CORE_API), str(REPO_ROOT / "packages")):
    if p not in sys.path:
        sys.path.insert(0, p)

import workflows
from schemas.api.actions import ExecutionStatus
from schemas.backups import (
    BackupType,
    ScaleInProtectionBackup,
    SgAndTgMappingBackup,
)
from schemas.precheck import PrecheckReasonCode
from schemas.runbooks import RunbookId
from services.aws import backup as bk
from services.aws import executor as ex

# --- 가짜 AWS — services/tests/test_execute_ec2_isolate.py와 같은 모양이다 ----------------
# 테스트 파일끼리 import하지 않는다(pyproject.toml importlib 모드 주석) — 필요한 만큼 옮겨 둔다.
ACCOUNT = "123456789012"
REGION = "ap-northeast-2"
INSTANCE = "i-0abc123456789def0"
INSTANCE_ARN = f"arn:aws:ec2:{REGION}:{ACCOUNT}:instance/{INSTANCE}"
TG_ARN = f"arn:aws:elasticloadbalancing:{REGION}:{ACCOUNT}:targetgroup/vigilantis-smoke-tg/0123456789abcdef"
ISOLATION_SG = "sg-0150150150150150a"
PRIMARY_ENI = "eni-0aaa111122223333a"
SECONDARY_ENI = "eni-0bbb444455556666b"
WEB_SG = "sg-0aaa111122223333a"
SSH_SG = "sg-0aaa444455556666a"
DB_SG = "sg-0bbb111122223333b"
PORT = 8080

OK = {"ResponseMetadata": {"RequestId": "req-ok"}}


def eni(eni_id: str, device_index: int, *groups: str) -> dict:
    return {
        "NetworkInterfaceId": eni_id,
        "Attachment": {"DeviceIndex": device_index},
        "Groups": [{"GroupId": group} for group in groups],
    }


def instance_with(*interfaces: dict) -> dict:
    return {
        "Reservations": [
            {
                "Instances": [
                    {"InstanceId": INSTANCE, "NetworkInterfaces": list(interfaces)}
                ]
            }
        ]
    }


TWO_ENIS = instance_with(
    eni(PRIMARY_ENI, 0, WEB_SG, SSH_SG), eni(SECONDARY_ENI, 1, DB_SG)
)
ISOLATED = instance_with(
    eni(PRIMARY_ENI, 0, ISOLATION_SG), eni(SECONDARY_ENI, 1, ISOLATION_SG)
)


def health(*entries: tuple) -> dict:
    return {
        "TargetHealthDescriptions": [
            {
                "Target": {"Id": INSTANCE, "Port": port},
                "TargetHealth": {
                    "State": state,
                    **({"Reason": reason} if reason else {}),
                },
            }
            for port, state, reason in entries
        ]
    }


REGISTERED = health((PORT, "healthy", None))
NOT_REGISTERED = health((80, "unused", "Target.NotRegistered"))
NOT_ASG_MEMBER = ScaleInProtectionBackup(status="NOT_MANAGED", reason="NOT_ASG_MEMBER")


def mapping(protection: ScaleInProtectionBackup | None = None) -> SgAndTgMappingBackup:
    return SgAndTgMappingBackup(
        scale_in_protection=protection,
        security_group_ids=[WEB_SG, SSH_SG],
        target_group_arn=TG_ARN,
        target_port=PORT,
        network_interfaces=[
            {
                "network_interface_id": PRIMARY_ENI,
                "security_group_ids": [WEB_SG, SSH_SG],
            },
            {"network_interface_id": SECONDARY_ENI, "security_group_ids": [DB_SG]},
        ],
    )


# 응답을 아는 작업만 — 밖을 부르면 즉시 실패한다(#449, 가짜가 조용히 넓어지지 않게)
MODELED = frozenset(
    {
        "ec2.describe_instances",
        "ec2.modify_network_interface_attribute",
        "elbv2.describe_target_health",
        "elbv2.deregister_targets",
        "elbv2.register_targets",
        "autoscaling.describe_auto_scaling_groups",
        "autoscaling.describe_auto_scaling_instances",
        "autoscaling.set_instance_protection",
    }
)


class FakeAws:
    def __init__(self, service: str, state: dict):
        self._service = service
        self._state = state

    def __getattr__(self, operation):
        key = f"{self._service}.{operation}"
        if key not in MODELED:
            # 워크플로가 예외를 삼켜도 놓치지 않게 기록해 두고, fixture 정리에서 실패시킨다
            self._state["unmodeled"].append(key)
            raise NotImplementedError(f"가짜 AWS가 모델링하지 않은 작업: {key}")

        def call(**kwargs):
            self._state["calls"].append((key, kwargs))
            outcome = self._state["overrides"].get(key)
            if isinstance(outcome, list):
                outcome = outcome.pop(0) if outcome else None
            if isinstance(outcome, BaseException):
                raise outcome
            if outcome is not None:
                return outcome
            if key == "ec2.describe_instances":
                return TWO_ENIS
            if key == "elbv2.describe_target_health":
                return REGISTERED
            return OK

        return call


@pytest.fixture
def aws(monkeypatch):
    state = {"overrides": {}, "calls": [], "unmodeled": []}

    def factory(service, region=None, **_):
        return FakeAws(service, state)

    monkeypatch.setattr(ex, "aws_client", factory)
    monkeypatch.setattr(bk, "aws_client", factory)
    handle = SimpleNamespace(calls=state["calls"])
    handle.set = lambda key, value: state["overrides"].__setitem__(key, value)
    yield handle
    assert state["unmodeled"] == [], (
        f"모델링하지 않은 AWS 작업 호출: {state['unmodeled']}"
    )


def changes(aws) -> list[str]:
    return [
        name
        for name, _ in aws.calls
        if ".describe_" not in name and not name.startswith("db.")
    ]


R = PrecheckReasonCode

COMMAND = {
    "runbook_id": RunbookId.RUNBOOK_EC2_ISOLATE.value,
    "target_arn": INSTANCE_ARN,
    "parameters": {
        "instance_id": INSTANCE,
        "target_group_arn": TG_ARN,
        "isolation_group_id": ISOLATION_SG,
        "evidence_id": "ev-1",
    },
    "evidence_ids": ["ev-1"],
}


class FakeDb:
    def __init__(self, timeline: list):
        self._timeline = timeline

    def commit(self):
        self._timeline.append(("db.commit", {}))

    def rollback(self):
        self._timeline.append(("db.rollback", {}))

    def begin_nested(self):
        return contextlib.nullcontext()


@pytest.fixture
def repo(monkeypatch, aws):
    """executions_repo 가짜 — 실행·백업 레코드를 메모리에 둔다."""
    state = SimpleNamespace(executions={}, records={}, steps=[])

    def get_execution(db, execution_id):
        return state.executions.get(execution_id)

    def create_backup_record(db, *, execution_id, target_arn, backup_type, payload):
        record = SimpleNamespace(
            backup_record_id=f"bk-{len(state.records) + 1}",
            execution_id=execution_id,
            target_arn=target_arn,
            backup_type=backup_type,
            payload=payload,
        )
        state.records[record.backup_record_id] = record
        return record

    def bind_backup_record(db, execution_id, backup_record_id):
        state.executions[execution_id].backup_record_id = backup_record_id
        return True

    def get_backup_record(db, backup_record_id):
        return state.records.get(backup_record_id)

    def add_step(db, step, *, execution_id):
        state.steps.append(step)

    def update_step_result(db, step, *, execution_id):
        state.steps.append(step)
        return True

    state.plans = {}
    state.results = {}
    # 계획은 AWS 호출 전에 한 번 읽는다 — DB 조회만 가짜로 갈아 끼운다
    monkeypatch.setattr(
        workflows,
        "_prefetch_autoscaling_plans",
        lambda db: {
            k: AutoscalingExecutionPlan.model_validate(v)
            for k, v in state.plans.items()
        },
    )
    monkeypatch.setattr(
        workflows.executions_repo,
        "get_scale_in_protection_result",
        lambda db, execution_id: state.results.get(execution_id),
    )

    def set_result(db, execution_id, result):
        state.results[execution_id] = result
        return True

    monkeypatch.setattr(
        workflows.executions_repo, "set_scale_in_protection_result", set_result
    )
    for name, fn in {
        "get_execution": get_execution,
        "lock_execution": get_execution,
        "create_backup_record": create_backup_record,
        "bind_backup_record": bind_backup_record,
        "get_backup_record": get_backup_record,
        "add_step": add_step,
        "update_step_result": update_step_result,
    }.items():
        monkeypatch.setattr(workflows.executions_repo, name, fn)

    state.db = FakeDb(aws.calls)
    return state


def execution(state, *, runbook=RunbookId.RUNBOOK_EC2_ISOLATE, **values):
    row = SimpleNamespace(
        execution_id=values.pop("execution_id", "exec-1"),
        runbook_id=runbook,
        status=ExecutionStatus.IN_PROGRESS,
        target_arn=INSTANCE_ARN,
        validated_command=values.pop("validated_command", COMMAND),
        candidate_id=None,
        backup_record_id=None,
        parent_execution_id=None,
        incident_id="inc-1",
    )
    for key, value in values.items():
        setattr(row, key, value)
    state.executions[row.execution_id] = row
    return row


def stored_backup(state, *, execution_id="exec-1", payload=None, backup_type=None):
    record = SimpleNamespace(
        backup_record_id="bk-origin",
        execution_id=execution_id,
        target_arn=INSTANCE_ARN,
        backup_type=backup_type or BackupType.SAVE_CURRENT_SG_AND_TG_MAPPING.value,
        payload=payload if payload is not None else mapping().model_dump(mode="json"),
    )
    state.records[record.backup_record_id] = record
    return record


# ============================================================ 격리 실행


def test_isolate_commits_the_backup_before_the_first_aws_change(aws, repo):
    execution(repo)
    outcome = workflows.run_ec2_isolate_execution(repo.db, "exec-1")

    assert outcome.succeeded
    names = [name for name, _ in aws.calls]
    first_change = names.index("elbv2.deregister_targets")
    backup_commit = names.index("db.commit")
    # 캡처 조회 → 백업 commit → 첫 변경. 변경 뒤에는 원래 SG를 물을 수 없다(ADR-0008 §1 ②)
    assert names.index("ec2.describe_instances") < backup_commit < first_change
    (record,) = repo.records.values()
    assert record.backup_type == BackupType.SAVE_CURRENT_SG_AND_TG_MAPPING.value
    assert repo.executions["exec-1"].backup_record_id == record.backup_record_id
    assert ex.sg_tg_mapping_backup(record.payload) == mapping().model_copy(
        update={"scale_in_protection": NOT_ASG_MEMBER}
    )


def test_isolate_does_not_start_when_the_backup_cannot_be_captured(aws, repo):
    execution(repo)
    aws.set("elbv2.describe_target_health", NOT_REGISTERED)
    outcome = workflows.run_ec2_isolate_execution(repo.db, "exec-1")

    assert not outcome.succeeded
    assert outcome.reason_code is R.PRECHECK_TARGET_NOT_FOUND
    assert outcome.steps == ()
    assert changes(aws) == []
    assert repo.records == {}


def test_isolate_reuses_the_bound_backup_on_retry(aws, repo):
    """재시도가 다시 캡처하면 이미 격리된 값이 "조치 직전"이 된다(ADR-0008 §1 ③)."""
    record = stored_backup(repo)
    execution(repo, backup_record_id=record.backup_record_id)
    workflows.run_ec2_isolate_execution(repo.db, "exec-1")

    assert "ec2.describe_instances" not in [name for name, _ in aws.calls]
    assert list(repo.records) == ["bk-origin"]


def test_isolate_without_the_validated_command_fails_without_touching_aws(aws, repo):
    """격리 SG·TG는 AI가 정하지 않는다 — 실행 명령에 없으면 짐작하지 않는다."""
    execution(repo, validated_command=None)
    outcome = workflows.run_ec2_isolate_execution(repo.db, "exec-1")
    assert outcome.reason_code is R.PRECHECK_PARAM_INVALID
    assert aws.calls == []


def test_isolate_command_for_another_instance_is_refused(aws, repo):
    other = {
        **COMMAND,
        "parameters": {**COMMAND["parameters"], "instance_id": "i-0fff000011112222f"},
    }
    execution(repo, validated_command=other)
    outcome = workflows.run_ec2_isolate_execution(repo.db, "exec-1")
    assert outcome.reason_code is R.PRECHECK_PARAM_INVALID
    assert aws.calls == []


# ============================================================ 격리 판정


DRAINING = health((PORT, "draining", "Target.DeregistrationInProgress"))


def judge_isolate(aws, repo, *, instance, registration):
    record = stored_backup(repo)
    execution(repo, backup_record_id=record.backup_record_id)
    aws.set("ec2.describe_instances", instance)
    aws.set("elbv2.describe_target_health", registration)
    return workflows.judge_ec2_isolate(repo.db, "exec-1")


def test_isolate_judged_success_when_every_eni_is_isolated_and_the_target_left(
    aws, repo
):
    judgement = judge_isolate(aws, repo, instance=ISOLATED, registration=DRAINING)
    assert judgement.next_status is ExecutionStatus.SUCCESS
    assert changes(aws) == []  # 판정은 자산을 바꾸지 않는다


def test_isolate_judged_failed_without_change_when_nothing_moved(aws, repo):
    judgement = judge_isolate(aws, repo, instance=TWO_ENIS, registration=REGISTERED)
    assert judgement.next_status is ExecutionStatus.FAILED
    assert "자산 변경 없음" in judgement.error_summary


def test_isolate_partial_is_reported_with_the_backup_to_restore_from(aws, repo):
    """주 ENI만 격리되고 보조 ENI가 열린 채 — 성공이라 적으면 열린 ENI를 격리됨으로 보고한다."""
    half = instance_with(
        eni(PRIMARY_ENI, 0, ISOLATION_SG),
        eni(SECONDARY_ENI, 1, DB_SG),
    )
    judgement = judge_isolate(aws, repo, instance=half, registration=NOT_REGISTERED)
    assert judgement.next_status is ExecutionStatus.FAILED
    assert "부분 적용" in judgement.error_summary
    assert "1/2" in judgement.error_summary
    assert "bk-origin" in judgement.error_summary


def test_isolate_judgement_defers_when_aws_cannot_be_asked(aws, repo):
    judgement = judge_isolate(
        aws,
        repo,
        instance=EndpointConnectionError(endpoint_url="x"),
        registration=DRAINING,
    )
    assert judgement.deferred
    assert judgement.defer_code is R.PRECHECK_AWS_ERROR


# ============================================================ 해제 실행


@pytest.fixture
def guardrail(monkeypatch):
    seen: list = []

    def run(db, execution_row, payload):
        seen.append(payload)
        return SimpleNamespace(
            command=object() if run.passes else None,
            result=SimpleNamespace(steps=[], failed_step=None),
            dry_run_not_applied=False,
        )

    run.passes = True
    run.seen = seen
    monkeypatch.setattr(workflows, "_run_rollback_guardrails", run)
    monkeypatch.setattr(
        workflows, "_rollback_evidence_ids", lambda db, origin: ["ev-1"]
    )
    return run


def unisolate_child(repo, **values):
    execution(repo, execution_id="origin-1")
    record = stored_backup(repo, execution_id="origin-1", **values)
    return execution(
        repo,
        execution_id="child-1",
        runbook=RunbookId.RUNBOOK_EC2_UNISOLATE,
        validated_command=None,
        parent_execution_id="origin-1",
        backup_record_id=record.backup_record_id,
    )


def test_unisolate_runs_guardrails_before_restoring_from_the_bound_backup(
    aws, repo, guardrail
):
    unisolate_child(repo)
    aws.set("ec2.describe_instances", ISOLATED)
    outcome = workflows.run_ec2_unisolate_execution(repo.db, "child-1")

    assert outcome.succeeded
    # 명령은 원복 값을 나르지 않는다 — 레코드를 가리키는 id만(ADR-0008 §4)
    (payload,) = guardrail.seen
    assert payload["parameters"] == {
        "instance_id": INSTANCE,
        "backup_record_id": "bk-origin",
        "evidence_id": "ev-1",
    }
    groups = [
        kw["Groups"]
        for name, kw in aws.calls
        if name.endswith("modify_network_interface_attribute")
    ]
    assert groups == [[WEB_SG, SSH_SG], [DB_SG]]


def test_unisolate_rejected_by_guardrails_touches_nothing(aws, repo, guardrail):
    unisolate_child(repo)
    guardrail.passes = False
    outcome = workflows.run_ec2_unisolate_execution(repo.db, "child-1")
    assert not outcome.succeeded
    assert aws.calls == []


@pytest.mark.parametrize(
    "values, code",
    [
        (
            {"backup_type": BackupType.SAVE_SG_FULL_RULES_JSON.value},
            R.PRECHECK_PARAM_INVALID,
        ),
        # 롤백 precheck가 읽던 두 키만 — 포트·ENI 목록이 없어 원복 근거가 못 된다
        (
            {"payload": {"security_group_ids": [WEB_SG], "target_group_arn": TG_ARN}},
            R.PRECHECK_PARAM_INVALID,
        ),
    ],
    ids=["other-backup-type", "payload-outside-contract"],
)
def test_unisolate_does_not_start_without_a_usable_backup(
    aws, repo, guardrail, values, code
):
    unisolate_child(repo, **values)
    outcome = workflows.run_ec2_unisolate_execution(repo.db, "child-1")
    assert outcome.reason_code is code
    assert aws.calls == [] and guardrail.seen == []


def test_unisolate_without_a_bound_backup_does_not_start(aws, repo, guardrail):
    child = unisolate_child(repo)
    child.backup_record_id = None
    outcome = workflows.run_ec2_unisolate_execution(repo.db, "child-1")
    assert outcome.reason_code is R.PRECHECK_TARGET_NOT_FOUND
    assert aws.calls == []


def test_unisolate_defers_without_steps_when_the_instance_cannot_be_read(
    aws, repo, guardrail
):
    unisolate_child(repo)
    aws.set("ec2.describe_instances", EndpointConnectionError(endpoint_url="x"))
    outcome = workflows.run_ec2_unisolate_execution(repo.db, "child-1")
    assert outcome.deferred and outcome.steps == ()


# ============================================================ 해제 판정


def judge_unisolate(aws, repo, *, instance, registration):
    unisolate_child(repo)
    aws.set("ec2.describe_instances", instance)
    aws.set("elbv2.describe_target_health", registration)
    return workflows.judge_ec2_unisolate(repo.db, "child-1")


def test_unisolate_judged_success_only_when_sgs_and_registration_are_back(aws, repo):
    judgement = judge_unisolate(aws, repo, instance=TWO_ENIS, registration=REGISTERED)
    assert judgement.next_status is ExecutionStatus.SUCCESS


@pytest.mark.parametrize(
    "instance, registration",
    [
        (TWO_ENIS, NOT_REGISTERED),  # SG만 돌아오고 재등록 전에 끊김
        (TWO_ENIS, DRAINING),  # 드레이닝은 등록이 아니다
        (ISOLATED, REGISTERED),  # 재등록됐으나 격리 SG가 남음
    ],
    ids=["not-reregistered", "draining", "sg-not-restored"],
)
def test_unisolate_judged_failed_when_either_axis_is_missing(
    aws, repo, instance, registration
):
    judgement = judge_unisolate(aws, repo, instance=instance, registration=registration)
    assert judgement.next_status is ExecutionStatus.FAILED
    assert "해제 미완" in judgement.error_summary


def test_unisolate_judgement_defers_when_the_registration_cannot_be_read(aws, repo):
    judgement = judge_unisolate(
        aws,
        repo,
        instance=TWO_ENIS,
        registration=EndpointConnectionError(endpoint_url="x"),
    )
    assert judgement.deferred


# ============================================================ ASG scale-in 보호 (보조 조치)
#
# 보호는 격리/해제 SUCCESS와 **별도 결과**다. 어떤 결과여도 TG/ENI 격리 성공과 해제 경로는
# 그대로이고, 잔여는 별도 레코드로 남는다. 보호는 같은 인스턴스의 생존을 보장하지 않는다.

from botocore.exceptions import ClientError
from schemas.autoscaling import (
    EXECUTION_TAG_KEY,
    SCALING_POLICY_NAME,
    SOURCE_INSTANCE_TAG_KEY,
    AutoscalingExecutionPlan,
    autoscaling_image_name,
)

ASG_SOURCE = "i-0fed000011112222f"
ASG_EXECUTION = "3f1c2b8a-0d5e-4c1a-9b7e-2a6f8d4c1e90"
ASG = f"vigilantis-asg-{ASG_SOURCE}"
PLAN = AutoscalingExecutionPlan.model_validate(
    {
        "target_arn": f"arn:aws:ec2:{REGION}:{ACCOUNT}:instance/{ASG_SOURCE}",
        "execution_id": ASG_EXECUTION,
        "min_size": 1,
        "max_size": 3,
        "desired_capacity": 1,
        "image_name": autoscaling_image_name(ASG_SOURCE, ASG_EXECUTION),
        "launch_template_name": f"vigilantis-lt-{ASG_SOURCE}",
        "launch_template_version": 1,
        "asg_name": ASG,
        "scaling_policy_name": SCALING_POLICY_NAME,
        "cpu_target_percent": 50,
        "default_instance_warmup_seconds": 300,
        "source": {
            "instance_id": ASG_SOURCE,
            "instance_type": "t3.micro",
            "subnet_id": "subnet-0abc123456789def0",
            "security_group_ids": [WEB_SG],
            "iam_instance_profile_arn": None,
            "ebs_optimized": False,
            "detailed_monitoring": False,
            "dedicated_tenancy": False,
            "associate_public_ip_address": True,
            "metadata_options": {
                "http_tokens": "required",
                "http_endpoint": "enabled",
                "http_put_response_hop_limit": 2,
                "http_protocol_ipv6": "disabled",
                "instance_metadata_tags": "disabled",
            },
            "preserved_tags": [{"key": "vigilantis:smoke", "value": "true"}],
        },
    }
)
ASG_TAGS = [
    {"Key": EXECUTION_TAG_KEY, "Value": ASG_EXECUTION},
    {"Key": SOURCE_INSTANCE_TAG_KEY, "Value": ASG_SOURCE},
    {"Key": "vigilantis:smoke", "Value": "true"},
]
MANAGED = ScaleInProtectionBackup(
    status="MANAGED",
    asg_name=ASG,
    previous_protected=False,
    autoscaling_execution_id=ASG_EXECUTION,
)
DOWN = EndpointConnectionError(endpoint_url="x")


def denied() -> ClientError:
    return ClientError(
        {
            "Error": {"Code": "ValidationError", "Message": ""},
            "ResponseMetadata": {"HTTPStatusCode": 400},
        },
        "Op",
    )


def asg_instances(*, protected: bool, asg: str = ASG) -> dict:
    return {
        "AutoScalingInstances": [
            {
                "InstanceId": INSTANCE,
                "AutoScalingGroupName": asg,
                "ProtectedFromScaleIn": protected,
            }
        ]
    }


def seed_managed_asg(repo, aws):
    """#446 실행의 저장된 계획 + 그 ASG의 실제 멤버인 격리 대상."""
    execution(
        repo,
        execution_id=ASG_EXECUTION,
        runbook=RunbookId.RUNBOOK_EC2_ENABLE_AUTOSCALING,
        validated_command=None,
    )
    repo.plans[ASG_EXECUTION] = PLAN.model_dump(mode="json")
    member = instance_with(
        eni(PRIMARY_ENI, 0, WEB_SG, SSH_SG), eni(SECONDARY_ENI, 1, DB_SG)
    )
    member["Reservations"][0]["Instances"][0]["Tags"] = [
        {"Key": "aws:autoscaling:groupName", "Value": ASG}
    ]
    aws.set("ec2.describe_instances", member)
    aws.set(
        "autoscaling.describe_auto_scaling_groups",
        {
            "AutoScalingGroups": [
                {
                    "AutoScalingGroupName": ASG,
                    "Tags": ASG_TAGS,
                    "Instances": [
                        {"InstanceId": INSTANCE, "ProtectedFromScaleIn": False}
                    ],
                }
            ]
        },
    )


def stored(repo, execution_id) -> dict | None:
    return repo.results.get(execution_id)


def isolate_managed(repo, aws, *, protection_calls):
    seed_managed_asg(repo, aws)
    aws.set("autoscaling.describe_auto_scaling_instances", protection_calls)
    execution(repo)
    return workflows.run_ec2_isolate_execution(repo.db, "exec-1")


def test_managed_member_is_protected_only_after_core_isolation(aws, repo):
    outcome = isolate_managed(
        repo,
        aws,
        protection_calls=[
            asg_instances(protected=False),
            asg_instances(protected=True),
        ],
    )

    assert outcome.succeeded
    names = [name for name, _ in aws.calls]
    set_at = names.index("autoscaling.set_instance_protection")
    assert (
        max(
            i
            for i, n in enumerate(names)
            if n == "ec2.modify_network_interface_attribute"
        )
        < set_at
    )
    # 쓰기 직전에 의도 기록이 commit된다
    assert "db.commit" in names[set_at - 2 : set_at]
    backup = ex.sg_tg_mapping_backup(repo.records["bk-1"].payload)
    assert backup.scale_in_protection == MANAGED
    final = stored(repo, "exec-1")
    assert (final["phase"], final["outcome"], final["reason"], final["residual"]) == (
        "PROTECT",
        "APPLIED",
        "WRITTEN",
        False,
    )
    assert final["write_may_have_begun"] is True and final["observed_at"]


def test_protection_failure_keeps_isolation_success_and_leaves_a_residual(aws, repo):
    aws.set("autoscaling.set_instance_protection", denied())
    outcome = isolate_managed(
        repo, aws, protection_calls=asg_instances(protected=False)
    )

    assert outcome.succeeded and outcome.reason_code is None
    assert [s.step_type for s in outcome.steps] == [
        ex.STEP_DEREGISTER_TARGET,
        ex.STEP_ISOLATE_ENI_SECURITY_GROUPS,
    ]
    final = stored(repo, "exec-1")
    assert (final["outcome"], final["reason"], final["residual"]) == (
        "FAILED",
        "WRITE_REJECTED",
        True,
    )


def test_lost_protection_response_is_recorded_unknown_without_inventing_success(
    aws, repo
):
    aws.set("autoscaling.set_instance_protection", DOWN)
    outcome = isolate_managed(
        repo, aws, protection_calls=[asg_instances(protected=False), DOWN]
    )

    assert outcome.succeeded
    final = stored(repo, "exec-1")
    assert (
        final["outcome"],
        final["reason"],
        final["write_may_have_begun"],
        final["residual"],
    ) == (
        "UNKNOWN",
        "WRITE_UNCERTAIN",
        True,
        True,
    )


def test_lookup_failure_at_capture_is_unknown_and_never_writes(aws, repo):
    seed_managed_asg(repo, aws)
    aws.set("autoscaling.describe_auto_scaling_groups", DOWN)
    execution(repo)
    outcome = workflows.run_ec2_isolate_execution(repo.db, "exec-1")

    assert outcome.succeeded  # 격리는 그대로 진행된다
    assert (
        ex.sg_tg_mapping_backup(repo.records["bk-1"].payload).scale_in_protection.status
        == "UNKNOWN"
    )
    assert "autoscaling.set_instance_protection" not in [n for n, _ in aws.calls]
    final = stored(repo, "exec-1")
    assert (final["outcome"], final["reason"], final["residual"]) == (
        "NOT_ATTEMPTED",
        "PRIOR_STATE_UNKNOWN",
        True,
    )


def test_membership_drift_after_isolation_skips_the_write(aws, repo):
    outcome = isolate_managed(
        repo, aws, protection_calls=asg_instances(protected=False, asg="another-asg")
    )
    assert outcome.succeeded
    assert "autoscaling.set_instance_protection" not in [n for n, _ in aws.calls]
    assert (stored(repo, "exec-1")["reason"], stored(repo, "exec-1")["residual"]) == (
        "MEMBERSHIP_DRIFT",
        True,
    )


def test_unrecordable_intent_means_no_write_and_isolation_still_succeeds(
    aws, repo, monkeypatch
):
    def broken(db, execution_id, result):
        raise RuntimeError("db down")

    monkeypatch.setattr(workflows, "_store_protection_result", broken)
    outcome = isolate_managed(
        repo, aws, protection_calls=asg_instances(protected=False)
    )
    assert outcome.succeeded
    assert "autoscaling.set_instance_protection" not in [n for n, _ in aws.calls]


def test_non_asg_target_isolation_is_unchanged(aws, repo):
    execution(repo)
    outcome = workflows.run_ec2_isolate_execution(repo.db, "exec-1")
    assert outcome.succeeded
    assert not [n for n, _ in aws.calls if n.startswith("autoscaling.")]
    assert repo.results == {}


def test_name_alone_does_not_make_an_asg_managed(aws, repo):
    """ASG 이름 규칙이 같아도 저장된 계획이 없으면 쓰지 않는다(NOT_MANAGED)."""
    seed_managed_asg(repo, aws)
    repo.plans.clear()
    execution(repo)
    assert workflows.run_ec2_isolate_execution(repo.db, "exec-1").succeeded
    assert (
        ex.sg_tg_mapping_backup(repo.records["bk-1"].payload).scale_in_protection.status
        == "NOT_MANAGED"
    )
    assert [n for n, _ in aws.calls if n.startswith("autoscaling.")] == [
        "autoscaling.describe_auto_scaling_groups"  # 읽기만 — 쓰기·멤버 재조회 없음
    ]
    assert repo.results == {}


def test_plans_are_read_before_any_aws_call_during_capture(aws, repo, monkeypatch):
    order: list = []
    original = workflows._prefetch_autoscaling_plans

    def spy(db):
        order.append(("plans", len(aws.calls)))
        return original(db)

    monkeypatch.setattr(workflows, "_prefetch_autoscaling_plans", spy)
    seed_managed_asg(repo, aws)
    aws.set(
        "autoscaling.describe_auto_scaling_instances", asg_instances(protected=True)
    )
    execution(repo)
    workflows.run_ec2_isolate_execution(repo.db, "exec-1")
    assert order == [("plans", 0)]


def test_retry_with_a_legacy_bound_backup_never_touches_the_asg(aws, repo):
    legacy = mapping().model_dump(mode="json", exclude={"scale_in_protection"})
    record = stored_backup(repo, payload=legacy)
    execution(repo, backup_record_id=record.backup_record_id)
    assert workflows.run_ec2_isolate_execution(repo.db, "exec-1").succeeded
    assert not [n for n, _ in aws.calls if n.startswith("autoscaling.")]
    final = stored(repo, "exec-1")
    assert (final["outcome"], final["reason"], final["residual"]) == (
        "NOT_ATTEMPTED",
        "PRIOR_STATE_UNKNOWN",
        True,
    )
    assert final["asg_name"] is None


def test_confirmed_rejection_replaces_the_same_attempts_intent(aws, repo):
    """한 시도 안의 의도(UNKNOWN)는 같은 시도의 확정 거절이 덮는다 — 시도 사이 이어 붙임은 없다."""
    aws.set("autoscaling.set_instance_protection", denied())
    isolate_managed(repo, aws, protection_calls=asg_instances(protected=False))
    final = stored(repo, "exec-1")
    assert (final["outcome"], final["reason"]) == ("FAILED", "WRITE_REJECTED")


def protect_result(repo, **overrides):
    values = {
        "phase": "PROTECT",
        "outcome": "UNKNOWN",
        "reason": "WRITE_UNCERTAIN",
        "asg_name": ASG,
        "instance_id": INSTANCE,
        "write_may_have_begun": True,
        "observed_protected": None,
        "residual": True,
        "observed_at": "2026-10-01T00:00:00+00:00",
    }
    values.update(overrides)
    repo.results["origin-1"] = values


def unisolate_managed(
    repo, aws, guardrail, *, protection=MANAGED, parent=True, asg_calls=None
):
    unisolate_child(repo, payload=mapping(protection).model_dump(mode="json"))
    if parent is not None:
        protect_result(repo, **(parent if isinstance(parent, dict) else {}))
    aws.set("ec2.describe_instances", ISOLATED)
    if asg_calls is not None:
        aws.set("autoscaling.describe_auto_scaling_instances", asg_calls)
    return workflows.run_ec2_unisolate_execution(repo.db, "child-1")


def test_unisolate_restores_the_captured_value_after_rereading_core_state(
    aws, repo, guardrail
):
    unisolate_child(repo, payload=mapping(MANAGED).model_dump(mode="json"))
    protect_result(repo)
    # 첫 조회(ENI 대조)는 격리 상태, 이후(복원 확인)는 복원된 상태
    aws.set("ec2.describe_instances", [ISOLATED, TWO_ENIS])
    aws.set(
        "autoscaling.describe_auto_scaling_instances",
        [asg_instances(protected=True), asg_instances(protected=False)],
    )
    outcome = workflows.run_ec2_unisolate_execution(repo.db, "child-1")

    assert outcome.succeeded
    names = [n for n, _ in aws.calls]
    assert names.index("elbv2.register_targets") < names.index(
        "autoscaling.set_instance_protection"
    )
    ((_, write),) = [
        c for c in aws.calls if c[0] == "autoscaling.set_instance_protection"
    ]
    assert write["ProtectedFromScaleIn"] is False
    final = stored(repo, "child-1")
    assert (final["phase"], final["outcome"], final["reason"]) == (
        "RESTORE",
        "APPLIED",
        "RESTORED",
    )
    # 의도 기록이 쓰기 이전에 commit됐다
    set_at = names.index("autoscaling.set_instance_protection")
    assert "db.commit" in names[set_at - 2 : set_at]


def test_restore_failure_never_suppresses_the_release(aws, repo, guardrail):
    aws.set("autoscaling.set_instance_protection", denied())
    unisolate_child(repo, payload=mapping(MANAGED).model_dump(mode="json"))
    protect_result(repo)
    aws.set("ec2.describe_instances", [ISOLATED, TWO_ENIS])
    aws.set(
        "autoscaling.describe_auto_scaling_instances", asg_instances(protected=True)
    )
    outcome = workflows.run_ec2_unisolate_execution(repo.db, "child-1")

    assert outcome.succeeded and not outcome.deferred
    assert [s.step_type for s in outcome.steps] == [
        ex.STEP_RESTORE_ENI_SECURITY_GROUPS,
        ex.STEP_REGISTER_TARGET,
    ]
    final = stored(repo, "child-1")
    assert (final["outcome"], final["reason"], final["residual"]) == (
        "FAILED",
        "WRITE_REJECTED",
        True,
    )


def test_unverifiable_core_state_leaves_protection_and_a_residual_but_release_stands(
    aws, repo, guardrail
):
    unisolate_child(repo, payload=mapping(MANAGED).model_dump(mode="json"))
    protect_result(repo)
    aws.set("ec2.describe_instances", [ISOLATED, DOWN])  # 복원 후 재조회 실패
    outcome = workflows.run_ec2_unisolate_execution(repo.db, "child-1")

    assert outcome.succeeded
    final = stored(repo, "child-1")
    assert (final["reason"], final["residual"]) == ("CORE_STATE_UNVERIFIED", True)
    assert "autoscaling.set_instance_protection" not in [n for n, _ in aws.calls]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"protection": None, "parent": None},  # 옛 백업: 모름
        {
            "protection": ScaleInProtectionBackup(
                status="UNKNOWN", reason="LOOKUP_FAILED"
            )
        },
        {"parent": None},  # 보호 결과 기록 없음
        {"parent": {"outcome": "FAILED", "reason": "WRITE_REJECTED"}},  # 확정 거절
        {
            "parent": {
                "outcome": "NOT_ATTEMPTED",
                "reason": "MEMBERSHIP_DRIFT",
                "write_may_have_begun": False,
            }
        },
    ],
    ids=[
        "legacy-backup",
        "unknown-prior-state",
        "no-protect-result",
        "rejected-protect",
        "never-wrote",
    ],
)
def test_uncertain_old_state_or_no_protection_write_means_no_restore_write(
    aws, repo, guardrail, kwargs
):
    outcome = unisolate_managed(repo, aws, guardrail, **kwargs)
    assert outcome.succeeded
    assert "autoscaling.set_instance_protection" not in [n for n, _ in aws.calls]
    assert "autoscaling.describe_auto_scaling_instances" not in [
        n for n, _ in aws.calls
    ]


def test_judged_isolate_success_names_a_residual_when_protection_never_ran(aws, repo):
    record = stored_backup(repo, payload=mapping(MANAGED).model_dump(mode="json"))
    execution(repo, backup_record_id=record.backup_record_id)
    aws.set("ec2.describe_instances", ISOLATED)
    aws.set("elbv2.describe_target_health", DRAINING)

    judgement = workflows.judge_ec2_isolate(repo.db, "exec-1")

    assert judgement.next_status is ExecutionStatus.SUCCESS  # 판정은 TG/ENI만 본다
    assert not [
        n for n, _ in aws.calls if n.startswith("autoscaling.")
    ]  # 마저 하지 않는다
    residual = stored(repo, "exec-1")
    assert (residual["phase"], residual["reason"], residual["residual"]) == (
        "PROTECT",
        "EXECUTION_INTERRUPTED",
        True,
    )


def test_judged_isolate_success_names_a_residual_for_a_legacy_backup(aws, repo):
    legacy = mapping().model_dump(mode="json", exclude={"scale_in_protection"})
    record = stored_backup(repo, payload=legacy)
    execution(repo, backup_record_id=record.backup_record_id)
    aws.set("ec2.describe_instances", ISOLATED)
    aws.set("elbv2.describe_target_health", DRAINING)

    judgement = workflows.judge_ec2_isolate(repo.db, "exec-1")

    assert judgement.next_status is ExecutionStatus.SUCCESS
    assert not [n for n, _ in aws.calls if n.startswith("autoscaling.")]
    residual = stored(repo, "exec-1")
    assert (
        residual["phase"],
        residual["reason"],
        residual["residual"],
        residual["asg_name"],
    ) == ("PROTECT", "EXECUTION_INTERRUPTED", True, None)


def test_judged_unisolate_success_names_a_residual_when_restore_never_ran(aws, repo):
    unisolate_child(repo, payload=mapping(MANAGED).model_dump(mode="json"))
    protect_result(repo)
    aws.set("ec2.describe_instances", TWO_ENIS)
    aws.set("elbv2.describe_target_health", REGISTERED)

    judgement = workflows.judge_ec2_unisolate(repo.db, "child-1")

    assert judgement.next_status is ExecutionStatus.SUCCESS
    assert not [n for n, _ in aws.calls if n.startswith("autoscaling.")]
    residual = stored(repo, "child-1")
    assert (residual["phase"], residual["reason"]) == (
        "RESTORE",
        "EXECUTION_INTERRUPTED",
    )
