"""SecOps 그래프 계약·노드 단락. 실제 모델·DB·AWS 호출은 하지 않는다."""

import logging

import pytest

from ai.agent import (
    CandidateProposalOutput, EvidenceSummaryOutput, ProposedCandidate,
    RiskReassessmentOutput, run_secops_graph,
)
from ai.capabilities import build_secops_capabilities
from ai.model_client import FakeAIModelClient
from schemas.agents import SecOpsGraphInput
from schemas.api.incidents import RiskLevel
from schemas.incidents import AgentInvocationStatus
from schemas.runbooks import RunbookId

EC2 = "arn:aws:ec2:ap-northeast-2:123456789012:instance/i-0abc"
NACL = "arn:aws:ec2:ap-northeast-2:123456789012:network-acl/acl-0abc"
SUMMARY = EvidenceSummaryOutput(observation="300초 동안 SSH 실패 120회",
                                diagnosis="SSH 반복 공격으로 추정", rationale="출발지 차단 필요")
RISK = RiskReassessmentOutput(reviewed_risk_level=RiskLevel.HIGH)


def graph_input():
    return SecOpsGraphInput.model_validate({
        "incident_id": "inc-1",
        "asset_context": {
            "arn": EC2, "resource_id": "i-0abc", "asset_type": "EC2",
            "resource_role": "PRIMARY", "account_id": "123456789012",
            "region": "ap-northeast-2", "state": "running", "spec": {},
            "relationships": [{"relation_type": "PROTECTED_BY", "target_arn": NACL}],
            "evaluation_status": "PENDING", "collected_at": "2026-09-11T00:00:00Z",
        },
        "initial_risk": {
            "threat_event_id": "threat-1", "initial_risk_level": "HIGH",
            "response_mode": "PRE_MITIGATION_0_5S", "reason_codes": ["RISK_SSH_BRUTEFORCE"],
        },
        "evidences": [{"evidence_id": "ev-1", "evidence_type": "THREAT", "content": {
            "event": {
                "threat_event_id": "threat-1", "source_event_id": "source-1",
                "event_type": "SSH_BRUTE_FORCE", "target_arn": EC2,
                "occurred_at": "2026-09-10T23:00:00Z", "collected_at": "2026-09-10T23:00:01Z",
                "deduplication_key": "ssh:1", "payload": {"source_ip": "203.0.113.10",
                "failed_attempt_count": 120, "window_seconds": 300},
            },
        }}],
        "capabilities": [{"runbook_id": "RUNBOOK_NACL_ADD_DENY", "purpose": "출발지 차단",
                          "allowed_target_asset_types": ["NACL"]}],
    })


def proposal(**over):
    values = dict(runbook_id=RunbookId.RUNBOOK_NACL_ADD_DENY, target_arn=NACL,
                  evidence_ids=["ev-1"], rule_number=100, cidr_block="203.0.113.10/32",
                  protocol="tcp")
    values.update(over)
    return ProposedCandidate(**values)


def test_secops_three_calls_and_typed_output():
    data = graph_input()
    before = data.model_dump(mode="json")
    # 서버 초기 판정과 다른 AI 재평가를 넣어 요약 표기의 원천을 대조한다.
    reviewed = RiskReassessmentOutput(reviewed_risk_level=RiskLevel.MEDIUM)
    client = FakeAIModelClient([reviewed, CandidateProposalOutput(candidates=[proposal()]), SUMMARY])
    output = run_secops_graph(data, client=client)
    assert output.invocation_status is AgentInvocationStatus.SUCCEEDED
    assert output.reviewed_risk_level is RiskLevel.MEDIUM
    assert output.candidates[0].target_arn == NACL
    assert len(client.sent) == 3
    assert data.model_dump(mode="json") == before
    assert client.sent[0]["user_payload"]["isolation_execution"] is None
    assert "reviewed_risk_level" not in client.sent[0]["user_payload"]
    assert "reviewed_risk_label" not in client.sent[0]["user_payload"]
    assert "reviewed_risk_label" not in client.sent[1]["user_payload"]
    assert client.sent[1]["user_payload"]["reviewed_risk_level"] == "MEDIUM"
    assert client.sent[2]["user_payload"]["reviewed_risk_level"] == "MEDIUM"
    assert client.sent[2]["user_payload"]["reviewed_risk_label"] == "중간"
    assert client.sent[2]["user_payload"]["initial_risk"]["initial_risk_level"] == "HIGH"
    assert client.sent[2]["user_payload"]["candidates"] == [
        output.candidates[0].model_dump(mode="json")
    ]


