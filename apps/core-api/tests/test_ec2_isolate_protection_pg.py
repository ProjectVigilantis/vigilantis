"""EC2 격리·해제의 ASG scale-in 보호 — 실제 PostgreSQL 통합 회귀(미기동 시 skip).

대역 테스트(test_ec2_isolate_workflow.py)는 저장소 계층을 가짜로 갈아 끼운다. 여기서는 **실제
executions 저장소·ORM 컬럼(action_executions.scale_in_protection_result)·사전 계획 조회 SQL**을 태워
다음을 굳힌다.

  - 보호 결과가 SG/TG 백업(불변)과 **다른 자리**에 남는다 — backup_records에 보조 행이 생기지 않는다
  - 응답 유실(UNKNOWN)·거절(FAILED)이 DB에 typed 잔여로 남고, 격리 실행 결과는 SUCCESS 그대로다
  - 보호 복원이 실패해도 기존 해제 경로(SG/TG 복원)는 막히지 않고, 그 잔여도 남는다
  - 이름이 아니라 **저장된 #446 계획**으로만 managed가 된다(실제 SQL로 조회)

AWS는 불필요 — 가짜 boto3 클라이언트. 실 AWS의 보호 쓰기·멤버십 응답은 스모크 몫이다.
"""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError
from sqlalchemy import select, text

CORE_API = Path(__file__).resolve().parents[1]
REPO_ROOT = Path(__file__).resolve().parents[3]
for p in (str(CORE_API), str(REPO_ROOT / "packages")):
    if p not in sys.path:
        sys.path.insert(0, p)

import dispatcher
import workflows
from db import models
from db.repositories import executions as exec_repo
from schemas.api.incidents import IncidentCategory
from schemas.autoscaling import (
    EXECUTION_TAG_KEY,
    SCALING_POLICY_NAME,
    SOURCE_INSTANCE_TAG_KEY,
    AutoscalingExecutionPlan,
    autoscaling_image_name,
)
from schemas.backups import ScaleInProtectionResult
from schemas.runbooks import RunbookId
from services.aws import backup as bk
from services.aws import executor as ex

ACCOUNT = "123456789012"
REGION = "ap-northeast-2"
INSTANCE = "i-0abc123456789def0"
INSTANCE_ARN = f"arn:aws:ec2:{REGION}:{ACCOUNT}:instance/{INSTANCE}"
TG_ARN = f"arn:aws:elasticloadbalancing:{REGION}:{ACCOUNT}:targetgroup/vigilantis-smoke-tg/0123456789abcdef"
ISOLATION_SG = "sg-0150150150150150a"
PRIMARY_ENI = "eni-0aaa111122223333a"
WEB_SG = "sg-0aaa111122223333a"
PORT = 8080
ASG_SOURCE = "i-0fed000011112222f"
ASG_EXECUTION = "3f1c2b8a-0d5e-4c1a-9b7e-2a6f8d4c1e90"
ASG = f"vigilantis-asg-{ASG_SOURCE}"

OK = {"ResponseMetadata": {"RequestId": "req-ok"}}
DOWN = EndpointConnectionError(endpoint_url="x")
MISSING_ID = "00000000-0000-4000-8000-000000000000"


def rejected() -> ClientError:
    return ClientError(
        {
            "Error": {"Code": "ValidationError", "Message": ""},
            "ResponseMetadata": {"HTTPStatusCode": 400},
        },
        "Op",
    )


def instance(groups, *, member=True):
    body = {
        "InstanceId": INSTANCE,
        "NetworkInterfaces": [
            {
                "NetworkInterfaceId": PRIMARY_ENI,
                "Attachment": {"DeviceIndex": 0},
                "Groups": [{"GroupId": g} for g in groups],
            }
        ],
    }
    if member:
        body["Tags"] = [{"Key": "aws:autoscaling:groupName", "Value": ASG}]
    return {"Reservations": [{"Instances": [body]}]}


def asg_group(protected=False):
    return {
        "AutoScalingGroups": [
            {
                "AutoScalingGroupName": ASG,
                "Tags": [
                    {"Key": EXECUTION_TAG_KEY, "Value": ASG_EXECUTION},
                    {"Key": SOURCE_INSTANCE_TAG_KEY, "Value": ASG_SOURCE},
                    {"Key": "vigilantis:smoke", "Value": "true"},
                ],
                "Instances": [
                    {"InstanceId": INSTANCE, "ProtectedFromScaleIn": protected}
                ],
            }
        ]
    }


