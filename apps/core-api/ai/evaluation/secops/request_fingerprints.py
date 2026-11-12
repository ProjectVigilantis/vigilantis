"""동결 답지를 대역 응답으로 주입해 실제 모델 경계의 요청 지문을 만든다.

후보 있음·없음의 허용 경로를 근거 선행 1회 호출(decision 대역)로 모두 재구성하고,
후보가 있는 경로는 코드가 조립한 실행 계획이 동결 답지와 같은지도 확인한다.
모델 품질이나 실제 실행 성공의
증거가 아니며 네트워크 호출·운영 프롬프트 원문 저장은 하지 않는다.
"""

from __future__ import annotations

from typing import Any

from schemas.incidents import AgentInvocationStatus

from ai import agent
from ai.model_client import FakeAIModelClient, build_outbound_payload

from .dataset import EvalCase, digest, fixture_nacl_rule_numbers
from .scoring import Answer


class _RequestRecorder(FakeAIModelClient):
    def __init__(self, outputs):
        super().__init__(outputs)
        self.request_hashes: list[dict[str, str]] = []

    def complete(self, request, response_model):
        payload = build_outbound_payload(request)
        self.request_hashes.append({
            "response_model": response_model.__name__,
            "sha256": digest({
                "system_prompt": payload["system_prompt"],
                "user_json": payload["user_json"],
                "response_schema": response_model.model_json_schema(),
            }),
        })
        return super().complete(request, response_model)


def _require_assembly_matches_answer(case_id: str, output, answer: Answer) -> None:
    """코드가 조립한 후보가 동결 답지와 같다 — 모델 호출 없이 조립과 답지의 합치를 증명한다."""
    expected = answer.candidate
    if expected is None or len(output.candidates) != 1:
        raise ValueError(f"{case_id}: assembled candidate count differs from the answer")
    actual = output.candidates[0].model_dump(mode="json")
    parameters = actual["parameters"]
    number = parameters.get("rule_number")
    if (actual["runbook_id"] != expected.runbook_id or actual["target_arn"] != expected.target_arn
            or set(actual["evidence_ids"]) != set(expected.evidence_ids)
            or parameters.get("cidr_block") != expected.cidr_block
            or parameters.get("protocol") != expected.protocol
            or type(number) is not int
            or not expected.rule_number_min <= number <= expected.rule_number_max):
        raise ValueError(f"{case_id}: assembled candidate differs from the frozen answer")


def request_manifest(cases: list[EvalCase], answers: dict[str, Answer]) -> list[dict[str, Any]]:
    """실제 그래프를 통과한 요청 순서·내용을 동일 답지로 재현한다."""
    result = []
    for case in cases:
        answer = answers[case.case_id]
        if not answer.allowed_statuses:
            raise ValueError(f"{case.case_id}: request replay requires a decided answer")
        for status in sorted(answer.allowed_statuses):
            # 근거 선행 그래프는 1회 호출이다. 조치를 제시하면 decision=[1]이고 실행 계획은 코드가 조립한다.
            client = _RequestRecorder([
                agent.SecOpsEvidenceDecisionOutput(
                    observation="동결 관측 대역이다.", diagnosis="위험 해석 대역이다.",
                    rationale="조치 판단 대역이다.",
                    decision=[1] if status == "SUCCEEDED" else [],
                ),
            ])
            output = agent.run_secops_graph(
                case.graph_input, client=client,
                nacl_rule_numbers=fixture_nacl_rule_numbers(case.graph_input),
            )
            if (output.invocation_status is AgentInvocationStatus.FAILED
                    or output.invocation_status.value != status
                    or len(client.request_hashes) != agent.SECOPS_MODEL_CALLS):
                raise ValueError(f"{case.case_id}: request replay did not complete {status}")
            if status == "SUCCEEDED":
                _require_assembly_matches_answer(case.case_id, output, answer)
            result.append({
                "case_id": case.case_id, "status": status, "requests": client.request_hashes,
            })
    return result
