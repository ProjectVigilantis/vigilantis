"""SecOps 그래프 계약·노드 단락. 실제 모델·DB·AWS 호출은 하지 않는다.

기본 그래프는 근거 3줄 + decision 1회 호출 뒤 코드가 실행 계획을 조립한다(#583 2단계).
1단계 그래프(후보 생성 → 요약)는 SecOpsGraphVariant.CANDIDATE_FIRST 대비책으로 뒤쪽 테스트가 본다.
"""

import json
import logging

import pytest

from ai.agent import (
    SECOPS_FALLBACK_MODEL_CALLS, SECOPS_MODEL_CALLS, CandidateProposalOutput, EvidenceSummaryOutput,
    ProposedCandidate, SecOpsEvidenceDecisionOutput, SecOpsGraphVariant, run_secops_graph,
)
from ai.capabilities import build_secops_capabilities
from ai.claude_client import _schema_for
from ai.model_client import FakeAIModelClient
from logging_config import JsonLineFormatter
from schemas.agents import SecOpsGraphInput
from schemas.incidents import AgentInvocationStatus
from schemas.runbooks import RunbookId

EC2 = "arn:aws:ec2:ap-northeast-2:123456789012:instance/i-0abc"
NACL = "arn:aws:ec2:ap-northeast-2:123456789012:network-acl/acl-0abc"
SUMMARY = EvidenceSummaryOutput(observation="300초 동안 SSH 실패 120회",
                                diagnosis="SSH 반복 공격으로 추정", rationale="출발지 차단 필요")
FALLBACK = SecOpsGraphVariant.CANDIDATE_FIRST
NACL_MENU = {"runbook_id": "RUNBOOK_NACL_ADD_DENY", "purpose": "출발지 차단",
             "allowed_target_asset_types": ["NACL"]}


def graph_input(*, nacls=(NACL,), source_ip="203.0.113.10", capability=NACL_MENU):
    return SecOpsGraphInput.model_validate({
        "incident_id": "inc-1",
        "asset_context": {
            "arn": EC2, "resource_id": "i-0abc", "asset_type": "EC2",
            "resource_role": "PRIMARY", "account_id": "123456789012",
            "region": "ap-northeast-2", "state": "running", "spec": {},
            "relationships": [{"relation_type": "PROTECTED_BY", "target_arn": arn} for arn in nacls],
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
                "deduplication_key": "ssh:1", "payload": {"source_ip": source_ip,
                "failed_attempt_count": 120, "window_seconds": 300},
            },
        }}],
        "capabilities": [capability],
    })


def decided(decision):
    return SecOpsEvidenceDecisionOutput(
        observation="300초 동안 SSH 실패 120회", diagnosis="SSH 반복 공격으로 추정",
        rationale="출발지 차단을 제시하되 정상 통신 영향을 먼저 확인", decision=decision,
    )


def proposal(**over):
    values = dict(runbook_id=RunbookId.RUNBOOK_NACL_ADD_DENY, target_arn=NACL,
                  evidence_ids=["ev-1"], rule_number=100, cidr_block="203.0.113.10/32",
                  protocol="tcp")
    values.update(over)
    return ProposedCandidate(**values)


def _failure_log(caplog):
    """FAILED 1건당 실패 로그 1줄(#424) — 그 레코드를 돌려준다."""
    records = [r for r in caplog.records if r.getMessage() == "agent_analysis_failed"]
    assert len(records) == 1
    fields = records[0].__dict__
    assert fields["incident_id"] == "inc-1" and fields["domain"] == "SECOPS"
    return records[0]


# ------------------------------------------------------------------------------
# 근거 선행 그래프 — 기본
# ------------------------------------------------------------------------------


