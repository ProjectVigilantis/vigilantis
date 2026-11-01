# ==============================================================================
# [파일 설명]  담당: 김승철 (API 계약 · BE)
# 연결 확인 판정(tenant_verification.judge_connection) — AWS 대역(예외를 던지는 함수)으로 결과 5갈래를 고정한다.
# (Post-MVP T-4 2단계 B · #533)
#
#   - LocalStack 은 신뢰 정책 · ExternalId 를 검사하지 않아 ASSUME_DENIED 를 재현하지 못한다 — 판정은 대역으로,
#     실제 역할의 OK · 틀린 ExternalId 의 ASSUME_DENIED 는 학원 계정 실측(#533 수용 기준)으로 본다.
#   - 오류는 botocore 가 실제로 만드는 모양(ClientError 응답 dict · BotoCoreError 하위 예외)으로 던진다.
# ==============================================================================

from __future__ import annotations

from datetime import UTC, datetime

import botocore.retries.standard as botocore_retry
import pytest
from botocore.exceptions import (
    ClientError,
    EndpointConnectionError,
    NoCredentialsError,
    ReadTimeoutError,
)
from schemas.api.tenants import TenantVerification, TenantVerificationResult
from tenant_verification import (
    ACCOUNT_MISMATCH_REASON,
    PLATFORM_CREDENTIAL_ERROR_CODES,
    UNREACHABLE_ERROR_CODES,
    VerificationOutcome,
    judge_connection,
)

R = TenantVerificationResult
ACCOUNT = "111122223333"
SESSION = object()


def _client_error(code: str | None, *, status: int = 400, operation: str = "AssumeRole") -> ClientError:
    error = {"Message": "denied"} if code is None else {"Code": code, "Message": "denied"}
    return ClientError({"Error": error, "ResponseMetadata": {"HTTPStatusCode": status}}, operation)


def _raise(exc: BaseException):
    def fail(*_args):
        raise exc

    return fail


def _judge(*, assume=None, caller=None, read=None, calls: list | None = None) -> VerificationOutcome:
    """기본은 세 단계가 모두 통과하는 대역이다. 부른 단계를 calls 에 남긴다."""
    calls = [] if calls is None else calls

    def step(name, fn, default):
        def wrapped(*args):
            calls.append(name)
            if args:
                assert args == (SESSION,), "받은 세션을 다음 단계에 그대로 넘긴다"
            return (fn or default)(*args)

        return wrapped

    return judge_connection(
        expected_account_id=ACCOUNT,
        assume=step("assume", assume, lambda: SESSION),
        caller_account=step("caller", caller, lambda _s: ACCOUNT),
        read=step("read", read, lambda _s: {"Reservations": []}),
    )


def test_every_step_passes_is_ok_and_runs_in_contract_order():
    calls: list = []
    assert _judge(calls=calls) == VerificationOutcome(R.OK, None)
    assert calls == ["assume", "caller", "read"]


# --- 1. AssumeRole -------------------------------------------------------------


@pytest.mark.parametrize("code", [
    "AccessDenied",  # 역할 없음 · 신뢰 정책이 플랫폼 주체나 ExternalId 를 허용하지 않음(AWS 는 둘을 가르지 않는다)
    "RegionDisabledException",  # 고객 계정에서 그 리전의 STS 가 꺼짐 — 고객 쪽 설정
    "ValidationError",  # 저장된 역할 ARN · ExternalId 가 STS 형식에 맞지 않음 — 다시 물어도 같다
])
def test_assume_refusal_is_assume_denied_and_stops_there(code):
    calls: list = []
    outcome = _judge(assume=_raise(_client_error(code, status=403)), calls=calls)
    assert outcome == VerificationOutcome(R.ASSUME_DENIED, code)
    assert calls == ["assume"]


# --- 묻지 못함은 판정하지 않는다 ------------------------------------------------------


