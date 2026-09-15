"""AWS 응답 → ④ 거절 사유 코드 매핑 테스트 (Issue #128, ADR-0007 §2).

AWS/DB 불필요 — 예외 객체를 직접 만들어 분류만 검증한다.
"""

import logging
import sys
from pathlib import Path

import pytest
from botocore.exceptions import (
    ClientError,
    EndpointConnectionError,
    ParamValidationError,
)

CORE_API = Path(__file__).resolve().parents[2]
REPO_ROOT = Path(__file__).resolve().parents[4]
for p in (str(CORE_API), str(REPO_ROOT / "packages")):
    if p not in sys.path:
        sys.path.insert(0, p)

from schemas.precheck import PrecheckReasonCode  # noqa: E402
from services.aws.errors import (  # noqa: E402
    DRY_RUN_SUCCESS_ERROR_CODE,
    RETRYABLE_REASON_CODES,
    aws_error_code,
    is_retryable,
    reason_code_for,
    run_dry_run,
)

R = PrecheckReasonCode


def _client_error(code: str) -> ClientError:
    return ClientError(
        {"Error": {"Code": code, "Message": "테스트용 응답"}}, "TestOperation"
    )


# --- ADR-0007 §2 매핑 표 --------------------------------------------------------

# (AWS 오류 코드, 기대 사유 코드) — 표의 각 행을 실제 AWS가 쓰는 코드 이름으로 편다
MAPPING_CASES = [
    ("UnauthorizedOperation", R.PRECHECK_UNAUTHORIZED),
    ("AccessDenied", R.PRECHECK_UNAUTHORIZED),
    ("AccessDeniedException", R.PRECHECK_UNAUTHORIZED),
    ("InvalidInstanceID.NotFound", R.PRECHECK_TARGET_NOT_FOUND),
    ("InvalidGroup.NotFound", R.PRECHECK_TARGET_NOT_FOUND),
    ("InvalidNetworkAclID.NotFound", R.PRECHECK_TARGET_NOT_FOUND),
    ("InvalidVolume.NotFound", R.PRECHECK_TARGET_NOT_FOUND),
    ("TargetGroupNotFound", R.PRECHECK_TARGET_NOT_FOUND),
    ("InvalidTarget", R.PRECHECK_TARGET_NOT_FOUND),
    ("IncorrectInstanceState", R.PRECHECK_INVALID_STATE),
    ("DependencyViolation", R.PRECHECK_INVALID_STATE),
    ("VolumeInUse", R.PRECHECK_INVALID_STATE),
    ("InvalidGroup.InUse", R.PRECHECK_INVALID_STATE),
    # 그 밖의 ClientError — elbv2·autoscaling이 Community에 없을 때 나오는 응답 포함
    ("InternalFailure", R.PRECHECK_AWS_ERROR),
    ("ValidationError", R.PRECHECK_AWS_ERROR),
    ("", R.PRECHECK_AWS_ERROR),
]


@pytest.mark.parametrize("code,expected", MAPPING_CASES)
def test_client_error_mapping(code, expected):
    assert reason_code_for(_client_error(code)) == expected


def test_param_validation_error_is_our_contract_problem():
    """botocore 클라이언트 단에서 나므로 네트워크 호출 이전이다 — AWS 문제가 아니다."""
    exc = ParamValidationError(report="Unknown parameter in input: DryRun")
    assert reason_code_for(exc) == R.PRECHECK_PARAM_INVALID


def test_transport_failure_is_aws_error():
    exc = EndpointConnectionError(endpoint_url="http://localhost:4566")
    assert reason_code_for(exc) == R.PRECHECK_AWS_ERROR


def test_non_aws_exception_is_not_classified():
    with pytest.raises(TypeError):
        reason_code_for(ValueError("관계 없는 예외"))


def test_aws_error_code_reads_the_response():
    assert aws_error_code(_client_error("DryRunOperation")) == "DryRunOperation"
    assert aws_error_code(ClientError({}, "TestOperation")) == ""


# --- DryRun 판정 규약 -----------------------------------------------------------


def test_dry_run_operation_exception_is_the_only_pass():
    def operation(**kwargs):
        raise _client_error(DRY_RUN_SUCCESS_ERROR_CODE)

    assert run_dry_run(operation, InstanceId="i-1") is None


def test_silent_success_is_rejected_and_logged_critical(caplog):
    """예외 없이 반환 = DryRun 플래그 미적용. LocalStack NACL 결함이 이 경로였다."""
    calls = []

    def operation(**kwargs):
        calls.append(kwargs)
        return {"Return": True}

    with caplog.at_level(logging.CRITICAL, logger="vigilantis.aws"):
        assert run_dry_run(operation, NetworkAclId="acl-1") == R.PRECHECK_AWS_ERROR

    assert calls == [{"DryRun": True, "NetworkAclId": "acl-1"}]
    assert any(rec.message == "precheck_dry_run_not_applied" for rec in caplog.records)


def test_dry_run_flag_is_added_by_the_helper():
    """호출부가 DryRun을 빠뜨려 실제 실행이 되는 사고를 구조로 막는다."""
    seen = {}

    def operation(**kwargs):
        seen.update(kwargs)
        raise _client_error(DRY_RUN_SUCCESS_ERROR_CODE)

    run_dry_run(operation, VolumeId="vol-1")
    assert seen == {"DryRun": True, "VolumeId": "vol-1"}


def test_caller_may_not_pass_dry_run_itself():
    def operation(**kwargs):  # 도달하지 않는다
        raise AssertionError("호출되면 안 됩니다")

    with pytest.raises(ValueError, match="run_dry_run이 붙입니다"):
        run_dry_run(operation, DryRun=True, VolumeId="vol-1")


@pytest.mark.parametrize("code,expected", MAPPING_CASES)
def test_dry_run_failures_use_the_same_mapping(code, expected):
    def operation(**kwargs):
        raise _client_error(code)

    assert run_dry_run(operation, InstanceId="i-1") == expected


def test_dry_run_param_validation_error_is_param_invalid():
    def operation(**kwargs):
        raise ParamValidationError(report="Unknown parameter in input: DryRun")

    assert run_dry_run(operation, TargetGroupArn="arn:...") == R.PRECHECK_PARAM_INVALID


# --- 판정 불가 재시도 대상 (Issue #249) ------------------------------------------


def test_only_transient_aws_errors_are_retried():
    """다시 물으면 답이 바뀔 수 있는 사유만 재시도한다 — 권한 거부·파라미터 오류는 같다."""
    assert RETRYABLE_REASON_CODES == {R.PRECHECK_AWS_ERROR}
    for code in set(R) - RETRYABLE_REASON_CODES:
        assert not is_retryable(code), code


@pytest.mark.parametrize(
    "exc",
    [
        ClientError(
            {
                "Error": {"Code": "RequestLimitExceeded"},
                "ResponseMetadata": {"HTTPStatusCode": 503},
            },
            "DescribeInstanceStatus",
        ),
        EndpointConnectionError(endpoint_url="http://localhost:4566"),
    ],
)
def test_throttling_and_transport_failures_are_retryable(exc):
    """판정 불가의 대표 두 갈래 — 스로틀링과 접속 실패는 재시도 대상으로 분류된다."""
    assert is_retryable(reason_code_for(exc))


def test_denied_probe_is_not_retryable():
    """권한 거부는 사람이 고치기 전에는 몇 번을 물어도 같다 — 곧바로 넘긴다."""
    assert not is_retryable(reason_code_for(_client_error("UnauthorizedOperation")))
