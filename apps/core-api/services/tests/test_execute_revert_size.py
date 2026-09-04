"""executor.execute_revert_size() 단위 테스트 (Issue #241, ADR-0008 §3-2·§6).

AWS 불필요 — boto3 클라이언트를 가짜로 갈아 끼우고 **상태 대조 3분기와 단계 기록**을
본다. 이 함수가 잘못 판정하면 나타나는 사고는 둘 다 조용하다: ③을 놓치면 자동 원복이
제3자의 변경을 덮어쓰고, ①을 놓치면 할 일이 없는 실행이 CRITICAL로 사람을 부른다.

LocalStack 실물 검증(타입이 실제로 백업 값으로 돌아가는가)은
test_execute_revert_localstack.py가 맡는다.
"""

import sys
from pathlib import Path

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError, WaiterError

CORE_API = Path(__file__).resolve().parents[2]
REPO_ROOT = Path(__file__).resolve().parents[4]
for p in (str(CORE_API), str(REPO_ROOT / "packages")):
    if p not in sys.path:
        sys.path.insert(0, p)

from schemas.executions import ExecutionEffect, ExecutionStepStatus  # noqa: E402
from schemas.precheck import PrecheckReasonCode  # noqa: E402
from services.aws import executor as ex  # noqa: E402

R = PrecheckReasonCode
S = ExecutionStepStatus
E = ExecutionEffect

ACCOUNT = "123456789012"
REGION = "ap-northeast-2"
INSTANCE = "i-0abc123456789def0"
INSTANCE_ARN = f"arn:aws:ec2:{REGION}:{ACCOUNT}:instance/{INSTANCE}"

BACKUP_TYPE = "t3.xlarge"   # 조치 이전 = 되돌릴 값
APPLIED_TYPE = "t3.medium"  # 원본 RIGHTSIZING이 적용한 값

STOP_RESPONSE = {
    "StoppingInstances": [{"InstanceId": INSTANCE, "PreviousState": {"Name": "stopped"}}],
    "ResponseMetadata": {"RequestId": "req-stop"},
}


def client_error(code: str, status: int | None = 400) -> ClientError:
    metadata: dict = {"RequestId": "req-err"}
    if status is not None:
        metadata["HTTPStatusCode"] = status
    return ClientError(
        {"Error": {"Code": code, "Message": code}, "ResponseMetadata": metadata}, "Op"
    )


def describe(instance_type: str) -> dict:
    return {
        "Reservations": [
            {"Instances": [{"InstanceId": INSTANCE, "InstanceType": instance_type}]}
        ]
    }


class FakeWaiter:
    def __init__(self, state):
        self._state = state

    def wait(self, **kwargs):
        self._state["calls"].append(("wait", kwargs))
        outcome = self._state["overrides"].get("wait")
        if isinstance(outcome, BaseException):
            raise outcome


class FakeEc2:
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
            if operation == "describe_instances":
                return describe(self._state["current_type"])
            if operation == "stop_instances":
                return STOP_RESPONSE
            return {}

        return call


@pytest.fixture
def aws(monkeypatch):
    """기본은 §3-2 ② 경로(현재 타입 = 원본이 적용한 값)다."""
    state = {"overrides": {}, "calls": [], "current_type": APPLIED_TYPE}

    def factory(service, region=None, **_):
        return FakeEc2(state)

    monkeypatch.setattr(ex, "aws_client", factory)

    def configure(current_type=None, **overrides):
        if current_type is not None:
            state["current_type"] = current_type
        state["overrides"].update(overrides)

    configure.calls = state["calls"]
    return configure


def run(recorded=None, *, target_arn=INSTANCE_ARN, restore_state="running"):
    return ex.execute_revert_size(
        target_arn,
        restore_instance_type=BACKUP_TYPE,
        applied_instance_type=APPLIED_TYPE,
        restore_state=restore_state,
        record_step=None if recorded is None else recorded.append,
    )


def operations(aws):
    return [name for name, _ in aws.calls]


def settled(recorded):
    """IN_PROGRESS 기록을 뺀 확정 단계만 — (순서, 유형, 상태, effect)."""
    return [
        (s.sequence, s.step_type, s.status, s.effect)
        for s in recorded
        if s.status is not S.IN_PROGRESS
    ]


