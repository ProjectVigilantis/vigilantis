"""EC2_ENABLE_AUTOSCALING 실행·판정 통합 테스트 — 실제 PostgreSQL 필요(미기동 시 skip). (Issue #446)

AWS 호출은 executor의 세 진입점(prepare·execute·inspect)을 가짜로 바꿔 막는다. 여기서 보는
것은 **워크플로가 소유한 보장**이다 — 계획·단계가 AWS 콜백보다 먼저 commit되는가, 저장된
계획만 재시도에 쓰이는가, 관측(ready·incomplete·query_failed)이 SUCCESS·FAILED·보류로
갈리는가, 요약이 단계로 남는가. executor 내부 호출 순서는 executor 테스트의 몫이다.
"""

import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

CORE_API = Path(__file__).resolve().parents[1]
REPO_ROOT = Path(__file__).resolve().parents[3]
for p in (str(CORE_API), str(REPO_ROOT / "packages")):
    if p not in sys.path:
        sys.path.insert(0, p)

import dispatcher  # noqa: E402
import workflows  # noqa: E402
from db import mappers  # noqa: E402
from db.repositories import executions as exec_repo  # noqa: E402
from db.repositories import incidents as incidents_repo  # noqa: E402
from schemas.api.actions import ExecutionStatus  # noqa: E402
from schemas.api.incidents import IncidentCategory, IncidentStatus  # noqa: E402
from schemas.autoscaling import (  # noqa: E402
    SCALING_POLICY_NAME,
    AutoscalingExecutionPlan,
    AutoscalingInspection,
    AutoscalingMetadataOptions,
    AutoscalingSourceIntent,
    autoscaling_image_name,
)
from schemas.candidates import CandidateStatus  # noqa: E402
from schemas.executions import (  # noqa: E402
    ExecutionEffect,
    ExecutionStepResult,
    ExecutionStepStatus,
)
from schemas.precheck import PrecheckReasonCode  # noqa: E402
from schemas.runbooks import RunbookId, TriggerSource  # noqa: E402
from services.aws import executor as ex  # noqa: E402

ACCOUNT = "123456789012"
REGION = "ap-northeast-2"
INSTANCE = "i-0abc123456789def0"
INSTANCE_ARN = f"arn:aws:ec2:{REGION}:{ACCOUNT}:instance/{INSTANCE}"
RUNBOOK = RunbookId.RUNBOOK_EC2_ENABLE_AUTOSCALING
MIN_SIZE, MAX_SIZE = 1, 3  # conftest의 기본 후보 파라미터

VALID_PARAMETERS = {
    "instance_id": INSTANCE,
    "min_size": MIN_SIZE,
    "max_size": MAX_SIZE,
    "evidence_id": "ev-1",
}

RETRY_NOW = workflows.VerificationRetryPolicy(max_attempts=3, interval_seconds=0)


def make_plan(execution_id, *, target_arn=INSTANCE_ARN, min_size=MIN_SIZE, max_size=MAX_SIZE):
    """schemas.autoscaling의 필수 필드를 전부 채운 계획."""
    return AutoscalingExecutionPlan(
        target_arn=target_arn,
        execution_id=execution_id,
        min_size=min_size,
        max_size=max_size,
        desired_capacity=min_size,
        image_name=autoscaling_image_name(INSTANCE, execution_id),
        launch_template_name=f"vigilantis-lt-{execution_id}",
        launch_template_version=1,
        asg_name=f"vigilantis-asg-{execution_id}",
        scaling_policy_name=SCALING_POLICY_NAME,
        cpu_target_percent=50,
        default_instance_warmup_seconds=300,
        source=AutoscalingSourceIntent(
            instance_id=INSTANCE,
            instance_type="t3.medium",
            subnet_id="subnet-0abc1234",
            security_group_ids=("sg-0abc1234",),
            iam_instance_profile_arn=None,
            ebs_optimized=False,
            detailed_monitoring=False,
            dedicated_tenancy=False,
            associate_public_ip_address=None,
            metadata_options=AutoscalingMetadataOptions(
                http_tokens=None,
                http_endpoint=None,
                http_put_response_hop_limit=None,
                http_protocol_ipv6=None,
                instance_metadata_tags=None,
            ),
            preserved_tags=(),
        ),
    )


def step(sequence, *, status=ExecutionStepStatus.SUCCESS, effect=ExecutionEffect.APPLIED):
    return ExecutionStepResult(
        sequence=sequence,
        affected_arn=INSTANCE_ARN,
        step_type=f"STEP_{sequence}",
        aws_operation=f"op{sequence}",
        status=status,
        effect=effect,
        occurred_at=datetime.now(UTC),
    )


def in_progress(sequence):
    return step(sequence, status=ExecutionStepStatus.IN_PROGRESS, effect=None)


def ready(summary="이미지·템플릿·ASG·정책 확인"):
    return AutoscalingInspection(status="ready", summary=summary)


def incomplete(summary="잔여물: image ami-0aaa, launch template vig-lt"):
    return AutoscalingInspection(status="incomplete", summary=summary)


def query_failed(summary="DescribeAutoScalingGroups 조회 실패"):
    return AutoscalingInspection(
        status="query_failed",
        summary=summary,
        reason_code=PrecheckReasonCode.PRECHECK_AWS_ERROR,
    )