def test_secops_no_proposal_keeps_summary_and_reviewed_risk():
    client = FakeAIModelClient([RISK, CandidateProposalOutput(candidates=[]), SUMMARY])
    output = run_secops_graph(graph_input(), client=client)
    assert output.invocation_status is AgentInvocationStatus.NO_PROPOSAL
    assert len(output.summary_lines) == 3
    assert output.reviewed_risk_level is RiskLevel.HIGH
    assert client.sent[2]["user_payload"]["candidates"] == []
    assert client.sent[2]["user_payload"]["reviewed_risk_label"] == "높음"


def _failure_log(caplog):
    """FAILED 1건당 실패 로그 1줄(#424) — 그 필드를 돌려준다."""
    records = [r for r in caplog.records if r.getMessage() == "agent_analysis_failed"]
    assert len(records) == 1
    fields = records[0].__dict__
    assert fields["incident_id"] == "inc-1" and fields["domain"] == "SECOPS"
    return fields


@pytest.mark.parametrize("completed,step", [
    (0, "reassess_risk"), (1, "propose_candidates"), (2, "summarize_evidence"),
])
def test_node_failure_stops_later_model_calls(completed, step, caplog):
    client = FakeAIModelClient([RISK, CandidateProposalOutput(candidates=[])][:completed])
    with caplog.at_level(logging.WARNING, logger="vigilantis.ai.agent"):
        output = run_secops_graph(graph_input(), client=client)
    assert output.invocation_status is AgentInvocationStatus.FAILED
    assert output.summary_lines == [] and output.candidates == []
    assert output.reviewed_risk_level is None
    assert len(client.sent) == completed + 1
    fields = _failure_log(caplog)
    assert fields["failed_step"] == step and fields["reason_code"] == "MODEL_UNAVAILABLE"


@pytest.mark.parametrize("over,reason", [
    ({"target_arn": EC2}, "TARGET_NOT_ALLOWED"),
    ({"target_arn": NACL.replace("acl-0abc", "acl-0def")}, "TARGET_NOT_ALLOWED"),
    ({"runbook_id": RunbookId.RUNBOOK_NACL_RESTORE}, "RUNBOOK_NOT_OFFERED"),
    ({"rule_number": None}, "CANDIDATE_CONTRACT_VIOLATION"),
    ({"evidence_ids": []}, "CANDIDATE_CONTRACT_VIOLATION"),
])
def test_invalid_proposal_fails_whole_graph(over, reason, caplog):
    client = FakeAIModelClient([RISK, CandidateProposalOutput(candidates=[proposal(**over)]), SUMMARY])
    with caplog.at_level(logging.WARNING, logger="vigilantis.ai.agent"):
        output = run_secops_graph(graph_input(), client=client)
    assert output.invocation_status is AgentInvocationStatus.FAILED
    assert len(client.sent) == 2  # 잘못된 후보는 요약 생성 전에 그래프를 중단시킨다.
    # 메뉴 밖·대상 밖(주입 의심)과 형식 위반을 사유로 가른다. 대상 밖이면 그 대상을 남긴다
    fields = _failure_log(caplog)
    assert fields["failed_step"] == "validate_candidates"
    assert fields["reason_code"] == reason
    if reason == "TARGET_NOT_ALLOWED":
        assert fields["target_arn"] == over["target_arn"]
    else:
        assert "target_arn" not in fields


def test_capability_menu_requires_ssh_and_direct_nacl_relation():
    from schemas.events import ThreatEventType

    asset = graph_input().asset_context
    assert len(build_secops_capabilities(asset=asset, event_type=ThreatEventType.SSH_BRUTE_FORCE)) == 1
    assert build_secops_capabilities(asset=asset, event_type=ThreatEventType.OPEN_IP) == []
    asset.relationships[0].relation_type = "SECURED_BY"
    assert build_secops_capabilities(asset=asset, event_type=ThreatEventType.SSH_BRUTE_FORCE) == []
