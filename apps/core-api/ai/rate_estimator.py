"""PASS 후보의 단가만 모델이 추정한다. 서버가 비교 문맥·금액·설명을 구성한다 (#347).

실험 B의 요청을 바이트 단위로 유지한 서비스 v1.0.0이다.
이전 후보와 실험 자료는 ai.savings 및 ai.evaluation.savings에 보존한다.
"""

import hashlib
import json
import logging
from decimal import ROUND_HALF_UP, Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationError
from schemas.agents import AgentAssetContext
from schemas.candidates import RunbookCandidateData
from schemas.runbooks import RunbookId
from schemas.savings import (
    AISavingsEstimate,
    RightsizingSavingsBasis,
    SavingsAssumptions,
    SavingsExplanationSource,
    SavingsReason,
    SavingsStatus,
)

from ai.model_client import AIModelClient, AIModelError, AIModelRequest
from ai.savings import HourlyRateText, invalid_estimate, savings_context

SAVINGS_MODEL_CALLS = 1
SAVINGS_PROMPT_VERSION = "v1.0.0"
_logger = logging.getLogger(__name__)
SAVINGS_SYSTEM_PROMPT = (
    '너는 AWS EC2 사양 변경을 비교할 시간당 단가를 작성한다.\n'
    'savings_context의 대상·리전·현재/목표 타입과 과금 가정을 그대로 사용한다. 목표 타입은 서버가 결정한 값을 사용한다.\n'
    '단가 식별: savings_context.region을 AWS 리전명과 연결하고, 그 리전의 current_instance_type과 target_instance_type에 해당하는 Linux 공유형 온디맨드 인스턴스 요금을 모델 지식에서 각각 찾는다. 기억한 단가의 리전·타입·과금 조건을 입력과 대조하고, 같은 리전·과금 조건에 맞는 현재/목표 단가를 한 쌍으로 사용한다. 시간당 단가는 소수 6자리 이내 USD 숫자 문자열로 낸다.\n'
    '비교 범위: 인스턴스 컴퓨팅 요금만 포함한다. 스토리지·네트워크·세금·할인·크레딧은 제외한다.\n'
    '두 단가는 추정값이며 실제 요금 조회 결과가 아니다. 월 금액 계산은 서버가 담당한다.\n'
    '입력 조건에 맞는 두 단가를 추정할 수 있으면 status=ESTIMATED로 낸다. 어느 한 단가라도 추정할 수 없으면 status=UNAVAILABLE로 두 단가를 모두 null로 둔다.'
)


class ProposedHourlyRates(BaseModel):
    """서버가 준 EC2 변경 조건의 시간당 두 단가를 작성한다.

    ESTIMATED는 USD 숫자 문자열 두 개, UNAVAILABLE는 두 단가 모두 null이다.
    월 절감액은 서버가 계산한다.
    """

    model_config = ConfigDict(extra="forbid")

    status: Literal["ESTIMATED", "UNAVAILABLE"]
    current_hourly_rate: HourlyRateText | None = None
    target_hourly_rate: HourlyRateText | None = None


def savings_request(asset: AgentAssetContext) -> AIModelRequest:
    context = savings_context(asset)
    if context is None:
        raise ValueError("단가 추정 대상 사양이 필요합니다")
    return AIModelRequest(system_prompt=SAVINGS_SYSTEM_PROMPT, user_payload={"savings_context": context})


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


def accept_hourly_rates(
    proposed: ProposedHourlyRates | None,
    asset: AgentAssetContext,
    *,
    diagnostics: list[dict] | None = None,
) -> AISavingsEstimate:
    """추정 단가를 검증한 뒤 서버 문맥·설명과 결합한다. 단가의 정확성은 보장하지 않는다."""
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
            "explanation_source": SavingsExplanationSource.SERVER_TEMPLATE,
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


def estimate_candidate_savings(
    candidate: RunbookCandidateData, *, asset: AgentAssetContext, client: AIModelClient,
) -> AISavingsEstimate:
    """Workflow가 PASS 뒤 호출한다. 입력 스냅샷과 후보를 결속하고 추정만 반환한다.

    처리 가능한 모델 오류는 추정 실패로 남긴다. 구현 결함·DB 오류를 숨기지 않으며
    실행 명령·후보 상태를 변경하지 않는다. 입력은 청구 실측값이 아닌 분석 스냅샷이다.
    """
    context = savings_context(asset)
    if (candidate.runbook_id is not RunbookId.RUNBOOK_EC2_RIGHTSIZING
            or context is None or candidate.target_arn != context["target_arn"]
            or getattr(candidate.parameters, "target_instance_type", None)
            != context["target_instance_type"]):
        return invalid_estimate(SavingsReason.CONTEXT_MISMATCH)
    try:
        response = client.complete(savings_request(asset), ProposedHourlyRates)
    except AIModelError as exc:
        # 공급자 원문·traceback을 남기지 않는다. 저장 상태와 호출 오류 분류는 별개다.
        _logger.warning("FinOps 단가 추정 호출 실패: %s", type(exc).__name__)
        return invalid_estimate(SavingsReason.MISSING_ESTIMATE)
    return accept_hourly_rates(response.output, asset)


def savings_prompt_fingerprint() -> str:
    material = "\n".join((
        SAVINGS_SYSTEM_PROMPT,
        json.dumps(SavingsAssumptions().model_dump(), sort_keys=True),
        json.dumps(ProposedHourlyRates.model_json_schema(), ensure_ascii=False),
    ))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()
