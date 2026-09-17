"""AI 추정 단가에 근거한 절감 예상의 저장·조회 계약. 실제 청구액이 아니다 (#347)."""

from decimal import ROUND_HALF_UP, Decimal
from enum import Enum
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_serializer,
    field_validator,
    model_validator,
)

from .runbooks import RunbookId

Money = Annotated[Decimal, Field(ge=0, le=100_000_000, decimal_places=2)]
HourlyRate = Annotated[Decimal, Field(ge=0, le=1_000_000, decimal_places=6)]


# 기존 공유 계약과 같은 str + Enum 표현을 유지한다(StrEnum은 str() 결과가 다르다).
class SavingsStatus(str, Enum):  # noqa: UP042
    ESTIMATED = "ESTIMATED"
    UNAVAILABLE = "UNAVAILABLE"
    INVALID = "INVALID"


class SavingsReason(str, Enum):  # noqa: UP042
    MODEL_UNAVAILABLE = "MODEL_UNAVAILABLE"
    MISSING_ESTIMATE = "MISSING_ESTIMATE"
    INVALID_ESTIMATE = "INVALID_ESTIMATE"
    CONTEXT_MISMATCH = "CONTEXT_MISMATCH"


class SavingsExplanationSource(str, Enum):  # noqa: UP042
    SERVER_TEMPLATE = "SERVER_TEMPLATE"


class _SavingsModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    @field_validator("*", check_fields=False)
    @classmethod
    def _storable_text(cls, value):
        if isinstance(value, str):
            if "\x00" in value:
                raise ValueError("절감 예상에 NUL을 저장할 수 없습니다")
            value.encode("utf-8")
        return value


class SavingsAssumptions(_SavingsModel):
    """현재 자산의 관측 사실이 아니라 모든 추정에 적용하는 비교 가정."""

    hours: Literal[730] = 730
    operating_system: Literal["LINUX"] = "LINUX"
    tenancy: Literal["SHARED"] = "SHARED"
    purchase_option: Literal["ON_DEMAND"] = "ON_DEMAND"
    included_cost: Literal["INSTANCE_COMPUTE_ONLY"] = "INSTANCE_COMPUTE_ONLY"
    pricing_source: Literal["MODEL_KNOWLEDGE"] = "MODEL_KNOWLEDGE"


class RightsizingSavingsBasis(_SavingsModel):
    """서버의 비교 문맥, 모델 추정 단가, 출처가 명시된 설명을 함께 보존한다."""

    target_arn: str = Field(min_length=1, max_length=512)
    region: str = Field(min_length=1, max_length=64)
    current_instance_type: str = Field(min_length=1, max_length=64)
    target_instance_type: str = Field(min_length=1, max_length=64)
    current_hourly_rate: HourlyRate
    target_hourly_rate: HourlyRate
    assumptions: SavingsAssumptions = Field(default_factory=SavingsAssumptions)
    explanation: str = Field(min_length=1, max_length=1000)
    explanation_source: SavingsExplanationSource = Field(
        description="설명 작성 주체. 서버가 비교 조건·계산 방법·추정 한계를 작성한다.",
    )

    @field_validator("explanation")
    @classmethod
    def _meaningful_explanation(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("절감 예상의 계산 근거가 비어 있습니다")
        return value

    @field_serializer("current_hourly_rate", "target_hourly_rate")
    def _rate_as_string(self, value: Decimal) -> str:
        return format(value, ".6f")


class AISavingsEstimate(_SavingsModel):
    """단가는 AI 추정이며 서버 계산 후에도 금액은 예상값이다.

    대상·리전·사양·가정은 서버의 비교 문맥이다. 설명 작성 주체는
    basis.explanation_source로 구분하며 서버 문장이 모델의 이해를 입증하지 않는다.

    정상 추정의 금액은 (현재 단가 - 목표 단가) × 730, 소수 둘째 자리 반올림이다.
    생성 주체에 관계없이 저장·조회에 같은 산식을 요구한다. 정확한 AWS 요금인지는
    오프라인 평가 몫이며 이 검증이 증명하지 않는다.
    """

    status: SavingsStatus
    currency: Literal["USD"] = "USD"
    period: Literal["MONTH"] = "MONTH"
    amount: Money | None = None
    basis: RightsizingSavingsBasis | None = None
    reason: SavingsReason | None = None

    @field_serializer("amount")
    def _amount_as_string(self, value: Decimal | None) -> str | None:
        return None if value is None else format(value, ".2f")

    @model_validator(mode="after")
    def _shape_and_arithmetic(self):
        if self.status is SavingsStatus.ESTIMATED:
            if self.amount is None or self.basis is None or self.reason is not None:
                raise ValueError("ESTIMATED에는 금액·근거가 필요하고 사유 코드는 없습니다")
            expected = (
                (self.basis.current_hourly_rate - self.basis.target_hourly_rate)
                * self.basis.assumptions.hours
            ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            if self.amount != expected:
                raise ValueError("월 절감액이 단가 차이 × 가동 시간과 일치하지 않습니다")
        else:
            if self.amount is not None or self.basis is not None or self.reason is None:
                raise ValueError("미산출 상태에는 사유만 남기며 금액·근거는 null입니다")
            if (self.status is SavingsStatus.UNAVAILABLE) != (
                self.reason is SavingsReason.MODEL_UNAVAILABLE
            ):
                raise ValueError("모델의 산출 불가와 서버의 계약 오류를 구분해야 합니다")
        return self


def validate_candidate_savings(
    estimate: AISavingsEstimate | None,
    runbook_id: RunbookId,
    target_arn: str,
    target_instance_type: str | None,
) -> None:
    if estimate is None:
        return  # 기존 레코드 및 추정 대상이 아닌 후보
    if runbook_id is not RunbookId.RUNBOOK_EC2_RIGHTSIZING:
        raise ValueError("절감 예상은 EC2 RIGHTSIZING 후보에만 제공됩니다")
    if estimate.basis is not None and (
        estimate.basis.target_arn != target_arn
        or estimate.basis.target_instance_type != target_instance_type
    ):
        raise ValueError("절감 예상의 대상·목표 사양이 실행 후보와 다릅니다")
