"""저장된 요약으로 추천 노드만 비교한다. 호출 클라이언트와 실행 예산은 호출자가 정한다."""

from copy import deepcopy

from schemas.agents import FinOpsGraphInput

from ai.evaluation.savings.legacy import (
    FinOpsCandidateProposalOutput,
    _propose_candidates,
    _validate_output_contract,
)

V040_SAVINGS_ORDER = (
    "status", "target_arn", "region", "current_instance_type", "target_instance_type",
    "current_hourly_rate", "target_hourly_rate", "amount", "explanation",
)


def proposal_model_in_order(order: tuple[str, ...]):
    """파싱 계약을 유지하며 모델에 전달할 절감 예상 필드 순서만 바꾼다."""
    schema = FinOpsCandidateProposalOutput.model_json_schema()
    estimate = schema["$defs"]["ProposedSavingsEstimate"]
    properties = estimate["properties"]
    if len(order) != len(properties) or set(order) != set(properties):
        raise ValueError("절감 예상 필드 전체를 중복 없이 지정해야 합니다")
    estimate["properties"] = {name: properties[name] for name in order}

    class OrderedProposalOutput(FinOpsCandidateProposalOutput):
        @classmethod
        def model_json_schema(cls, *args, **kwargs):
            # SDK의 strict 변환이 원본 스키마를 수정할 수 있어 매번 사본을 전달한다.
            return deepcopy(schema)

    # SDK가 사용하는 응답 이름도 비교 조건이다. 순서 외의 입력은 동일하게 둔다.
    OrderedProposalOutput.__name__ = FinOpsCandidateProposalOutput.__name__
    return OrderedProposalOutput


def replay_recommendation(
    graph_input: FinOpsGraphInput,
    summary_lines: list[str],
    *,
    client,
    response_model=FinOpsCandidateProposalOutput,
):
    """요약 재호출 없이 기존 추천·출력 검증 노드를 실행한다. 전체 그래프 계측은 아니다."""
    class SelectedResponseClient:
        def complete(self, request, requested_model):
            if requested_model is not FinOpsCandidateProposalOutput:
                raise ValueError("추천 출력 모델만 재생할 수 있습니다")
            return client.complete(request, response_model)

    state = {
        "graph_input": graph_input,
        "summary_lines": list(summary_lines),
        "client": SelectedResponseClient(),
    }
    state.update(_propose_candidates(state))
    return _validate_output_contract(state)["output"]