def asg_instance(protected):
    return {
        "AutoScalingInstances": [
            {
                "InstanceId": INSTANCE,
                "AutoScalingGroupName": ASG,
                "ProtectedFromScaleIn": protected,
            }
        ]
    }


HEALTH = {
    "TargetHealthDescriptions": [
        {"Target": {"Id": INSTANCE, "Port": PORT}, "TargetHealth": {"State": "healthy"}}
    ]
}


class FakeAws:
    def __init__(self, service, state):
        self._service = service
        self._state = state

    def __getattr__(self, operation):
        key = f"{self._service}.{operation}"

        def call(**kwargs):
            self._state["calls"].append((key, kwargs))
            outcome = self._state["overrides"].get(key)
            if isinstance(outcome, list):
                outcome = outcome.pop(0) if outcome else None
            if isinstance(outcome, BaseException):
                raise outcome
            if outcome is not None:
                return outcome
            if key == "elbv2.describe_target_health":
                return HEALTH
            return OK

        return call


@pytest.fixture
def aws(monkeypatch):
    state = {"overrides": {}, "calls": []}

    def factory(service, region=None, **_):
        return FakeAws(service, state)

    monkeypatch.setattr(ex, "aws_client", factory)
    monkeypatch.setattr(bk, "aws_client", factory)
    handle = SimpleNamespace(calls=state["calls"])
    handle.set = lambda key, value: state["overrides"].__setitem__(key, value)
    handle.names = lambda: [name for name, _ in state["calls"]]
    return handle


