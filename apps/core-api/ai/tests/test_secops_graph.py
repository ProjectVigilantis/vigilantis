"""SecOps 그래프 계약·노드 단락. 실제 모델·DB·AWS 호출은 하지 않는다."""

import logging

import pytest

from ai.agent import (
    SECOPS_MODEL_CALLS, CandidateProposalOutput, EvidenceSummaryOutput, ProposedCandidate,
    run_secops_graph,
)
from ai.capabilities import build_secops_capabilities
from ai.model_client import FakeAIModelClient
from schemas.agents import SecOpsGraphInput
from schemas.incidents import AgentInvocationStatus
from schemas.runbooks import RunbookId

EC2 = "arn:aws:ec2:ap-northeast-2:123456789012:instance/i-0abc"
NACL = "arn:aws:ec2:ap-northeast-2:123456789012:network-acl/acl-0abc"
SUMMARY = EvidenceSummaryOutput(observation="300초 동안 SSH 실패 120회",
                                diagnosis="SSH 반복 공격으로 추정", rationale="출발지 차단 필요")


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


def test_secops_two_calls_and_null_reviewed_risk():
    """정상 경로는 후보·요약 2회다. 등급 호출이 없어 reviewed_risk_level은 null이다(ADR-0005 1차 개정)."""
    data = graph_input()
    before = data.model_dump(mode="json")
    client = FakeAIModelClient([CandidateProposalOutput(candidates=[proposal()]), SUMMARY])
    output = run_secops_graph(data, client=client, nacl_rule_numbers={NACL: 1})
    assert output.invocation_status is AgentInvocationStatus.SUCCEEDED
    assert output.reviewed_risk_level is None
    assert output.candidates[0].target_arn == NACL
    assert len(client.sent) == SECOPS_MODEL_CALLS == 2
    assert data.model_dump(mode="json") == before
    assert client.sent[0]["user_payload"]["isolation_execution"] is None
    for request in client.sent:
        # 서버 초기 판정은 그대로 전달되고, 재평가 등급·표기는 어느 호출에도 실리지 않는다.
        assert request["user_payload"]["initial_risk"]["initial_risk_level"] == "HIGH"
        assert "reviewed_risk_level" not in request["user_payload"]
        assert "reviewed_risk_label" not in request["user_payload"]
    assert "initial_risk_label" not in client.sent[0]["user_payload"]
    assert client.sent[1]["user_payload"]["initial_risk_label"] == "높음"
    assert client.sent[1]["user_payload"]["candidates"] == [
        output.candidates[0].model_dump(mode="json")
    ]


def test_secops_no_proposal_keeps_summary_with_null_reviewed_risk():
    client = FakeAIModelClient([CandidateProposalOutput(candidates=[]), SUMMARY])
    output = run_secops_graph(graph_input(), client=client, nacl_rule_numbers={NACL: 1})
    assert output.invocation_status is AgentInvocationStatus.NO_PROPOSAL
    assert len(output.summary_lines) == 3
    assert output.reviewed_risk_level is None
    assert client.sent[1]["user_payload"]["candidates"] == []
    assert client.sent[1]["user_payload"]["initial_risk_label"] == "높음"