@pytest.mark.parametrize("exc", [
    _client_error("Throttling"),
    _client_error("RequestLimitExceeded"),
    _client_error("InvalidClientTokenId", status=403),  # 플랫폼 자신의 자격 문제 — 고객이 답한 것이 아니다
    _client_error("SignatureDoesNotMatch", status=403),
    _client_error("SomethingNew", status=503),  # 모르는 코드라도 5xx 는 묻지 못한 것이다
    _client_error(None, status=500),
    EndpointConnectionError(endpoint_url="https://sts.ap-northeast-2.amazonaws.com"),
    ReadTimeoutError(endpoint_url="https://sts.ap-northeast-2.amazonaws.com"),
    NoCredentialsError(),
], ids=lambda e: type(e).__name__ if not isinstance(e, ClientError) else e.response["Error"].get("Code", "no-code"))
@pytest.mark.parametrize("step", ["assume", "caller", "read"])
def test_not_being_able_to_ask_is_unreachable_at_every_step(exc, step):
    code = exc.response["Error"].get("Code") if isinstance(exc, ClientError) else None
    assert _judge(**{step: _raise(exc)}) == VerificationOutcome(R.UNREACHABLE, code or type(exc).__name__)


# --- 2. GetCallerIdentity · 계정 대조 ------------------------------------------------


def test_other_account_is_account_mismatch_and_does_not_read():
    calls: list = []
    outcome = _judge(caller=lambda _s: "444455556666", calls=calls)
    assert outcome == VerificationOutcome(R.ACCOUNT_MISMATCH, ACCOUNT_MISMATCH_REASON)
    assert calls == ["assume", "caller"]
    assert "444455556666" not in outcome.reason_code and ACCOUNT not in outcome.reason_code


def test_caller_identity_error_is_never_a_refusal():
    # 권한이 필요 없는 호출이라 4xx 여도 고객이 거절한 것이 아니다 — 확인하지 못한 것이다
    outcome = _judge(caller=_raise(_client_error("AccessDenied", status=403, operation="GetCallerIdentity")))
    assert outcome == VerificationOutcome(R.UNREACHABLE, "AccessDenied")


# --- 3. 최소 읽기 ---------------------------------------------------------------


@pytest.mark.parametrize("code", [
    "UnauthorizedOperation",  # 권한 정책이 붙지 않음(EC2 의 거절 코드)
    "AccessDenied",
    "OptInRequired",  # 고객 계정에서 그 리전이 꺼져 있음 — 고객 쪽 설정
])
def test_read_refusal_is_read_denied(code):
    outcome = _judge(read=_raise(_client_error(code, status=403, operation="DescribeInstances")))
    assert outcome == VerificationOutcome(R.READ_DENIED, code)


# --- 사유 · 계약 모양 -------------------------------------------------------------


def test_reason_without_an_aws_code_is_the_exception_class_name():
    outcome = _judge(assume=_raise(_client_error(None, status=403)))
    assert outcome == VerificationOutcome(R.ASSUME_DENIED, "ClientError")


def test_non_aws_exceptions_are_not_recorded_as_a_result():
    # 구현 결함(응답 모양 오해 등)을 확인 결과로 덮지 않는다
    with pytest.raises(KeyError):
        _judge(caller=_raise(KeyError("Account")))


@pytest.mark.parametrize("outcome", [
    _judge(),
    _judge(assume=_raise(_client_error("AccessDenied", status=403))),
    _judge(caller=lambda _s: "444455556666"),
    _judge(read=_raise(_client_error("UnauthorizedOperation", status=403))),
    _judge(read=_raise(EndpointConnectionError(endpoint_url="https://ec2.amazonaws.com"))),
], ids=lambda o: o.result.value)
def test_every_outcome_fits_the_contract_shape(outcome):
    # OK ⇔ reason_code 없음 — 계약 모델과 DB CHECK(verification_shape)가 같은 짝을 요구한다
    TenantVerification(result=outcome.result, verified_at=datetime.now(UTC), reason_code=outcome.reason_code)


def test_unreachable_codes_cover_botocore_standard_retry_tables():
    # botocore 가 「다시 물을 것」으로 보는 스로틀링 · 일시 오류는 모두 묻지 못함이다 — 갱신으로 표가 늘면 여기서 걸린다
    throttled = botocore_retry.ThrottledRetryableChecker._THROTTLED_ERROR_CODES
    transient = botocore_retry.TransientRetryableChecker._TRANSIENT_ERROR_CODES
    assert set(throttled) | set(transient) <= UNREACHABLE_ERROR_CODES
    assert not UNREACHABLE_ERROR_CODES & PLATFORM_CREDENTIAL_ERROR_CODES
