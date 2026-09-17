"""요약 → 추천 → 절감 예상 3호출 진단. 서비스 그래프의 기본 경로를 바꾸지 않는다."""

import hashlib
import json
from dataclasses import dataclass, field
from time import perf_counter
from typing import Literal

from schemas.agents import AgentGraphOutput, FinOpsGraphInput
from schemas.incidents import AgentInvocationStatus
from schemas.runbooks import RunbookId

from ai.agent import CandidateProposalOutput
from ai.evaluation.savings.isolation import (
    price_only_prompt_fingerprint,
    price_only_request,
)
from ai.evaluation.savings.legacy import (
    _FINOPS_PROPOSAL_SYSTEM_PROMPT,
    _FINOPS_SUMMARY_SYSTEM_PROMPT,
    _PARAMETER_CONSTRAINTS,
    EvidenceSummaryOutput,
    FinOpsCandidateProposalOutput,
    FinOpsProposedCandidate,
    _propose_candidates,
    _summarize_evidence,
    _validate_output_contract,
)
from ai.evaluation.savings.price_table import (
    ProposedTableSavings,
    accept_price_table,
    price_table_prompt_fingerprint,
    price_table_request,
)
from ai.evaluation.savings.rate_only import (
    rate_only_prompt_fingerprint,
    rate_only_request,
)
from ai.model_client import AIModelClient, AIModelError, AIModelRequest, AIModelResponse
from ai.savings import (
    ProposedHourlyRates,
    ProposedSavingsEstimate,
    accept_hourly_rates,
    accept_savings_estimate,
)

THREE_CALL_PROMPT_VERSION = "v0.7.0"


def recommendation_only_prompt() -> str:
    return _FINOPS_PROPOSAL_SYSTEM_PROMPT.split(
        "RUNBOOK_EC2_RIGHTSIZING에는 ai_savings_estimate를 함께 작성한다. ", 1,
    )[0].rstrip()


def three_call_prompt_fingerprint(*, price_mode: Literal["pair", "table", "rates"] = "pair") -> str:
    if price_mode not in {"pair", "table", "rates"}:
        raise ValueError("알 수 없는 가격 실험 방식입니다")
    price_fingerprint = {
        "pair": price_only_prompt_fingerprint,
        "table": price_table_prompt_fingerprint,
        "rates": rate_only_prompt_fingerprint,
    }[price_mode]
    material = [
        _FINOPS_SUMMARY_SYSTEM_PROMPT,
        recommendation_only_prompt(),
        EvidenceSummaryOutput.model_json_schema(),
        CandidateProposalOutput.model_json_schema(),
        {key.value: list(value) for key, value in _PARAMETER_CONSTRAINTS.items()},
        price_fingerprint(),
    ]
    return hashlib.sha256(json.dumps(material, ensure_ascii=False).encode("utf-8")).hexdigest()


class _RecordingClient:
    def __init__(self, inner: AIModelClient):
        self.inner = inner
        self.calls: list[dict] = []

    def complete(self, request, response_model):
        stages = {
            EvidenceSummaryOutput: "summary",
            CandidateProposalOutput: "recommendation",
            ProposedSavingsEstimate: "savings",
            ProposedTableSavings: "savings",
            ProposedHourlyRates: "savings",
        }
        if response_model not in stages or len(self.calls) >= 3:
            raise ValueError("3호출 진단의 호출 순서·예산 밖입니다")
        stage = stages[response_model]
        if stage != ("summary", "recommendation", "savings")[len(self.calls)]:
            raise ValueError("3호출 진단의 단계 순서가 다릅니다")
        item = {"number": len(self.calls) + 1, "stage": stage, "status": "STARTED"}
        self.calls.append(item)
        start = perf_counter()
        try:
            response = self.inner.complete(request, response_model)
        except AIModelError as exc:
            item.update(status="ERROR", error=type(exc).__name__, error_phase=exc.phase)
            usage = exc.usage
            raise
        else:
            item.update(status="RETURNED", model=response.model)
            usage = response.usage
            return response
        finally:
            item["elapsed_seconds"] = round(perf_counter() - start, 3)
            # 예상하지 못한 실행기 오류는 계속 실행하지 않고 호출자에게 전파한다.
            if item["status"] != "STARTED":
                item["usage"] = None if usage is None else {
                    "prompt_tokens": usage.prompt_tokens,
                    "completion_tokens": usage.completion_tokens,
                    "cached_prompt_tokens": usage.cached_prompt_tokens,
                }