def test_evidence_first_is_one_call_and_the_code_assembles_the_nacl_candidate():
    """모델은 decision만 내고 대상·CIDR·protocol·rule number·근거는 입력에서 조립된다."""
    data = graph_input()
    before = data.model_dump(mode="json")
    client = FakeAIModelClient([decided([1])])
    output = run_secops_graph(data, client=client, nacl_rule_numbers={NACL: 7})
    assert output.invocation_status is AgentInvocationStatus.SUCCEEDED
    assert output.reviewed_risk_level is None
    assert output.summary_lines == [
        "300초 동안 SSH 실패 120회", "SSH 반복 공격으로 추정",
        "출발지 차단을 제시하되 정상 통신 영향을 먼저 확인",
    ]
    [candidate] = output.candidates
    assert candidate.runbook_id is RunbookId.RUNBOOK_NACL_ADD_DENY
    assert candidate.target_arn == NACL
    assert candidate.parameters.model_dump() == {
        "rule_number": 7, "cidr_block": "203.0.113.10/32", "protocol": "tcp",
    }
    assert candidate.evidence_ids == ["ev-1"]
    assert len(client.sent) == SECOPS_MODEL_CALLS == 1
    assert data.model_dump(mode="json") == before

    payload = client.sent[0]["user_payload"]
    # 메뉴는 목적 문구만 — Runbook ID·파라미터 명세·조치 대상 목록은 근거 호출에 없다(#243)
    assert payload["available_actions"] == [{"action_id": 1, "purpose": "출발지 차단"}]
    assert "capabilities" not in payload and "action_targets" not in payload
    assert "RUNBOOK_" not in json.dumps(payload, ensure_ascii=False)
    assert payload["initial_risk"]["initial_risk_level"] == "HIGH"
    assert payload["initial_risk_label"] == "높음"
    assert "reviewed_risk_level" not in payload
    assert payload["isolation_execution"] is None
    assert payload["asset"] == data.asset_context.model_dump(mode="json")


@pytest.mark.parametrize("basis, label", [
    ("SINGLE_ATTEMPT", "현재 규칙 기준 단발 시도"),
    ("COUNT_AND_RATE_MET", "현재 규칙 기준 반복 횟수와 빈도 조건을 모두 충족"),
    ("COUNT_ONLY_MET", "현재 규칙 기준 반복 횟수 조건만 충족하며 빈도 조건은 미충족"),
    ("RATE_ONLY_MET", "현재 규칙 기준 짧은 관측 창에 집중된 실패로 빈도 조건만 충족"),
    ("NEITHER_MET", "현재 규칙 기준 반복 횟수와 빈도 조건 모두 미충족"),
])
def test_current_rule_basis_is_sent_as_korean_text(basis, label):
    data = SecOpsGraphInput.model_validate({
        **graph_input().model_dump(), "current_rule_risk_basis": basis,
    })
    client = FakeAIModelClient([decided([])])
    output = run_secops_graph(data, client=client, nacl_rule_numbers={NACL: 1})
    assert output.invocation_status is AgentInvocationStatus.NO_PROPOSAL
    payload = client.sent[0]["user_payload"]
    assert payload["current_rule_risk_basis"] == label
    assert basis not in json.dumps(payload, ensure_ascii=False)
    assert payload["initial_risk"] == data.initial_risk.model_dump(mode="json")


def test_empty_decision_is_no_proposal_with_three_lines():
    client = FakeAIModelClient([decided([])])
    output = run_secops_graph(graph_input(), client=client, nacl_rule_numbers={NACL: 1})
    assert output.invocation_status is AgentInvocationStatus.NO_PROPOSAL
    assert len(output.summary_lines) == 3
    assert output.candidates == [] and output.reviewed_risk_level is None
    assert len(client.sent) == 1


def test_evidence_lines_precede_decision_in_the_requested_schema():
    """보장 ① — 결정을 싣는 호출의 스키마에서 근거 3줄이 decision보다 먼저 온다(#583)."""
    order = ["observation", "diagnosis", "rationale", "decision"]
    assert list(SecOpsEvidenceDecisionOutput.model_json_schema()["properties"]) == order
    # 실제로 나가는 스키마(Claude 어댑터 변환본)도 같은 순서다
    assert list(_schema_for(SecOpsEvidenceDecisionOutput)["properties"]) == order