@pytest.fixture()
def reserved(db, make_incident, make_candidate):
    """접수 직후 — IN_PROGRESS 실행 1건(후보 min1·max3), Incident는 조치 진행 중."""

    def _make(*, validated_command=None, parameters=None, target_arn=INSTANCE_ARN):
        incident = make_incident(
            db,
            category=IncidentCategory.FINOPS,
            subject_arn=INSTANCE_ARN,
            status=IncidentStatus.ANALYZING,
        )
        candidate = make_candidate(
            db,
            incident,
            runbook_id=RUNBOOK,
            target_arn=target_arn,
            parameters=parameters,
            status=CandidateStatus.CLAIMED,
        )
        incidents_repo.update_incident_status(
            db,
            incident.incident_id,
            expected=incident.status,
            next_status=IncidentStatus.ACTION_IN_PROGRESS,
        )
        execution = exec_repo.create_execution(
            db,
            incident_id=incident.incident_id,
            runbook_id=RUNBOOK,
            target_arn=target_arn,
            trigger_source=TriggerSource.USER_APPROVAL,
            candidate_id=candidate.candidate_id,
            validated_command=validated_command,
        )
        db.commit()
        return incident.incident_id, execution.execution_id

    return _make


@pytest.fixture()
def aws(db, monkeypatch):
    """executor 세 진입점의 가짜 — 호출 시점의 DB 상태를 함께 기록한다."""
    state = SimpleNamespace(
        prepare_calls=[],
        execute_calls=[],
        inspect_calls=[],
        prepare_result=None,  # None이면 의도와 같은 계획을 돌려준다
        execute_steps=(1, 2, 3, 4),
        execute_failure=None,  # (reason_code, summary) — 마지막 단계를 FAILED로 끝낸다
        inspection=ready(),
    )

    def committed_plan(execution_id):
        # savepoint 모드 세션에서 rollback은 commit되지 않은 것만 되돌린다 — 살아남으면 commit된 것
        db.rollback()
        return exec_repo.get_autoscaling_plan(db, execution_id)

    def prepare(target_arn, *, execution_id, min_size, max_size):
        state.prepare_calls.append(
            {"in_transaction": db.in_transaction(), "execution_id": execution_id}
        )
        if state.prepare_result is not None:
            return state.prepare_result
        return (
            make_plan(execution_id, target_arn=target_arn, min_size=min_size, max_size=max_size),
            None,
            None,
        )

    def execute(target_arn, *, plan, record_step):
        call = {
            "in_transaction": db.in_transaction(),
            "plan": plan,
            "plan_committed": committed_plan(plan.execution_id),
            "step_committed_before_call": [],
        }
        state.execute_calls.append(call)
        results = []
        for sequence in state.execute_steps:
            record_step(in_progress(sequence))
            db.rollback()
            call["step_committed_before_call"].append(
                sequence
                in {s.sequence for s in exec_repo.list_steps(db, plan.execution_id)}
            )
            last = sequence == state.execute_steps[-1]
            if last and state.execute_failure is not None:
                code, summary = state.execute_failure
                failed = ExecutionStepResult(
                    sequence=sequence,
                    affected_arn=INSTANCE_ARN,
                    step_type=f"STEP_{sequence}",
                    aws_operation=f"op{sequence}",
                    status=ExecutionStepStatus.FAILED,
                    effect=ExecutionEffect.UNKNOWN,
                    error_summary=summary,
                    occurred_at=datetime.now(UTC),
                )
                record_step(failed)
                results.append(failed)
                return ex.ExecutionOutcome(
                    steps=tuple(results), reason_code=code, error_summary=summary
                )
            done = step(sequence)
            record_step(done)
            results.append(done)
        return ex.ExecutionOutcome(steps=tuple(results))

    def inspect(plan):
        state.inspect_calls.append({"in_transaction": db.in_transaction()})
        return state.inspection

    monkeypatch.setattr(ex, "prepare_ec2_autoscaling_plan", prepare, raising=False)
    monkeypatch.setattr(ex, "execute_ec2_enable_autoscaling", execute, raising=False)
    monkeypatch.setattr(ex, "inspect_ec2_autoscaling", inspect, raising=False)
    return state


def asg_cycle(db, policy=RETRY_NOW):
    return dispatcher.dispatch_pending(db, None, policy, dispatcher.LANE_AUTOSCALING)


def status_of(db, execution_id):
    return exec_repo.get_execution(db, execution_id).status


def steps_by_sequence(db, execution_id):
    return {s.sequence: s for s in exec_repo.list_steps(db, execution_id)}


# --- 등록 --------------------------------------------------------------------


def test_runner_judge_pair_and_success_gate_are_registered():
    assert dispatcher._RUNNERS[RUNBOOK] is workflows.run_ec2_enable_autoscaling_execution
    assert dispatcher._JUDGES[RUNBOOK] is workflows.judge_ec2_enable_autoscaling
    assert RUNBOOK in dispatcher._AWAIT_JUDGEMENT_ON_SUCCESS


# --- 실행: AWS 콜백보다 먼저 commit ----------------------------------------------


def test_plan_and_steps_are_committed_before_aws_callbacks(db, reserved, aws):
    _incident_id, execution_id = reserved()

    outcome = workflows.run_ec2_enable_autoscaling_execution(db, execution_id)

    assert outcome.succeeded
    # 읽기 전용 준비도 AWS 호출이므로 DB 트랜잭션을 쥐고 부르지 않는다
    assert aws.prepare_calls == [{"in_transaction": False, "execution_id": execution_id}]
    (call,) = aws.execute_calls
    assert call["in_transaction"] is False
    # 첫 AWS 변경 콜백 시점에 계획이 이미 commit되어 있고, 같은 계획이 넘어간다
    assert call["plan_committed"] == call["plan"].model_dump(mode="json")
    assert call["step_committed_before_call"] == [True, True, True, True]
    assert sorted(steps_by_sequence(db, execution_id)) == [1, 2, 3, 4]


def test_plan_roundtrips_through_jsonb(db, reserved, aws):
    _incident_id, execution_id = reserved()
    workflows.run_ec2_enable_autoscaling_execution(db, execution_id)
    db.expire_all()

    stored = exec_repo.get_autoscaling_plan(db, execution_id)

    assert AutoscalingExecutionPlan.model_validate(stored) == aws.execute_calls[0]["plan"]
    # validated_command의 의미는 바꾸지 않는다
    assert exec_repo.get_execution(db, execution_id).validated_command is None