class _RecommendationClient:
    """기존 노드를 재사용하며 실제 두 번째 요청에서 가격 업무·출력 필드를 제거한다."""

    def __init__(self, inner: _RecordingClient):
        self.inner = inner

    def complete(self, request, response_model):
        if response_model is not FinOpsCandidateProposalOutput:
            return self.inner.complete(request, response_model)
        payload = dict(request.user_payload)
        payload.pop("savings_context", None)
        response = self.inner.complete(AIModelRequest(
            system_prompt=recommendation_only_prompt(), user_payload=payload,
        ), CandidateProposalOutput)
        return AIModelResponse(
            output=FinOpsCandidateProposalOutput(candidates=[
                FinOpsProposedCandidate(**candidate.model_dump())
                for candidate in response.output.candidates
            ]),
            usage=response.usage, model=response.model,
        )


@dataclass
class ThreeCallResult:
    output: AgentGraphOutput
    calls: list[dict]
    elapsed_seconds: float
    price_stage: str = "NOT_RUN"
    savings_diagnostics: list[dict] = field(default_factory=list)
    price_table: dict[str, str | None] | None = None
    table_diagnostics: list[dict] = field(default_factory=list)

    def record(self, *, case_id: str, repeat: int) -> dict:
        return {
            "case_id": case_id, "repeat": repeat,
            **self.output.model_dump(mode="json"),
            "execution_mode": "three_call_diagnostic",
            "price_stage": self.price_stage,
            "savings_diagnostics": self.savings_diagnostics,
            "calls": self.calls,
            "elapsed_seconds": self.elapsed_seconds,
            **({"price_table": self.price_table, "table_diagnostics": self.table_diagnostics}
               if self.price_table is not None else {}),
        }


def run_three_call(
    graph_input: FinOpsGraphInput, *, client: AIModelClient,
    price_mode: Literal["pair", "table", "rates"] = "pair",
) -> ThreeCallResult:
    """앞선 후보가 유효하고 RIGHTSIZING을 선택한 경우에만 세 번째 호출을 실행한다."""
    start = perf_counter()
    if price_mode not in {"pair", "table", "rates"}:
        raise ValueError("알 수 없는 가격 실험 방식입니다")
    table_request = price_table_request(graph_input.asset_context) if price_mode == "table" else None
    rates_request = rate_only_request(graph_input.asset_context) if price_mode == "rates" else None
    recording = _RecordingClient(client)
    state = {"graph_input": graph_input, "client": _RecommendationClient(recording)}
    state.update(_summarize_evidence(state))
    if not state.get("failure"):
        state.update(_propose_candidates(state))
    output = _validate_output_contract(state)["output"]
    result = ThreeCallResult(output=output, calls=recording.calls, elapsed_seconds=0)
    targets = [c for c in output.candidates if c.runbook_id is RunbookId.RUNBOOK_EC2_RIGHTSIZING]
    if output.invocation_status is AgentInvocationStatus.SUCCEEDED and targets:
        candidate = targets[0]  # 중복 런북은 앞선 AgentGraphOutput 검증이 거절한다.
        try:
            response = recording.complete(
                table_request or rates_request or price_only_request(graph_input.asset_context),
                {"table": ProposedTableSavings, "rates": ProposedHourlyRates,
                 "pair": ProposedSavingsEstimate}[price_mode],
            )
        except AIModelError:
            result.price_stage = "ERROR"
        else:
            result.price_stage = "RETURNED"
            if price_mode == "table":
                accepted = accept_price_table(response.output, graph_input.asset_context)
                estimate = accepted.estimate
                result.price_table = accepted.hourly_rates
                result.table_diagnostics = accepted.table_diagnostics
                result.savings_diagnostics = accepted.savings_diagnostics
            elif price_mode == "rates":
                estimate = accept_hourly_rates(
                    response.output, graph_input.asset_context,
                    diagnostics=result.savings_diagnostics,
                )
            else:
                estimate = accept_savings_estimate(
                    response.output, graph_input.asset_context,
                    diagnostics=result.savings_diagnostics,
                )
            candidates = [
                c.model_copy(update={"ai_savings_estimate": estimate}) if c is candidate else c
                for c in output.candidates
            ]
            result.output = AgentGraphOutput.model_validate({
                **output.model_dump(), "candidates": [c.model_dump() for c in candidates],
            })
    result.elapsed_seconds = round(perf_counter() - start, 3)
    return result
