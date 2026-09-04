# ==============================================================================
# [파일 설명]  담당: 안성일 (AI / Guardrail)
# LangGraph 도메인 그래프(FinOps·SecOps)의 진입점입니다. 그래프 구조는 ADR-0005가
# 확정했고, 모델 호출은 ai/model_client.py 경계를 경유합니다(Issue #115).
# 이 파일은 FinOps 그래프를 구현합니다(Issue #209). 요약 프롬프트 v1과 그 판·해시는
# Issue #243입니다. SecOps 그래프는 reassess_risk가 Risk Evaluator 출력 계약에 의존해
# 아직 없습니다.
#
# 계약 원칙
#   - 입출력은 packages/schemas/agents.py 계약으로만 주고받는다. 그래프 내부 State와
#     모델 구조화 출력 모델은 그 계약과 분리한다(ADR-0005 §Consequences).
#   - Checkpointer를 두지 않는다. 한 번 불리면 Terminal 결과 1회를 반환하고 끝난다
#     — 업무 상태의 원천은 PostgreSQL이다(ADR-0005 설계 원칙 2).
#   - 모델 호출은 주입받은 AIModelClient로만 한다. OpenAI SDK를 직접 부르지 않는다
#     (ADR-0005 설계 원칙 3).
#   - Guardrail·DB 저장·AWS 실행·승인은 그래프 밖이다(ADR-0005 설계 원칙 3).
#   - 모델이 지어낼 수 없는 값은 그래프가 고정한다 — 후보 Runbook은 입력 capabilities
#     안에서만, target_arn은 입력 자산과 그 관계 자산 안에서만 받는다. 벗어나면 FAILED다.
#   - 후보 evidence_ids가 입력 Evidence 안에 있는지와 FINOPS의 reviewed_risk_level=null은
#     여기서 보지 않는다 — 계약이 Workflow 몫으로 못 박았다(schemas/agents.py 계약 원칙).
# ==============================================================================

from __future__ import annotations

import hashlib
import json
from typing import Any, Optional, TypedDict

from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, ConfigDict, StrictBool, StrictInt, ValidationError

from ai.model_client import AIModelClient, AIModelError, AIModelRequest
from schemas.agents import (
    AgentGraphOutput,
    FinOpsGraphInput,
    RunbookCandidateDraft,
    RunbookCapability,
)
from schemas.evidence import EvidenceType
from schemas.incidents import AgentInvocationStatus
from schemas.runbook_parameters import CANDIDATE_PARAMETER_MODELS
from schemas.runbooks import RunbookId

