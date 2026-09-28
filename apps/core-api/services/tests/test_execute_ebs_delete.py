"""executor.execute_ebs_delete_unattached() 단위 테스트 (Issue #369).

AWS 불필요 — boto3 클라이언트를 가짜로 갈아 끼우고 **호출 순서와 단계 effect**를 본다.

이 런북은 되돌릴 수 없다. 등록 롤백이 없고 백업 레코드 4종에도 EBS가 없어(ADR-0008 §5),
데이터를 지키는 장치는 삭제 직전의 스냅숏 하나뿐이다. 그래서 여기서 굳히는 것은 셋이다.

  - **스냅숏이 완료되기 전에는 delete_volume이 나가지 않는다.** 진행 중 스냅숏으로 지워도
    되는지를 AWS 동작에 기대지 않는다.
  - **판정 이후 붙은 볼륨은 스냅숏도 만들지 않고 거절한다.** 가드레일 ④는 후보 생성
    시점에 1회 돌고, 관제자가 누르기까지 그 사이가 있다.
  - **스냅숏 ID가 단계 요약에 남는다.** 볼륨을 되살릴 유일한 근거다.

LocalStack 실물 검증은 test_execute_ebs_localstack.py가 맡는다.
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
VOLUME = "vol-0abc123456789def0"
VOLUME_ARN = f"arn:aws:ec2:{REGION}:{ACCOUNT}:volume/{VOLUME}"
SNAPSHOT = "snap-0abc123456789def0"

UNATTACHED = {"Volumes": [{"VolumeId": VOLUME, "State": "available", "Attachments": []}]}
SNAPSHOT_RESPONSE = {
    "SnapshotId": SNAPSHOT,
    "State": "pending",
    "ResponseMetadata": {"RequestId": "req-snap"},
}
DELETE_RESPONSE = {"ResponseMetadata": {"RequestId": "req-delete"}}


def client_error(code: str, status: int = 400) -> ClientError:
    return ClientError(
        {
            "Error": {"Code": code, "Message": code},
            "ResponseMetadata": {"RequestId": "req-err", "HTTPStatusCode": status},
        },
        "Op",
    )


def waiter_error() -> WaiterError:
    """상한을 넘겨 끝난 대기 — BotoCoreError 계열이라 effect가 UNKNOWN이 된다."""
    return WaiterError(
        name="snapshot_completed",
        reason="Max attempts exceeded",
        last_response={"Snapshots": [{"SnapshotId": SNAPSHOT, "State": "pending"}]},
    )


class FakeWaiter:
    def __init__(self, state, name):
        self._state = state
        self._name = name

    def wait(self, **kwargs):
        key = f"waiter:{self._name}"
        self._state["calls"].append((key, kwargs))
        outcome = self._state["overrides"].get(key)
        if isinstance(outcome, BaseException):
            raise outcome
        return None


class FakeEc2:
    _DEFAULTS = {
        "describe_volumes": UNATTACHED,
        "create_snapshot": SNAPSHOT_RESPONSE,
        "delete_volume": DELETE_RESPONSE,
    }

    def __init__(self, state):
        self._state = state

    def get_waiter(self, name):
        return FakeWaiter(self._state, name)

    def __getattr__(self, operation):
        def call(**kwargs):
            self._state["calls"].append((operation, kwargs))
            outcome = self._state["overrides"].get(operation)
            if isinstance(outcome, BaseException):
                raise outcome
            if outcome is not None:
                return outcome
            return self._DEFAULTS.get(operation, {})

        return call


@pytest.fixture
def aws(monkeypatch):
    """기본은 삭제 성공 경로. configure(...)로 실패시킬 호출만 바꾼다."""
    state = {"overrides": {}, "calls": []}

    monkeypatch.setattr(ex, "aws_client", lambda service, region=None, **_: FakeEc2(state))

    def configure(**overrides):
        state["overrides"].update(overrides)

    configure.calls = state["calls"]
    return configure


def run(recorded=None, *, target_arn=VOLUME_ARN):
    return ex.execute_ebs_delete_unattached(
        target_arn, record_step=None if recorded is None else recorded.append
    )


def operations(aws) -> list[str]:
    return [name for name, _ in aws.calls]


# ------------------------------------------------------------------ 성공 경로


def test_deletes_after_a_completed_snapshot(aws):
    outcome = run()

    assert outcome.succeeded
    # 순서가 계약이다 — 상태 재확인 → 스냅숏 → 완료 확인 → 삭제
    assert operations(aws) == [
        "describe_volumes",
        "create_snapshot",
        "waiter:snapshot_completed",
        "delete_volume",
    ]
    assert aws.calls[1][1]["VolumeId"] == VOLUME
    assert aws.calls[2][1]["SnapshotIds"] == [SNAPSHOT]
    assert aws.calls[3][1]["VolumeId"] == VOLUME


def test_records_three_steps_and_no_compare_step_on_the_happy_path(aws):
    """진행하는 대조는 기록하지 않는다(STEP_COMPARE_VOLUME_STATE 주석).

    기록하면 "단계 1건 이상 = 자산이 바뀌었을 수 있다"는 회수 규약(ADR-0008 §7)이
    거짓이 되어, 스냅숏도 만들기 전에 끊긴 실행이 재실행 대신 종료 판정으로 간다.
    """
    outcome = run()

    assert [step.step_type for step in outcome.steps] == [
        ex.STEP_CREATE_SNAPSHOT,
        ex.STEP_WAIT_SNAPSHOT,
        ex.STEP_DELETE_VOLUME,
    ]
    assert [step.sequence for step in outcome.steps] == [2, 3, 4]
    assert all(step.effect is E.APPLIED for step in outcome.steps)


def test_keeps_the_snapshot_id_in_the_step_summary(aws):
    """볼륨을 되살릴 유일한 근거다 — 원본 볼륨 ID와 함께 남는다."""
    outcome = run()

    snapshot_step = outcome.steps[0]
    assert SNAPSHOT in snapshot_step.result_summary
    assert VOLUME in snapshot_step.result_summary
    # 삭제 단계에도 남겨 실행 상세만 보고 복구 근거를 찾을 수 있게 한다
    assert SNAPSHOT in outcome.steps[-1].result_summary


def test_reports_every_step_to_the_recorder(aws):
    recorded = []
    run(recorded)

    # 단계마다 IN_PROGRESS 1건 + 종료 1건
    assert [step.status for step in recorded] == [
        S.IN_PROGRESS, S.SUCCESS,
        S.IN_PROGRESS, S.SUCCESS,
        S.IN_PROGRESS, S.SUCCESS,
    ]


# -------------------------------------------------- 상태 재확인(①)이 막는 것


def test_does_not_snapshot_or_delete_a_volume_that_got_attached(aws):
    """판정 이후 누군가 붙였다 — 스냅숏도 만들지 않는다(DoD 2)."""
    aws(
        describe_volumes={
            "Volumes": [
                {
                    "VolumeId": VOLUME,
                    "State": "in-use",
                    "Attachments": [{"InstanceId": "i-0123456789abcdef0"}],
                }
            ]
        }
    )

    outcome = run()

    assert not outcome.succeeded
    assert outcome.reason_code is R.PRECHECK_INVALID_STATE
    assert operations(aws) == ["describe_volumes"]
    # 대조 자체가 결론인 경우라 단계를 남긴다 — 자산은 바뀌지 않았다
    assert [(s.step_type, s.effect) for s in outcome.steps] == [
        (ex.STEP_COMPARE_VOLUME_STATE, E.NOT_APPLIED)
    ]


def test_rejects_an_available_volume_that_still_has_an_attachment(aws):
    """State만 보지 않는다 — 부착 목록이 비어야 삭제 조건이 성립한다."""
    aws(
        describe_volumes={
            "Volumes": [
                {
                    "VolumeId": VOLUME,
                    "State": "available",
                    "Attachments": [{"InstanceId": "i-0123456789abcdef0"}],
                }
            ]
        }
    )

    outcome = run()

    assert outcome.reason_code is R.PRECHECK_INVALID_STATE
    assert "create_snapshot" not in operations(aws)


def test_succeeds_when_the_volume_is_already_gone(aws):
    """이미 없다 — 지울 것이 없고 다시 물어도 답이 같다(execute_nacl_restore ①과 같은 결)."""
    aws(describe_volumes=client_error("InvalidVolume.NotFound", 400))

    outcome = run()

    assert outcome.succeeded
    assert operations(aws) == ["describe_volumes"]
    assert outcome.steps[0].step_type == ex.STEP_COMPARE_VOLUME_STATE
    assert outcome.steps[0].effect is E.NOT_APPLIED
    assert "스냅숏 없이" in outcome.steps[0].result_summary


def test_defers_when_the_lookup_itself_fails(aws):
    """AWS에 닿지 못했다 — 자산을 만지지 않았으므로 실패가 아니라 보류다(Issue #249)."""
    aws(describe_volumes=EndpointConnectionError(endpoint_url="https://ec2"))

    outcome = run()

    assert outcome.deferred
    assert outcome.reason_code is R.PRECHECK_AWS_ERROR
    assert outcome.steps == ()


def test_rejects_an_arn_that_is_not_a_volume(aws):
    outcome = run(target_arn=f"arn:aws:ec2:{REGION}:{ACCOUNT}:instance/i-0123456789abcdef0")

    assert outcome.reason_code is R.PRECHECK_PARAM_INVALID
    assert operations(aws) == []


# ------------------------------------------- 스냅숏(②·③)이 성립해야 삭제로 간다


def test_does_not_delete_when_the_snapshot_call_is_rejected(aws):
    aws(create_snapshot=client_error("SnapshotCreationPerVolumeRateExceeded", 400))

    outcome = run()

    assert not outcome.succeeded
    assert "delete_volume" not in operations(aws)
    assert outcome.steps[-1].status is S.FAILED
    assert outcome.steps[-1].effect is E.NOT_APPLIED


def test_does_not_delete_when_the_snapshot_never_completes(aws):
    """대기 상한을 넘겼다 — 진행 중 스냅숏으로 지우지 않는다(DoD 3)."""
    aws(**{"waiter:snapshot_completed": waiter_error()})

    outcome = run()

    assert not outcome.succeeded
    assert operations(aws) == [
        "describe_volumes",
        "create_snapshot",
        "waiter:snapshot_completed",
    ]
    assert "delete_volume" not in operations(aws)
    wait_step = outcome.steps[-1]
    assert wait_step.step_type == ex.STEP_WAIT_SNAPSHOT
    assert wait_step.status is S.FAILED
    # 스냅숏이 끝났는지 모르는 상태다 — 볼륨은 그대로라 판정이 현물을 본다
    assert wait_step.effect is E.UNKNOWN
    # 스냅숏 ID는 남는다 — 완료를 사람이 확인하고 이어 갈 수 있어야 한다
    assert SNAPSHOT in (wait_step.error_summary or "")


def test_does_not_delete_when_the_snapshot_response_has_no_id(aws):
    """ID가 없으면 완료를 확인할 방법도, 되살릴 근거를 남길 방법도 없다."""
    aws(create_snapshot={"ResponseMetadata": {"RequestId": "req-snap"}})

    outcome = run()

    assert not outcome.succeeded
    assert outcome.reason_code is R.PRECHECK_AWS_ERROR
    assert "delete_volume" not in operations(aws)
    assert "waiter:snapshot_completed" not in operations(aws)
    # 스냅숏이 만들어졌는지조차 모른다 — NOT_APPLIED로 적으면 남은 스냅숏이 기록에서 사라진다
    assert outcome.steps[-1].status is S.FAILED
    assert outcome.steps[-1].effect is E.UNKNOWN


def test_marks_a_racing_delete_failure_as_not_applied(aws):
    """대조와 삭제 사이에 붙었으면 VolumeInUse(4xx)다 — 되돌릴 것 없는 실패."""
    aws(delete_volume=client_error("VolumeInUse", 400))

    outcome = run()

    assert not outcome.succeeded
    assert outcome.steps[-1].step_type == ex.STEP_DELETE_VOLUME
    assert outcome.steps[-1].effect is E.NOT_APPLIED


def test_marks_an_interrupted_delete_as_unknown(aws):
    """5xx·연결 실패는 적용 여부를 알 수 없다 — 현물 판정으로 간다."""
    aws(delete_volume=client_error("InternalError", 500))

    outcome = run()

    assert not outcome.succeeded
    assert outcome.steps[-1].effect is E.UNKNOWN


# ------------------------------------------------------------------ 대기 상수


def test_waits_with_the_documented_bound(aws):
    """상한을 바꾸면 여기서 걸린다 — dispatcher 한 주기를 붙잡는 값이다."""
    run()

    config = aws.calls[2][1]["WaiterConfig"]
    assert config["Delay"] == ex.SNAPSHOT_WAIT_DELAY_SECONDS
    assert config["MaxAttempts"] == ex.SNAPSHOT_WAIT_MAX_ATTEMPTS
    # 정지 대기(200초)보다 짧아야 한다 — 삭제는 미뤄도 손해가 없는 조치다
    assert (
        ex.SNAPSHOT_WAIT_DELAY_SECONDS * ex.SNAPSHOT_WAIT_MAX_ATTEMPTS
        < ex.STOP_WAIT_DELAY_SECONDS * ex.STOP_WAIT_MAX_ATTEMPTS
    )