# ------------------------------------------------------------ §3-2 상태 대조 3분기


def test_already_restored_makes_no_aws_change(aws):
    """① 현재 타입이 백업 값이면 되돌릴 것이 없다 — 변경 호출을 하지 않는다."""
    aws(current_type=BACKUP_TYPE)
    recorded = []

    outcome = run(recorded)

    assert outcome.succeeded and not outcome.deferred
    assert operations(aws) == ["describe_instances"]
    assert settled(recorded) == [
        (1, ex.STEP_COMPARE_INSTANCE_TYPE, S.SUCCESS, E.NOT_APPLIED)
    ]


def test_already_restored_wins_over_as_applied(aws):
    """원본이 같은 타입으로 '변경'했으면 ①과 ②가 동시에 참인데 ①이 이긴다.

    ②로 가면 할 일이 없는 실행이 정지·기동까지 하고, 사전 거절로 처리하면 아무 일도
    없었다는 사실이 CRITICAL로 올라가 사람을 부른다 (ADR-0008 §3-2).
    """
    aws(current_type=BACKUP_TYPE)

    outcome = ex.execute_revert_size(
        INSTANCE_ARN,
        restore_instance_type=BACKUP_TYPE,
        applied_instance_type=BACKUP_TYPE,
        restore_state="running",
    )

    assert outcome.succeeded
    assert "stop_instances" not in operations(aws)


def test_as_applied_proceeds_and_does_not_record_the_comparison(aws):
    """② 우리가 바꾼 그대로면 진행한다. 대조 단계는 남기지 않는다.

    남기면 "단계 1건 이상 = 자산이 바뀌었을 수 있다"는 회수 규약(ADR-0008 §7)이
    거짓이 되어, 아무것도 안 바꾼 실행이 재실행 대신 종료 판정으로 간다.
    """
    recorded = []

    outcome = run(recorded)

    assert outcome.succeeded
    assert settled(recorded) == [
        (1, ex.STEP_STOP_INSTANCE, S.SUCCESS, E.APPLIED),
        (2, ex.STEP_MODIFY_INSTANCE_TYPE, S.SUCCESS, E.APPLIED),
        (3, ex.STEP_START_INSTANCE, S.SUCCESS, E.APPLIED),
    ]
    assert ex.STEP_COMPARE_INSTANCE_TYPE not in [s.step_type for s in recorded]


def test_third_party_drift_stops_before_any_change(aws):
    """③ 둘 다 아니면 제3자가 바꾼 것이다 — 덮어쓰지 않고 중단한다."""
    aws(current_type="c5.large")
    recorded = []

    outcome = run(recorded)

    assert not outcome.succeeded and not outcome.deferred
    assert outcome.reason_code is R.PRECHECK_INVALID_STATE
    assert "c5.large" in outcome.error_summary
    assert operations(aws) == ["describe_instances"]
    assert settled(recorded) == [
        (1, ex.STEP_COMPARE_INSTANCE_TYPE, S.SUCCESS, E.NOT_APPLIED)
    ]


def test_third_party_drift_is_logged_as_critical(aws, caplog):
    aws(current_type="c5.large")

    with caplog.at_level("CRITICAL", logger="vigilantis.aws"):
        run()

    assert "revert_size_third_party_drift" in caplog.text


# ------------------------------------------------------------------ 대조 불가


def test_probe_failure_defers_instead_of_failing(aws):
    """대조를 못 한 것은 원복이 실패했다는 근거가 아니다 — 보류다.

    실패로 확정하면 되돌릴 것이 그대로 남은 자산에 "원복 실패"가 기록되고, 자식이
    종료 상태로 닫혀 다시 시도할 자리가 사라진다.
    """
    aws(describe_instances=EndpointConnectionError(endpoint_url="https://ec2"))

    outcome = run()

    assert outcome.deferred and not outcome.succeeded
    assert outcome.steps == ()
    assert "stop_instances" not in operations(aws)


def test_missing_instance_is_a_verdict_not_a_deferral(aws):
    """인스턴스가 없으면 다시 물어도 답이 같다 — 보류하지 않고 확정한다."""
    aws(describe_instances=client_error("InvalidInstanceID.NotFound"))

    outcome = run()

    assert not outcome.deferred
    assert outcome.reason_code is R.PRECHECK_TARGET_NOT_FOUND