# ------------------------------------------------------------------------------
# 프롬프트 — v1 (Issue #243)
# ------------------------------------------------------------------------------
# 요약 3줄의 역할은 근거(observation) / 진단(diagnosis) / 결론 근거(rationale)다.
# 세 번째 줄이 조치를 되풀이하지 않는 것은 어떤 런북을 어느 대상에 어떤 값으로 돌릴지가
# RecommendationItem(runbook_id·target_arn·display_parameters)으로 이미 화면에 나가기
# 때문이고, 첫 줄이 근거를 떠맡는 것은 근거 본문을 여는 조회 엔드포인트가 없어 이 줄이
# "모델이 무엇을 보고 판단했는가"의 유일한 노출 경로이기 때문이다. NO_PROPOSAL도 요약
# 3줄이 필수인데(schemas/agents.py), "왜 조치가 없는가"를 쓸 자리가 rationale이다.
#
# 지시문은 결함 체크리스트(ai/evaluation/summary_defects.md)와 1:1이다 — 항목 1 근거 없음
# → observation 줄 · 2 단정/추정 → diagnosis 줄 · 3 진단↔조치 → rationale 줄 · 4 같은 말
# → "각각 새 정보" 줄 · 5 구조화 값 되읽기 → 마지막 줄. 금지형이 아니라 지시형으로 쓴다
# — 금지가 쌓일수록 빈 후보가 가장 안전한 답이 되어 NO_PROPOSAL 도피가 는다.
#
# 항목 밖의 규칙 둘은 v1을 세우는 실측에서 나왔다(summary_defects.md §실측이 더한 규칙).
#   - "verdict는 규칙 엔진이 이미 내린 판정이다" — 요약이 판정을 유보하자 후보 노드가 그
#     문장을 따라 후보를 비웠다(A7 NO_PROPOSAL 4/60). 판정을 다시 내리는 자리는 관제자다.
#   - 요약 호출에 available_actions(목적 문구만)를 싣고 rationale은 그중 하나만 다룬다 —
#     메뉴 없이는 카드에 없는 조치(중지·종료)를 권했고(31/60), 런북 이름을 주자 메뉴를
#     열거해 후보 노드가 언급된 런북을 전부 담았다(후보 2건 35/60).
#
# 모델이 볼 수 없는데 어기면 호출 전체가 FAILED가 되는 계약 사실 둘을 모델에게 보인다.
#   - 같은 runbook_id 중복 금지(schemas/agents.py) — 출력 전역 규칙이라 후보 프롬프트에.
#   - min_size ≤ max_size(schemas/runbook_parameters.py) — 런북 1종 전용 model_validator라
#     parameter_schema(JSON Schema)에 나타나지 않는다. 그 런북의 capability 페이로드에만
#     _PARAMETER_CONSTRAINTS로 싣는다. 프롬프트에 런북 이름을 박으면 그 런북이 메뉴에
#     없는 인시던트에도 지시가 나가고, 런북 목록이 프롬프트와 계약 두 곳에 생긴다.
#
# 문구를 바꾸면 prompt_fingerprint()가 움직여 승인 스냅샷(ai/evaluation/
# summary_prompt_snapshot.json) 대조 테스트가 실패한다 — 재통과 절차는
# docs/AI_SUMMARY_BASELINE.md. 판 이름(PROMPT_VERSION)은 사람이 부르기 위한 것이고
# 판정은 해시가 한다. 프롬프트 전문은 스냅샷에 남기지 않는다(ADR-0005 미보존 대상).
#
# 비밀값 라벨 표기(`token:`·`password:` 같은 형태)를 프롬프트에 쓰지 않는다 —
# build_outbound_payload()가 system_prompt에도 마스킹을 적용해 지침이 조용히 잘린다
# (ai/model_client.py 계약 원칙).

PROMPT_VERSION = "v1"