# --- 실행: 저장된 계획 · 의도 고정 ---------------------------------------------


def test_retry_without_steps_uses_only_the_persisted_plan(db, reserved, aws):
    _incident_id, execution_id = reserved()
    persisted = make_plan(execution_id)
    assert exec_repo.bind_autoscaling_plan(
        db, execution_id, persisted.model_dump(mode="json")
    )
    db.commit()
    # 원본이 바뀌어 준비가 다른 계획을 돌려주더라도 쓰지 않는다 — 준비를 다시 부르지도 않는다
    aws.prepare_result = (make_plan(execution_id, min_size=2), None, None)

    outcome = workflows.run_ec2_enable_autoscaling_execution(db, execution_id)

    assert outcome.succeeded
    assert aws.prepare_calls == []
    assert aws.execute_calls[0]["plan"] == persisted


def test_persisted_plan_that_drifted_from_intent_is_not_overwritten(db, reserved, aws):
    _incident_id, execution_id = reserved()
    drifted = make_plan(execution_id, min_size=2).model_dump(mode="json")
    exec_repo.bind_autoscaling_plan(db, execution_id, drifted)
    db.commit()

    outcome = workflows.run_ec2_enable_autoscaling_execution(db, execution_id)

    assert not outcome.succeeded
    assert outcome.reason_code is PrecheckReasonCode.PRECHECK_PARAM_INVALID
    assert outcome.steps == () and aws.execute_calls == []
    assert exec_repo.get_autoscaling_plan(db, execution_id) == drifted


def test_bind_is_once_only(db, reserved):
    _incident_id, execution_id = reserved()
    first = make_plan(execution_id).model_dump(mode="json")
    second = make_plan(execution_id, max_size=4).model_dump(mode="json")

    assert exec_repo.bind_autoscaling_plan(db, execution_id, first)
    assert not exec_repo.bind_autoscaling_plan(db, execution_id, second)
    db.expire_all()
    assert exec_repo.get_autoscaling_plan(db, execution_id) == first


def test_plan_stored_by_another_actor_during_prepare_is_not_overwritten(
    db, reserved, aws, monkeypatch
):
    _incident_id, execution_id = reserved()
    theirs = make_plan(execution_id, max_size=2).model_dump(mode="json")

    original_prepare = ex.prepare_ec2_autoscaling_plan

    def racing_prepare(*args, **kwargs):
        result = original_prepare(*args, **kwargs)
        exec_repo.bind_autoscaling_plan(db, execution_id, theirs)
        db.commit()
        return result

    monkeypatch.setattr(ex, "prepare_ec2_autoscaling_plan", racing_prepare)
    outcome = workflows.run_ec2_enable_autoscaling_execution(db, execution_id)

    assert not outcome.succeeded
    assert outcome.reason_code is PrecheckReasonCode.PRECHECK_PARAM_INVALID
    assert aws.execute_calls == []
    assert exec_repo.get_autoscaling_plan(db, execution_id) == theirs


def test_prepared_plan_that_disagrees_with_intent_is_rejected_before_persisting(
    db, reserved, aws
):
    _incident_id, execution_id = reserved()
    aws.prepare_result = (make_plan(execution_id, max_size=4), None, None)

    outcome = workflows.run_ec2_enable_autoscaling_execution(db, execution_id)

    assert outcome.reason_code is PrecheckReasonCode.PRECHECK_PARAM_INVALID
    assert exec_repo.get_autoscaling_plan(db, execution_id) is None
    assert aws.execute_calls == []


# --- 실행: 의도 검증 -------------------------------------------------------------


@pytest.mark.parametrize(
    "case, command",
    [
        ("params of another shape", {"parameters": {"min_size": 1}}),
        (
            "instance of another resource",
            {
                "parameters": {
                    "instance_id": "i-0ffffffffffffffff",
                    "min_size": 1,
                    "max_size": 3,
                    "evidence_id": "ev-1",
                }
            },
        ),
        (
            "min above max",
            {
                "parameters": {
                    "instance_id": INSTANCE,
                    "min_size": 4,
                    "max_size": 3,
                    "evidence_id": "ev-1",
                }
            },
        ),
        ("empty command", {}),
        ("missing parameters", {"runbook_id": RUNBOOK.value, "target_arn": INSTANCE_ARN}),
        (
            "non-mapping parameters",
            {"runbook_id": RUNBOOK.value, "target_arn": INSTANCE_ARN, "parameters": [1, 3]},
        ),
        (
            "missing runbook identity",
            {"target_arn": INSTANCE_ARN, "parameters": VALID_PARAMETERS},
        ),
        (
            "missing target identity",
            {"runbook_id": RUNBOOK.value, "parameters": VALID_PARAMETERS},
        ),
        ("another runbook", {"runbook_id": "RUNBOOK_EC2_RIGHTSIZING"}),
        ("another target", {"target_arn": f"arn:aws:ec2:{REGION}:{ACCOUNT}:instance/i-0bbb"}),
    ],
)
def test_invalid_immutable_command_never_reaches_aws(db, reserved, aws, case, command):
    _incident_id, execution_id = reserved(validated_command=command)

    outcome = workflows.run_ec2_enable_autoscaling_execution(db, execution_id)

    assert outcome.reason_code is PrecheckReasonCode.PRECHECK_PARAM_INVALID, case
    assert aws.prepare_calls == [] and aws.execute_calls == []
    assert exec_repo.get_autoscaling_plan(db, execution_id) is None


def test_target_that_is_not_an_instance_never_reaches_aws(db, reserved, aws):
    volume_arn = f"arn:aws:ec2:{REGION}:{ACCOUNT}:volume/vol-0abc123456789def0"
    _incident_id, execution_id = reserved(target_arn=volume_arn)

    outcome = workflows.run_ec2_enable_autoscaling_execution(db, execution_id)

    assert outcome.reason_code is PrecheckReasonCode.PRECHECK_PARAM_INVALID
    assert aws.prepare_calls == []


