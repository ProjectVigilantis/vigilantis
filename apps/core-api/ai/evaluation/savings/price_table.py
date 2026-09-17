"""같은 조건의 T3 가격표를 먼저 생성하는 v0.8.0 실험. 목표 사양은 서버가 정한다."""

import hashlib
import json
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError
from schemas.agents import AgentAssetContext
from schemas.savings import AISavingsEstimate, HourlyRate

from ai.model_client import AIModelRequest
from ai.savings import ProposedSavingsEstimate, accept_savings_estimate, savings_context

PRICE_TABLE_PROMPT_VERSION = "v0.8.0"
TABLE_INSTANCE_TYPES = ("t3.small", "t3.medium", "t3.large", "t3.xlarge")


class ProposedT3HourlyRates(BaseModel):
    """입력과 같은 리전·과금 조건에 속하는 시간당 USD 추정값. 실제 조회 가격이 아니다."""

    model_config = ConfigDict(extra="forbid")

    small: str | None = Field(default=None, alias="t3.small")
    medium: str | None = Field(default=None, alias="t3.medium")
    large: str | None = Field(default=None, alias="t3.large")
    xlarge: str | None = Field(default=None, alias="t3.xlarge")


class ProposedTableSavings(BaseModel):
    """가격표 → 공개 근거 → 월 금액 순서. 대상과 전후 사양은 서버 입력을 복사한다."""

    model_config = ConfigDict(extra="forbid")

    status: str
    target_arn: str | None = None
    region: str | None = None
    current_instance_type: str | None = None
    target_instance_type: str | None = None
    hourly_rates: ProposedT3HourlyRates
    explanation: str | None = None
    amount: str | None = None


PRICE_TABLE_SYSTEM_PROMPT = (
    "너는 서버가 확정한 AWS EC2 사양 변경의 월 절감 예상과 산출 근거를 작성한다.\n"
    "savings_context의 대상·리전·현재/목표 타입과 과금 가정을 그대로 사용한다. "
    "목표 타입은 서버가 결정한 값이며 출력에도 그대로 복사한다.\n"
    "가격표 구성: savings_context.region을 AWS 리전명과 연결하고, 그 리전의 "
    "Linux 공유형 온디맨드 인스턴스 요금을 모델 지식에서 찾는다. "
    "table_instance_types의 사양들을 같은 리전·과금 조건에 속하는 한 가격표로 구성해 "
    "hourly_rates에 먼저 적는다. 각 행은 그 사양의 시간당 USD 단가이며 소수 6자리 이내 "
    "문자열로 쓴다. 각 행의 리전·사양·과금 조건이 같은 가격표에 속하는지 대조한다.\n"
    "월 차액 계산: 서버가 준 current_instance_type과 target_instance_type에 해당하는 "
    "두 행을 hourly_rates에서 찾아 (현재 단가-목표 단가)×730을 계산한다. "
    "마지막 월 금액만 소수 둘째 자리로 반올림해 amount 문자열로 낸다. "
    "인스턴스 컴퓨팅 요금만 포함하며 제외 비용은 스토리지·네트워크·세금·할인·크레딧이다.\n"
    "explanation에는 서버가 정한 변경 전후 사양의 단가, 적용한 리전명·과금 조건과 계산식, "
    "모델 지식 기반 추정이라는 한계를 한국어로 짧게 설명한다. "
    "출처는 요금 API·청구서 조회가 아닌 모델 지식이다.\n"
    "현재·목표 두 단가를 추정할 수 있으면 status=ESTIMATED로 낸다. "
    "그 외 행의 단가를 추정할 수 없으면 그 행만 null로 둔다. "
    "현재·목표 중 어느 한 단가라도 추정할 수 없으면 status=UNAVAILABLE로 "
    "현재·목표의 두 행과 amount를 null로 둔다."
)


def _table_context(asset: AgentAssetContext) -> dict:
    context = savings_context(asset)
    if context is None or any(context[key] not in TABLE_INSTANCE_TYPES for key in (
        "current_instance_type", "target_instance_type",
    )):
        raise ValueError("이 가격표 실험은 서버가 정한 T3 네 사양 범위에만 적용합니다")
    return context


def price_table_request(asset: AgentAssetContext) -> AIModelRequest:
    return AIModelRequest(
        system_prompt=PRICE_TABLE_SYSTEM_PROMPT,
        user_payload={"savings_context": _table_context(asset),
                      "table_instance_types": list(TABLE_INSTANCE_TYPES)},
    )


def price_table_prompt_fingerprint() -> str:
    material = [PRICE_TABLE_SYSTEM_PROMPT, list(TABLE_INSTANCE_TYPES),
                ProposedTableSavings.model_json_schema()]
    return hashlib.sha256(json.dumps(material, ensure_ascii=False).encode("utf-8")).hexdigest()


@dataclass
class AcceptedTableSavings:
    estimate: AISavingsEstimate
    hourly_rates: dict[str, str | None]
    table_diagnostics: list[dict]
    savings_diagnostics: list[dict]


def accept_price_table(
    proposed: ProposedTableSavings, asset: AgentAssetContext,
) -> AcceptedTableSavings:
    context = _table_context(asset)
    rates = proposed.hourly_rates.model_dump(by_alias=True)
    # 행을 찾는 키는 서버가 결정한 현재·목표 사양이다.
    selected = ProposedSavingsEstimate(
        status=proposed.status, target_arn=proposed.target_arn, region=proposed.region,
        current_instance_type=proposed.current_instance_type,
        target_instance_type=proposed.target_instance_type,
        current_hourly_rate=rates[context["current_instance_type"]],
        target_hourly_rate=rates[context["target_instance_type"]],
        explanation=proposed.explanation, amount=proposed.amount,
    )
    savings_diagnostics: list[dict] = []
    estimate = accept_savings_estimate(selected, asset, diagnostics=savings_diagnostics)

    # 실험 표도 유효한 숫자만 보존한다. 거절된 원래 값·예외 메시지는 남기지 않는다.
    adapter = TypeAdapter(HourlyRate, config=ConfigDict(allow_inf_nan=False))
    safe_rates: dict[str, str | None] = {}
    table_diagnostics: list[dict] = []
    for name, value in rates.items():
        safe_rates[name] = None
        if value is None:
            continue
        try:
            rate = adapter.validate_python(value)
        except ValidationError as exc:
            table_diagnostics.extend({"location": ["hourly_rates", name], "rule": item["type"]}
                                     for item in exc.errors(include_input=False, include_context=False,
                                                            include_url=False))
        else:
            safe_rates[name] = format(rate, ".6f")
    return AcceptedTableSavings(estimate, safe_rates, table_diagnostics, savings_diagnostics)
