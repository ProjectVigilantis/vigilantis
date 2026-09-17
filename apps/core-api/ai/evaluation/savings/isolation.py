"""추천 업무를 제외한 절감 예상 진단. 서비스 그래프와 독립적인 실험 도우미다."""

import hashlib
import json
from collections import Counter
from decimal import Decimal

from schemas.agents import AgentAssetContext
from schemas.savings import AISavingsEstimate, SavingsAssumptions

from ai.evaluation.savings.legacy import _FINOPS_PROPOSAL_SYSTEM_PROMPT
from ai.evaluation.savings.scoring import score_estimate, scorer_version
from ai.model_client import AIModelClient, AIModelRequest
from ai.savings import ProposedSavingsEstimate, accept_savings_estimate, savings_context

PRICE_ONLY_PROMPT_VERSION = "v0.6.0"


def price_only_system_prompt() -> str:
    """현재 한국어 가격 지시를 재사용하고 추천 후보 작성 지시만 분리한다."""
    _, pricing = _FINOPS_PROPOSAL_SYSTEM_PROMPT.split(
        "RUNBOOK_EC2_RIGHTSIZING에는 ai_savings_estimate를 함께 작성한다. ", 1,
    )
    pricing, _ = pricing.rsplit("조치 후보는 유지한다. ", 1)
    pricing = pricing.rstrip().removesuffix("null로 두고") + "null로 둔다."
    return "너는 AWS EC2 사양 변경의 월 절감 예상과 산출 근거를 작성한다.\n" + pricing


def price_only_request(asset: AgentAssetContext) -> AIModelRequest:
    context = savings_context(asset)
    if context is None:
        raise ValueError("절감 예상 대상 사양이 필요합니다")
    return AIModelRequest(
        system_prompt=price_only_system_prompt(),
        user_payload={"savings_context": context},
    )


def run_price_only(asset: AgentAssetContext, *, client: AIModelClient) -> AISavingsEstimate:
    """동일한 내부 출력 타입·서버 문맥 검증으로 절감 예상 한 건만 받는다."""
    response = client.complete(price_only_request(asset), ProposedSavingsEstimate)
    return accept_savings_estimate(response.output, asset)


def price_only_prompt_fingerprint() -> str:
    material = "\n".join((
        price_only_system_prompt(),
        json.dumps(SavingsAssumptions().model_dump(), sort_keys=True),
        json.dumps(ProposedSavingsEstimate.model_json_schema(), ensure_ascii=False),
    ))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def score_price_only_report(raw: dict, spec: dict) -> dict:
    """진단 산출률·가격만 판정한다. 서비스 품질 기준선 판정을 만들지 않는다."""
    if raw["fixed_set"] != spec["fixed_set"]:
        raise ValueError("평가 입력 지문이 다릅니다")
    case_ids = raw["case_ids"]
    if not case_ids or len(set(case_ids)) != len(case_ids) or raw["repeats"] < 1:
        raise ValueError("진단 케이스와 반복 수가 필요합니다")
    if set(case_ids) - spec["cases"].keys() or any(
        run["case_id"] not in case_ids for run in raw["runs"]
    ):
        raise ValueError("선택한 평가 케이스 밖의 결과입니다")
    results = []
    for run in raw["runs"]:
        grade = (
            score_estimate(
                run["ai_savings_estimate"], spec["cases"][run["case_id"]],
                spec["price_reference"], spec["criteria"],
            )
            if run["response_status"] == "RETURNED"
            else {"status": "MODEL_CALL_FAILED"}
        )
        results.append({"case_id": run["case_id"], "repeat": run["repeat"], **grade})
    counts = Counter(item["status"] for item in results)
    identities = Counter((item["case_id"], item["repeat"]) for item in results)
    complete = identities == Counter(
        (case_id, repeat)
        for case_id in case_ids for repeat in range(1, raw["repeats"] + 1)
    )
    estimated = counts["PASS"] + counts["PRICE_DEVIATION"]
    coverage = Decimal(estimated) / len(results) if results else Decimal(0)
    return {
        "execution_mode": "price_only_diagnostic",
        "scorer_version": scorer_version(spec["criteria"]),
        "spec_version": spec["version"],
        "prompt_version": raw["prompt_version"],
        "prompt_sha256": raw["prompt_sha256"],
        "model_snapshots": raw["model_snapshots"],
        "fixed_set": spec["fixed_set"],
        "criteria": spec["criteria"],
        "probe_complete": complete,
        "probe_passed": (
            complete
            and coverage >= Decimal(spec["criteria"]["minimum_estimated_fraction"])
            and counts["PASS"] + counts["UNAVAILABLE"] == len(results)
        ),
        "service_baseline_evaluated": False,
        "runs": len(results),
        "estimated": estimated,
        "not_estimated": len(results) - estimated,
        "not_estimated_fraction": str(1 - coverage),
        "status_counts": dict(sorted(counts.items())),
        "results": results,
    }
