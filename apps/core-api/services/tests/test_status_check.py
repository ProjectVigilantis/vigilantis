"""rollback.wait_for_status_check() 단위 테스트 (Issue #240).

AWS 불필요 — boto3 클라이언트를 가짜로 갈아 끼우고 **3분기 판정**을 본다.

판정이 곧 자동 원복의 방아쇠다. OK를 잘못 주면 부팅 실패한 자산이 그대로 남고,
FAILED·TIMED_OUT을 잘못 주면 멀쩡한 자산을 되돌린다. 그래서 waiter 실패를
"실패"와 "아직"으로 가르는 근거(인스턴스 상태·검사 결과)를 전수로 고정한다.
LocalStack 실물 검증은 test_execute_localstack.py 계열이 맡는다.
"""

import sys
from pathlib import Path

import pytest
from botocore.exceptions import (
    ClientError,
    EndpointConnectionError,
    WaiterError,
)

CORE_API = Path(__file__).resolve().parents[2]
REPO_ROOT = Path(__file__).resolve().parents[4]
for p in (str(CORE_API), str(REPO_ROOT / "packages")):
    if p not in sys.path:
        sys.path.insert(0, p)

from schemas.precheck import PrecheckReasonCode  # noqa: E402
from services.aws import rollback as rb  # noqa: E402

R = PrecheckReasonCode
V = rb.StatusCheckVerdict

ACCOUNT = "123456789012"
REGION = "ap-northeast-2"
INSTANCE = "i-0abc123456789def0"
INSTANCE_ARN = f"arn:aws:ec2:{REGION}:{ACCOUNT}:instance/{INSTANCE}"
VOLUME_ARN = f"arn:aws:ec2:{REGION}:{ACCOUNT}:volume/vol-0abc123456789def0"


def client_error(code: str, status: int = 400) -> ClientError:
    return ClientError(
        {
            "Error": {"Code": code, "Message": code},
            "ResponseMetadata": {"HTTPStatusCode": status},
        },
        "DescribeInstanceStatus",
    )


def waiter_error() -> WaiterError:
    """boto3가 MaxAttempts를 소진했을 때 내는 예외."""
    return WaiterError(
        name=rb.WAITER_NAME, reason="Max attempts exceeded", last_response={}
    )


def status_response(state: str, system: str, instance: str) -> dict:
    return {
        "InstanceStatuses": [
            {
                "InstanceId": INSTANCE,
                "InstanceState": {"Name": state},
                "SystemStatus": {"Status": system},
                "InstanceStatus": {"Status": instance},
            }
        ]
    }


class FakeWaiter:
    def __init__(self, error):
        self._error = error

    def wait(self, **kwargs):
        if self._error is not None:
            raise self._error


class FakeEc2:
    """waiter 결과와 뒤이은 describe 응답을 따로 주입한다."""

    def __init__(self, *, waiter=None, describe=None):
        self._waiter = waiter
        self._describe = describe
        self.calls: list[tuple[str, dict]] = []

    def get_waiter(self, name):
        assert name == rb.WAITER_NAME
        self.calls.append(("get_waiter", {"name": name}))
        return FakeWaiter(self._waiter)

    def describe_instance_status(self, **kwargs):
        self.calls.append(("describe_instance_status", kwargs))
        if isinstance(self._describe, BaseException):
            raise self._describe
        return self._describe if self._describe is not None else {}


@pytest.fixture
def ec2(monkeypatch):
    """가짜 EC2를 세우고 돌려준다 — 호출부는 waiter·describe만 정하면 된다."""
    holder: dict = {}

    def build(*, waiter=None, describe=None):
        client = FakeEc2(waiter=waiter, describe=describe)
        holder["client"] = client
        monkeypatch.setattr(rb, "aws_client", lambda service, region=None, **_: client)
        return client

    return build


def judge(**kwargs):
    """대기 설정을 인자로 넘겨 설정 캐시·실제 대기 없이 즉시 끝낸다."""
    return rb.wait_for_status_check(INSTANCE_ARN, delay_seconds=1, max_attempts=1)


# ------------------------------------------------------------------- 3분기


def test_two_of_two_passes(ec2):
    """waiter가 통과하면 OK — 추가 조회 없이 끝난다."""
    client = ec2(waiter=None)

    outcome = judge()

    assert outcome.verdict is V.OK and outcome.booted
    assert outcome.reason_code is None
    assert "describe_instance_status" not in [name for name, _ in client.calls]


def test_impaired_check_is_a_failure(ec2):
    """AWS가 이미 이상으로 판정했다 — 더 기다릴 이유가 없다."""
    ec2(
        waiter=waiter_error(),
        describe=status_response("running", "ok", "impaired"),
    )

    outcome = judge()

    assert outcome.verdict is V.FAILED and not outcome.booted
    assert outcome.instance_state == "running"