def test_valid_immutable_command_wins_over_the_candidate(db, reserved, aws):
    _incident_id, execution_id = reserved(
        validated_command={
            "runbook_id": RUNBOOK.value,
            "target_arn": INSTANCE_ARN,
            "parameters": {
                "instance_id": INSTANCE,
                "min_size": 2,
                "max_size": 4,
                "evidence_id": "ev-1",
            },
        }
    )

    outcome = workflows.run_ec2_enable_autoscaling_execution(db, execution_id)

    assert outcome.succeeded
    plan = aws.execute_calls[0]["plan"]
    assert (plan.min_size, plan.max_size) == (2, 4)


# --- 실행: 준비 실패 -----------------------------------------------------------


def test_prepare_query_failure_defers_then_holds_after_retries(db, reserved, aws):
    _incident_id, execution_id = reserved()
    aws.prepare_result = (
        None,
        PrecheckReasonCode.PRECHECK_AWS_ERROR,
        "DescribeInstances 조회 실패",
    )

    first = asg_cycle(db)
    second = asg_cycle(db)
    third = asg_cycle(db)

    assert (first.deferred, second.deferred, third.held) == (1, 1, 1)
    execution = exec_repo.get_execution(db, execution_id)
    assert execution.status is ExecutionStatus.FAILED  # 단계 없음 → 자산 변경 없음
    assert execution.verification_reason_code is PrecheckReasonCode.PRECHECK_AWS_ERROR
    assert exec_repo.get_autoscaling_plan(db, execution_id) is None
    assert exec_repo.list_steps(db, execution_id) == []
    assert aws.execute_calls == []


def test_prepare_precondition_failure_fails_without_retry(db, reserved, aws):
    _incident_id, execution_id = reserved()
    aws.prepare_result = (
        None,
        PrecheckReasonCode.PRECHECK_INVALID_STATE,
        "인스턴스가 running이 아닙니다",
    )

    report = asg_cycle(db)

    assert report.closed == 1 and report.deferred == 0
    execution = exec_repo.get_execution(db, execution_id)
    assert execution.status is ExecutionStatus.FAILED
    assert "PRECHECK_INVALID_STATE" in execution.error_summary
    assert "running" in execution.error_summary


# --- 디스패치 → 판정 -----------------------------------------------------------


def test_success_waits_for_the_observation_then_records_it(db, reserved, aws):
    _incident_id, execution_id = reserved()

    first = asg_cycle(db)

    # 실행 반환은 성공의 경계가 아니다
    assert first.started == 1 and first.awaiting_status_check == 1 and first.closed == 0
    assert status_of(db, execution_id) is ExecutionStatus.IN_PROGRESS
    assert aws.inspect_calls == []

    second = asg_cycle(db)

    assert second.judged == 1 and second.closed == 1 and second.started == 0
    assert aws.execute_calls and len(aws.execute_calls) == 1
    assert aws.inspect_calls == [{"in_transaction": False}]
    execution = exec_repo.get_execution(db, execution_id)
    assert execution.status is ExecutionStatus.SUCCESS and execution.error_summary is None
    observation = steps_by_sequence(db, execution_id)[5]
    contract = mappers.to_step_result(observation)
    assert contract.status is ExecutionStepStatus.SUCCESS
    assert contract.effect is ExecutionEffect.NOT_APPLIED  # 읽기 전용
    assert "[ready]" in contract.result_summary
    assert "이미지·템플릿·ASG·정책 확인" in contract.result_summary
    assert "애플리케이션 서비스 가능 여부는 검증하지 않았습니다" in contract.result_summary
    assert "원본 인스턴스는 유지" in contract.result_summary


def test_incomplete_observation_fails_with_named_residuals_and_no_compensation(
    db, reserved, aws
):
    _incident_id, execution_id = reserved()
    aws.inspection = incomplete()
    asg_cycle(db)

    report = asg_cycle(db)

    assert report.closed == 1
    execution = exec_repo.get_execution(db, execution_id)
    assert execution.status is ExecutionStatus.FAILED
    assert "image ami-0aaa" in execution.error_summary
    assert "launch template vig-lt" in execution.error_summary
    contract = mappers.to_step_result(steps_by_sequence(db, execution_id)[5])
    assert "[incomplete]" in contract.result_summary
    assert "image ami-0aaa" in contract.result_summary
    assert len(aws.execute_calls) == 1  # 재실행·보상 없음
    assert aws.prepare_calls != [] and len(aws.inspect_calls) == 1


def test_partial_execution_is_reconciled_by_observation_not_rollback(db, reserved, aws):
    _incident_id, execution_id = reserved()
    aws.execute_failure = (PrecheckReasonCode.PRECHECK_AWS_ERROR, "CreateAutoScalingGroup 5xx")
    aws.inspection = incomplete("잔여물: image ami-0aaa, launch template vig-lt")

    first = asg_cycle(db)

    # 자산이 바뀐 채 실패 → 자동 원복 신호가 아니라 다음 주기의 현물 판정
    assert first.awaiting_judgement == 1 and first.rollback_initiated == 0
    assert status_of(db, execution_id) is ExecutionStatus.IN_PROGRESS

    asg_cycle(db)

    assert status_of(db, execution_id) is ExecutionStatus.FAILED
    assert len(aws.execute_calls) == 1


def test_interrupted_execution_is_judged_only_never_resumed(db, reserved, aws):
    _incident_id, execution_id = reserved()
    plan = make_plan(execution_id)
    exec_repo.bind_autoscaling_plan(db, execution_id, plan.model_dump(mode="json"))
    exec_repo.add_step(db, in_progress(1), execution_id=execution_id)
    db.commit()
    aws.inspection = incomplete("잔여물: image ami-0aaa")

    report = asg_cycle(db)

    assert report.judged == 1 and report.started == 0
    assert aws.execute_calls == [] and aws.prepare_calls == []
    assert status_of(db, execution_id) is ExecutionStatus.FAILED


