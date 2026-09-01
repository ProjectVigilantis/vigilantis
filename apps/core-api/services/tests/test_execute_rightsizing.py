"""executor.execute_rightsizing() 단위 테스트 (Issue #211).

AWS 불필요 — boto3 클라이언트를 가짜로 갈아 끼우고 **단계 기록과 effect**를 본다.
effect는 자동 원복이 "자산이 실제로 바뀌었는가"를 판단하는 유일한 입력이라,
낙관적으로 적히면 되돌릴 것을 되돌리지 않거나 멀쩡한 자산을 건드린다.
LocalStack 실물 검증은 test_execute_localstack.py가 맡는다.
"""

import sys
from pathlib import Path

import pytest
from botocore.exceptions import (
    ClientError,
    EndpointConnectionError,
    ParamValidationError,
    WaiterError,
)

CORE_API = Path(__file__).resolve().parents[2]
REPO_ROOT = Path(__file__).resolve().parents[4]
for p in (str(CORE_API), str(REPO_ROOT / "packages")):
    if p not in sys.path:
        sys.path.insert(0, p)

from schemas.executions import (  # noqa: E402
    ExecutionEffect,
    ExecutionStepStatus,
)
from schemas.precheck import PrecheckReasonCode  # noqa: E402
from services.aws import executor as ex  # noqa: E402

R = PrecheckReasonCode
S = ExecutionStepStatus
E = ExecutionEffect

ACCOUNT = "123456789012"
REGION = "ap-northeast-2"
INSTANCE = "i-0abc123456789def0"
INSTANCE_ARN = f"arn:aws:ec2:{REGION}:{ACCOUNT}:instance/{INSTANCE}"
TARGET_TYPE = "t3.micro"

STOP_RESPONSE = {
    "StoppingInstances": [
        {"InstanceId": INSTANCE, "PreviousState": {"Name": "running"}}
    ],
    "ResponseMetadata": {"RequestId": "req-stop"},
}


def client_error(
    code: str, operation: str = "Op", message: str = "", status: int | None = 400
) -> ClientError:
    """AWS 오류 응답 1건. status가 None이면 응답에 상태 코드가 없는 경우다."""
    metadata: dict = {"RequestId": "req-err"}
    if status is not None:
        metadata["HTTPStatusCode"] = status
    return ClientError(
        {"Error": {"Code": code, "Message": message}, "ResponseMetadata": metadata},
        operation,
    )


def waiter_error() -> WaiterError:
    return WaiterError(name="instance_stopped", reason="Max attempts exceeded", last_response={})


class FakeWaiter:
    def __init__(self, state):
        self._state = state

    def wait(self, **kwargs):
        self._state["calls"].append(("wait", kwargs))
        outcome = self._state["overrides"].get("wait")
        if isinstance(outcome, BaseException):
            raise outcome


class FakeEc2:
    """호출을 기록하고 지정된 응답·예외를 돌려주는 boto3 클라이언트 대역."""

    def __init__(self, state):
        self._state = state

    def get_waiter(self, name):
        self._state["calls"].append(("get_waiter", {"name": name}))
        return FakeWaiter(self._state)

    def __getattr__(self, operation):
        def call(**kwargs):
            self._state["calls"].append((operation, kwargs))
            outcome = self._state["overrides"].get(operation)
            if isinstance(outcome, BaseException):
                raise outcome
            if outcome is not None:
                return outcome
            return STOP_RESPONSE if operation == "stop_instances" else {}

        return call


@pytest.fixture
def aws(monkeypatch):
    """기본은 전부 통과 경로. configure(...)로 실패시킬 호출만 바꾼다."""
    state = {"overrides": {}, "calls": [], "clients": []}

    def factory(service, region=None, **_):
        state["clients"].append((service, region))
        return FakeEc2(state)

    monkeypatch.setattr(ex, "aws_client", factory)

    def configure(**overrides):
        state["overrides"].update(overrides)

    configure.calls = state["calls"]
    configure.clients = state["clients"]
    return configure


@pytest.fixture
def recorded():
    """record_step으로 들어온 단계 전부 — IN_PROGRESS 기록까지 순서대로 담는다."""
    steps = []
    return steps


def run(aws, recorded=None, *, target_arn=INSTANCE_ARN, target_type=TARGET_TYPE):
    return ex.execute_rightsizing(
        target_arn,
        target_instance_type=target_type,
        record_step=None if recorded is None else recorded.append,
    )


def operations(aws):
    return [name for name, _ in aws.calls]


# ------------------------------------------------------------------ 성공 경로


def test_stop_modify_start_runs_in_that_order(aws):
    """타입 변경은 stopped 상태에서만 받는다 — 정지가 조치의 일부다."""
    outcome = run(aws)

    assert outcome.succeeded
    assert operations(aws) == [
        "stop_instances",
        "get_waiter",
        "wait",
        "modify_instance_attribute",
        "start_instances",
    ]


