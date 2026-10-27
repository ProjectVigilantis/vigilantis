# ==============================================================================
# [파일 설명]  담당: 김승철 (API 계약 · BE)
# 테넌트 연결 확인의 판정 — AWS 호출 세 단계의 결과를 계약의 결과 5갈래로 접는다.
# (Post-MVP T-4 2단계 B · #533)
#
#   - 계약: packages/schemas/api/tenants.py TenantVerificationResult · TenantVerification.
#     순서대로 처음 실패한 자리를 싣는다 — AssumeRole(ASSUME_DENIED) → GetCallerIdentity · 계정 대조
#     (ACCOUNT_MISMATCH) → 최소 읽기 1건(READ_DENIED). 묻지 못했으면 UNREACHABLE(연결 여부를 판정하지 않는다).
#   - 이 모듈은 AWS 를 직접 부르지 않는다. 세 단계를 호출부가 함수로 넘긴다 — 플랫폼 자격 · 고객 역할 세션을
#     만드는 일은 services/aws/client.py 의 테넌트 층(#533 A) 몫이고, 판정은 그와 따로 고정한다
#     (LocalStack 은 신뢰 정책 · ExternalId 를 검사하지 않아 판정을 실측으로 고정할 수 없다).
#   - 「묻지 못함」과 「거절됨」을 가른다. 스로틀링 · 5xx · 접속 실패 · 플랫폼 자신의 자격 문제는 고객 계정이
#     답한 것이 아니라 UNREACHABLE 이다. 그 밖의 AWS 오류는 그 단계의 거절로 본다 — 사람이 고치기 전에는
#     다시 물어도 같은 답이다.
#   - reason_code 는 AWS 오류 코드, 없으면 예외 클래스명이다(계약). 계정 ID · 역할 ARN 은 싣지 않는다.
#   - AWS 예외가 아닌 것(구현 결함)은 삼키지 않고 그대로 던진다 — 확인 결과로 기록하지 않는다.
# ==============================================================================

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Final, TypeVar

from botocore.exceptions import BotoCoreError, ClientError
from schemas.api.tenants import TenantVerificationResult
from services.aws.errors import aws_error_code

_Session = TypeVar("_Session")

#: AWS 가 「지금은 답하지 못한다」고 한 오류 코드 — 스로틀링과 일시 오류(botocore 표준 재시도의 두 표를 덮는다).
UNREACHABLE_ERROR_CODES: Final[frozenset[str]] = frozenset({
    "BandwidthLimitExceeded",
    "EC2ThrottledException",
    "LimitExceededException",
    "PriorRequestNotComplete",
    "ProvisionedThroughputExceededException",
    "RequestLimitExceeded",
    "RequestThrottled",
    "RequestThrottledException",
    "RequestTimeout",
    "RequestTimeoutException",
    "ServiceUnavailable",
    "SlowDown",
    "ThrottledException",
    "Throttling",
    "ThrottlingException",
    "TooManyRequestsException",
    "TransactionInProgressException",
    "InternalError",
    "InternalFailure",
    "InternalServiceError",
})

#: 플랫폼 자신의 자격이 거절된 코드 — 고객 계정의 판단이 아니라 우리 쪽 장애다. 고객 역할로 받은 세션이
#: 만료된 경우(ExpiredToken)도 확인 1회 안에서는 나오지 않아야 할 일이라 같은 자리에 둔다.
PLATFORM_CREDENTIAL_ERROR_CODES: Final[frozenset[str]] = frozenset({
    "AuthFailure",
    "ExpiredToken",
    "ExpiredTokenException",
    "IncompleteSignature",
    "InvalidClientTokenId",
    "MissingAuthenticationToken",
    "RequestExpired",
    "SignatureDoesNotMatch",
    "UnrecognizedClientException",
})

#: 계정 대조 실패의 사유 — AWS 오류가 아니라 우리가 내린 판정이라 따로 이름을 둔다.
ACCOUNT_MISMATCH_REASON: Final[str] = "CallerAccountMismatch"


@dataclass(frozen=True)
class VerificationOutcome:
    """연결 확인 1회의 판정. OK 면 reason_code 는 None(계약 TenantVerification 과 같은 짝)."""

    result: TenantVerificationResult
    reason_code: str | None = None


OK: Final[VerificationOutcome] = VerificationOutcome(TenantVerificationResult.OK)


def _reason(exc: BaseException) -> str:
    code = aws_error_code(exc) if isinstance(exc, ClientError) else ""
    return code or type(exc).__name__


def _unreachable(exc: BaseException) -> bool:
    """고객 계정이 답한 것이 아닌가 — 접속 실패 · 스로틀링 · 5xx · 플랫폼 자격 문제."""
    if isinstance(exc, BotoCoreError):
        return True  # 엔드포인트 접속 실패 · 읽기 시간 초과 · 자격 증명 없음 — 요청이 닿지 않았다
    code = aws_error_code(exc)
    if code in UNREACHABLE_ERROR_CODES or code in PLATFORM_CREDENTIAL_ERROR_CODES:
        return True
    status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    return isinstance(status, int) and status >= 500


def _failed_at(step: TenantVerificationResult, exc: BaseException) -> VerificationOutcome:
    result = TenantVerificationResult.UNREACHABLE if _unreachable(exc) else step
    return VerificationOutcome(result, _reason(exc))


def judge_connection(
    *,
    expected_account_id: str,
    assume: Callable[[], _Session],
    caller_account: Callable[[_Session], str],
    read: Callable[[_Session], object],
) -> VerificationOutcome:
    """세 단계를 순서대로 부르고 처음 실패한 자리를 판정한다.

    assume: 플랫폼 자격으로 고객 역할을 AssumeRole 해 그 세션(또는 자격)을 돌려준다.
    caller_account: 그 세션으로 sts:GetCallerIdentity 를 불러 계정 ID 를 돌려준다.
    read: 그 세션으로 최소 읽기 1건(ec2:DescribeInstances · regions[0])을 부른다. 반환값은 보지 않는다.

    GetCallerIdentity 는 권한이 필요 없는 호출이라 그 단계의 AWS 오류는 거절이 아니라 「확인하지 못함」이다.
    """
    try:
        session = assume()
    except (ClientError, BotoCoreError) as exc:
        return _failed_at(TenantVerificationResult.ASSUME_DENIED, exc)

    try:
        account = caller_account(session)
    except (ClientError, BotoCoreError) as exc:
        return VerificationOutcome(TenantVerificationResult.UNREACHABLE, _reason(exc))
    if account != expected_account_id:
        return VerificationOutcome(TenantVerificationResult.ACCOUNT_MISMATCH, ACCOUNT_MISMATCH_REASON)

    try:
        read(session)
    except (ClientError, BotoCoreError) as exc:
        return _failed_at(TenantVerificationResult.READ_DENIED, exc)
    return OK
