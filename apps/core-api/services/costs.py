# ==============================================================================
# [파일 설명]  담당: 김승철 (API 계약 · BE)
# 자산 유형별 비용(GET /api/v1/costs/by-asset-type · #572 11번)의 데이터 원천 — AWS Cost Explorer 입니다.
#
# 한 번 받는 데 `GetCostAndUsage` 를 페이지 수만큼 부른다. 호출(페이지)마다 과금되므로($0.01) 받아 두는 것은
# 라우터 몫이고(routers/costs.py), 여기는 받은 결과를 네 칸으로 묶기만 한다.
#
#   - 고객사 자격(tenant_scope)으로만 부른다. 소속 없는 MVP 기록의 프로세스 자격 경로는 두지 않는다.
#   - 30일 구간을 월 단위(MONTHLY)로 받고 SERVICE · USAGE_TYPE 으로 묶는다. 일 단위는 행이 30배라 페이지가
#     늘고 그만큼 과금된다. GroupBy 는 두 개가 상한이다.
#   - REGION 필터 = 고객사의 관제 리전(같은 화면의 자산 현황과 같은 근거). 리전 없는(global) 요금은 빠진다.
#   - RECORD_TYPE=Usage 만 센다 — 크레딧 · 환불 · 세금을 빼서 「자원 사용 금액」이 된다. 크레딧이 있는 계정은
#     크레딧이 음수로 들어와 합계가 0 근처가 되고, 그러면 유형별 비율을 그릴 수 없다.
#     한계: RI · Savings Plans 로 덮인 사용분은 `DiscountedUsage` · `SavingsPlanCoveredUsage` 로 따로 잡혀 빠진다.
#     그런 고객사는 EC2 칸이 실제보다 작다. 생기면 필터를 「크레딧 · 환불 · 세금 제외」로 바꾼다(PR #581 리뷰).
#   - EBS 는 `GetCostAndUsage` 필터가 `EBS:` 같은 와일드카드를 받지 않아, 묶어 받은 뒤 USAGE_TYPE 이름으로 가른다.
# ==============================================================================

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Sequence

from botocore.exceptions import ClientError

from schemas.api.costs import COST_BUCKETS, CostBucket

from services.aws.client import TenantAccess, aws_client, tenant_scope
from services.collector import _failure_reason

#: Cost Explorer 엔드포인트는 us-east-1 하나다(리전 서비스가 아니다).
CE_REGION = "us-east-1"

_EC2_COMPUTE = "Amazon Elastic Compute Cloud - Compute"
_EC2_OTHER = "EC2 - Other"
# Cost Explorer 의 SERVICE 값은 앞의 이름이다. #572 본문의 짧은 이름도 같은 서비스로 받는다.
_LOAD_BALANCING = frozenset({"Amazon Elastic Load Balancing", "Elastic Load Balancing"})
_EBS_MARK = "EBS:"

_CENT = Decimal("0.01")

#: Cost Explorer 오류 코드 → 응답 사유 코드. 표에 없으면 AWS 오류 코드를 그대로 싣는다.
_REASONS = {
    "AccessDeniedException": "COST_EXPLORER_ACCESS_DENIED",
    "DataUnavailableException": "COST_EXPLORER_DATA_UNAVAILABLE",
    "LimitExceededException": "COST_EXPLORER_THROTTLED",
    "ThrottlingException": "COST_EXPLORER_THROTTLED",
    "RequestLimitExceeded": "COST_EXPLORER_THROTTLED",
}
# 계정에서 Cost Explorer 를 켜지 않았을 때도 AccessDeniedException 이다 — 문구로만 가를 수 있다.
_NOT_ENABLED_MARK = "not enabled for cost explorer"


class CostUnavailable(RuntimeError):
    """비용을 읽지 못했다. ``reason_code`` 는 응답의 사유로 그대로 나간다(고객사 ID 는 싣지 않는다)."""

    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


@dataclass(frozen=True)
class FetchedCosts:
    """네 칸의 금액(소수 2자리로 반올림, COST_BUCKETS 순서)과 추정치 여부."""

    amounts: tuple[tuple[CostBucket, Decimal], ...]
    estimated: bool


def bucket_of(service: str, usage_type: str) -> CostBucket:
    """Cost Explorer 한 줄(SERVICE · USAGE_TYPE)의 칸."""
    if service == _EC2_COMPUTE:
        return CostBucket.EC2
    if service == _EC2_OTHER and _EBS_MARK in usage_type:
        return CostBucket.EBS
    if service in _LOAD_BALANCING:
        return CostBucket.ALB_TARGET_GROUP
    return CostBucket.OTHER


def reason_of(exc: BaseException) -> str:
    """실패를 응답 사유 코드로. Cost Explorer 오류는 계약 이름으로 바꾸고, 그 밖(AssumeRole 거절 등)은 그대로 둔다."""
    if isinstance(exc, CostUnavailable):
        return exc.reason_code
    if isinstance(exc, ClientError):
        error = exc.response.get("Error", {})
        code = error.get("Code") or ""
        if code == "AccessDeniedException" and _NOT_ENABLED_MARK in (error.get("Message") or "").lower():
            return "COST_EXPLORER_NOT_ENABLED"
        if code in _REASONS:
            return _REASONS[code]
    return _failure_reason(exc)


def fetch_costs(
    access: TenantAccess, *, regions: Sequence[str], period_start: date, period_end: date
) -> FetchedCosts:
    """그 고객사의 구간 비용을 네 칸으로 묶는다. 실패는 CostUnavailable 로 올린다(사유는 reason_of)."""
    sums = dict.fromkeys(COST_BUCKETS, Decimal("0"))
    estimated = False
    try:
        with tenant_scope(access):
            ce = aws_client("ce", CE_REGION)
            request = {
                "TimePeriod": {"Start": period_start.isoformat(), "End": period_end.isoformat()},
                "Granularity": "MONTHLY",
                "Metrics": ["UnblendedCost"],
                "GroupBy": [
                    {"Type": "DIMENSION", "Key": "SERVICE"},
                    {"Type": "DIMENSION", "Key": "USAGE_TYPE"},
                ],
                "Filter": {
                    "And": [
                        {"Dimensions": {"Key": "REGION", "Values": list(regions)}},
                        {"Dimensions": {"Key": "RECORD_TYPE", "Values": ["Usage"]}},
                    ]
                },
            }
            while True:
                page = ce.get_cost_and_usage(**request)
                for period in page.get("ResultsByTime", []):
                    estimated = estimated or bool(period.get("Estimated"))
                    for group in period.get("Groups", []):
                        service, usage_type = group["Keys"]
                        cost = group["Metrics"]["UnblendedCost"]
                        if cost.get("Unit") != "USD":
                            raise CostUnavailable("COST_CURRENCY_UNSUPPORTED")
                        sums[bucket_of(service, usage_type)] += Decimal(cost["Amount"])
                token = page.get("NextPageToken")
                if not token:
                    break
                request["NextPageToken"] = token
    except CostUnavailable:
        raise
    except Exception as exc:
        raise CostUnavailable(reason_of(exc)) from exc

    # Usage 만 세므로 음수는 없어야 한다 — 보정 줄이 섞여 음수가 되면 0 으로 둔다(계약은 음수를 받지 않는다).
    amounts = tuple(
        (bucket, max(value, Decimal("0")).quantize(_CENT, rounding=ROUND_HALF_UP))
        for bucket, value in sums.items()
    )
    return FetchedCosts(amounts=amounts, estimated=estimated)
