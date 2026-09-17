"""평가 전용 후보 A. 서버 식별자는 입력에만 두고 설명과 두 단가를 생성한다."""

from decimal import ROUND_HALF_UP, Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationError
from schemas.agents import AgentAssetContext
from schemas.savings import (
    AISavingsEstimate,
    RightsizingSavingsBasis,
    SavingsReason,
    SavingsStatus,
)

from ai.model_client import AIModelRequest
from ai.savings import (
    SAVINGS_SYSTEM_PROMPT,
    HourlyRateText,
    invalid_estimate,
    savings_context,
)

PROMPT_VERSION = "candidate-A-v1"
SYSTEM_PROMPT = SAVINGS_SYSTEM_PROMPT.replace(
    "목표 타입은 서버가 결정한 값을 복사한다.",
    "목표 타입은 서버가 결정한 값을 사용한다.",
)


class ProposedHourlyRates(BaseModel):
    """서버가 준 EC2 변경 조건의 시간당 두 단가와 추정 근거를 작성한다.

    ESTIMATED는 USD 숫자 문자열 두 개, UNAVAILABLE는 두 단가 모두 null이다.
    월 절감액은 서버가 계산한다.
    """

    # SDK에 전달되는 이름·설명을 유지해 네 필드 제거 외의 스키마 차이를 막는다.
    model_config = ConfigDict(extra="forbid")

    status: Literal["ESTIMATED", "UNAVAILABLE"]
    explanation: str | None = None
    current_hourly_rate: HourlyRateText | None = None
    target_hourly_rate: HourlyRateText | None = None


def compact_request(asset: AgentAssetContext) -> AIModelRequest:
    context = savings_context(asset)
    if context is None:
        raise ValueError("단가 추정 대상 사양이 필요합니다")
    return AIModelRequest(system_prompt=SYSTEM_PROMPT, user_payload={"savings_context": context})


def accept_compact_rates(
    proposed: ProposedHourlyRates | None,
    asset: AgentAssetContext,
    *,
    diagnostics: list[dict] | None = None,
) -> AISavingsEstimate:
    """요청 문맥으로 저장 근거를 구성한다. 모델의 문맥 해석·가격 정확성을 검사하지 않는다."""
    def reject(reason: SavingsReason, issues: list[dict]) -> AISavingsEstimate:
        if diagnostics is not None:
            diagnostics.extend(issues)
        return invalid_estimate(reason)

    if proposed is None:
        return reject(SavingsReason.MISSING_ESTIMATE, [
            {"location": [], "rule": "MISSING_ESTIMATE"},
        ])
    try:
        proposed = ProposedHourlyRates.model_validate(proposed.model_dump())
    except ValidationError as exc:
        return reject(SavingsReason.INVALID_ESTIMATE, _issues(exc))

    context = savings_context(asset)
    if context is None:
        return reject(SavingsReason.CONTEXT_MISMATCH, [
            {"location": [], "rule": "MISSING_SERVER_CONTEXT"},
        ])
    rate_fields = ("current_hourly_rate", "target_hourly_rate")
    if proposed.status == "UNAVAILABLE":
        present = [key for key in rate_fields if getattr(proposed, key) is not None]
        if present:
            return reject(SavingsReason.INVALID_ESTIMATE, [
                {"location": [key], "rule": "UNAVAILABLE_REQUIRES_NULL"} for key in present
            ])
        return AISavingsEstimate(
            status=SavingsStatus.UNAVAILABLE, reason=SavingsReason.MODEL_UNAVAILABLE,
        )
    missing = [key for key in rate_fields if getattr(proposed, key) is None]
    if missing:
        return reject(SavingsReason.INVALID_ESTIMATE, [
            {"location": [key], "rule": "MISSING_RATE"} for key in missing
        ])
    try:
        basis = RightsizingSavingsBasis.model_validate({
            **{key: context[key] for key in (
                "target_arn", "region", "current_instance_type", "target_instance_type",
                "assumptions",
            )},
            "current_hourly_rate": proposed.current_hourly_rate,
            "target_hourly_rate": proposed.target_hourly_rate,
            "explanation": proposed.explanation,
        })
    except ValidationError as exc:
        return reject(SavingsReason.INVALID_ESTIMATE, _issues(exc, prefix=("basis",)))
    amount = (
        (basis.current_hourly_rate - basis.target_hourly_rate) * basis.assumptions.hours
    ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    try:
        return AISavingsEstimate(status=SavingsStatus.ESTIMATED, amount=amount, basis=basis)
    except ValidationError as exc:
        return reject(SavingsReason.INVALID_ESTIMATE, _issues(exc))


def _issues(exc: ValidationError, *, prefix: tuple[str, ...] = ()) -> list[dict]:
    return [
        {"location": [*prefix, *error["loc"]], "rule": error["type"]}
        for error in exc.errors(include_input=False, include_context=False, include_url=False)
    ]