def build_plan(execution_id: str = ASG_EXECUTION) -> AutoscalingExecutionPlan:
    return AutoscalingExecutionPlan.model_validate(
        {
            "target_arn": f"arn:aws:ec2:{REGION}:{ACCOUNT}:instance/{ASG_SOURCE}",
            "execution_id": execution_id,
            "min_size": 1,
            "max_size": 3,
            "desired_capacity": 1,
            "image_name": autoscaling_image_name(ASG_SOURCE, execution_id),
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


@pytest.fixture
def seeded(db, make_incident, make_execution):
    """#446 실행(저장 계획 포함) + 격리 실행 1건."""
    incident = make_incident(
        db, category=IncidentCategory.SECOPS, subject_arn=INSTANCE_ARN
    )
    plan_execution = make_execution(
        db,
        incident,
        runbook_id=RunbookId.RUNBOOK_EC2_ENABLE_AUTOSCALING,
        target_arn=build_plan().target_arn,
    )
    assert exec_repo.bind_autoscaling_plan(
        db,
        plan_execution.execution_id,
        build_plan(ASG_EXECUTION).model_dump(mode="json"),
    )
    isolate = make_execution(
        db,
        incident,
        runbook_id=RunbookId.RUNBOOK_EC2_ISOLATE,
        target_arn=INSTANCE_ARN,
        validated_command=COMMAND,
    )
    db.commit()
    return SimpleNamespace(
        incident=incident,
        plan_execution_id=plan_execution.execution_id,
        isolate_id=isolate.execution_id,
    )


def arrange_managed_asg(aws, seeded, monkeypatch):
    # 소유 태그의 execution-id는 DB가 발급한 실행 ID여야 저장 계획과 대조된다
    group = asg_group()
    for tag in group["AutoScalingGroups"][0]["Tags"]:
        if tag["Key"] == EXECUTION_TAG_KEY:
            tag["Value"] = seeded.plan_execution_id
    aws.set("ec2.describe_instances", instance([WEB_SG]))
    aws.set("autoscaling.describe_auto_scaling_groups", group)


def stored_plan_matches_execution_id(db, seeded):
    """저장 계획의 execution_id(UUID)를 실제 행 ID와 같게 다시 저장한다."""
    plan = build_plan(seeded.plan_execution_id)
    db.execute(
        models.ActionExecution.__table__.update()
        .where(models.ActionExecution.execution_id == seeded.plan_execution_id)
        .values(
            autoscaling_plan=plan.model_dump(mode="json"),
        )
    )
    db.commit()


def run_isolate(db, seeded):
    return workflows.run_ec2_isolate_execution(db, seeded.isolate_id)


def stored_result(db, execution_id):
    db.expire_all()
    return exec_repo.get_scale_in_protection_result(db, execution_id)


def backup_rows(db):
    return db.execute(select(models.BackupRecord)).scalars().all()


# ------------------------------------------------------------------ 저장소 접근자


def test_result_accessors_round_trip_and_overwrite(db, seeded):
    assert exec_repo.get_scale_in_protection_result(db, seeded.isolate_id) is None
    first = {"phase": "PROTECT", "outcome": "UNKNOWN"}
    assert exec_repo.set_scale_in_protection_result(db, seeded.isolate_id, first)
    db.commit()
    assert stored_result(db, seeded.isolate_id) == first
    second = {"phase": "PROTECT", "outcome": "APPLIED"}
    assert exec_repo.set_scale_in_protection_result(
        db, seeded.isolate_id, second
    )  # 갱신 가능
    db.commit()
    assert stored_result(db, seeded.isolate_id) == second
    assert exec_repo.set_scale_in_protection_result(db, MISSING_ID, first) is False
    assert exec_repo.get_scale_in_protection_result(db, MISSING_ID) is None


# ------------------------------------------------------------------ 격리 + 보호


def test_lost_response_is_persisted_as_typed_unknown_while_isolation_succeeds(
    db, seeded, aws, monkeypatch
):
    stored_plan_matches_execution_id(db, seeded)
    arrange_managed_asg(aws, seeded, monkeypatch)
    aws.set(
        "autoscaling.describe_auto_scaling_instances",
        [asg_instance(False), asg_instance(False)],
    )
    aws.set("autoscaling.set_instance_protection", DOWN)

    outcome = run_isolate(db, seeded)

    assert outcome.succeeded
    result = ScaleInProtectionResult.model_validate(
        stored_result(db, seeded.isolate_id)
    )
    assert (result.phase, result.outcome, result.reason) == (
        "PROTECT",
        "UNKNOWN",
        "WRITE_UNCERTAIN",
    )
    assert (
        result.write_may_have_begun
        and result.residual
        and result.observed_at.tzinfo is not None
    )
    # 보조 결과는 불변 SG/TG 백업과 별개 — 백업 행은 하나뿐이고 이전 값이 그대로 담긴다
    (row,) = backup_rows(db)
    assert row.backup_type == "SAVE_CURRENT_SG_AND_TG_MAPPING"
    assert row.payload["scale_in_protection"]["status"] == "MANAGED"
    assert row.payload["scale_in_protection"]["previous_protected"] is False
    # 코어 격리 단계는 SUCCESS로 남았다
    steps = exec_repo.list_steps(db, seeded.isolate_id)
    assert [s.step_type for s in steps] == [
        ex.STEP_DEREGISTER_TARGET,
        ex.STEP_ISOLATE_ENI_SECURITY_GROUPS,
    ]


def test_name_alone_without_a_stored_plan_is_not_managed(db, seeded, aws, monkeypatch):
    # 저장 계획의 execution_id가 ASG 태그와 다르다(seeded는 UUID 불일치 상태) → 소유 불일치
    arrange_managed_asg(aws, seeded, monkeypatch)
    assert run_isolate(db, seeded).succeeded
    (row,) = backup_rows(db)
    assert row.payload["scale_in_protection"]["status"] == "NOT_MANAGED"
    assert "autoscaling.set_instance_protection" not in aws.names()
    assert stored_result(db, seeded.isolate_id) is None


# ------------------------------------------------------------------ 해제 가용성


@pytest.fixture
def guardrail(monkeypatch):
    def run(db, execution_row, payload):
        return SimpleNamespace(
            command=object(), result=SimpleNamespace(steps=[], failed_step=None)
        )

    monkeypatch.setattr(workflows, "_run_rollback_guardrails", run)
    monkeypatch.setattr(
        workflows, "_rollback_evidence_ids", lambda db, origin: ["ev-1"]
    )


def release_child(db, seeded, make_execution):
    origin = exec_repo.get_execution(db, seeded.isolate_id)
    child = make_execution(
        db,
        seeded.incident,
        runbook_id=RunbookId.RUNBOOK_EC2_UNISOLATE,
        target_arn=INSTANCE_ARN,
        parent=origin,
    )
    assert exec_repo.bind_backup_record(db, child.execution_id, origin.backup_record_id)
    db.commit()
    return child.execution_id


def isolated_then_released(
    db, seeded, aws, make_execution, *, restore_calls, restore_write=None
):
    stored_plan_matches_execution_id(db, seeded)
    arrange_managed_asg(aws, seeded, None)
    aws.set(
        "autoscaling.describe_auto_scaling_instances",
        [asg_instance(False), asg_instance(True)],
    )
    assert run_isolate(db, seeded).succeeded  # 격리 + 보호 APPLIED
    assert stored_result(db, seeded.isolate_id)["outcome"] == "APPLIED"

    child_id = release_child(db, seeded, make_execution)
    aws.calls.clear()
    # 해제: ENI 대조(격리 상태) → 복원 후 재조회(원래 SG)
    aws.set("ec2.describe_instances", [instance([ISOLATION_SG]), instance([WEB_SG])])
    aws.set("autoscaling.describe_auto_scaling_instances", restore_calls)
    if restore_write is not None:
        aws.set("autoscaling.set_instance_protection", restore_write)
    return child_id


def test_release_restores_protection_after_rereading_core_state(
    db, seeded, aws, guardrail, make_execution
):
    child_id = isolated_then_released(
        db,
        seeded,
        aws,
        make_execution,
        restore_calls=[asg_instance(True), asg_instance(False)],
    )
    outcome = workflows.run_ec2_unisolate_execution(db, child_id)

    assert outcome.succeeded
    result = stored_result(db, child_id)
    assert (result["phase"], result["outcome"], result["reason"]) == (
        "RESTORE",
        "APPLIED",
        "RESTORED",
    )
    names = aws.names()
    assert names.index("elbv2.register_targets") < names.index(
        "autoscaling.set_instance_protection"
    )


def test_failed_protection_restore_keeps_the_release_available_and_records_a_residual(
    db, seeded, aws, guardrail, make_execution
):
    child_id = isolated_then_released(
        db,
        seeded,
        aws,
        make_execution,
        restore_calls=asg_instance(True),
        restore_write=rejected(),
    )
    outcome = workflows.run_ec2_unisolate_execution(db, child_id)

    assert outcome.succeeded and not outcome.deferred
    assert [s.step_type for s in outcome.steps] == [
        ex.STEP_RESTORE_ENI_SECURITY_GROUPS,
        ex.STEP_REGISTER_TARGET,
    ]
    result = stored_result(db, child_id)
    assert (result["outcome"], result["reason"], result["residual"]) == (
        "FAILED",
        "WRITE_REJECTED",
        True,
    )


def test_unreadable_asg_at_release_still_releases_and_leaves_a_residual(
    db, seeded, aws, guardrail, make_execution
):
    child_id = isolated_then_released(
        db, seeded, aws, make_execution, restore_calls=DOWN
    )
    outcome = workflows.run_ec2_unisolate_execution(db, child_id)

    assert outcome.succeeded
    result = stored_result(db, child_id)
    assert (result["reason"], result["residual"]) == ("LOOKUP_FAILED", True)
    assert "autoscaling.set_instance_protection" not in aws.names()


def test_release_after_a_rejected_protect_does_not_touch_protection(
    db, seeded, aws, guardrail, make_execution
):
    stored_plan_matches_execution_id(db, seeded)
    arrange_managed_asg(aws, seeded, None)
    aws.set("autoscaling.describe_auto_scaling_instances", asg_instance(False))
    aws.set("autoscaling.set_instance_protection", rejected())
    assert run_isolate(db, seeded).succeeded
    assert stored_result(db, seeded.isolate_id)["reason"] == "WRITE_REJECTED"

    child_id = release_child(db, seeded, make_execution)
    aws.calls.clear()
    aws.set("ec2.describe_instances", [instance([ISOLATION_SG]), instance([WEB_SG])])
    assert workflows.run_ec2_unisolate_execution(db, child_id).succeeded
    assert not [n for n in aws.names() if n.startswith("autoscaling.")]
    assert stored_result(db, child_id)["reason"] == "NO_PROTECTION_WRITE"


def test_old_approved_plan_is_still_proven_after_hundreds_of_later_plans(
    db, seeded, aws, make_execution, monkeypatch
):
    """오래 살아 있는 승인 ASG가 무관한 뒤 실행 때문에 보호를 잃으면 안 된다(나이·개수 상한 금지)."""
    stored_plan_matches_execution_id(db, seeded)
    for _ in range(510):
        later = make_execution(
            db,
            seeded.incident,
            runbook_id=RunbookId.RUNBOOK_EC2_ENABLE_AUTOSCALING,
            target_arn=build_plan().target_arn,
        )
        assert exec_repo.bind_autoscaling_plan(
            db,
            later.execution_id,
            build_plan(later.execution_id).model_dump(mode="json"),
        )
    db.commit()
    assert len(workflows._prefetch_autoscaling_plans(db)) >= 511

    arrange_managed_asg(aws, seeded, monkeypatch)
    aws.set(
        "autoscaling.describe_auto_scaling_instances",
        [asg_instance(False), asg_instance(True)],
    )
    assert run_isolate(db, seeded).succeeded

    (row,) = backup_rows(db)
    assert row.payload["scale_in_protection"]["status"] == "MANAGED"
    assert stored_result(db, seeded.isolate_id)["outcome"] == "APPLIED"


def failing_sql(db):
    """실제 PostgreSQL 오류를 낸다(트랜잭션이 실패 상태가 된다) — 예외 대역이 아니다."""
    db.execute(text("SELECT 1/0"))


def test_parent_result_lookup_sql_failure_keeps_release_success_and_writes_nothing(
    db, seeded, aws, guardrail, make_execution, monkeypatch
):
    child_id = isolated_then_released(
        db,
        seeded,
        aws,
        make_execution,
        restore_calls=[asg_instance(True), asg_instance(False)],
    )

    def broken_lookup(session, execution_id):
        failing_sql(session)

    monkeypatch.setattr(
        workflows.executions_repo, "get_scale_in_protection_result", broken_lookup
    )

    outcome = workflows.run_ec2_unisolate_execution(db, child_id)

    assert outcome.succeeded  # 코어 SG/TG 복원 SUCCESS는 보조 조회 실패로 깨지지 않는다
    assert "elbv2.register_targets" in aws.names()
    assert "autoscaling.set_instance_protection" not in aws.names()
    monkeypatch.undo()
    assert stored_result(db, child_id) is None  # 세션이 복구되어 이어서 쓸 수 있다


def test_residual_sql_failure_still_lets_the_dispatcher_close_isolation_and_run_release(
    db, seeded, aws, guardrail, make_execution, monkeypatch
):
    stored_plan_matches_execution_id(db, seeded)
    arrange_managed_asg(aws, seeded, None)
    aws.set(
        "autoscaling.describe_auto_scaling_instances",
        [asg_instance(False), asg_instance(True)],
    )
    assert run_isolate(db, seeded).succeeded
    # 끊긴 격리를 흉내 낸다: 보호 기록이 남지 못한 상태(잔여 기록이 필요한 모양)
    db.execute(
        models.ActionExecution.__table__.update()
        .where(models.ActionExecution.execution_id == seeded.isolate_id)
        .values(scale_in_protection_result=None)
    )
    db.commit()
    child_id = release_child(db, seeded, make_execution)
    aws.calls.clear()
    aws.set(
        "ec2.describe_instances",
        [
            instance([ISOLATION_SG]),
            instance([ISOLATION_SG]),
            instance([WEB_SG]),
            instance([WEB_SG]),
        ],
    )
    aws.set(
        "elbv2.describe_target_health",
        [{"TargetHealthDescriptions": []}, {"TargetHealthDescriptions": []}, HEALTH],
    )
    aws.set("autoscaling.describe_auto_scaling_instances", asg_instance(False))

    real_set = exec_repo.set_scale_in_protection_result

    def broken_set(session, execution_id, result):
        if (
            execution_id == seeded.isolate_id
            and result.get("reason") == "EXECUTION_INTERRUPTED"
        ):
            failing_sql(session)
        return real_set(session, execution_id, result)

    monkeypatch.setattr(
        workflows.executions_repo, "set_scale_in_protection_result", broken_set
    )

    report = dispatcher.dispatch_pending(
        db, None, workflows.VerificationRetryPolicy(max_attempts=3, interval_seconds=0)
    )

    db.expire_all()
    # 격리는 SUCCESS로 확정됐고 같은 주기에 해제가 끝나 원본이 ROLLED_BACK이 됐다
    assert exec_repo.get_execution(db, seeded.isolate_id).status.value == "ROLLED_BACK"
    assert exec_repo.get_execution(db, child_id).status.value == "SUCCESS"
    assert "elbv2.register_targets" in aws.names()  # 해제도 같은 주기에 실행됐다
    assert exec_repo.list_steps(db, child_id)
    assert report.started >= 1
