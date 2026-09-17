"""가드레일 통과 후보의 단가를 추정하고 서버가 월 절감액을 계산한다. DB 접근은 없다."""

import hashlib
import json
import logging
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from schemas.agents import AgentAssetContext
from schemas.candidates import RunbookCandidateData
from schemas.rightsizing_policy import rightsizing_target_type
from schemas.runbooks import RunbookId
from schemas.savings import (
    AISavingsEstimate,
    RightsizingSavingsBasis,
    SavingsAssumptions,
    SavingsReason,
    SavingsStatus,
)

from ai.model_client import AIModelClient, AIModelError, AIModelRequest

SAVINGS_MODEL_CALLS = 1  # 같은 runbook_id의 후보는 하나뿐이다(AgentGraphOutput).
SAVINGS_PROMPT_VERSION = "v0.9.1"
_logger = logging.getLogger(__name__)


class ProposedSavingsEstimate(BaseModel):
    """RIGHTSIZING에만 작성한다. 금액 문자열은 USD, 월 730시간 기준이다.

    ESTIMATED면 서버가 준 대상·리전·변경 전후 사양과 시간당 두 단가, 월 절감액,
    짧은 계산 근거를 채운다. 단가를 추정할 수 없으면 UNAVAILABLE와 null 금액을 낸다.
    수치·문맥 검증은 후보 계약과 독립적으로 수행해 추정 오류가 조치를 없애지 않게 한다.
    """

    model_config = ConfigDict(extra="forbid")

    status: str
    target_arn: str | None = None
    region: str | None = None
    current_instance_type: str | None = None
    target_instance_type: str | None = None
    explanation: str | None = None
    current_hourly_rate: str | None = None
    target_hourly_rate: str | None = None
    amount: str | None = None


HourlyRateText = Annotated[
    str, Field(pattern=r"^(0|[1-9][0-9]{0,6})(\.[0-9]{1,6})?$"),
]


class ProposedHourlyRates(BaseModel):
    """서버가 준 EC2 변경 조건의 시간당 두 단가와 추정 근거를 작성한다.

    ESTIMATED는 USD 숫자 문자열 두 개, UNAVAILABLE는 두 단가 모두 null이다.
    월 절감액은 서버가 계산한다.
    """

    model_config = ConfigDict(extra="forbid")

    status: Literal["ESTIMATED", "UNAVAILABLE"]
    target_arn: str
    region: str
    current_instance_type: str
    target_instance_type: str
    explanation: str | None = None
    current_hourly_rate: HourlyRateText | None = None
    target_hourly_rate: HourlyRateText | None = None


