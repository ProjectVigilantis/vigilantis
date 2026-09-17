"""v0.9.0 단가 전용 후보. 월 절감액 산술은 서버 수용 함수가 소유한다."""

import hashlib
import json

from schemas.agents import AgentAssetContext
from schemas.savings import SavingsAssumptions

from ai.model_client import AIModelRequest
from ai.savings import ProposedHourlyRates, savings_context

RATE_ONLY_PROMPT_VERSION = "v0.9.0"

RATE_ONLY_SYSTEM_PROMPT = (
    "너는 AWS EC2 사양 변경을 비교할 시간당 단가와 추정 근거를 작성한다.\n"
    "savings_context의 대상·리전·현재/목표 타입과 과금 가정을 그대로 사용한다. "
    "목표 타입은 서버가 결정한 값을 복사한다.\n"
    "단가 식별: savings_context.region을 AWS 리전명과 연결하고, 그 리전의 "
    "current_instance_type과 target_instance_type에 해당하는 Linux 공유형 온디맨드 "
    "인스턴스 요금을 모델 지식에서 각각 찾는다. 기억한 단가의 리전·타입·과금 조건을 "
    "입력과 대조하고, 같은 리전·과금 조건에 맞는 현재/목표 단가를 한 쌍으로 사용한다. "
    "시간당 단가는 소수 6자리 이내 USD 숫자 문자열로 낸다.\n"
    "비교 범위: 인스턴스 컴퓨팅 요금만 포함한다. "
    "스토리지·네트워크·세금·할인·크레딧은 제외한다.\n"
    "explanation에는 적용한 리전명·현재/목표 타입·과금 조건·포함/제외 비용과 "
    "모델 지식 기반 추정이라는 한계를 한국어로 짧게 설명한다. "
    "두 단가는 추정값이며 실제 요금 조회 결과가 아니다. "
    "월 금액 계산은 서버가 담당하므로 단가와 비교 조건의 설명만 작성한다.\n"
    "입력 조건에 맞는 두 단가를 추정할 수 있으면 status=ESTIMATED로 낸다. "
    "어느 한 단가라도 추정할 수 없으면 status=UNAVAILABLE로 두 단가를 모두 null로 두고 "
    "explanation에 산출할 수 없는 이유를 짧게 적는다."
)


def rate_only_request(asset: AgentAssetContext) -> AIModelRequest:
    context = savings_context(asset)
    if context is None:
        raise ValueError("단가 추정 대상 사양이 필요합니다")
    return AIModelRequest(
        system_prompt=RATE_ONLY_SYSTEM_PROMPT, user_payload={"savings_context": context},
    )


def rate_only_prompt_fingerprint() -> str:
    material = "\n".join((
        RATE_ONLY_SYSTEM_PROMPT,
        json.dumps(SavingsAssumptions().model_dump(), sort_keys=True),
        json.dumps(ProposedHourlyRates.model_json_schema(), ensure_ascii=False),
    ))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()