@pytest.mark.parametrize("entries,expected", [
    ([{"RuleNumber": 100, "RuleAction": "allow"}], 1),
    ([{"RuleNumber": 1, "RuleAction": "deny"}, {"RuleNumber": 100, "RuleAction": "allow"}], 2),
    ([{"RuleNumber": 1, "Egress": True, "RuleAction": "allow"},
      {"RuleNumber": 100, "RuleAction": "allow"}], 1),
    ([{"RuleNumber": 1, "RuleAction": "deny"}, {"RuleNumber": 32767, "RuleAction": "deny"}], 2),
    ([{"RuleNumber": 1, "RuleAction": "allow"}], None),
    ([{"RuleNumber": 1, "RuleAction": "deny"}, {"RuleNumber": 2, "RuleAction": "allow"}], None),
])
def test_nacl_candidate_uses_server_number_before_an_existing_allow(monkeypatch, caplog, entries, expected):
    """서버 조회·후보·요약·precheck가 같은 빈 번호를 쓰고, 없으면 후보를 만들지 않는다."""
    from agent_dispatcher import (
        GraphInputUnavailable,
        resolve_secops_rule_numbers,
        verify_graph_output,
    )
    from schemas.runbook_parameters import build_precheck_parameters
    from services.aws import executor as ex

    acl_id = "acl-0abc123456789def0"
    target = NACL.replace("acl-0abc", acl_id)
    data = graph_input()
    data.asset_context.relationships[0].target_arn = target
    calls = []
    class Ec2:
        def describe_network_acls(self, **kwargs):
            calls.append(kwargs)
            return {"NetworkAcls": [{"Entries": entries}]}
    monkeypatch.setattr(ex, "aws_client", lambda *args: Ec2())
    caplog.set_level(logging.INFO, logger="vigilantis.aws")
    if expected is None:
        with pytest.raises(GraphInputUnavailable, match="PRECHECK_INVALID_STATE"):
            resolve_secops_rule_numbers(data, open_rule_numbers={})
        assert calls == [{"NetworkAclIds": [acl_id]}]
        assert caplog.records[-1].precheck_evidence["detail"] == "no_eligible_inbound_rule_number"
        return
    numbers = resolve_secops_rule_numbers(data, open_rule_numbers={})
    assert numbers == {target: expected}
    selection = caplog.records[-1].precheck_evidence
    assert selection["source"] == "candidate_describe_network_acls"
    assert selection["rule_number"] == expected
    client = FakeAIModelClient([
        CandidateProposalOutput(candidates=[proposal(target_arn=target)]), SUMMARY,
    ])
    output = run_secops_graph(data, client=client, nacl_rule_numbers=numbers)
    assert verify_graph_output(data, output, data.incident_id, nacl_rule_numbers=numbers) == output
    candidate = output.candidates[0]
    assert candidate.parameters.rule_number == expected
    assert client.sent[1]["user_payload"]["candidates"][0]["parameters"]["rule_number"] == expected
    params = build_precheck_parameters(
        candidate.runbook_id, candidate.parameters, resource_id=acl_id, evidence_ids=candidate.evidence_ids,
    )
    assert ex.precheck(candidate.runbook_id, candidate.target_arn, params).passed
    assert calls == [{"NetworkAclIds": [acl_id]}] * 2


def _failure_log(caplog):
    """FAILED 1건당 실패 로그 1줄(#424) — 그 필드를 돌려준다."""
    records = [r for r in caplog.records if r.getMessage() == "agent_analysis_failed"]
    assert len(records) == 1
    fields = records[0].__dict__
    assert fields["incident_id"] == "inc-1" and fields["domain"] == "SECOPS"
    return fields


def test_secops_output_rejects_a_rule_number_not_selected_by_the_server(caplog):
    from agent_dispatcher import verify_graph_output

    data = graph_input()
    client = FakeAIModelClient([CandidateProposalOutput(candidates=[proposal()]), SUMMARY])
    output = run_secops_graph(data, client=client, nacl_rule_numbers={NACL: 1})
    output.candidates[0].parameters.rule_number = 2
    with caplog.at_level(logging.WARNING, logger="vigilantis.agent_dispatcher"):
        checked = verify_graph_output(data, output, data.incident_id, nacl_rule_numbers={NACL: 1})
    assert checked.invocation_status is AgentInvocationStatus.FAILED
    assert checked.candidates == []
    assert [r.reason_code for r in caplog.records
            if r.getMessage() == "agent_output_contract_violation"] == ["SERVER_VALUE_MISMATCH"]


@pytest.mark.parametrize("completed,step", [
    (0, "propose_candidates"), (1, "summarize_evidence"),
])
def test_node_failure_stops_later_model_calls(completed, step, caplog):
    client = FakeAIModelClient([CandidateProposalOutput(candidates=[])][:completed])
    with caplog.at_level(logging.WARNING, logger="vigilantis.ai.agent"):
        output = run_secops_graph(graph_input(), client=client, nacl_rule_numbers={NACL: 1})
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
    ({"cidr_block": None}, "CANDIDATE_CONTRACT_VIOLATION"),
    ({"evidence_ids": []}, "CANDIDATE_CONTRACT_VIOLATION"),
])
def test_invalid_proposal_fails_whole_graph(over, reason, caplog):
    client = FakeAIModelClient([CandidateProposalOutput(candidates=[proposal(**over)]), SUMMARY])
    with caplog.at_level(logging.WARNING, logger="vigilantis.ai.agent"):
        output = run_secops_graph(graph_input(), client=client, nacl_rule_numbers={NACL: 1})
    assert output.invocation_status is AgentInvocationStatus.FAILED
    assert len(client.sent) == 1  # 잘못된 후보는 요약 생성 전에 그래프를 중단시킨다.
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
