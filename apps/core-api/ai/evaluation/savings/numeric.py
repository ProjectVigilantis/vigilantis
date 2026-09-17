"""평가 전용 후보 B. AI는 상태·두 단가만 생성하고 설명은 서버가 구성한다."""

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

from ai.evaluation.savings.compact import SYSTEM_PROMPT as A_SYSTEM_PROMPT
from ai.model_client import AIModelRequest
from ai.savings import HourlyRateText, invalid_estimate, savings_context

PROMPT_VERSION = "candidate-B-v1"
EXPLANATION_SOURCE = "SERVER_TEMPLATE"
PROMPT_EDITS = (
    ("시간당 단가와 추정 근거를 작성한다.", "시간당 단가를 작성한다."),
    (
        (
            "explanation에는 적용한 리전명·현재/목표 타입·과금 조건·포함/제외 비용과 "
            "모델 지식 기반 추정이라는 한계를 한국어로 짧게 설명한다. "
        ),
        "",
    ),
    (
        "월 금액 계산은 서버가 담당하므로 단가와 비교 조건의 설명만 작성한다.",
        "월 금액 계산은 서버가 담당한다.",
    ),
    (
        (
            "status=UNAVAILABLE로 두 단가를 모두 null로 두고 "
            "explanation에 산출할 수 없는 이유를 짧게 적는다."
        ),
        "status=UNAVAILABLE로 두 단가를 모두 null로 둔다.",
    ),
    (
        (
            "\n공개 근거의 필드명과 상태 코드는 한국어로 풀어 쓴다. 예를 들어 "
            "pricing_source=MODEL_KNOWLEDGE는 모델 지식 기반 추정이라고 표현한다."
        ),
        "",
    ),
)


def without_explanation_instructions(prompt: str) -> str:
    for before, after in PROMPT_EDITS:
        if prompt.count(before) != 1:
            raise ValueError("EXPLANATION_INSTRUCTION_DRIFT")
        prompt = prompt.replace(before, after)
    return prompt


SYSTEM_PROMPT = without_explanation_instructions(A_SYSTEM_PROMPT)


class ProposedHourlyRates(BaseModel):
    """서버가 준 EC2 변경 조건의 시간당 두 단가를 작성한다.

    ESTIMATED는 USD 숫자 문자열 두 개, UNAVAILABLE는 두 단가 모두 null이다.
    월 절감액은 서버가 계산한다.
    """

    model_config = ConfigDict(extra="forbid")

    status: Literal["ESTIMATED", "UNAVAILABLE"]
    current_hourly_rate: HourlyRateText | None = None
    target_hourly_rate: HourlyRateText | None = None


def numeric_request(asset: AgentAssetContext) -> AIModelRequest:
    context = savings_context(asset)
    if context is None:
        raise ValueError("단가 추정 대상 사양이 필요합니다")
    return AIModelRequest(system_prompt=SYSTEM_PROMPT, user_payload={"savings_context": context})


def server_explanation(context: dict) -> str:
    """서버가 정한 비교 조건을 서술한다. 모델의 기억·문맥 해석을 입증하지 않는다."""
    return (
        f"{context['region']} 리전의 {context['current_instance_type']}에서 "
        f"{context['target_instance_type']}으로 변경하는 비교입니다. "
        "Linux·공유형·온디맨드 컴퓨팅 요금만 포함합니다. "
        "두 시간당 단가는 모델 지식 기반 추정값이며 실제 요금 조회 결과가 아닙니다. "
        "서버가 두 단가의 차이에 월 730시간을 곱했습니다. "
        "스토리지·네트워크·세금·할인·크레딧은 제외합니다."
    )


def accept_numeric_rates(
    proposed: ProposedHourlyRates | None,
    asset: AgentAssetContext,
    *,
    diagnostics: list[dict] | None = None,
) -> AISavingsEstimate:
    """기존 채점 형식으로 변환한다. 결과 장부에는 설명 출처 SERVER_TEMPLATE를 별도 기록한다."""
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
            "explanation": server_explanation(context),
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