def accept_hourly_rates(
    proposed: ProposedHourlyRates | None, asset: AgentAssetContext,
    *, diagnostics: list[dict] | None = None,
) -> AISavingsEstimate:
    """단가의 형식·문맥을 수용한 뒤 계산한다. 실제 AWS 단가 정확성은 보장하지 않는다."""
    def reject(reason: SavingsReason, issues: list[dict]) -> AISavingsEstimate:
        if diagnostics is not None:
            diagnostics.extend(issues)
        return invalid_estimate(reason)

    if proposed is None:
        return reject(SavingsReason.MISSING_ESTIMATE, [
            {"location": [], "rule": "MISSING_ESTIMATE"},
        ])
    try:
        # SDK 밖의 model_copy/construct 경로도 동일한 계약을 통과시킨다.
        proposed = ProposedHourlyRates.model_validate(proposed.model_dump())
    except ValidationError as exc:
        return reject(SavingsReason.INVALID_ESTIMATE, [
            {"location": list(error["loc"]), "rule": error["type"]}
            for error in exc.errors(include_input=False, include_context=False, include_url=False)
        ])
    context = savings_context(asset)
    if context is None:
        return reject(SavingsReason.CONTEXT_MISMATCH, [
            {"location": [], "rule": "MISSING_SERVER_CONTEXT"},
        ])
    mismatched = [key for key in (
        "target_arn", "region", "current_instance_type", "target_instance_type"
    ) if getattr(proposed, key) != context[key]]
    if mismatched:
        return reject(SavingsReason.CONTEXT_MISMATCH, [
            {"location": [key], "rule": "CONTEXT_MISMATCH"} for key in mismatched
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
                "target_arn", "region", "current_instance_type", "target_instance_type"
            )},
            "assumptions": context["assumptions"],
            "current_hourly_rate": proposed.current_hourly_rate,
            "target_hourly_rate": proposed.target_hourly_rate,
            "explanation": proposed.explanation,
        })
    except ValidationError as exc:
        return reject(SavingsReason.INVALID_ESTIMATE, [
            {"location": ["basis", *error["loc"]], "rule": error["type"]}
            for error in exc.errors(include_input=False, include_context=False, include_url=False)
        ])
    amount = (
        (basis.current_hourly_rate - basis.target_hourly_rate) * basis.assumptions.hours
    ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    try:
        return AISavingsEstimate(status=SavingsStatus.ESTIMATED, amount=amount, basis=basis)
    except ValidationError as exc:
        return reject(SavingsReason.INVALID_ESTIMATE, [
            {"location": list(error["loc"]), "rule": error["type"]}
            for error in exc.errors(include_input=False, include_context=False, include_url=False)
        ])


def savings_context(asset: AgentAssetContext) -> dict | None:
    current = getattr(asset.spec, "instance_type", None)
    target = rightsizing_target_type(current)
    if target is None:
        return None
    return {
        "target_arn": asset.arn,
        "region": asset.region,
        "current_instance_type": current,
        "target_instance_type": target,
        "currency": "USD",
        "period": "MONTH",
        "assumptions": SavingsAssumptions().model_dump(mode="json"),
    }


def invalid_estimate(reason: SavingsReason) -> AISavingsEstimate:
    return AISavingsEstimate(status=SavingsStatus.INVALID, reason=reason)


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



def accept_savings_estimate(
    proposed: ProposedSavingsEstimate | None, asset: AgentAssetContext,
    *, diagnostics: list[dict] | None = None,
) -> AISavingsEstimate:
    """선택적 실험 진단에는 필드·규칙만 남긴다. 입력값·예외 메시지는 보존하지 않는다."""
    def reject(reason: SavingsReason, issues: list[dict]) -> AISavingsEstimate:
        if diagnostics is not None:
            diagnostics.extend(issues)
        return invalid_estimate(reason)

    if proposed is None:
        return reject(SavingsReason.MISSING_ESTIMATE, [
            {"location": [], "rule": "MISSING_ESTIMATE"},
        ])
    if proposed.status == "UNAVAILABLE":
        present = [name for name in ("amount", "current_hourly_rate", "target_hourly_rate")
                   if getattr(proposed, name) is not None]
        if present:
            return reject(SavingsReason.INVALID_ESTIMATE, [
                {"location": [name], "rule": "UNAVAILABLE_REQUIRES_NULL"} for name in present
            ])
        return AISavingsEstimate(
            status=SavingsStatus.UNAVAILABLE, reason=SavingsReason.MODEL_UNAVAILABLE
        )
    if proposed.status != "ESTIMATED":
        return reject(SavingsReason.INVALID_ESTIMATE, [
            {"location": ["status"], "rule": "UNKNOWN_STATUS"},
        ])
    context = savings_context(asset)
    if context is None:
        return reject(SavingsReason.CONTEXT_MISMATCH, [
            {"location": [], "rule": "MISSING_SERVER_CONTEXT"},
        ])
    mismatched = [key for key in (
        "target_arn", "region", "current_instance_type", "target_instance_type"
    ) if getattr(proposed, key) != context[key]]
    if mismatched:
        return reject(SavingsReason.CONTEXT_MISMATCH, [
            {"location": [key], "rule": "CONTEXT_MISMATCH"} for key in mismatched
        ])
    try:
        return AISavingsEstimate.model_validate({
            "status": "ESTIMATED",
            "amount": proposed.amount,
            "basis": {
                key: value for key, value in proposed.model_dump().items()
                if key not in ("status", "amount")
            },
        })
    except ValidationError as exc:
        issues = []
        for error in exc.errors(include_input=False, include_context=False, include_url=False):
            rule = error["type"]
            if not error["loc"] and rule == "value_error":
                # ESTIMATED를 위에서 고정하므로 이 모델의 root invariant는 두 갈래다.
                rule = "MISSING_AMOUNT" if proposed.amount is None else "ARITHMETIC_MISMATCH"
            issues.append({"location": list(error["loc"]), "rule": rule})
        return reject(SavingsReason.INVALID_ESTIMATE, issues)
    except ValueError:
        return reject(SavingsReason.INVALID_ESTIMATE, [
            {"location": [], "rule": "VALUE_ERROR"},
        ])


SAVINGS_SYSTEM_PROMPT = (
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
    "\n공개 근거의 필드명과 상태 코드는 한국어로 풀어 쓴다. 예를 들어 "
    "pricing_source=MODEL_KNOWLEDGE는 모델 지식 기반 추정이라고 표현한다."
)


def savings_request(asset: AgentAssetContext) -> AIModelRequest:
    context = savings_context(asset)
    if context is None:
        raise ValueError("단가 추정 대상 사양이 필요합니다")
    return AIModelRequest(system_prompt=SAVINGS_SYSTEM_PROMPT, user_payload={"savings_context": context})


def savings_prompt_fingerprint() -> str:
    material = "\n".join((
        SAVINGS_SYSTEM_PROMPT,
        json.dumps(SavingsAssumptions().model_dump(), sort_keys=True),
        json.dumps(ProposedHourlyRates.model_json_schema(), ensure_ascii=False),
    ))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()