_SUMMARY_SYSTEM_PROMPT = (
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

_PROPOSAL_SYSTEM_PROMPT = (
    "너는 분석 결과를 조치 후보로 옮긴다. capabilities에 실린 Runbook만 고르고, "
    "target_arn은 allowed_target_arns에 있는 값만 쓴다. evidence_ids에는 입력 evidences의 "
    "evidence_id만 인용한다. 고른 Runbook의 required_parameters에 적힌 키는 "
    "parameter_schema의 제약과 parameter_constraints를 지켜 반드시 값을 채우고, 그 목록에 "
    "없는 키는 null로 둔다. candidates에는 summary_lines의 세 번째 줄(rationale)이 뒷받침한 "
    "조치를 담는다 — 한 대상에 Runbook 하나가 원칙이고, 둘 이상은 대상이 서로 다르거나 함께 "
    "실행해야 할 때다. runbook_id마다 후보는 하나만 담는다. candidates를 비우는 것은 "
    "capabilities 중 이 자산에 적용할 Runbook이 없을 때다. 요약이 추가 확인을 권해도 후보는 "
    "그대로 낸다 — 승인 여부는 관제자가 정한다."
)

# 런북별 파라미터 제약 문구. 원천은 계약 모델의 model_validator이며, 문구가 계약과 갈리지
# 않는지는 테스트가 계약 모델에 min>max를 넣어 확인한다(ai/tests/test_finops_graph.py).
_PARAMETER_CONSTRAINTS: dict[RunbookId, tuple[str, ...]] = {
    RunbookId.RUNBOOK_EC2_ENABLE_AUTOSCALING: ("min_size는 max_size 이하로 정한다",),
}


# ------------------------------------------------------------------------------
# 모델 구조화 출력 — 그래프 내부 모델이며 외부 계약이 아니다
# ------------------------------------------------------------------------------


class EvidenceSummaryOutput(BaseModel):
    """summarize_evidence 노드가 모델에서 받는 출력 — CoT 3줄.

    list[str]이 아니라 필드 3개로 받는다. 요약이 정확히 3줄이어야 한다는 것을 길이
    검증이 아니라 구조로 보장하기 위해서다 — 모델이 2줄이나 4줄을 낼 자리가 없다.

    필드 이름이 곧 역할이다(#243) — 근거 / 진단 / 결론 근거. 이 이름은 JSON Schema로
    모델에 나가므로 프롬프트의 일부이며, 바꾸면 prompt_fingerprint()가 움직인다.
    """

    model_config = ConfigDict(extra="forbid")

    observation: str
    diagnosis: str
    rationale: str


class ProposedCandidate(BaseModel):
    """모델이 낸 후보 1건. 계약(RunbookCandidateDraft)으로 옮기기 전 단계다.

    파라미터를 Runbook별 union이 아니라 평평한 nullable 필드로 받는다. union으로 두면
    AI가 정할 값이 0개인 Runbook 3종의 스키마가 모두 빈 객체라 모델이 갈라낼 수 없다.
    어느 키가 실제로 쓰이는지는 runbook_id가 정하며, 조립은 _parameter_values()가 한다.

    자원 ID·현재 스펙 같은 조회값은 여기에 없다 — AI가 정하는 값만 싣는다
    (packages/schemas/runbook_parameters.py 계약 원칙 ①).
    """

    model_config = ConfigDict(extra="forbid")

    runbook_id: RunbookId
    target_arn: str
    evidence_ids: list[str]
    # Runbook별로 쓰이는 값 — 고른 Runbook이 받지 않는 키는 null이다.
    # 계약(runbook_parameters.py)과 같은 Strict 타입을 쓴다. 여기서 느슨하게 받으면
    # "2"·"true" 같은 값이 여기서 조용히 변환돼 계약의 Strict 검사를 지나간다.
    rule_number: Optional[StrictInt] = None
    cidr_block: Optional[str] = None
    protocol: Optional[str] = None
    egress: Optional[StrictBool] = None
    target_instance_type: Optional[str] = None
    min_size: Optional[StrictInt] = None
    max_size: Optional[StrictInt] = None


class CandidateProposalOutput(BaseModel):
    """propose_candidates 노드가 모델에서 받는 출력. 빈 목록이 NO_PROPOSAL이 된다."""

    model_config = ConfigDict(extra="forbid")

    candidates: list[ProposedCandidate]


# ------------------------------------------------------------------------------
# 판 — 모델에게 나가는 지시 전부를 해시 하나로 접는다 (Issue #243)
# ------------------------------------------------------------------------------
# 시스템 프롬프트 2개와 제약 문구만이 아니라 구조화 출력 모델의 JSON Schema도 넣는다 —
# openai_client.py가 response_format으로 스키마를 모델에 보내므로 필드 이름을 바꾸는
# 것도 지시를 바꾸는 것이다. 문자열만 해시하면 이름을 바꿔도 해시가 서 있다.
# RunbookId enum이 바뀌어도 움직이는데, 그것은 모델의 메뉴가 바뀐 것이라 재통과가 맞다.


def prompt_material() -> str:
    """해시 대상 전문. 테스트가 무엇이 해시에 들어가는지 확인하는 데도 쓴다."""
    constraints = {
        runbook_id.value: list(texts)
        for runbook_id, texts in sorted(_PARAMETER_CONSTRAINTS.items(), key=lambda kv: kv[0].value)
    }
    sections = (
        ("summary_system_prompt", _SUMMARY_SYSTEM_PROMPT),
        ("proposal_system_prompt", _PROPOSAL_SYSTEM_PROMPT),
        ("parameter_constraints", json.dumps(constraints, ensure_ascii=False, sort_keys=True)),
        (
            "summary_output_schema",
            json.dumps(EvidenceSummaryOutput.model_json_schema(), ensure_ascii=False, sort_keys=True),
        ),
        (
            "proposal_output_schema",
            json.dumps(CandidateProposalOutput.model_json_schema(), ensure_ascii=False, sort_keys=True),
        ),
    )
    return "\n".join(f"[{name}]\n{body}" for name, body in sections)


def prompt_fingerprint() -> str:
    """승인 스냅샷과 대조하는 값. 사람이 부르는 이름은 PROMPT_VERSION이고 판정은 이것이 한다."""
    return hashlib.sha256(prompt_material().encode("utf-8")).hexdigest()


# ------------------------------------------------------------------------------
# 그래프 State — 노드 사이 전달용이며 외부로 나가지 않는다
# ------------------------------------------------------------------------------


class _FinOpsState(TypedDict, total=False):
    """모델 호출 경계(client)도 State로 받는다.

    Checkpointer가 없어 State를 직렬화하지 않으므로 주입 객체를 실어도 된다. 컴파일된
    그래프를 모듈 수준에 하나만 두고 호출마다 client를 바꿔 끼우기 위한 선택이다.
    """

    graph_input: FinOpsGraphInput
    client: AIModelClient
    summary_lines: list[str]
    proposals: list[ProposedCandidate]
    failure: str
    output: AgentGraphOutput


# ------------------------------------------------------------------------------
# 입력에서 파생하는 허용 집합 — 모델이 고를 수 있는 값의 범위
# ------------------------------------------------------------------------------


def _allowed_target_arns(graph_input: FinOpsGraphInput) -> list[str]:
    """조치 대상이 될 수 있는 ARN. 인시던트 자산과 그 관계 자산뿐이다.

    자산 하나로 고정하지 않는 것은 FinOps 조치의 대상이 관계 자산일 수 있기 때문이다
    (EC2에 ATTACHED_TO로 달린 EBS 볼륨 → RUNBOOK_EBS_DELETE_UNATTACHED). 순서를 보존해
    모델에 보내는 목록이 호출마다 흔들리지 않게 한다.
    """
    arns = [graph_input.asset_context.arn]
    for relationship in graph_input.asset_context.relationships:
        if relationship.target_arn not in arns:
            arns.append(relationship.target_arn)
    return arns


def _capability_payload(capability: RunbookCapability) -> dict[str, Any]:
    """Capability 1건 + 그 Runbook이 요구하는 파라미터 명세.

    명세를 함께 싣지 않으면 모델은 어느 키를 채워야 하는지 알 수 없다 — Capability
    계약에는 파라미터 메타데이터가 없고(#49가 세부 계약 확정까지 제외로 둔 항목),
    빈 값으로 온 후보는 계약 검증에서 거절되어 호출 전체가 FAILED가 된다. 명세의
    원천은 계약 모델 자신이라 그래프가 지어내는 값이 아니다.

    parameter_constraints는 JSON Schema에 나타나지 않는 model_validator 제약의 문구다
    (#243). 제약이 없는 런북은 빈 목록이다 — 채울 것이 없다는 것도 정보다.
    """
    payload = capability.model_dump(mode="json")
    model = CANDIDATE_PARAMETER_MODELS.get(capability.runbook_id)
    schema = model.model_json_schema() if model is not None else {}
    payload["required_parameters"] = sorted(model.model_fields) if model else []
    payload["parameter_schema"] = schema.get("properties", {})
    payload["parameter_constraints"] = list(_PARAMETER_CONSTRAINTS.get(capability.runbook_id, ()))
    return payload


def _parameter_values(runbook_id: RunbookId, proposal: ProposedCandidate) -> dict[str, Any]:
    """평평한 후보 필드에서 그 Runbook의 파라미터 키만 추린다.

    고른 Runbook이 받지 않는 키는 버린다 — 실행으로 나가지 않는 값이라 무해하다.
    반대로 필요한 키가 null이면 여기서 걸러내지 않는다. RunbookCandidateDraft 검증이
    거절해 FAILED가 되어야 하며, 조용히 채우면 모델이 정하지 않은 값이 실행으로 간다.
    """
    model = CANDIDATE_PARAMETER_MODELS.get(runbook_id)
    if model is None:
        return {}  # 롤백 3종 — 후보가 될 수 없다는 판정은 계약 검증기가 한다
    values = proposal.model_dump()
    return {name: values[name] for name in model.model_fields if name in values}


def _to_draft(proposal: ProposedCandidate, graph_input: FinOpsGraphInput) -> RunbookCandidateDraft:
    """후보 1건을 계약으로 옮긴다. 옮길 수 없으면 예외를 올려 FAILED로 간다."""
    offered = {capability.runbook_id for capability in graph_input.capabilities}
    if proposal.runbook_id not in offered:
        raise ValueError(f"입력 capabilities에 없는 Runbook입니다: {proposal.runbook_id.value}")
    if proposal.target_arn not in _allowed_target_arns(graph_input):
        raise ValueError("target_arn이 인시던트 자산·관계 자산 밖입니다")
    return RunbookCandidateDraft.model_validate(
        {
            "runbook_id": proposal.runbook_id.value,
            "target_arn": proposal.target_arn,
            "parameters": _parameter_values(proposal.runbook_id, proposal),
            "evidence_ids": proposal.evidence_ids,
        }
    )


# ------------------------------------------------------------------------------
# 모델로 나가는 페이로드
# ------------------------------------------------------------------------------
# mode="json"으로 덤프한다 — python 모드는 AssetItem.collected_at을 datetime으로 남겨
# build_outbound_payload()가 직렬화 실패로 세운다(ai/model_client.py).


def _incident_payload(graph_input: FinOpsGraphInput) -> dict[str, Any]:
    """모델에게 나갈 값. 같은 값을 두 번 싣지 않는다.

    RuleEvidence는 RuleEvaluationResult를 그대로 감싼 모델이라(packages/schemas/evidence.py),
    RULE 근거가 있는 인시던트에서는 최상위 rule_evaluation이 그 근거의 복사본이다. 둘 다
    실으면 같은 판정이 한 실행에서 네 번 나간다 — 후보 호출이 이 페이로드를 다시 보내기
    때문이다(_proposal_payload). 골든 6건 기준 입력의 14.6%가 그 중복이었다.

    **근거 쪽을 남기고 최상위를 뺀다.** 후보가 evidence_ids로 인용할 ID가 근거에만 있어
    반대로는 뺄 수 없다. RULE 근거가 없는 인시던트에서는 최상위를 그대로 실어, 판정이
    페이로드에서 사라지지 않게 한다.

    값이 다를 때를 여기서 가리지 않는다 — RULE 근거가 있으면 그 evaluation이 최상위와
    같다는 것을 입력 계약이 이미 강제한다(FinOpsGraphInput, Issue #265). 여기서 다시
    비교하면 계약이 거절한 조합을 위한 분기가 되어 도달하지 않는다.
    """
    rule_evaluation = graph_input.rule_evaluation.model_dump(mode="json")
    evidences = [evidence.model_dump(mode="json") for evidence in graph_input.evidences]
    carried_by_evidence = any(
        evidence["evidence_type"] == EvidenceType.RULE.value for evidence in evidences
    )

    payload: dict[str, Any] = {
        "incident_id": graph_input.incident_id,
        "asset": graph_input.asset_context.model_dump(mode="json"),
    }
    if not carried_by_evidence:
        payload["rule_evaluation"] = rule_evaluation
    payload["evidences"] = evidences
    return payload


def _summary_payload(graph_input: FinOpsGraphInput) -> dict[str, Any]:
    """요약 노드로 나갈 값 — 인시던트 페이로드 + 가능한 조치의 목적 문구.

    메뉴를 싣는 것은 rationale이 "카드의 조치가 왜 이 자산에 맞는가"를 쓰려면 무엇이
    메뉴에 있는지 알아야 하기 때문이다(#243). v1 1차 실측에서 메뉴 없이 쓴 rationale의
    31/60이 카드에 없는 조치(중지·종료·삭제)를 권했다.

    Runbook 이름·파라미터 명세는 싣지 않는다 — 요약 노드는 조치를 고르지 않고(그 일은
    propose_candidates가 한다), 이름을 주면 문장이 이름을 되읽는다. purpose 문구만으로
    "과대 스펙 EC2 다운사이징"처럼 사람 말로 조치를 가리킬 수 있다.
    """
    payload = _incident_payload(graph_input)
    payload["available_actions"] = [capability.purpose for capability in graph_input.capabilities]
    return payload


def _proposal_payload(graph_input: FinOpsGraphInput, summary_lines: list[str]) -> dict[str, Any]:
    payload = _incident_payload(graph_input)
    payload["summary_lines"] = summary_lines
    payload["capabilities"] = [
        _capability_payload(capability) for capability in graph_input.capabilities
    ]
    payload["allowed_target_arns"] = _allowed_target_arns(graph_input)
    return payload


# ------------------------------------------------------------------------------
# 노드
# ------------------------------------------------------------------------------
# 실패 사유 문자열에 모델 응답이나 프롬프트를 담지 않는다(ADR-0005 미보존 대상).
# 경계 예외의 클래스 이름까지만 남긴다.


def _summarize_evidence(state: _FinOpsState) -> dict[str, Any]:
    graph_input = state["graph_input"]
    request = AIModelRequest(
        system_prompt=_SUMMARY_SYSTEM_PROMPT,
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
        system_prompt=_PROPOSAL_SYSTEM_PROMPT,
        user_payload=_proposal_payload(graph_input, state["summary_lines"]),
    )
    try:
        response = state["client"].complete(request, CandidateProposalOutput)
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


def _failed_output() -> AgentGraphOutput:
    """FAILED는 빈 요약·빈 후보·reviewed_risk_level=null이다(#49 불변식)."""
    return AgentGraphOutput(invocation_status=AgentInvocationStatus.FAILED)


def _after_summarize(state: _FinOpsState) -> str:
    return "validate_output_contract" if state.get("failure") else "propose_candidates"


# ------------------------------------------------------------------------------
# 그래프 — ADR-0005 §Decision의 FinOps 노드 순서 그대로
# ------------------------------------------------------------------------------


def _build_finops_graph():
    builder = StateGraph(_FinOpsState)
    builder.add_node("summarize_evidence", _summarize_evidence)
    builder.add_node("propose_candidates", _propose_candidates)
    builder.add_node("validate_output_contract", _validate_output_contract)

    builder.add_edge(START, "summarize_evidence")
    # 요약이 실패하면 후보 생성을 건너뛴다. 그래도 validate를 지나게 두는 것은
    # AgentGraphOutput을 만드는 자리를 한 곳으로 유지하기 위해서다.
    builder.add_conditional_edges(
        "summarize_evidence",
        _after_summarize,
        {
            "propose_candidates": "propose_candidates",
            "validate_output_contract": "validate_output_contract",
        },
    )
    builder.add_edge("propose_candidates", "validate_output_contract")
    builder.add_edge("validate_output_contract", END)
    # checkpointer 없이 컴파일한다 — 중단점도 재개도 없다(ADR-0005 설계 원칙 2)
    return builder.compile()


FINOPS_GRAPH = _build_finops_graph()


def run_finops_graph(
    graph_input: FinOpsGraphInput,
    *,
    client: AIModelClient,
) -> AgentGraphOutput:
    """FinOps 그래프 1회 호출. Terminal 결과 1건을 반환한다.

    호출부(Workflow)가 할 일은 그래프 밖이다 — AI 호출 상태 선점(Claim), 후보의
    Guardrail 검증, DB 저장, 승인·실행은 여기서 하지 않는다.
    """
    final_state = FINOPS_GRAPH.invoke({"graph_input": graph_input, "client": client})
    return final_state["output"]