@pytest.mark.parametrize("decision", [[2], [0], [1, 1]])
def test_decision_that_does_not_match_the_menu_fails_without_another_call(decision, caplog):
    """메뉴 밖·중복 decision은 출력 전체를 FAILED로 낸다. 모델을 다시 부르지 않는다."""
    client = FakeAIModelClient([decided(decision), decided([1])])
    with caplog.at_level(logging.WARNING, logger="vigilantis.ai.agent"):
        output = run_secops_graph(graph_input(), client=client, nacl_rule_numbers={NACL: 1})
    assert output.invocation_status is AgentInvocationStatus.FAILED
    assert output.summary_lines == [] and output.candidates == []
    assert output.reviewed_risk_level is None
    assert len(client.sent) == 1
    record = _failure_log(caplog)
    assert (record.failed_step, record.reason_code) == ("assemble_plan", "DECISION_MISMATCH")
    assert (record.decision_count, record.menu_size) == (len(decision), 1)
    # 모델이 낸 action_id 값은 로그에 남지 않는다 — 건수뿐이다
    logged = json.loads(JsonLineFormatter().format(record))
    assert "decision" not in logged and decision not in list(logged.values())


@pytest.mark.parametrize("data,numbers,reason,details", [
    # 메뉴 빌더는 이 입력에 메뉴를 내지 않지만(ai/capabilities.py) 조립은 대상을 다시 센다
    (graph_input(nacls=(NACL, NACL.replace("acl-0abc", "acl-0def"))), {NACL: 1},
     "SERVER_PARAMETER_UNAVAILABLE", {"target_count": 2}),
    (graph_input(), {}, "SERVER_PARAMETER_UNAVAILABLE", {}),
    (graph_input(source_ip="2001:db8::10"), {NACL: 1}, "SOURCE_ADDRESS_UNSUPPORTED", {}),
    (graph_input(capability={"runbook_id": "RUNBOOK_EC2_ISOLATE", "purpose": "격리",
                             "allowed_target_asset_types": ["EC2"]}), {NACL: 1},
     "SERVER_PARAMETER_UNAVAILABLE", {"runbook_id": "RUNBOOK_EC2_ISOLATE"}),
], ids=["two_nacls", "no_rule_number", "ipv6_source", "no_assembly_rule"])
def test_unassemblable_decision_fails_the_whole_output_without_another_call(
    data, numbers, reason, details, caplog,
):
    client = FakeAIModelClient([decided([1]), decided([1])])
    with caplog.at_level(logging.WARNING, logger="vigilantis.ai.agent"):
        output = run_secops_graph(data, client=client, nacl_rule_numbers=numbers)
    assert output.invocation_status is AgentInvocationStatus.FAILED
    assert output.failure_reason.value == reason
    assert output.summary_lines == [] and output.candidates == []
    assert len(client.sent) == 1
    record = _failure_log(caplog)
    assert (record.failed_step, record.reason_code) == ("assemble_plan", reason)
    for key, value in details.items():
        assert getattr(record, key) == value


def test_model_failure_skips_plan_assembly(caplog):
    client = FakeAIModelClient([])
    with caplog.at_level(logging.WARNING, logger="vigilantis.ai.agent"):
        output = run_secops_graph(graph_input(), client=client, nacl_rule_numbers={NACL: 1})
    assert output.invocation_status is AgentInvocationStatus.FAILED
    assert output.summary_lines == [] and output.candidates == []
    assert len(client.sent) == 1
    record = _failure_log(caplog)
    assert (record.failed_step, record.reason_code) == ("summarize_evidence", "MODEL_UNAVAILABLE")


# ------------------------------------------------------------------------------
# 1단계 그래프 — SECOPS_GRAPH_VARIANT=CANDIDATE_FIRST 대비책
# ------------------------------------------------------------------------------


