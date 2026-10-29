"""rollback.probe_status_check() 단위 테스트 (Issue #240 · #434 1-3).

AWS 불필요 — boto3 클라이언트를 가짜로 갈아 끼우고 **관측 4분기**를 본다.

관측이 곧 자동 원복의 방아쇠다. OK를 잘못 주면 부팅 실패한 자산이 그대로 남고,
FAILED를 잘못 주면 멀쩡한 자산을 되돌린다. 그래서 조회 1회의 응답을 "실패"·"아직"·
"판정 불가"로 가르는 근거(인스턴스 상태·검사 결과)를 전수로 고정한다. 기다리지 않는다는
사실(waiter 없음)도 여기서 고정한다 — 판정 1건이 디스패처 한 주기를 붙잡던 것이
구조 변경의 이유였다. LocalStack 실물 검증은 test_status_check_localstack.py가 맡는다.
"""

import sys
from datetime import timedelta
from pathlib import Path

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError

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


class FakeEc2:
    """describe 응답 하나만 주입한다 — waiter는 없다(있으면 호출 자체가 실패한다)."""

    def __init__(self, describe):
        self._describe = describe
        self.calls: list[tuple[str, dict]] = []

    def describe_instance_status(self, **kwargs):
        self.calls.append(("describe_instance_status", kwargs))
        if isinstance(self._describe, BaseException):
            raise self._describe
        return self._describe if self._describe is not None else {}


@pytest.fixture
def ec2(monkeypatch):
    """가짜 EC2를 세우고 돌려준다 — 호출부는 describe 응답만 정하면 된다."""

    def build(describe=None):
        client = FakeEc2(describe)
        monkeypatch.setattr(rb, "aws_client", lambda service, region=None, **_: client)
        return client

    return build


def probe():
    return rb.probe_status_check(INSTANCE_ARN)


# ------------------------------------------------------------------- 관측 4분기


def test_two_of_two_passes(ec2):
    """running이고 두 검사가 모두 ok면 OK — 조회 1회로 끝난다."""
    client = ec2(status_response("running", "ok", "ok"))

    outcome = probe()

    assert outcome.verdict is V.OK and outcome.booted
    assert outcome.reason_code is None
    assert [name for name, _ in client.calls] == ["describe_instance_status"]


def test_probe_asks_for_all_instances(ec2):
    """DescribeInstanceStatus 기본 호출은 running만 돌려준다 — stopped를 보려면 IncludeAllInstances다."""
    client = ec2(status_response("running", "ok", "ok"))

    probe()

    assert client.calls[-1][1]["IncludeAllInstances"] is True


def test_impaired_check_is_a_failure(ec2):
    """AWS가 이미 이상으로 판정했다 — 더 기다릴 이유가 없다."""
    ec2(status_response("running", "ok", "impaired"))

    outcome = probe()

    assert outcome.verdict is V.FAILED and not outcome.booted
    assert outcome.instance_state == "running"
    assert not outcome.undecided


@pytest.mark.parametrize("state", ["stopped", "stopping", "shutting-down", "terminated"])
def test_instance_not_running_is_a_failure(ec2, state):
    """기동을 요청했는데 running 계열이 아니면 부팅에 실패한 것이다.

    이 경로가 곧 자동 원복 시연이다 — 타입 변경 뒤 인스턴스가 뜨지 못하면
    DescribeInstanceStatus는 기본 조회에서 **빈 응답**만 주므로 IncludeAllInstances로
    물어야 실패를 실패로 읽는다.
    """
    ec2(status_response(state, "ok", "ok"))

    outcome = probe()

    assert outcome.verdict is V.FAILED
    assert outcome.instance_state == state


@pytest.mark.parametrize(
    "state, system, instance",
    [
        ("pending", "initializing", "initializing"),
        ("running", "initializing", "initializing"),
        ("running", "insufficient-data", "insufficient-data"),
        ("running", "initializing", "ok"),  # 인스턴스 검사만 ok — 2/2가 아니다
        ("running", "ok", "initializing"),  # 시스템 검사만 ok — 2/2가 아니다
    ],
)
def test_not_yet_two_of_two_is_pending_not_failed(ec2, state, system, instance):
    """아직 판정 전이다 — 실패로 접으면 부팅 중인 자산을 되돌리게 된다.

    한쪽 검사만 ok인 관측도 같다(Issue #434 1-3). 전에는 waiter가 InstanceStatus 하나만
    보고 통과시켰고, 재조회만 두 검사를 봤다 — 이제 정상 경로도 둘 다 본다.
    """
    ec2(status_response(state, system, instance))

    outcome = probe()

    assert outcome.verdict is V.PENDING and not outcome.booted
    assert outcome.undecided  # 관측된 '아직' — 예산을 대면 TIMED_OUT이 된다
    assert outcome.probe_failed is False and outcome.reason_code is None
    assert f"{system}/{instance}" in outcome.summary


def test_empty_status_response_is_a_failure(ec2):
    """IncludeAllInstances로 물었는데도 없으면 대상 자체가 없는 것이다."""
    ec2({"InstanceStatuses": []})

    outcome = probe()

    assert outcome.verdict is V.FAILED
    assert outcome.reason_code is R.PRECHECK_TARGET_NOT_FOUND


