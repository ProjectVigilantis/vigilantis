"""기존 v0.3~v0.9 실험의 FinOps 요청·수용 경로. 서비스는 이 모듈을 import하지 않는다."""

import hashlib
import json
from typing import Any

from pydantic import ValidationError
from schemas.agents import AgentGraphOutput, FinOpsGraphInput, RunbookCandidateDraft
from schemas.incidents import AgentInvocationStatus
from schemas.runbooks import RunbookId
from schemas.savings import SavingsAssumptions

from ai.agent import (
    _PARAMETER_CONSTRAINTS,
    EvidenceSummaryOutput,
    FinOpsCandidateProposalOutput,
    FinOpsProposedCandidate,
    _allowed_target_arns,
    _canonical_evidence_ids,
    _failed_output,
    _FinOpsState,
    _parameter_values,
    _summary_payload,
)
from ai.agent import (
    _proposal_payload as _runtime_proposal_payload,
)
from ai.model_client import AIModelError, AIModelRequest
from ai.savings import accept_savings_estimate, savings_context


def _proposal_payload(graph_input, summary_lines):
    payload = _runtime_proposal_payload(graph_input, summary_lines)
    if any(c.runbook_id is RunbookId.RUNBOOK_EC2_RIGHTSIZING for c in graph_input.capabilities):
        payload["savings_context"] = savings_context(graph_input.asset_context)
    return payload


_FINOPS_SUMMARY_SYSTEM_PROMPT = (
    "너는 AWS 비용 최적화 인시던트를 관제자에게 설명한다. 관제자는 이 세 줄과 조치 카드만 "
    "보고 승인할지 차단할지 정한다. 아래 세 줄을 각각 한국어 한 문장으로 쓴다.\n"
    "verdict는 규칙 엔진이 이미 내린 판정이다. 요약은 그 판정이 어떤 값에서 나왔는지를 "
    "보여 주는 것이고, 판정을 다시 내리는 자리는 승인 단계의 관제자다. 입력에 없는 값(예: "
    "최대 사용률 없음)은 없다고 적되, 규칙이 그 값 없이 내린 판정을 그대로 설명한다.\n"
    "observation: 이 판정의 근거가 된 입력 사실을 쓴다. 인용하는 숫자·식별자·기간은 입력에 "
    "있는 값을 그대로 옮기고, 판정의 문턱이 된 값(평균·최대 사용률, 관측 기간과 데이터포인트 "
    "수)을 포함한다.\n"
    "diagnosis: 그 사실이 자산 상태에 대해 무엇을 뜻하는지 쓴다. 입력이 확정한 것은 단정으로, "
    "입력에서 추론한 것은 추정으로 구분해 쓴다.\n"
    "rationale: available_actions 중 observation과 diagnosis가 뒷받침하는 조치 하나에 대해 "
    "그 조치가 이 자산에 맞는 이유를 쓴다. 다른 조치는 다루지 않고, 조치의 이름과 값은 조치 "
    "카드가 보여 준다. 입력의 판정이 후보가 아니라고 하면 왜 조치가 없는지를 쓴다.\n"
    "세 줄은 각각 새 정보를 싣는다 — 같은 사실은 한 줄에만 둔다.\n"
    "health_score·verdict·skip_reason_code와 조치의 런북·대상·파라미터는 구조화 필드로 이미 "
    "화면에 나간다. 그 값은 근거로 인용할 때만 쓴다."
)


_FINOPS_PROPOSAL_SYSTEM_PROMPT = (
    "너는 분석 결과를 조치 후보로 옮긴다. capabilities에 실린 Runbook만 고르고, "
    "target_arn은 allowed_target_arns에 있는 값만 쓴다. evidence_ids에는 입력 evidences의 "
    "evidence_id만 인용한다. 고른 Runbook의 required_parameters에 적힌 키는 "
    "parameter_schema의 제약과 parameter_constraints를 지켜 반드시 값을 채우고, 그 목록에 "
    "없는 키는 null로 둔다. candidates에는 summary_lines의 세 번째 줄(rationale)이 뒷받침한 "
    "조치를 담는다 — 한 대상에 Runbook 하나가 원칙이고, 둘 이상은 대상이 서로 다르거나 함께 "
    "실행해야 할 때다. runbook_id마다 후보는 하나만 담는다. candidates를 비우는 것은 "
    "capabilities 중 이 자산에 적용할 Runbook이 없을 때다. 요약이 추가 확인을 권해도 후보는 "
    "그대로 낸다 — 승인 여부는 관제자가 정한다.\n"
    "RUNBOOK_EC2_RIGHTSIZING에는 ai_savings_estimate를 함께 작성한다. "
    "savings_context의 대상·리전·현재/목표 타입과 과금 가정을 그대로 사용한다. "
    "목표 타입은 서버가 결정한 값을 복사한다. 단가 식별 후 월 차액을 계산한다.\n"
    "단가 식별: savings_context.region을 AWS 리전명과 연결하고, 그 리전의 "
    "current_instance_type과 target_instance_type에 해당하는 Linux 공유형 온디맨드 "
    "인스턴스 요금을 모델 지식에서 각각 찾는다. 기억한 단가의 리전·타입·과금 조건을 "
    "입력과 대조하고, 같은 리전·과금 조건에 맞는 현재/목표 단가를 한 쌍으로 사용한다. "
    "시간당 단가는 소수 6자리 이내 USD 문자열로 낸다.\n"
    "월 차액 계산: 위 두 단가로 (현재 단가-목표 단가)×730을 계산하고, "
    "마지막 월 금액만 소수 둘째 자리로 반올림해 문자열로 낸다. "
    "인스턴스 컴퓨팅 요금만 포함하며 제외 비용은 스토리지·네트워크·세금·할인·크레딧이다. "
    "explanation에는 적용한 리전명·과금 조건과 계산식, 모델 지식 기반 추정이라는 "
    "한계를 한국어로 짧게 설명한다. 출처는 요금 API·청구서 조회가 아닌 모델 지식이다. "
    "입력 조건에 맞는 두 단가를 추정할 수 있으면 status=ESTIMATED로 낸다. "
    "어느 한 단가라도 추정할 수 없으면 status=UNAVAILABLE로 단가·금액을 null로 두고 "
    "조치 후보는 유지한다. "
    "다른 런북의 ai_savings_estimate는 null이다."
)