def test_fallback_two_calls_and_null_reviewed_risk():
    """대비책은 후보·요약 2회다. 등급 호출이 없어 reviewed_risk_level은 null이다(ADR-0005 1차 개정)."""
    data = graph_input()
    before = data.model_dump(mode="json")
    client = FakeAIModelClient([CandidateProposalOutput(candidates=[proposal()]), SUMMARY])
    output = run_secops_graph(data, client=client, nacl_rule_numbers={NACL: 1}, variant=FALLBACK)
    assert output.invocation_status is AgentInvocationStatus.SUCCEEDED
    assert output.reviewed_risk_level is None
    assert output.candidates[0].target_arn == NACL
    assert len(client.sent) == SECOPS_FALLBACK_MODEL_CALLS == 2
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


def test_fallback_no_proposal_keeps_summary_with_null_reviewed_risk():
    client = FakeAIModelClient([CandidateProposalOutput(candidates=[]), SUMMARY])
    output = run_secops_graph(graph_input(), client=client, nacl_rule_numbers={NACL: 1},
                              variant=FALLBACK)
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
    """서버 조회·조립된 후보·Workflow 검증·precheck가 같은 빈 번호를 쓰고, 없으면 후보를 만들지 않는다."""
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
    client = FakeAIModelClient([decided([1])])
    output = run_secops_graph(data, client=client, nacl_rule_numbers=numbers)
    # 코드가 조립한 후보는 Workflow 검증(_contract_violation)을 그대로 통과한다
    assert verify_graph_output(data, output, data.incident_id, nacl_rule_numbers=numbers) == output
    candidate = output.candidates[0]
    assert candidate.target_arn == target
    assert candidate.parameters.rule_number == expected
    params = build_precheck_parameters(
        candidate.runbook_id, candidate.parameters, resource_id=acl_id, evidence_ids=candidate.evidence_ids,
    )
    assert ex.precheck(candidate.runbook_id, candidate.target_arn, params).passed
    assert calls == [{"NetworkAclIds": [acl_id]}] * 2


def test_secops_output_rejects_a_rule_number_not_selected_by_the_server(caplog):
    from agent_dispatcher import verify_graph_output

    data = graph_input()
    client = FakeAIModelClient([decided([1])])
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
def test_fallback_node_failure_stops_later_model_calls(completed, step, caplog):
    client = FakeAIModelClient([CandidateProposalOutput(candidates=[])][:completed])
    with caplog.at_level(logging.WARNING, logger="vigilantis.ai.agent"):
        output = run_secops_graph(graph_input(), client=client, nacl_rule_numbers={NACL: 1},
                                  variant=FALLBACK)
    assert output.invocation_status is AgentInvocationStatus.FAILED
    assert output.summary_lines == [] and output.candidates == []
    assert output.reviewed_risk_level is None
    assert len(client.sent) == completed + 1
    record = _failure_log(caplog)
    assert record.failed_step == step and record.reason_code == "MODEL_UNAVAILABLE"


@pytest.mark.parametrize("over,reason", [
    ({"target_arn": EC2}, "TARGET_NOT_ALLOWED"),
    ({"target_arn": NACL.replace("acl-0abc", "acl-0def")}, "TARGET_NOT_ALLOWED"),
    ({"runbook_id": RunbookId.RUNBOOK_NACL_RESTORE}, "RUNBOOK_NOT_OFFERED"),
    ({"cidr_block": None}, "CANDIDATE_CONTRACT_VIOLATION"),
    ({"evidence_ids": []}, "CANDIDATE_CONTRACT_VIOLATION"),
])
def test_fallback_invalid_proposal_fails_whole_graph(over, reason, caplog):
    client = FakeAIModelClient([CandidateProposalOutput(candidates=[proposal(**over)]), SUMMARY])
    with caplog.at_level(logging.WARNING, logger="vigilantis.ai.agent"):
        output = run_secops_graph(graph_input(), client=client, nacl_rule_numbers={NACL: 1},
                                  variant=FALLBACK)
    assert output.invocation_status is AgentInvocationStatus.FAILED
    assert len(client.sent) == 1  # 잘못된 후보는 요약 생성 전에 그래프를 중단시킨다.
    # 메뉴 밖·대상 밖(주입 의심)과 형식 위반을 사유로 가른다. 대상 밖이면 그 대상을 남긴다
    fields = _failure_log(caplog).__dict__
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