def test_target_type_reaches_the_modify_call(aws):
    run(aws)

    modify = next(kwargs for name, kwargs in aws.calls if name == "modify_instance_attribute")
    assert modify == {"InstanceId": INSTANCE, "InstanceType": {"Value": TARGET_TYPE}}


def test_client_is_built_for_the_arn_region(aws):
    """다른 리전 클라이언트로 부르면 대상 자원을 찾지 못한다."""
    run(aws)

    assert aws.clients == [("ec2", REGION)]


def test_every_step_is_applied_and_summarized(aws):
    outcome = run(aws)

    assert [(s.sequence, s.step_type, s.status, s.effect) for s in outcome.steps] == [
        (1, ex.STEP_STOP_INSTANCE, S.SUCCESS, E.APPLIED),
        (2, ex.STEP_MODIFY_INSTANCE_TYPE, S.SUCCESS, E.APPLIED),
        (3, ex.STEP_START_INSTANCE, S.SUCCESS, E.APPLIED),
    ]
    assert all(step.affected_arn == INSTANCE_ARN for step in outcome.steps)
    assert all(step.result_summary for step in outcome.steps)


def test_each_call_is_recorded_before_and_after(recorded, aws):
    """호출 직전 IN_PROGRESS가 먼저 남아야 프로세스가 죽어도 진행 지점이 남는다."""
    run(aws, recorded)

    assert [(s.sequence, s.status) for s in recorded] == [
        (1, S.IN_PROGRESS), (1, S.SUCCESS),
        (2, S.IN_PROGRESS), (2, S.SUCCESS),
        (3, S.IN_PROGRESS), (3, S.SUCCESS),
    ]


def test_aws_request_id_is_carried_from_the_response(aws):
    outcome = run(aws)

    assert outcome.steps[0].aws_request_id == "req-stop"


def test_stop_waiter_is_bounded(aws):
    """대기는 무한하지 않다 — 초과는 상태 불명으로 끝난다."""
    run(aws)

    _, kwargs = next(call for call in aws.calls if call[0] == "wait")
    assert kwargs["InstanceIds"] == [INSTANCE]
    assert kwargs["WaiterConfig"] == {
        "Delay": ex.STOP_WAIT_DELAY_SECONDS,
        "MaxAttempts": ex.STOP_WAIT_MAX_ATTEMPTS,
    }


# ------------------------------------------------------------------ 조치 직전 상태


def test_instance_that_was_already_stopped_is_not_started(aws):
    """원래 멈춰 있던 인스턴스를 켜는 것은 이 런북이 요청받은 변경이 아니다."""
    aws(
        stop_instances={
            "StoppingInstances": [
                {"InstanceId": INSTANCE, "PreviousState": {"Name": "stopped"}}
            ]
        }
    )

    outcome = run(aws)

    assert outcome.succeeded
    assert "start_instances" not in operations(aws)
    assert (outcome.steps[2].status, outcome.steps[2].effect) == (S.SUCCESS, E.NOT_APPLIED)


def test_unknown_previous_state_starts_the_instance(aws):
    """상태를 읽지 못했을 때 켜야 할 것을 끈 채로 두는 편이 더 나쁘다."""
    aws(stop_instances={"StoppingInstances": []})

    outcome = run(aws)

    assert outcome.succeeded
    assert "start_instances" in operations(aws)


# ------------------------------------------------------------------ 실패 분기


def test_stop_rejection_changes_nothing(aws):
    """AWS가 거절한 호출은 자산을 바꾸지 않는다 — 되돌릴 것이 없다."""
    aws(stop_instances=client_error("IncorrectInstanceState"))

    outcome = run(aws)

    assert not outcome.succeeded
    assert outcome.reason_code is R.PRECHECK_INVALID_STATE
    assert [(s.sequence, s.status, s.effect) for s in outcome.steps] == [
        (1, S.FAILED, E.NOT_APPLIED)
    ]
    assert "modify_instance_attribute" not in operations(aws)


def test_stop_confirmation_timeout_leaves_the_state_unknown(aws):
    """정지 요청은 접수됐고 최종 상태만 모른다 — 그 상태로 타입 변경을 걸지 않는다."""
    aws(wait=waiter_error())

    outcome = run(aws)

    assert outcome.reason_code is R.PRECHECK_AWS_ERROR
    assert [(s.status, s.effect) for s in outcome.steps] == [(S.FAILED, E.UNKNOWN)]
    assert "modify_instance_attribute" not in operations(aws)


