"""rollback.wait_for_status_check() 단위 테스트 (Issue #240).

AWS 불필요 — boto3 클라이언트를 가짜로 갈아 끼우고 **3분기 판정**을 본다.

판정이 곧 자동 원복의 방아쇠다. OK를 잘못 주면 부팅 실패한 자산이 그대로 남고,
FAILED·TIMED_OUT을 잘못 주면 멀쩡한 자산을 되돌린다. 그래서 waiter 실패를
"실패"와 "아직"으로 가르는 근거(인스턴스 상태·검사 결과)를 전수로 고정한다.
LocalStack 실물 검증은 test_execute_localstack.py 계열이 맡는다.
"""

import sys
from pathlib import Path

import boto3
import pytest
from botocore.config import Config
from botocore.exceptions import (
    ClientError,
    EndpointConnectionError,
    WaiterError,
)
from botocore.stub import Stubber

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


def waiter_interrupted(code: str = "RequestLimitExceeded", status: int = 503) -> WaiterError:
    """AWS 오류 응답이 waiter를 끊었을 때 boto3가 내는 예외 — 대기 시간을 다 쓴 것이 아니다.

    boto3 waiter는 acceptor에 없는 오류 응답을 받으면 MaxAttempts와 무관하게 그 자리에서
    멈추고, 그 응답을 last_response에 싣는다(botocore.waiter.Waiter.wait).
    """
    return WaiterError(
        name=rb.WAITER_NAME,
        reason=f"An error occurred ({code}): {code}",
        last_response={
            "Error": {"Code": code, "Message": code},
            "ResponseMetadata": {"HTTPStatusCode": status},
        },
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
    assert outcome.probe_failed is False  # 관측된 타임아웃 — 원복 대상이다


def test_empty_status_response_is_a_failure(ec2):
    """IncludeAllInstances로 물었는데도 없으면 대상 자체가 없는 것이다."""
    ec2(waiter=waiter_error(), describe={"InstanceStatuses": []})

    outcome = judge()

    assert outcome.verdict is V.FAILED
    assert outcome.reason_code is R.PRECHECK_TARGET_NOT_FOUND


def test_healthy_probe_after_the_last_attempt_is_ok(ec2):
    """마지막 시도 뒤에 2/2가 왔다 — 재조회가 정상을 보면 되돌릴 근거가 없다."""
    ec2(waiter=waiter_error(), describe=status_response("running", "ok", "ok"))

    outcome = judge()

    assert outcome.verdict is V.OK and outcome.booted
    assert outcome.probe_failed is False


def test_half_passed_checks_are_not_two_of_two(ec2):
    """인스턴스 검사만 ok면 2/2가 아니다 — 성공으로 올리지 않는다."""
    ec2(waiter=waiter_error(), describe=status_response("running", "initializing", "ok"))

    outcome = judge()

    assert outcome.verdict is V.TIMED_OUT and not outcome.booted


# ------------------------------------------- 끊긴 waiter ≠ 소진된 waiter (PR #341 리뷰)


def test_real_waiter_cut_by_throttling_reads_a_healthy_instance_as_ok(monkeypatch):
    """스로틀링이 waiter를 첫 시도에서 끊어도 재조회가 running·ok/ok면 부팅은 성공이다.

    실제 botocore waiter로 재현한다 — 끊긴 waiter가 어떤 예외를 내는지가 이 판정의 입력이라
    가짜 waiter로는 그 모양을 보증할 수 없다. 전에는 이 경로가 '제한 시간 소진'으로 읽혀
    ROLLBACK_INITIATED — 자동 원복의 입력 — 이 됐고, 재시도·보류 정책(Issue #249)을 거치지
    않은 채 첫 조회 오류에서 원복이 시작됐다.
    """
    client = boto3.client(
        "ec2",
        region_name=REGION,
        aws_access_key_id="testing",
        aws_secret_access_key="testing",
        # SDK 재시도를 끈다 — 스로틀링 응답이 곧바로 waiter에 닿아야 끊김이 재현된다
        config=Config(retries={"mode": "standard", "total_max_attempts": 1}),
    )
    stubber = Stubber(client)
    stubber.add_client_error(
        "describe_instance_status",
        service_error_code="RequestLimitExceeded",
        service_message="Request limit exceeded.",
        http_status_code=503,
        expected_params={"InstanceIds": [INSTANCE]},
    )
    stubber.add_response(
        "describe_instance_status",
        status_response("running", "ok", "ok"),
        expected_params={"InstanceIds": [INSTANCE], "IncludeAllInstances": True},
    )
    monkeypatch.setattr(rb, "aws_client", lambda service, region=None, **_: client)

    with stubber:
        outcome = judge()
        stubber.assert_no_pending_responses()

    assert outcome.verdict is V.OK and outcome.booted
    assert outcome.probe_failed is False


def test_interrupted_waiter_with_a_booting_instance_defers(ec2):
    """끊긴 waiter의 '아직'은 제한 시간을 다 기다린 관측이 아니다 — 판정 불가로 보류한다.

    기다린 시간이 0초일 수도 있다. 여기서 타임아웃으로 확정하면 부팅 중인 자산을
    되돌린다. 사유 코드는 waiter를 끊은 오류의 것이다 — 재시도 여부가 그 코드로 갈린다.
    """
    ec2(
        waiter=waiter_interrupted(),
        describe=status_response("pending", "initializing", "initializing"),
    )

    outcome = judge()

    assert outcome.verdict is V.TIMED_OUT
    assert outcome.probe_failed is True  # 판정 불가 — 자동 원복 입력이 아니다
    assert outcome.reason_code is R.PRECHECK_AWS_ERROR  # 다시 물을 가치가 있다
    assert outcome.instance_state == "pending"


def test_interrupted_waiter_carries_the_code_that_cut_it(ec2):
    """권한 거부로 끊겼으면 그 사유다 — 재시도로 붙잡지 않고 첫 실패에서 사람에게 넘긴다."""
    ec2(
        waiter=waiter_interrupted("UnauthorizedOperation", 403),
        describe=status_response("running", "initializing", "initializing"),
    )

    outcome = judge()

    assert outcome.probe_failed is True
    assert outcome.reason_code is R.PRECHECK_UNAUTHORIZED


@pytest.mark.parametrize(
    "state, system, instance",
    [("stopped", "not-applicable", "not-applicable"), ("running", "ok", "impaired")],
)
def test_interrupted_waiter_still_reads_a_real_failure(ec2, state, system, instance):
    """끊겼어도 재조회가 확정적인 실패를 보면 실패다 — 관측한 그대로 자동 원복의 입력이다."""
    ec2(waiter=waiter_interrupted(), describe=status_response(state, system, instance))

    outcome = judge()

    assert outcome.verdict is V.FAILED
    assert outcome.probe_failed is False


def test_not_found_left_by_the_waiter_is_an_exhausted_wait(ec2):
    """InvalidInstanceID.NotFound는 waiter가 '아직'으로 읽고 계속 기다리는 오류다.

    그 응답이 last_response에 남았다면 대기 시간을 다 쓴 것이지 끊긴 것이 아니다 —
    기존대로 관측된 타임아웃이다.
    """
    exhausted = WaiterError(
        name=rb.WAITER_NAME,
        reason="Max attempts exceeded",
        last_response={"Error": {"Code": "InvalidInstanceID.NotFound", "Message": "x"}},
    )
    ec2(waiter=exhausted, describe=status_response("pending", "initializing", "initializing"))

    outcome = judge()

    assert outcome.verdict is V.TIMED_OUT
    assert outcome.probe_failed is False


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
    assert outcome.probe_failed is True  # 판정 불가 — 자동 원복 입력이 아니다


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
    assert outcome.probe_failed is True  # 판정 불가 — 자동 원복 입력이 아니다


def test_probe_failure_must_carry_its_reason_code():
    """판정 불가에 사유가 없으면 다시 물을지 가를 수 없다 — 영원한 보류가 된다 (Issue #249)."""
    with pytest.raises(ValueError, match="사유 코드"):
        rb.StatusCheckOutcome(verdict=V.TIMED_OUT, summary="조회 실패", probe_failed=True)


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