# ------------------------------------------------------------- AWS 오류 분류


def test_probe_error_defers_instead_of_failing(ec2):
    """상태를 못 물어본 것은 부팅 실패가 아니다 — 사유 코드를 실어 판정 불가로 남긴다.

    조회 실패를 FAILED로 접으면 일시적인 권한·네트워크 문제가 멀쩡한 인스턴스의
    자동 원복을 부른다. reason_code가 채워진 PENDING + probe_failed가 그 구분이다.
    """
    ec2(client_error("UnauthorizedOperation", 403))

    outcome = probe()

    assert outcome.verdict is V.PENDING
    assert outcome.reason_code is R.PRECHECK_UNAUTHORIZED
    assert outcome.probe_failed is True  # 판정 불가 — 자동 원복 입력이 아니다
    assert not outcome.undecided  # 관측이 아니다 — 예산을 대도 TIMED_OUT이 되지 않는다


def test_throttled_probe_is_retryable(ec2):
    """스로틀링은 다시 물을 가치가 있는 실패다 — 재시도 정책(#249)이 읽는 코드다."""
    ec2(client_error("RequestLimitExceeded", 503))

    outcome = probe()

    assert outcome.probe_failed is True
    assert outcome.reason_code is R.PRECHECK_AWS_ERROR


def test_missing_instance_probe_is_a_failure(ec2):
    """인스턴스가 없으면 2/2는 영원히 오지 않는다 — 보류가 아니라 실패다."""
    ec2(client_error("InvalidInstanceID.NotFound", 400))

    outcome = probe()

    assert outcome.verdict is V.FAILED
    assert outcome.reason_code is R.PRECHECK_TARGET_NOT_FOUND


def test_connection_error_defers(ec2):
    """AWS에 닿지 못했으면 자산 상태를 본 적이 없다 — 실패로 확정하지 않는다."""
    ec2(EndpointConnectionError(endpoint_url="http://localstack:4566"))

    outcome = probe()

    assert outcome.verdict is V.PENDING
    assert outcome.reason_code is R.PRECHECK_AWS_ERROR
    assert outcome.probe_failed is True


def test_probe_failure_must_carry_its_reason_code():
    """판정 불가에 사유가 없으면 다시 물을지 가를 수 없다 — 영원한 보류가 된다 (Issue #249)."""
    with pytest.raises(ValueError, match="사유 코드"):
        rb.StatusCheckOutcome(verdict=V.PENDING, summary="조회 실패", probe_failed=True)


def test_probe_failure_cannot_be_a_verdict():
    """자산 상태를 본 적이 없는데 실패·성공·타임아웃으로 확정된 결과는 만들 수 없다."""
    with pytest.raises(ValueError, match="PENDING"):
        rb.StatusCheckOutcome(
            verdict=V.TIMED_OUT,
            summary="조회 실패",
            reason_code=R.PRECHECK_AWS_ERROR,
            probe_failed=True,
        )


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
    ec2(error)

    outcome = probe()

    assert outcome.verdict in (V.FAILED, V.PENDING)


# ------------------------------------------------------------------ 예산 판정


def test_timed_out_is_built_from_an_observed_pending(ec2):
    """TIMED_OUT은 관측이 아니라 판정이다 — PENDING 관측에 예산 초과를 더해 만든다."""
    ec2(status_response("running", "initializing", "ok"))
    observed = probe()

    outcome = rb.timed_out(observed, timedelta(seconds=180))

    assert outcome.verdict is V.TIMED_OUT and not outcome.probe_failed
    assert outcome.instance_state == "running"
    assert "180초" in outcome.summary and "initializing/ok" in outcome.summary


def test_timed_out_refuses_a_probe_failure(ec2):
    """조회 실패에 예산 초과를 더해 실패로 확정할 수 없다 — 자산을 본 적이 없다."""
    ec2(client_error("RequestLimitExceeded", 503))
    observed = probe()

    with pytest.raises(ValueError, match="관측된 PENDING"):
        rb.timed_out(observed, timedelta(seconds=180))


def test_budget_is_the_product_of_the_two_settings(monkeypatch):
    """예산은 설정값 둘의 곱이다 — 이름은 waiter 시절 것이지만 의미는 벽시계 상한이다."""
    monkeypatch.setattr(
        rb,
        "get_settings",
        lambda: type(
            "S",
            (),
            {"STATUS_CHECK_WAIT_DELAY_SECONDS": 15, "STATUS_CHECK_WAIT_MAX_ATTEMPTS": 12},
        )(),
    )

    assert rb.status_check_budget() == timedelta(seconds=180)


# --------------------------------------------------------------- 배선 오류


def test_non_instance_arn_raises(ec2):
    """자산 상태에 대한 판정이 아니라 호출부 배선 오류다 — 삼키면 멀쩡한 실행에
    '기동 실패' 기록이 붙고 그 기록이 자동 원복의 입력이 된다."""
    ec2(status_response("running", "ok", "ok"))

    with pytest.raises(ValueError, match="인스턴스 ARN이 아닙니다"):
        rb.probe_status_check(VOLUME_ARN)