def test_interrupted_execution_whose_resources_exist_is_judged_ready(db, reserved, aws):
    _incident_id, execution_id = reserved()
    exec_repo.bind_autoscaling_plan(
        db, execution_id, make_plan(execution_id).model_dump(mode="json")
    )
    exec_repo.add_step(db, in_progress(4), execution_id=execution_id)
    db.commit()

    asg_cycle(db)

    assert status_of(db, execution_id) is ExecutionStatus.SUCCESS


def test_query_failure_records_failed_observation_before_unverified(
    db, reserved, aws
):
    _incident_id, execution_id = reserved()
    asg_cycle(db)  # 실행 → 판정 대기
    aws.inspection = query_failed()

    first = asg_cycle(db)
    second = asg_cycle(db)
    third = asg_cycle(db)

    assert (first.deferred, second.deferred, third.held) == (1, 1, 1)
    assert len(aws.inspect_calls) == 3
    execution = exec_repo.get_execution(db, execution_id)
    assert execution.status is ExecutionStatus.UNVERIFIED
    assert execution.verification_attempts == 3
    # 조회하지 못한 관측은 SUCCESS가 아니라 FAILED·NOT_APPLIED 단계 5로 남는다
    steps = steps_by_sequence(db, execution_id)
    assert sorted(steps) == [1, 2, 3, 4, 5]
    observation = mappers.to_step_result(steps[5])
    assert observation.status is ExecutionStepStatus.FAILED
    assert observation.effect is ExecutionEffect.NOT_APPLIED
    assert observation.result_summary is None
    assert len(aws.execute_calls) == 1


def _residual_names(db, execution_id):
    """저장된 계획에서만 읽은 잔여 자원 이름 — 요약 문자열을 해석하지 않는다."""
    db.expire_all()
    plan = AutoscalingExecutionPlan.model_validate(
        exec_repo.get_autoscaling_plan(db, execution_id)
    )
    return {
        "image": plan.image_name,
        "launch_template": plan.launch_template_name,
        "asg": plan.asg_name,
        "policy": plan.scaling_policy_name,
    }


def test_exhausted_hold_keeps_residual_names_identifiable_from_the_plan(db, reserved, aws):
    _incident_id, execution_id = reserved()
    asg_cycle(db)
    aws.inspection = query_failed("요약은 이름을 담지 않는다")
    for _ in range(3):
        asg_cycle(db)

    execution = exec_repo.get_execution(db, execution_id)
    assert execution.status is ExecutionStatus.UNVERIFIED
    assert _residual_names(db, execution_id) == {
        "image": autoscaling_image_name(INSTANCE, execution_id),
        "launch_template": f"vigilantis-lt-{execution_id}",
        "asg": f"vigilantis-asg-{execution_id}",
        "policy": SCALING_POLICY_NAME,
    }
    # 확정이 계획을 지우거나 바꾸지 않는다
    assert exec_repo.get_autoscaling_plan(db, execution_id) == make_plan(
        execution_id
    ).model_dump(mode="json")
    # 소진 보류의 요약과 단계 5가 계획한 이름을 싣고, 존재를 확인한 것처럼 쓰지 않는다
    names = _residual_names(db, execution_id)
    observation = mappers.to_step_result(steps_by_sequence(db, execution_id)[5])
    for text in (execution.error_summary, observation.error_summary):
        assert all(name in text for name in names.values())
        assert "존재 미확인" in text
    assert "요약은 이름을 담지 않는다" in observation.error_summary  # 관측된 것은 따로
    assert "PRECHECK_AWS_ERROR" in observation.error_summary
    assert execution.verification_attempts == 3 and len(aws.inspect_calls) == 3


def test_public_incident_detail_on_exhausted_hold(client_pg, db, reserved, aws):
    """공개 표현 — 상세 응답이 실제로 무엇을 싣는지 고정한다.

    ExecutionSummaryItem은 status와 typed verification_hold만 싣는다. error_summary와 단계는
    공개 DTO에 없으므로 잔여 자원 이름은 아직 공개 응답으로 나가지 않는다(DB의 단계 5·
    error_summary에만 있다)."""
    incident_id, _execution_id = reserved()
    asg_cycle(db)
    aws.inspection = query_failed()
    for _ in range(3):
        asg_cycle(db)

    body = client_pg.get(f"/api/v1/incidents/{incident_id}").json()

    (item,) = body["executions"]
    assert item["status"] == ExecutionStatus.UNVERIFIED.value
    assert item["verification_hold"]["reason_code"] == "PRECHECK_AWS_ERROR"
    assert item["verification_hold"]["attempts"] == 3
    assert set(item) == {
        "execution_id", "runbook_id", "status", "available_recovery_runbook_ids",
        "verification_hold", "updated_at",
    }


def test_lost_first_image_response_is_identifiable_by_plan_name_not_image_id(
    db, reserved, aws
):
    """첫 이미지 생성 응답이 유실돼 ImageId를 모르는 채 끊겨도 이름은 계획에 있다."""
    _incident_id, execution_id = reserved()
    exec_repo.bind_autoscaling_plan(
        db, execution_id, make_plan(execution_id).model_dump(mode="json")
    )
    exec_repo.add_step(db, in_progress(1), execution_id=execution_id)  # 응답 없음: 결과 미기록
    db.commit()
    aws.inspection = query_failed()

    for _ in range(3):
        asg_cycle(db)

    assert status_of(db, execution_id) is ExecutionStatus.UNVERIFIED
    assert aws.execute_calls == []  # 이어서 쓰지 않는다
    first = mappers.to_step_result(steps_by_sequence(db, execution_id)[1])
    assert first.status is ExecutionStepStatus.IN_PROGRESS and first.aws_request_id is None
    assert _residual_names(db, execution_id)["image"] == autoscaling_image_name(
        INSTANCE, execution_id
    )


