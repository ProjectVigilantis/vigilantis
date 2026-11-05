# ==============================================================================
# [파일 설명]  담당: 김승철 (API 계약 · BE)
# GET /api/v1/costs/by-asset-type 외부 응답 DTO입니다(#572 11번). 고객사 하나의 최근 30일 자원 사용 금액을
# 자산 유형 네 칸(EC2 · EBS · ALB_TARGET_GROUP · OTHER)으로 나눠 싣습니다. 원천은 AWS Cost Explorer
# `GetCostAndUsage` 이고, 서버가 고객사 역할로 하루 한 번 받아 둡니다(routers/costs.py).
#
# 계약 원칙 (metrics.py 와 동일)
#   - snake_case, UTC ISO 8601("Z") 문자열, nullable 은 null 반환.
#   - AWS 원본 응답을 그대로 노출하지 않는다.
#   - 읽지 못해도 500 이 아니라 status=UNAVAILABLE 과 사유 코드다 — 화면은 자산 개수 비율로 대신 그린다.
#
# 금액은 소수 2자리 문자열이다(부동소수 오차 없이 화면이 그대로 적게). total 은 반올림한 네 칸의 합이라
# 화면의 합계와 칸 합이 어긋나지 않는다. 구간 끝(period_end)은 포함하지 않는다(Cost Explorer 규칙).
# ==============================================================================

from __future__ import annotations

from datetime import date
from decimal import Decimal
from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_serializer, model_validator

from .metrics import AxisStatus, UtcDateTime
from .tenants import AwsRegion


class CostBucket(str, Enum):
    """비용을 묶는 자산 유형 칸. 서버가 Cost Explorer 의 SERVICE · USAGE_TYPE 으로 가른다(services/costs.py).

    보안 그룹 · NACL · Auto Scaling 그룹 · 시작 템플릿은 AWS 요금이 없어 칸이 없다.
    """

    EC2 = "EC2"
    EBS = "EBS"
    #: 요금은 대상 그룹이 아니라 로드 밸런서에 붙는다 — 화면의 대상 그룹 줄이 이 칸을 쓴다.
    ALB_TARGET_GROUP = "ALB_TARGET_GROUP"
    #: 위에 들지 않는 나머지(데이터 전송 · NAT 게이트웨이 · CloudWatch 등).
    OTHER = "OTHER"


#: 응답의 칸 순서. 0원 칸도 빠짐없이 싣는다.
COST_BUCKETS: tuple[CostBucket, ...] = tuple(CostBucket)


class CostItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bucket: CostBucket
    #: 소수 2자리(USD). 음수가 아니다 — 크레딧 · 환불은 세지 않는다(RECORD_TYPE=Usage).
    amount: Decimal = Field(ge=0, decimal_places=2)

    @field_serializer("amount")
    def _amount_text(self, amount: Decimal) -> str:
        return f"{amount:.2f}"


class AssetCostsResponse(BaseModel):
    """자산 유형별 최근 30일 자원 사용 금액. 청구 금액(크레딧 · 세금 반영)이 아니다."""

    model_config = ConfigDict(extra="forbid")

    status: AxisStatus
    #: UNAVAILABLE 의 사유. READY 면 null.
    #: COST_EXPLORER_ACCESS_DENIED · COST_EXPLORER_NOT_ENABLED · COST_EXPLORER_DATA_UNAVAILABLE ·
    #: COST_EXPLORER_THROTTLED · COST_CURRENCY_UNSUPPORTED · TENANT_<상태> · 그 밖의 AWS 오류 코드.
    reason_code: Optional[str] = None
    currency: Literal["USD"] = "USD"
    #: 조회 구간(UTC 날짜). 끝은 포함하지 않는다.
    period_start: date
    period_end: date
    #: Cost Explorer 의 REGION 필터로 건 리전 — 고객사의 관제 리전이다. 관제 밖 리전 · 리전 없는(global) 요금은 빠진다.
    regions: list[AwsRegion]
    #: 네 칸의 합(반올림한 칸의 합). UNAVAILABLE 이면 null.
    total: Optional[Decimal] = Field(None, ge=0, decimal_places=2)
    #: 네 칸 전부(0원 포함), COST_BUCKETS 순서. UNAVAILABLE 이면 빈 배열.
    items: list[CostItem] = Field(default_factory=list)
    #: 구간 안에 확정되지 않은 금액(Cost Explorer `Estimated`)이 있으면 true. UNAVAILABLE 이면 false.
    estimated: bool = False
    #: 서버가 이 결과를 정한 시각 — Cost Explorer 에서 받은 시각이고, UNAVAILABLE 이면 실패를 정한 시각이다.
    retrieved_at: UtcDateTime

    @field_serializer("total")
    def _total_text(self, total: Optional[Decimal]) -> Optional[str]:
        return None if total is None else f"{total:.2f}"

    @model_validator(mode="after")
    def _shape_by_status(self) -> "AssetCostsResponse":
        if self.status is AxisStatus.READY:
            if self.reason_code is not None or self.total is None:
                raise ValueError("READY 는 reason_code=null 이고 total 이 있어야 합니다")
            if [i.bucket for i in self.items] != list(COST_BUCKETS):
                raise ValueError("READY 의 items 는 네 칸을 COST_BUCKETS 순서로 싣습니다")
            if sum((i.amount for i in self.items), Decimal("0")) != self.total:
                raise ValueError("total 은 items 금액의 합이어야 합니다")
        else:
            if not self.reason_code or self.total is not None or self.items or self.estimated:
                raise ValueError("UNAVAILABLE 은 reason_code 만 싣습니다(total=null · items=[] · estimated=false)")
        return self