@pytest.mark.parametrize("state", ["stopped", "stopping", "shutting-down", "terminated"])
def test_instance_not_running_is_a_failure(ec2, state):
    """기동을 요청했는데 running 계열이 아니면 부팅에 실패한 것이다.

    이 경로가 곧 자동 원복 시연이다 — 타입 변경 뒤 인스턴스가 뜨지 못하면
    DescribeInstanceStatus는 기본 조회에서 **빈 응답**만 주므로, 여기서
    IncludeAllInstances로 다시 물어야 실패를 실패로 읽는다.
    """
    client = ec2(waiter=waiter_error(), describe=status_response(state, "ok", "ok"))

    outcome = judge()

    assert outcome.verdict is V.FAILED
    assert outcome.instance_state == state
    describe = dict(client.calls[-1][1])
    assert describe["IncludeAllInstances"] is True


def test_still_initializing_is_a_timeout(ec2):
    """아직 판정 전이다 — 실패로 접으면 부팅 중인 자산을 되돌리게 된다."""
    ec2(
        waiter=waiter_error(),
        describe=status_response("pending", "initializing", "initializing"),
    )

    outcome = judge()

    assert outcome.verdict is V.TIMED_OUT
    assert outcome.reason_code is None  # 상태는 확인했다 — AWS 오류가 아니다


def test_empty_status_response_is_a_failure(ec2):
    """IncludeAllInstances로 물었는데도 없으면 대상 자체가 없는 것이다."""
    ec2(waiter=waiter_error(), describe={"InstanceStatuses": []})

    outcome = judge()

    assert outcome.verdict is V.FAILED
    assert outcome.reason_code is R.PRECHECK_TARGET_NOT_FOUND


# ------------------------------------------------------------- AWS 오류 분류


def test_probe_error_defers_instead_of_failing(ec2):
    """상태를 못 물어본 것은 부팅 실패가 아니다 — 사유 코드를 실어 보류로 남긴다.

    조회 실패를 FAILED로 접으면 일시적인 권한·네트워크 문제가 멀쩡한 인스턴스의
    자동 원복을 부른다. reason_code가 채워진 TIMED_OUT이 그 구분이다.
    """
    ec2(waiter=waiter_error(), describe=client_error("UnauthorizedOperation", 403))

    outcome = judge()

    assert outcome.verdict is V.TIMED_OUT
    assert outcome.reason_code is R.PRECHECK_UNAUTHORIZED


def test_missing_instance_probe_is_a_failure(ec2):
    """인스턴스가 없으면 2/2는 영원히 오지 않는다 — 보류가 아니라 실패다."""
    ec2(waiter=waiter_error(), describe=client_error("InvalidInstanceID.NotFound", 400))

    outcome = judge()

    assert outcome.verdict is V.FAILED
    assert outcome.reason_code is R.PRECHECK_TARGET_NOT_FOUND


def test_waiter_call_error_defers(ec2):
    """waiter 호출 자체가 끊기면 자산 상태를 본 적이 없다 — 실패로 확정하지 않는다."""
    ec2(waiter=EndpointConnectionError(endpoint_url="http://localstack:4566"))

    outcome = judge()

    assert outcome.verdict is V.TIMED_OUT
    assert outcome.reason_code is R.PRECHECK_AWS_ERROR


@pytest.mark.parametrize(
    "error",
    [
        client_error("RequestLimitExceeded", 503),
        client_error("InvalidParameterValue"),
        EndpointConnectionError(endpoint_url="http://localstack:4566"),
    ],
)
def test_no_aws_error_escapes(ec2, error):
    """어떤 AWS 오류에도 예외를 던지지 않는다 — 판정 1건이 스캔 전체를 멈추면 안 된다."""
    ec2(waiter=waiter_error(), describe=error)

    outcome = judge()

    assert outcome.verdict in (V.FAILED, V.TIMED_OUT)


# --------------------------------------------------------------- 배선 오류


def test_non_instance_arn_raises(ec2):
    """자산 상태에 대한 판정이 아니라 호출부 배선 오류다 — 삼키면 멀쩡한 실행에
    '기동 실패' 기록이 붙고 그 기록이 자동 원복의 입력이 된다."""
    ec2(waiter=None)

    with pytest.raises(ValueError, match="인스턴스 ARN이 아닙니다"):
        rb.wait_for_status_check(VOLUME_ARN, delay_seconds=1, max_attempts=1)


# ------------------------------------------------------------------ 대기 설정


def test_wait_config_comes_from_settings_when_not_given(ec2, monkeypatch):
    """시연에서 조여야 할 값이라 코드 상수로 굳히지 않는다 — 설정이 원천이다."""
    captured: dict = {}

    class CapturingWaiter:
        def wait(self, **kwargs):
            captured.update(kwargs["WaiterConfig"])

    class CapturingEc2:
        def get_waiter(self, name):
            return CapturingWaiter()

    monkeypatch.setattr(rb, "aws_client", lambda *a, **k: CapturingEc2())
    monkeypatch.setattr(
        rb,
        "get_settings",
        lambda: type(
            "S",
            (),
            {"STATUS_CHECK_WAIT_DELAY_SECONDS": 7, "STATUS_CHECK_WAIT_MAX_ATTEMPTS": 3},
        )(),
    )

    rb.wait_for_status_check(INSTANCE_ARN)

    assert captured == {"Delay": 7, "MaxAttempts": 3}