def test_query_recovering_before_exhaustion_settles_normally(db, reserved, aws):
    _incident_id, execution_id = reserved()
    asg_cycle(db)
    aws.inspection = query_failed()
    asg_cycle(db)
    assert status_of(db, execution_id) is ExecutionStatus.IN_PROGRESS

    aws.inspection = ready()
    asg_cycle(db)

    execution = exec_repo.get_execution(db, execution_id)
    assert execution.status is ExecutionStatus.SUCCESS
    assert execution.verification_attempts == 0  # 판정이 내려지면 보류 기록을 지운다
    # 앞선 조회 실패 단계는 성공한 관측으로 갱신된다 — 실패 흔적이 남지 않는다
    observation = mappers.to_step_result(steps_by_sequence(db, execution_id)[5])
    assert observation.status is ExecutionStepStatus.SUCCESS
    assert observation.error_summary is None and "[ready]" in observation.result_summary


def test_judgement_without_a_plan_hands_off_to_a_human(db, reserved, aws):
    _incident_id, execution_id = reserved()
    exec_repo.add_step(db, in_progress(1), execution_id=execution_id)
    db.commit()

    asg_cycle(db)

    execution = exec_repo.get_execution(db, execution_id)
    assert execution.status is ExecutionStatus.FAILED
    assert "계획" in execution.error_summary
    assert aws.inspect_calls == []


def test_observation_step_is_updated_not_duplicated_when_judged_again(db, reserved, aws):
    _incident_id, execution_id = reserved()
    asg_cycle(db)
    aws.inspection = incomplete("잔여물: image ami-0aaa")
    workflows.judge_ec2_enable_autoscaling(db, execution_id)  # 확정 전에 끊긴 판정
    aws.inspection = ready()

    asg_cycle(db)

    observation = [
        s for s in exec_repo.list_steps(db, execution_id) if s.sequence == 5
    ]
    assert len(observation) == 1 and "[ready]" in observation[0].result_summary
    assert status_of(db, execution_id) is ExecutionStatus.SUCCESS

# --- 실제 경계: 가짜는 AWS 전송(aws_client)뿐 — prepare·execute·inspect는 진짜 executor ------------