def test_deferred_outcome_cannot_carry_steps(aws):
    """자산을 만졌으면 보류가 아니다 — 되돌릴 것이 남은 실패다."""
    aws(current_type=BACKUP_TYPE)
    step = run([]).steps[0]

    with pytest.raises(ValueError):
        ex.ExecutionOutcome(
            steps=(step,),
            reason_code=R.PRECHECK_AWS_ERROR,
            error_summary="조회 실패",
            deferred=True,
        )


def test_deferred_outcome_needs_a_reason_code():
    """보류도 사유별로 모여야 한다 — 재시도 정책(#249)이 읽을 축이다."""
    with pytest.raises(ValueError):
        ex.ExecutionOutcome(deferred=True)


# ------------------------------------------------------------------ 단계 분기


def test_restore_state_decides_whether_to_start(aws):
    """다시 켤지는 백업 레코드의 state가 정한다(ADR-0008 §4).

    원본 실행이 읽은 PreviousState가 아니다 — 그 값은 지금 stopped라고 말하는데,
    조치 이전에 running이었으면 원복은 켜야 한다.
    """
    recorded = []

    outcome = run(recorded, restore_state="stopped")

    assert outcome.succeeded
    assert "start_instances" not in operations(aws)
    assert settled(recorded)[-1] == (
        3,
        ex.STEP_START_INSTANCE,
        S.SUCCESS,
        E.NOT_APPLIED,
    )


def test_stop_failure_keeps_the_trace_and_stops(aws):
    aws(stop_instances=client_error("IncorrectInstanceState"))
    recorded = []

    outcome = run(recorded)

    assert not outcome.succeeded
    assert outcome.reason_code is not None
    assert settled(recorded) == [(1, ex.STEP_STOP_INSTANCE, S.FAILED, E.NOT_APPLIED)]
    assert "modify_instance_attribute" not in operations(aws)


def test_stop_wait_failure_is_unknown_effect(aws):
    """정지 요청은 접수됐고 최종 상태만 확인하지 못했다 — 바뀌었을 수 있다."""
    aws(wait=WaiterError(name="instance_stopped", reason="timeout", last_response={}))
    recorded = []

    run(recorded)

    assert settled(recorded) == [(1, ex.STEP_STOP_INSTANCE, S.FAILED, E.UNKNOWN)]


def test_modify_failure_leaves_the_instance_stopped(aws):
    """타입 원복이 실패하면 멈춘 채로 남는다 — 원복의 원복은 없다(ADR-0008 §6)."""
    aws(modify_instance_attribute=client_error("InvalidParameterValue"))
    recorded = []

    outcome = run(recorded)

    assert not outcome.succeeded
    assert settled(recorded) == [
        (1, ex.STEP_STOP_INSTANCE, S.SUCCESS, E.APPLIED),
        (2, ex.STEP_MODIFY_INSTANCE_TYPE, S.FAILED, E.NOT_APPLIED),
    ]
    assert "start_instances" not in operations(aws)


def test_modify_uses_the_backup_type_not_the_applied_type(aws):
    run()

    modify = next(
        kwargs
        for name, kwargs in aws.calls
        if name == "modify_instance_attribute"
    )
    assert modify["InstanceType"] == {"Value": BACKUP_TYPE}


# ------------------------------------------------------------------ 배선 거절


@pytest.mark.parametrize(
    "target_arn",
    [
        "arn:aws:ec2:ap-northeast-2:123456789012:security-group/sg-1",
        "not-an-arn",
    ],
)
def test_non_instance_arn_is_rejected_without_calling_aws(aws, target_arn):
    outcome = run(target_arn=target_arn)

    assert not outcome.succeeded and outcome.steps == ()
    assert aws.calls == []


def test_empty_applied_type_is_rejected_without_calling_aws(aws):
    """대조 축이 없으면 ②와 ③을 가를 수 없다 — 모르는 채로 되돌리지 않는다."""
    outcome = ex.execute_revert_size(
        INSTANCE_ARN,
        restore_instance_type=BACKUP_TYPE,
        applied_instance_type="",
        restore_state="running",
    )

    assert not outcome.succeeded and aws.calls == []