def test_modify_failure_leaves_the_instance_stopped(aws):
    """되돌리는 것은 REVERT_SIZE 몫이다 — 실행부가 자체 보상을 하지 않는다."""
    aws(modify_instance_attribute=client_error("InvalidParameterValue"))

    outcome = run(aws)

    assert not outcome.succeeded
    assert [(s.sequence, s.status, s.effect) for s in outcome.steps] == [
        (1, S.SUCCESS, E.APPLIED),
        (2, S.FAILED, E.NOT_APPLIED),
    ]
    assert "start_instances" not in operations(aws)


def test_start_failure_keeps_the_applied_type_change_visible(aws):
    """타입은 이미 바뀐 채 멈춰 있다 — 원복 판단이 그 사실을 읽을 수 있어야 한다."""
    aws(start_instances=client_error("InsufficientInstanceCapacity"))

    outcome = run(aws)

    assert not outcome.succeeded
    assert outcome.steps[1].effect is E.APPLIED
    assert (outcome.steps[2].status, outcome.steps[2].effect) == (S.FAILED, E.NOT_APPLIED)


def test_server_error_leaves_the_effect_unknown(aws):
    """5xx는 AWS가 작업을 시작했는지 알려 주지 않는다 — 변경 없음으로 단정하면
    이미 바뀐 자산이 기록에서 사라진다."""
    aws(modify_instance_attribute=client_error("InternalError", status=500))

    outcome = run(aws)

    assert not outcome.succeeded
    assert outcome.steps[-1].effect is E.UNKNOWN


def test_throttling_is_a_rejection_even_though_it_is_5xx(aws):
    """스로틀링은 작업 이전에 반려된 것이라 자산이 그대로다(EC2는 503으로 보낸다)."""
    aws(modify_instance_attribute=client_error("RequestLimitExceeded", status=503))

    outcome = run(aws)

    assert outcome.steps[-1].effect is E.NOT_APPLIED


def test_response_without_a_status_code_is_unknown(aws):
    """읽지 못한 것을 '변경 없음'으로 적지 않는다."""
    aws(modify_instance_attribute=client_error("SomethingOdd", status=None))

    outcome = run(aws)

    assert outcome.steps[-1].effect is E.UNKNOWN


def test_unreachable_endpoint_is_unknown_not_rejected(aws):
    """요청이 닿았는지 모르는 실패를 NOT_APPLIED로 적으면 바뀐 자산을 놓친다."""
    aws(modify_instance_attribute=EndpointConnectionError(endpoint_url="http://x"))

    outcome = run(aws)

    assert outcome.reason_code is R.PRECHECK_AWS_ERROR
    assert outcome.steps[-1].effect is E.UNKNOWN


def test_param_validation_error_is_a_rejection(aws):
    """botocore가 호출 이전에 거른 것이므로 자산은 그대로다."""
    aws(modify_instance_attribute=ParamValidationError(report="bad"))

    outcome = run(aws)

    assert outcome.reason_code is R.PRECHECK_PARAM_INVALID
    assert outcome.steps[-1].effect is E.NOT_APPLIED


def test_failed_step_records_the_reason(aws):
    aws(stop_instances=client_error("UnauthorizedOperation", message="not allowed"))

    outcome = run(aws)

    assert "UnauthorizedOperation" in outcome.steps[0].error_summary
    assert outcome.error_summary and outcome.reason_code is R.PRECHECK_UNAUTHORIZED


def test_failure_is_recorded_before_the_call_returns(recorded, aws):
    aws(stop_instances=client_error("UnauthorizedOperation"))

    run(aws, recorded)

    assert [(s.sequence, s.status) for s in recorded] == [(1, S.IN_PROGRESS), (1, S.FAILED)]


# ------------------------------------------------------------------ 진입 거절


@pytest.mark.parametrize(
    "target_arn",
    [
        f"arn:aws:ec2:{REGION}:{ACCOUNT}:volume/vol-0abc123456789def0",
        "i-0abc123456789def0",
        "",
    ],
)
def test_non_instance_target_never_reaches_aws(target_arn, aws):
    outcome = run(aws, target_arn=target_arn)

    assert outcome.reason_code is R.PRECHECK_PARAM_INVALID
    assert outcome.steps == () and aws.calls == []


@pytest.mark.parametrize("target_type", ["", "   "])
def test_empty_target_type_never_reaches_aws(target_type, aws):
    outcome = run(aws, target_type=target_type)

    assert outcome.reason_code is R.PRECHECK_PARAM_INVALID
    assert outcome.steps == () and aws.calls == []


def test_outcome_requires_reason_and_summary_together():
    """실패에 사유만 남고 설명이 없으면 관제자가 이유를 읽을 수 없다."""
    with pytest.raises(ValueError):
        ex.ExecutionOutcome(reason_code=R.PRECHECK_AWS_ERROR)