class BoundaryAws:
    """허용한 작업만 받는 상태형 AWS 전송 대역. 생성은 상태를 남기고 조회는 그 상태를 읽는다.

    표에 없는 작업(삭제·정지·부착·수정)은 AssertionError — 보상 삭제·원본 조작이 없다는 것을 본다.
    변경 작업이 불리는 시점에 DB에서 계획·단계가 commit돼 있는지도 여기서 기록한다.
    """

    ALLOWED = frozenset(
        {
            "describe_instances", "create_image", "describe_images", "describe_snapshots",
            "create_launch_template", "describe_launch_templates",
            "describe_launch_template_versions", "describe_instance_status",
            "describe_auto_scaling_instances", "create_auto_scaling_group",
            "describe_auto_scaling_groups", "put_scaling_policy", "describe_policies",
        }
    )
    MUTATIONS = frozenset(
        {"create_image", "create_launch_template", "create_auto_scaling_group", "put_scaling_policy"}
    )

    def __init__(self, db, execution_id_of):
        self.db = db
        self.execution_id_of = execution_id_of
        self.calls = []
        self.committed_at_mutation = []
        self.faults = {}  # operation -> (exception, applied)
        self.images, self.snapshots, self.templates = {}, {}, {}
        self.template_data, self.groups, self.policies = {}, {}, {}
        self.statuses, self._ids = {}, 0
        self.snapshot_count = 1
        self.instance = {
            "InstanceId": INSTANCE, "InstanceType": "t3.micro", "State": {"Name": "running"},
            "RootDeviceType": "ebs", "SubnetId": "subnet-0abc123456789def0",
            "EbsOptimized": False, "Monitoring": {"State": "disabled"},
            "Placement": {"AvailabilityZone": "ap-northeast-2a", "Tenancy": "default"},
            "NetworkInterfaces": [{
                "Attachment": {"DeviceIndex": 0},
                "Groups": [{"GroupId": "sg-0abc123456789def0"}],
            }],
            "Tags": [{"Key": "Name", "Value": "app"}],
        }

    def client(self, service, region=None):
        world = self

        class Client:
            def __getattr__(self, operation):
                if operation not in world.ALLOWED:
                    raise AssertionError(f"계약 밖 AWS 작업: {service}.{operation}")
                return lambda **kwargs: world.invoke(operation, kwargs)

        return Client()

    def mutations(self):
        return [op for op, _ in self.calls if op in self.MUTATIONS]

    def invoke(self, operation, kwargs):
        self.calls.append((operation, kwargs))
        if operation in self.MUTATIONS:
            # 변경 요청이 나가기 직전 — 이미 commit된 것만 남는다(rollback으로 확인)
            self.db.rollback()
            execution_id = self.execution_id_of()
            self.committed_at_mutation.append(
                (
                    operation,
                    exec_repo.get_autoscaling_plan(self.db, execution_id) is not None,
                    sorted(s.sequence for s in exec_repo.list_steps(self.db, execution_id)),
                )
            )
        fault = self.faults.pop(operation, None)
        if fault is not None and not fault[1]:
            raise fault[0]
        result = getattr(self, f"_{operation}")(**kwargs)
        if fault is not None:
            raise fault[0]
        return result

    def _id(self, prefix):
        self._ids += 1
        return f"{prefix}-{self._ids:017x}"

    def _describe_instances(self, InstanceIds):
        return {"Reservations": [{"Instances": [self.instance]}]}

    def _describe_auto_scaling_instances(self, InstanceIds, NextToken=None):
        return {"AutoScalingInstances": []}

    def _create_image(self, InstanceId, Name, Description, NoReboot, TagSpecifications):
        tags = {spec["ResourceType"]: spec["Tags"] for spec in TagSpecifications}
        image_id = self._id("ami")
        mappings = []
        for index in range(self.snapshot_count):
            snapshot_id = self._id("snap")
            self.snapshots[snapshot_id] = {
                "SnapshotId": snapshot_id, "OwnerId": ACCOUNT, "State": "completed",
                "Tags": tags["snapshot"],
            }
            mappings.append(
                {"DeviceName": f"/dev/sd{chr(ord('f') + index)}", "Ebs": {"SnapshotId": snapshot_id}}
            )
        self.images[image_id] = {
            "ImageId": image_id, "Name": Name, "OwnerId": ACCOUNT, "State": "available",
            "BlockDeviceMappings": mappings,
            "Tags": tags["image"],
        }
        return {"ImageId": image_id}

    def _describe_images(self, ImageIds=None, Owners=None, IncludeDisabled=None, Filters=None):
        if ImageIds is not None:
            return {"Images": [self.images[i] for i in ImageIds if i in self.images]}
        (prefix,) = next(f["Values"] for f in Filters if f["Name"] == "name")
        return {"Images": [i for i in self.images.values() if i["Name"].startswith(prefix)]}

    def _describe_snapshots(self, OwnerIds, Filters):
        ids = next(f["Values"] for f in Filters if f["Name"] == "snapshot-id")
        return {"Snapshots": [self.snapshots[s] for s in ids if s in self.snapshots]}

    def _create_launch_template(
        self, LaunchTemplateName, LaunchTemplateData, TagSpecifications, VersionDescription
    ):
        template_id = self._id("lt")
        self.templates[template_id] = {
            "LaunchTemplateId": template_id, "LaunchTemplateName": LaunchTemplateName,
            "LatestVersionNumber": 1, "DefaultVersionNumber": 1,
            "Tags": TagSpecifications[0]["Tags"],
        }
        self.template_data[template_id] = LaunchTemplateData
        return {"LaunchTemplate": self.templates[template_id]}

    def _describe_launch_templates(self, Filters):
        (prefix,) = next(f["Values"] for f in Filters if f["Name"] == "launch-template-name")
        return {"LaunchTemplates": [
            t for t in self.templates.values() if t["LaunchTemplateName"].startswith(prefix)
        ]}

    def _describe_launch_template_versions(self, LaunchTemplateId, Versions):
        return {"LaunchTemplateVersions": [{
            "LaunchTemplateId": LaunchTemplateId, "VersionNumber": 1,
            "LaunchTemplateData": self.template_data[LaunchTemplateId],
        }]}

    def _create_auto_scaling_group(
        self, AutoScalingGroupName, LaunchTemplate, MinSize, MaxSize, DesiredCapacity,
        VPCZoneIdentifier, DefaultInstanceWarmup, Tags,
    ):
        template_id = LaunchTemplate["LaunchTemplateId"]
        members = []
        for _ in range(DesiredCapacity):
            instance_id = self._id("i")
            self.statuses[instance_id] = ("running", "ok", "ok")
            members.append({
                "InstanceId": instance_id, "LifecycleState": "InService", "HealthStatus": "Healthy",
                "LaunchTemplate": {"LaunchTemplateId": template_id, "Version": LaunchTemplate["Version"]},
            })
        self.groups[AutoScalingGroupName] = {
            "AutoScalingGroupName": AutoScalingGroupName, "LaunchTemplate": LaunchTemplate,
            "MinSize": MinSize, "MaxSize": MaxSize, "DesiredCapacity": DesiredCapacity,
            "VPCZoneIdentifier": VPCZoneIdentifier, "DefaultInstanceWarmup": DefaultInstanceWarmup,
            "Instances": members,
            "Tags": [{**tag, "PropagateAtLaunch": False} for tag in Tags],
        }
        return {}

    def _describe_auto_scaling_groups(self, AutoScalingGroupNames):
        return {"AutoScalingGroups": [self.groups[n] for n in AutoScalingGroupNames if n in self.groups]}

    def _put_scaling_policy(self, AutoScalingGroupName, PolicyName, PolicyType, TargetTrackingConfiguration):
        self.policies[(AutoScalingGroupName, PolicyName)] = {
            "AutoScalingGroupName": AutoScalingGroupName, "PolicyName": PolicyName,
            "PolicyType": PolicyType, "TargetTrackingConfiguration": TargetTrackingConfiguration,
            "Enabled": True,
        }
        return {"PolicyARN": "arn:aws:autoscaling:policy"}

    def _describe_policies(self, AutoScalingGroupName, PolicyNames):
        return {"ScalingPolicies": [
            p for (g, n), p in self.policies.items() if g == AutoScalingGroupName and n in PolicyNames
        ]}

    def _describe_instance_status(self, InstanceIds, IncludeAllInstances):
        return {"InstanceStatuses": [
            {"InstanceId": i, "InstanceState": {"Name": s[0]},
             "SystemStatus": {"Status": s[1]}, "InstanceStatus": {"Status": s[2]}}
            for i in InstanceIds if (s := self.statuses.get(i))
        ]}


@pytest.fixture()
def transport(db, monkeypatch):
    """진짜 executor를 두고 전송만 바꾼다. 실행 id는 테스트가 reserved() 뒤에 채운다."""
    from services.aws import autoscaling as asg_module

    current = {}
    world = BoundaryAws(db, lambda: current["execution_id"])
    world.current = current
    settings = SimpleNamespace(
        AUTOSCALING_IMAGE_WAIT_DELAY_SECONDS=1, AUTOSCALING_IMAGE_WAIT_MAX_ATTEMPTS=2,
        AUTOSCALING_READY_WAIT_DELAY_SECONDS=1, AUTOSCALING_READY_WAIT_MAX_ATTEMPTS=2,
    )
    factory = lambda service, region=None, **_: world.client(service, region)  # noqa: E731
    monkeypatch.setattr(ex, "aws_client", factory)
    monkeypatch.setattr(asg_module, "aws_client", factory)
    monkeypatch.setattr(asg_module, "get_settings", lambda: settings)
    monkeypatch.setattr(asg_module, "_sleep", lambda _seconds: None)
    return world