def finops_prompt_material() -> str:
    """해시 대상 전문. 테스트가 무엇이 해시에 들어가는지 확인하는 데도 쓴다."""
    constraints = {
        runbook_id.value: list(texts)
        for runbook_id, texts in sorted(_PARAMETER_CONSTRAINTS.items(), key=lambda kv: kv[0].value)
    }
    sections = (
        ("summary_system_prompt", _FINOPS_SUMMARY_SYSTEM_PROMPT),
        ("proposal_system_prompt", _FINOPS_PROPOSAL_SYSTEM_PROMPT),
        ("parameter_constraints", json.dumps(constraints, ensure_ascii=False, sort_keys=True)),
        ("savings_assumptions", json.dumps(SavingsAssumptions().model_dump(), sort_keys=True)),
        (
            "summary_output_schema",
            json.dumps(EvidenceSummaryOutput.model_json_schema(), ensure_ascii=False),
        ),
        (
            "proposal_output_schema",
            json.dumps(FinOpsCandidateProposalOutput.model_json_schema(), ensure_ascii=False),
        ),
    )
    return "\n".join(f"[{name}]\n{body}" for name, body in sections)


def finops_prompt_fingerprint() -> str:
    """승인 스냅샷과 대조하는 값. 사람이 부르는 이름은 FINOPS_PROMPT_VERSION이고 판정은 이것이 한다."""
    return hashlib.sha256(finops_prompt_material().encode("utf-8")).hexdigest()


def _to_draft(proposal: FinOpsProposedCandidate, graph_input: FinOpsGraphInput) -> RunbookCandidateDraft:
    """후보 1건을 계약으로 옮긴다. 옮길 수 없으면 예외를 올려 FAILED로 간다."""
    offered = {capability.runbook_id for capability in graph_input.capabilities}
    if proposal.runbook_id not in offered:
        raise ValueError(f"입력 capabilities에 없는 Runbook입니다: {proposal.runbook_id.value}")
    if proposal.target_arn not in _allowed_target_arns(graph_input):
        raise ValueError("target_arn이 인시던트 자산·관계 자산 밖입니다")
    is_rightsizing = proposal.runbook_id is RunbookId.RUNBOOK_EC2_RIGHTSIZING
    if is_rightsizing and proposal.target_arn != graph_input.asset_context.arn:
        raise ValueError("다운사이징 대상의 사양 스냅샷이 없습니다")
    return RunbookCandidateDraft.model_validate(
        {
            "runbook_id": proposal.runbook_id.value,
            "target_arn": proposal.target_arn,
            "parameters": _parameter_values(
                proposal.runbook_id, proposal, graph_input.asset_context
            ),
            "evidence_ids": _canonical_evidence_ids(proposal.evidence_ids, graph_input),
            "ai_savings_estimate": (
                accept_savings_estimate(proposal.ai_savings_estimate, graph_input.asset_context)
                if is_rightsizing else None
            ),
        }
    )


def _summarize_evidence(state: _FinOpsState) -> dict[str, Any]:
    graph_input = state["graph_input"]
    request = AIModelRequest(
        system_prompt=_FINOPS_SUMMARY_SYSTEM_PROMPT,
        user_payload=_summary_payload(graph_input),
    )
    try:
        response = state["client"].complete(request, EvidenceSummaryOutput)
    except AIModelError as exc:
        return {"failure": f"summarize_evidence: {type(exc).__name__}"}
    summary = response.output
    return {"summary_lines": [summary.observation, summary.diagnosis, summary.rationale]}


def _propose_candidates(state: _FinOpsState) -> dict[str, Any]:
    graph_input = state["graph_input"]
    request = AIModelRequest(
        system_prompt=_FINOPS_PROPOSAL_SYSTEM_PROMPT,
        user_payload=_proposal_payload(graph_input, state["summary_lines"]),
    )
    try:
        response = state["client"].complete(request, FinOpsCandidateProposalOutput)
    except AIModelError as exc:
        return {"failure": f"propose_candidates: {type(exc).__name__}"}
    return {"proposals": list(response.output.candidates)}


def _validate_output_contract(state: _FinOpsState) -> dict[str, Any]:
    """출력 계약(#49 불변식)만 검사한다 — 4단계 Guardrail이 아니다(ADR-0005).

    후보 1건이라도 계약으로 옮길 수 없으면 그 건만 버리지 않고 전체를 FAILED로 낸다.
    NO_PROPOSAL은 "조치할 것이 없다"는 업무 판단이라, 형식 실패를 거기에 접으면 서버가
    하지 않은 판단이 관제 화면과 DB에 남는다.
    """
    if state.get("failure"):
        return {"output": _failed_output()}

    try:
        drafts = [_to_draft(proposal, state["graph_input"]) for proposal in state["proposals"]]
        output = AgentGraphOutput(
            invocation_status=(
                AgentInvocationStatus.SUCCEEDED if drafts else AgentInvocationStatus.NO_PROPOSAL
            ),
            summary_lines=state["summary_lines"],
            candidates=drafts,
        )
    except (ValidationError, ValueError):
        return {"output": _failed_output()}
    return {"output": output}
