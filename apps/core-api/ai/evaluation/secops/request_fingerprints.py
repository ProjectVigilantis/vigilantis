"""동결 답지를 대역 응답으로 주입해 실제 모델 경계의 요청 지문을 만든다.

후보 있음·없음의 허용 경로를 모두 재구성한다. 모델 품질이나 실제 실행 성공의
증거가 아니며 네트워크 호출·운영 프롬프트 원문 저장은 하지 않는다.
"""

from __future__ import annotations

from typing import Any

from schemas.incidents import AgentInvocationStatus

from ai import agent
from ai.model_client import FakeAIModelClient, build_outbound_payload

from .dataset import EvalCase, digest
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


def request_manifest(cases: list[EvalCase], answers: dict[str, Answer]) -> list[dict[str, Any]]:
    """실제 그래프를 통과한 요청 순서·내용을 동일 답지로 재현한다."""
    result = []
    for case in cases:
        answer = answers[case.case_id]
        if not answer.allowed_statuses:
            raise ValueError(f"{case.case_id}: request replay requires a decided answer")
        for risk in sorted(answer.reviewed_risk_levels):
            for status in sorted(answer.allowed_statuses):
                proposals = []
                if status == "SUCCEEDED":
                    expected = answer.candidate
                    if expected is None:
                        raise ValueError(f"{case.case_id}: proposal answer missing")
                    proposals.append(agent.ProposedCandidate(
                        runbook_id=expected.runbook_id, target_arn=expected.target_arn,
                        evidence_ids=expected.evidence_ids, cidr_block=expected.cidr_block,
                        protocol=expected.protocol, rule_number=expected.rule_number_min,
                    ))
                client = _RequestRecorder([
                    agent.RiskReassessmentOutput(reviewed_risk_level=risk),
                    agent.CandidateProposalOutput(candidates=proposals),
                    agent.EvidenceSummaryOutput(
                        observation="동결 관측 대역이다.", diagnosis="위험 해석 대역이다.",
                        rationale="조치 판단 대역이다.",
                    ),
                ])
                output = agent.run_secops_graph(case.graph_input, client=client)
                if (output.invocation_status is AgentInvocationStatus.FAILED
                        or output.invocation_status.value != status
                        or len(client.request_hashes) != agent.SECOPS_MODEL_CALLS):
                    raise ValueError(f"{case.case_id}: request replay did not complete {status}")
                result.append({
                    "case_id": case.case_id, "risk": risk, "status": status,
                    "requests": client.request_hashes,
                })
    return result