def test_real_executor_boundary_commits_before_writes_and_judges_ready(db, reserved, transport):
    """prepare → 영속 계획 → 진짜 쓰기 4건 → 진짜 읽기 전용 관측 → 디스패처 최종 상태(PostgreSQL)."""
    _incident_id, execution_id = reserved()
    transport.current["execution_id"] = execution_id

    asg_cycle(db)  # 실행: 준비·쓰기. 요청 접수는 성공이 아니다
    assert status_of(db, execution_id) is ExecutionStatus.IN_PROGRESS
    asg_cycle(db)  # 판정: 읽기 전용 관측

    execution = exec_repo.get_execution(db, execution_id)
    assert execution.status is ExecutionStatus.SUCCESS
    assert transport.mutations() == [
        "create_image", "create_launch_template", "create_auto_scaling_group", "put_scaling_policy",
    ]
    # 변경 요청마다 그 시점에 계획이 commit돼 있고, 앞 단계(+자기 IN_PROGRESS)가 commit돼 있다
    assert transport.committed_at_mutation == [
        ("create_image", True, [1]),
        ("create_launch_template", True, [1, 2]),
        ("create_auto_scaling_group", True, [1, 2, 3]),
        ("put_scaling_policy", True, [1, 2, 3, 4]),
    ]
    image_call = next(kwargs for op, kwargs in transport.calls if op == "create_image")
    assert image_call["NoReboot"] is True
    plan = AutoscalingExecutionPlan.model_validate(exec_repo.get_autoscaling_plan(db, execution_id))
    assert (plan.min_size, plan.max_size, plan.desired_capacity) == (MIN_SIZE, MAX_SIZE, MIN_SIZE)
    steps = steps_by_sequence(db, execution_id)
    assert sorted(steps) == [1, 2, 3, 4, 5]
    observation = mappers.to_step_result(steps[5])
    assert observation.status is ExecutionStepStatus.SUCCESS
    assert "구성 확인" in observation.result_summary
    # 원본은 그대로, 판정은 쓰지 않는다
    assert transport.instance["State"]["Name"] == "running"
    assert transport.mutations().count("create_image") == 1


def test_real_executor_lost_image_response_is_judged_from_the_real_inspection(db, reserved, transport):
    """첫 이미지 요청이 적용된 뒤 응답이 유실 — 다시 쓰지 않고 진짜 관측이 잔여 자원을 이름으로 댄다."""
    from botocore.exceptions import ReadTimeoutError

    _incident_id, execution_id = reserved()
    transport.current["execution_id"] = execution_id
    transport.faults["create_image"] = (
        ReadTimeoutError(endpoint_url="https://ec2.ap-northeast-2.amazonaws.com/"),
        True,
    )

    for _ in range(3):
        asg_cycle(db)

    execution = exec_repo.get_execution(db, execution_id)
    plan = AutoscalingExecutionPlan.model_validate(exec_repo.get_autoscaling_plan(db, execution_id))
    # 다시 쓰지 않았다 — 보상 삭제도, 이어 쓰기도 없다
    assert transport.mutations() == ["create_image"]
    assert len(transport.images) == 1  # 요청은 적용됐다
    first = mappers.to_step_result(steps_by_sequence(db, execution_id)[1])
    assert first.status is ExecutionStepStatus.FAILED and first.effect is ExecutionEffect.UNKNOWN
    # 구성이 갖춰졌다고 말하지 않는다 — 미완 판정에 계획 이름과 잔여 자원이 남는다
    assert execution.status is ExecutionStatus.FAILED
    assert plan.image_name == next(iter(transport.images.values()))["Name"]
    image_id = next(iter(transport.images))
    assert image_id in execution.error_summary  # 잔여 자원이 실제 ID로 이름 붙는다

def test_real_executor_applied_lost_asg_response_names_ami_lt_asg_despite_many_snapshots(
    db, reserved, transport
):
    """AMI에 EBS 스냅샷 11개 + ASG 생성 적용 후 응답 유실 — 요약 길이 제한이 소유 자원 이름을 지우지 않는다."""
    from botocore.exceptions import ReadTimeoutError

    _incident_id, execution_id = reserved()
    transport.current["execution_id"] = execution_id
    transport.snapshot_count = 11
    transport.faults["create_auto_scaling_group"] = (
        ReadTimeoutError(endpoint_url="https://autoscaling.ap-northeast-2.amazonaws.com/"),
        True,
    )

    for _ in range(3):
        asg_cycle(db)

    execution = exec_repo.get_execution(db, execution_id)
    plan = AutoscalingExecutionPlan.model_validate(exec_repo.get_autoscaling_plan(db, execution_id))
    # 정책 단계는 시작되지 않았고, 다시 쓰거나 지우지 않았다
    assert transport.mutations() == [
        "create_image", "create_launch_template", "create_auto_scaling_group",
    ]
    assert len(next(iter(transport.images.values()))["BlockDeviceMappings"]) == 11
    assert plan.asg_name in transport.groups
    image_id = next(iter(transport.images))
    template_id = next(iter(transport.templates))
    assert execution.status is ExecutionStatus.FAILED
    observation = mappers.to_step_result(steps_by_sequence(db, execution_id)[5])
    for text in (execution.error_summary, observation.error_summary or observation.result_summary):
        assert image_id in text
        assert template_id in text or plan.launch_template_name in text
        assert plan.asg_name in text
