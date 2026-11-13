"""LangGraph FinOps 그래프 테스트 (Issue #209).

모델은 FakeAIModelClient로만 부른다 — 실제 API 호출 0회다. 확인하는 것은 셋이다.
① 최종 상태 3갈래(SUCCEEDED·NO_PROPOSAL·FAILED)가 준비한 응답만으로 재현되는가
② 모델이 지어낼 수 없는 값(메뉴 밖 Runbook·대상 밖 ARN)이 FAILED로 막히는가
③ 모델로 나간 값이 마스킹 경로를 지났는가

프롬프트 문구의 품질은 여기서 보지 않는다. 문구·필드명·출력 스키마가 바뀌었는데 승인
스냅샷이 갱신되지 않은 것만 잡는다(#243 — 재통과 절차는 apps/core-api/ai/evaluation/summary/baseline.md).
"""

import io
import json
import logging
from pathlib import Path
from types import SimpleNamespace

import httpx2
import pytest
from ai.agent import (
    _FINOPS_PROPOSAL_SYSTEM_PROMPT,
    _FINOPS_SUMMARY_SYSTEM_PROMPT,
    _PARAMETER_CONSTRAINTS,
    FINOPS_PROMPT_VERSION,
    CandidateProposalOutput,
    EvidenceSummaryOutput,
    FinOpsCandidateProposalOutput,
    FinOpsEvidenceDecisionOutput,
    FinOpsProposedCandidate,
    ProposedCandidate,
    _capability_payload,
    finops_prompt_fingerprint,
    finops_prompt_material,
    finops_request_fingerprint,
    run_finops_graph,
)
from ai.model_client import (
    AIModelAdapterError,
    AIModelContractError,
    AIModelRejectedError,
    FailureKind,
    FakeAIModelClient,
)
from ai.model_retry import RetryPolicy
from ai.openai_client import OpenAIModelClient
from logging_config import JsonLineFormatter
from openai import APITimeoutError
from pydantic import ValidationError
from schemas.agents import FinOpsGraphInput
from schemas.incidents import AgentInvocationStatus
from schemas.runbook_parameters import (
    CANDIDATE_PARAMETER_MODELS,
    Ec2EnableAutoscalingCandidateParameters,
    Ec2RightsizingCandidateParameters,
)

ACCOUNT = "123456789012"
REGION = "ap-northeast-2"
EC2_ARN = f"arn:aws:ec2:{REGION}:{ACCOUNT}:instance/i-0abc123456789def0"
VOLUME_ARN = f"arn:aws:ec2:{REGION}:{ACCOUNT}:volume/vol-0abc123456789def0"
OTHER_ARN = f"arn:aws:ec2:{REGION}:{ACCOUNT}:instance/i-0fff888877776666e"

ASSET_CONTEXT = {
    "arn": EC2_ARN,
    "resource_id": "i-0abc123456789def0",
    "asset_type": "EC2",
    "resource_role": "PRIMARY",
    "account_id": ACCOUNT,
    "region": REGION,
    "state": "running",
    "spec": {"instance_type": "t3.xlarge"},
    # 조치 대상이 관계 자산일 수 있다 — EBS 삭제 후보의 target_arn이 여기서 나온다
    "relationships": [{"relation_type": "ATTACHED_TO", "target_arn": VOLUME_ARN}],
    "evaluation_status": "COMPLETED",
    "health_score": 3,
    "verdict": "COST_CANDIDATE",
    "collected_at": "2026-08-31T09:00:00Z",
}

RULE_RESULT = {
    "asset_arn": EC2_ARN,
    "collection_run_id": "run-20260831-001",
    "evaluation_status": "COMPLETED",
    "verdict": "COST_CANDIDATE",
    "health_score": 3,
    "reason": "3일 평균 CPU 3%",
    "evaluated_at": "2026-08-31T09:00:00Z",
}

EVIDENCE = {
    "evidence_id": "ev-0001",
    "evidence_type": "RULE",
    "content": {"evaluation": RULE_RESULT},
}

RIGHTSIZING_CAPABILITY = {
    "runbook_id": "RUNBOOK_EC2_RIGHTSIZING",
    "purpose": "과대 스펙 EC2 다운사이징",
    "allowed_target_asset_types": ["EC2"],
}

EBS_CAPABILITY = {
    "runbook_id": "RUNBOOK_EBS_DELETE_UNATTACHED",
    "purpose": "미연결 EBS 볼륨 삭제",
    "allowed_target_asset_types": ["EBS"],
}

def summary(*decision) -> FinOpsEvidenceDecisionOutput:
    return FinOpsEvidenceDecisionOutput(
        observation="t3.xlarge 인스턴스의 3일 평균 CPU가 3%다.",
        diagnosis="현재 스펙에 비해 사용률이 낮아 과대 스펙으로 보인다.",
        rationale="3일 내내 낮은 사용률이라 다운사이징으로 비용을 줄일 수 있다.",
        decision=list(decision),
    )


# 메뉴 순서가 action_id다 — 1은 다운사이징, 2는 EBS 삭제
SUMMARY = summary(1)
SUMMARY_EBS = summary(2)
SUMMARY_BOTH = summary(1, 2)


def make_input(**over) -> FinOpsGraphInput:
    base = {
        "domain": "FINOPS",
        "incident_id": "inc-20260831-001",
        "asset_context": ASSET_CONTEXT,
        "rule_evaluation": RULE_RESULT,
        "evidences": [EVIDENCE],
        "capabilities": [RIGHTSIZING_CAPABILITY, EBS_CAPABILITY],
    }
    base.update(over)
    return FinOpsGraphInput.model_validate(base)


def rightsizing_proposal(**over) -> FinOpsProposedCandidate:
    base = {
        "runbook_id": "RUNBOOK_EC2_RIGHTSIZING",
        "evidence_ids": ["ev-0001"],
    }
    base.update(over)
    return FinOpsProposedCandidate.model_validate(base)


def ebs_proposal(**over) -> FinOpsProposedCandidate:
    return rightsizing_proposal(runbook_id="RUNBOOK_EBS_DELETE_UNATTACHED", **over)


def proposals(*candidates) -> FinOpsCandidateProposalOutput:
    return FinOpsCandidateProposalOutput(candidates=list(candidates))


def run(*outputs, graph_input=None):
    """준비한 모델 응답으로 그래프를 1회 돌린다. 응답이 모자라면 그 호출이 실패한다."""
    client = FakeAIModelClient(list(outputs))
    output = run_finops_graph(graph_input or make_input(), client=client)
    return output, client


# ------------------------------------------------------------------------------
# 최종 상태 3갈래
# ------------------------------------------------------------------------------


def test_succeeded_carries_three_summary_lines_and_candidate():
    output, client = run(SUMMARY, proposals(rightsizing_proposal()))

    assert output.invocation_status == AgentInvocationStatus.SUCCEEDED
    assert output.summary_lines == [
        SUMMARY.observation,
        SUMMARY.diagnosis,
        SUMMARY.rationale,
    ]
    assert len(output.candidates) == 1
    candidate = output.candidates[0]
    assert candidate.runbook_id.value == "RUNBOOK_EC2_RIGHTSIZING"
    assert candidate.target_arn == EC2_ARN
    assert candidate.evidence_ids == ["ev-0001"]
    assert isinstance(candidate.parameters, Ec2RightsizingCandidateParameters)
    assert candidate.parameters.target_instance_type == "t3.medium"
    # 추정은 Workflow 후속 처리다. 그래프는 미실행을 오류로 만들지 않는다.
    assert len(client.sent) == 2



def test_other_runbook_uses_only_summary_and_proposal_calls():
    output, client = run(SUMMARY_EBS, proposals(ebs_proposal()))
    assert len(client.sent) == 2

    assert output.invocation_status is AgentInvocationStatus.SUCCEEDED


def test_target_is_the_input_asset_even_for_a_related_asset_runbook():
    # 대상은 모델이 아니라 코드가 정한다 — 관계 자산(EBS 볼륨)이 있어도 입력 자산의 ARN이다
    output, _ = run(SUMMARY_EBS, proposals(ebs_proposal()))

    assert output.candidates[0].target_arn == EC2_ARN
    assert VOLUME_ARN not in {candidate.target_arn for candidate in output.candidates}


def test_two_actions_in_the_decision_yield_two_candidates():
    output, client = run(SUMMARY_BOTH, proposals(ebs_proposal(), rightsizing_proposal()))

    assert output.invocation_status is AgentInvocationStatus.SUCCEEDED
    assert {c.runbook_id.value for c in output.candidates} == {
        "RUNBOOK_EC2_RIGHTSIZING", "RUNBOOK_EBS_DELETE_UNATTACHED",
    }
    assert len(client.sent) == 2


def test_empty_candidates_are_a_decision_mismatch_not_no_proposal(caplog):
    # decision이 비어 있지 않으므로 빈 후보는 업무 판단(NO_PROPOSAL)이 아니라 전사 불일치다
    fields = _failure_fields(caplog, SUMMARY, proposals())

    assert fields["reason_code"] == "DECISION_MISMATCH"
    assert fields["failed_step"] == "validate_output_contract"
    assert (fields["decision_count"], fields["candidate_count"]) == (1, 0)


# ------------------------------------------------------------------------------
# decision — 메뉴 대응 검사와 전사
# ------------------------------------------------------------------------------


@pytest.mark.parametrize("decision", [[], [0], [3], [-1], [1, 1], [1, 3]])
def test_invalid_decision_fails_without_a_proposal_call(caplog, decision):
    # 메뉴(2건)와 맞지 않는 decision은 후보 호출 없이 FAILED다 — 다른 선택을 얻으려 다시 부르지 않는다
    fields = _failure_fields(caplog, summary(*decision), proposals(rightsizing_proposal()))

    assert fields["reason_code"] == "DECISION_MISMATCH"
    assert fields["failed_step"] == "summarize_evidence"
    assert (fields["decision_count"], fields["menu_size"]) == (len(decision), 2)
    # 로그에는 모델이 낸 값이 아니라 건수만 남는다
    assert "decision" not in fields


def test_rejected_decision_makes_exactly_one_model_call():
    client = FakeAIModelClient([summary(), proposals(rightsizing_proposal())])
    output = run_finops_graph(make_input(), client=client)

    assert output.invocation_status == AgentInvocationStatus.FAILED
    assert output.failure_reason.value == "DECISION_MISMATCH"
    assert output.summary_lines == [] and output.candidates == []
    assert len(client.sent) == 1


@pytest.mark.parametrize("decision", [["1"], [1.5], [True], "1"])
def test_decision_type_violations_are_model_contract_errors_not_mismatches(decision):
    # 정수가 아닌 값은 모델 경계의 구조화 출력 검증이 거절한다 — 그래프의 대응 검사까지 오지 않는다
    with pytest.raises(ValidationError):
        FinOpsEvidenceDecisionOutput(
            observation="a", diagnosis="b", rationale="c", decision=decision
        )


def test_model_contract_failure_on_summary_is_not_reported_as_a_mismatch(caplog):
    class _ContractClient:
        def complete(self, request, response_model):
            raise AIModelContractError("정수가 아닌 decision", phase="response")

    fields = _failure_fields(caplog, client=_ContractClient())

    assert fields["reason_code"] == "MODEL_CONTRACT_VIOLATION"
    assert fields["failed_step"] == "summarize_evidence"


@pytest.mark.parametrize(
    "decided,candidates",
    [
        (SUMMARY_BOTH, [rightsizing_proposal()]),                       # 개수 부족
        (SUMMARY, [rightsizing_proposal(), ebs_proposal()]),            # 개수 초과
        (SUMMARY, [ebs_proposal()]),                                    # 같은 개수, 다른 Runbook
        (SUMMARY_BOTH, [rightsizing_proposal(), rightsizing_proposal()]),  # 개수는 같고 중복
        (SUMMARY, [rightsizing_proposal(runbook_id="RUNBOOK_NACL_ADD_DENY")]),  # 메뉴 밖
        (SUMMARY, [rightsizing_proposal(runbook_id="RUNBOOK_EC2_REVERT_SIZE")]),  # 롤백 런북
    ],
    ids=["missing", "extra", "wrong_runbook", "duplicate", "off_menu", "rollback"],
)
def test_candidates_must_match_the_decision_before_conversion(caplog, decided, candidates):
    fields = _failure_fields(caplog, decided, proposals(*candidates))

    assert fields["reason_code"] == "DECISION_MISMATCH"
    assert fields["failed_step"] == "validate_output_contract"


def test_mismatch_is_checked_before_candidate_contract_errors(caplog):
    # 파라미터가 빠진 후보라도 대응이 먼저 어긋나면 사유는 DECISION_MISMATCH다
    broken = ebs_proposal(evidence_ids=[])
    fields = _failure_fields(caplog, SUMMARY, proposals(broken))

    assert fields["reason_code"] == "DECISION_MISMATCH"


def test_accepted_decision_stays_fixed_through_proposal_client_retries():
    # 후보 호출이 한 번 시간 초과로 재시도돼도 요약·decision은 다시 만들지 않는다
    calls = []
    results = [
        _sdk_completion(summary(2)),
        APITimeoutError(httpx2.Request("POST", "https://api.openai.com/v1/chat/completions")),
        _sdk_completion(proposals(ebs_proposal())),
    ]

    def parse(**kwargs):
        calls.append(kwargs)
        outcome = results.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    client = OpenAIModelClient(
        client=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(parse=parse))),
        model="m",
        policy=RetryPolicy(timeout_seconds=1.0, max_attempts=2, retry_backoff_seconds=0.0),
    )
    output = run_finops_graph(make_input(), client=client)

    assert output.invocation_status == AgentInvocationStatus.SUCCEEDED
    assert [call["response_format"] for call in calls] == [
        FinOpsEvidenceDecisionOutput, FinOpsCandidateProposalOutput, FinOpsCandidateProposalOutput,
    ]
    # 두 후보 시도는 같은 요청이고 받아들인 decision [2]를 싣는다
    assert calls[1]["messages"] == calls[2]["messages"]
    assert json.loads(calls[1]["messages"][1]["content"])["decision"] == [2]


def test_failed_when_summary_call_fails_and_proposal_is_skipped():
    # 준비한 응답이 없으면 첫 호출이 경계 예외로 실패한다
    output, client = run()

    assert output.invocation_status == AgentInvocationStatus.FAILED
    assert output.summary_lines == []
    assert output.candidates == []
    # 요약이 실패하면 후보 생성 노드를 건너뛴다
    assert len(client.sent) == 1


def test_failed_when_proposal_call_fails():
    output, client = run(SUMMARY)

    assert output.invocation_status == AgentInvocationStatus.FAILED
    assert output.summary_lines == []
    assert len(client.sent) == 2


def test_finops_output_never_carries_reviewed_risk_level():
    # 위험도 재평가는 SecOps 전용 노드다 — FinOps 그래프에는 그 노드가 없다
    for outputs in (
        (SUMMARY, proposals(rightsizing_proposal())),
        (SUMMARY, proposals()),
        (),
    ):
        output, _ = run(*outputs)
        assert output.reviewed_risk_level is None


# ------------------------------------------------------------------------------
# 모델이 지어낼 수 없는 값
# ------------------------------------------------------------------------------


def test_failed_when_runbook_is_outside_offered_capabilities():
    # 구조화 출력의 runbook_id는 정적 enum이라 메뉴 밖 값이 나올 수 있다.
    # NACL 차단은 AI 추천 7종이라 계약 검증만으로는 통과한다.
    proposal = rightsizing_proposal(
        runbook_id="RUNBOOK_NACL_ADD_DENY",
        rule_number=100,
        cidr_block="203.0.113.0/24",
        protocol="-1",
    )
    output, _ = run(SUMMARY, proposals(proposal))

    assert output.invocation_status == AgentInvocationStatus.FAILED
    assert output.candidates == []


def test_failed_when_rollback_runbook_is_proposed():
    proposal = rightsizing_proposal(runbook_id="RUNBOOK_EC2_REVERT_SIZE")
    output, _ = run(SUMMARY, proposals(proposal))

    assert output.invocation_status == AgentInvocationStatus.FAILED


def test_model_has_no_slot_for_the_target_arn():
    # 대상은 코드가 정한다 — 모델이 대상을 실어 보내면 구조화 출력 검증이 거절한다
    with pytest.raises(ValidationError):
        rightsizing_proposal(target_arn=OTHER_ARN)
    assert "target_arn" not in FinOpsProposedCandidate.model_fields


def test_failed_when_required_parameter_is_missing():
    # AI가 정해야 하는 값이 빠지면 서버가 대신 채우지 않는다
    autoscaling = {
        "runbook_id": "RUNBOOK_EC2_ENABLE_AUTOSCALING",
        "purpose": "고정 대수 EC2를 Auto Scaling 그룹으로 전환",
        "allowed_target_asset_types": ["EC2"],
    }
    proposal = rightsizing_proposal(runbook_id="RUNBOOK_EC2_ENABLE_AUTOSCALING", min_size=1)
    output, _ = run(
        SUMMARY, proposals(proposal), graph_input=make_input(capabilities=[autoscaling])
    )

    assert output.invocation_status == AgentInvocationStatus.FAILED


def test_model_has_no_slot_for_the_rightsizing_target():
    # 목표 타입은 서버 규칙이 정한다(#251) — 모델 출력에 자리가 없어 실어 보내면 거절된다
    with pytest.raises(ValidationError):
        rightsizing_proposal(target_instance_type="t3.nano")


@pytest.mark.parametrize(
    "current,expected",
    [("t3.xlarge", "t3.medium"), ("t3.large", "t3.small"), ("t3a.medium", "t3a.small")],
)
def test_rightsizing_target_is_computed_from_the_asset_snapshot(current, expected):
    graph_input = make_input(asset_context={**ASSET_CONTEXT, "spec": {"instance_type": current}})
    output, _ = run(SUMMARY, proposals(rightsizing_proposal()), graph_input=graph_input)

    assert output.invocation_status == AgentInvocationStatus.SUCCEEDED
    assert output.candidates[0].parameters.target_instance_type == expected


@pytest.mark.parametrize("current", ["t3.small", "m5.large", "c5.large", None])
def test_failed_when_the_server_cannot_compute_the_target(current):
    # 메뉴 빌더가 이런 자산에는 다운사이징을 올리지 않는다(ai/capabilities.py 축 ③).
    # 메뉴를 거치지 않은 입력이 와도 규칙이 내지 않은 값을 지어 채우지 않는다
    graph_input = make_input(asset_context={**ASSET_CONTEXT, "spec": {"instance_type": current}})
    output, _ = run(SUMMARY, proposals(rightsizing_proposal()), graph_input=graph_input)

    assert output.invocation_status == AgentInvocationStatus.FAILED


def test_failed_when_one_candidate_of_two_violates_contract():
    # 성한 후보만 남기고 넘기지 않는다 — NO_PROPOSAL·SUCCEEDED 둘 다 업무 판단이라
    # 형식 실패를 거기에 접으면 서버가 하지 않은 판단이 남는다
    output, _ = run(
        SUMMARY_BOTH, proposals(ebs_proposal(), rightsizing_proposal(evidence_ids=[]))
    )

    assert output.invocation_status == AgentInvocationStatus.FAILED
    assert output.candidates == []


def test_failed_when_evidence_ids_are_empty():
    proposal = rightsizing_proposal(evidence_ids=[])
    output, _ = run(SUMMARY, proposals(proposal))

    assert output.invocation_status == AgentInvocationStatus.FAILED


def test_evidence_ids_follow_the_input_order_not_the_model_order():
    # 첫 항목이 실행 파라미터 evidence_id가 된다 — 순서는 모델이 아니라 입력이 정한다
    # (#251 v2 재계측에서 같은 두 근거의 순서만 뒤집힌 회차가 나왔다)
    from schemas.assets import MetricName

    metric = {
        "evidence_id": "ev-0002",
        "evidence_type": "METRIC",
        "content": {
            "metric_name": MetricName.CPU_UTILIZATION.value,
            "window_start": "2026-08-28T09:00:00Z",
            "window_end": "2026-08-31T09:00:00Z",
            "summary": {"cpu_datapoints": 72, "cpu_avg": 3.0, "cpu_max": 9.0},
        },
    }
    graph_input = make_input(evidences=[EVIDENCE, metric])
    proposal = rightsizing_proposal(evidence_ids=["ev-0002", "ev-0001"])
    output, _ = run(SUMMARY, proposals(proposal), graph_input=graph_input)

    assert output.invocation_status == AgentInvocationStatus.SUCCEEDED
    assert output.candidates[0].evidence_ids == ["ev-0001", "ev-0002"]


def test_evidence_ids_outside_the_input_are_kept_for_the_workflow_to_reject():
    # 그래프는 거르지 않는다 — 입력 밖 인용의 거절은 Workflow ⓐ가 한다(agent_dispatcher.py)
    proposal = rightsizing_proposal(evidence_ids=["ev-unknown", "ev-0001"])
    output, _ = run(SUMMARY, proposals(proposal))

    assert output.candidates[0].evidence_ids == ["ev-0001", "ev-unknown"]


def test_parameters_of_other_runbooks_are_dropped():
    # 고른 Runbook이 받지 않는 키를 모델이 채워도 실행으로 나가지 않는다
    proposal = rightsizing_proposal(rule_number=100, cidr_block="203.0.113.0/24")
    output, _ = run(SUMMARY, proposals(proposal))

    assert output.invocation_status == AgentInvocationStatus.SUCCEEDED
    assert output.candidates[0].parameters.model_dump() == {"target_instance_type": "t3.medium"}


# ------------------------------------------------------------------------------
# 모델로 나가는 페이로드
# ------------------------------------------------------------------------------


def test_outbound_payload_is_masked():
    # RULE 근거와 최상위는 같은 객체여야 하므로(FinOpsGraphInput 계약, #265) 둘 다 바꾼다.
    # 페이로드에 실리는 쪽은 근거이고, 마스킹이 그 content까지 닿는지가 이 테스트다.
    rule = dict(RULE_RESULT, reason="수집 계정 키 AKIAIOSFODNN7EXAMPLE 로 조회함")
    graph_input = make_input(
        rule_evaluation=rule,
        evidences=[dict(EVIDENCE, content={"evaluation": rule})],
    )
    _, client = run(SUMMARY, proposals(rightsizing_proposal()), graph_input=graph_input)

    for sent in client.sent:
        assert "AKIAIOSFODNN7EXAMPLE" not in sent["user_json"]
        assert "[REDACTED]" in sent["user_json"]


def test_proposal_payload_carries_the_menu_and_summary():
    _, client = run(SUMMARY, proposals(rightsizing_proposal()))
    proposal_payload = client.sent[1]["user_payload"]

    # 대상은 코드가 정하므로 허용 대상 목록을 싣지 않는다
    assert "allowed_target_arns" not in proposal_payload
    assert [(c["action_id"], c["runbook_id"]) for c in proposal_payload["capabilities"]] == [
        (1, "RUNBOOK_EC2_RIGHTSIZING"),
        (2, "RUNBOOK_EBS_DELETE_UNATTACHED"),
    ]
    assert proposal_payload["summary_lines"] == [
        SUMMARY.observation,
        SUMMARY.diagnosis,
        SUMMARY.rationale,
    ]
    assert proposal_payload["decision"] == [1]


def test_proposal_capabilities_are_the_shared_payload_plus_action_id():
    # action_id만 더하고 나머지는 SecOps와 공유하는 capability 명세 그대로다
    graph_input = make_input()
    _, client = run(SUMMARY, proposals(rightsizing_proposal()), graph_input=graph_input)
    sent = client.sent[1]["user_payload"]["capabilities"]

    for index, (item, capability) in enumerate(zip(sent, graph_input.capabilities), start=1):
        assert item == {"action_id": index, **_capability_payload(capability)}


def test_proposal_payload_carries_required_parameters_per_runbook():
    # 이걸 빼면 모델은 어느 키를 채워야 하는지 알 수 없고, 빈 값으로 온 후보가
    # 계약 검증에서 거절되어 호출 전체가 FAILED가 된다(#209 실제 호출에서 확인)
    autoscaling = {
        "runbook_id": "RUNBOOK_EC2_ENABLE_AUTOSCALING",
        "purpose": "고정 대수 EC2를 Auto Scaling 그룹으로 전환",
        "allowed_target_asset_types": ["EC2"],
    }
    graph_input = make_input(capabilities=[RIGHTSIZING_CAPABILITY, autoscaling, EBS_CAPABILITY])
    _, client = run(SUMMARY, proposals(rightsizing_proposal()), graph_input=graph_input)
    by_id = {c["runbook_id"]: c for c in client.sent[1]["user_payload"]["capabilities"]}

    assert by_id["RUNBOOK_EC2_ENABLE_AUTOSCALING"]["required_parameters"] == ["max_size", "min_size"]
    assert set(by_id["RUNBOOK_EC2_ENABLE_AUTOSCALING"]["parameter_schema"]) == {"max_size", "min_size"}

    # 목표 타입은 그래프가 규칙으로 계산한다(#251) — 명세에 실으면 모델에게 채우라는 지시가 된다
    assert by_id["RUNBOOK_EC2_RIGHTSIZING"]["required_parameters"] == []
    assert by_id["RUNBOOK_EC2_RIGHTSIZING"]["parameter_schema"] == {}

    # AI가 정할 값이 0개인 Runbook은 빈 목록이다 — 채울 자리가 없다는 것도 정보다
    assert by_id["RUNBOOK_EBS_DELETE_UNATTACHED"]["required_parameters"] == []
    assert by_id["RUNBOOK_EBS_DELETE_UNATTACHED"]["parameter_schema"] == {}


def test_rule_evaluation_is_not_sent_twice():
    # RuleEvidence는 RuleEvaluationResult를 그대로 감싼 모델이라, RULE 근거가 있으면
    # 최상위 rule_evaluation은 그 복사본이다. 후보 호출이 이 페이로드를 다시 보내므로
    # 그대로 두면 같은 판정이 한 실행에서 네 번 나간다
    _, client = run(SUMMARY, proposals(rightsizing_proposal()))

    for sent in client.sent[:2]:
        payload = sent["user_payload"]
        assert "rule_evaluation" not in payload
        # 판정은 사라지지 않는다 — 근거 쪽에 그대로 있다
        assert payload["evidences"][0]["content"]["evaluation"]["verdict"] == (
            RULE_RESULT["verdict"]
        )


def test_rule_evaluation_is_sent_when_no_evidence_carries_it():
    # 근거가 없거나 값이 다르면 빼지 않는다 — 빼면 판정이 페이로드에서 사라진다
    _, client = run(SUMMARY, proposals(), graph_input=make_input(evidences=[]))

    assert client.sent[0]["user_payload"]["rule_evaluation"]["verdict"] == (
        RULE_RESULT["verdict"]
    )


@pytest.mark.parametrize("carried_by_evidence", [True, False])
def test_summary_payload_carries_action_purposes_but_not_runbook_ids(carried_by_evidence):
    # rationale이 카드의 조치를 설명하려면 메뉴에 무엇이 있는지 알아야 한다(#243). 다만
    # 요약 노드는 action_id로 고르므로 Runbook 이름·파라미터 명세·허용 대상은 싣지 않는다
    # — 이름을 주면 문장이 이름을 되읽고, 메뉴 전체를 열거한다(v1 2차 실측)
    graph_input = make_input(evidences=[EVIDENCE] if carried_by_evidence else [])
    _, client = run(SUMMARY, proposals(), graph_input=graph_input)
    summary_payload = client.sent[0]["user_payload"]

    # 메뉴는 안정된 1부터의 action_id와 목적 문구뿐이다
    assert summary_payload["available_actions"] == [
        {"action_id": 1, "purpose": "과대 스펙 EC2 다운사이징"},
        {"action_id": 2, "purpose": "미연결 EBS 볼륨 삭제"},
    ]
    assert "capabilities" not in summary_payload
    assert "RUNBOOK_EC2_RIGHTSIZING" not in client.sent[0]["user_json"]
    assert "allowed_target_arns" not in summary_payload
    assert summary_payload["incident_id"] == "inc-20260831-001"
    assert [e["evidence_id"] for e in summary_payload["evidences"]] == (
        ["ev-0001"] if carried_by_evidence else []
    )
    assert "skip_reason_code" not in client.sent[0]["user_json"]
    assert summary_payload["asset"]["verdict"] == "COST_CANDIDATE"
    evaluation = (
        summary_payload["evidences"][0]["content"]["evaluation"]
        if carried_by_evidence
        else summary_payload["rule_evaluation"]
    )
    assert evaluation["verdict"] == "COST_CANDIDATE"
    assert "skip_reason_code" in client.sent[1]["user_json"]
    assert graph_input.asset_context.skip_reason_code is None


def test_payload_is_json_serializable_through_the_boundary():
    # python 모드로 덤프하면 AssetItem.collected_at이 datetime으로 남아 경계가 세운다
    _, client = run(SUMMARY, proposals())

    assert client.sent[0]["user_payload"]["asset"]["collected_at"].startswith("2026-08-31")


@pytest.mark.parametrize("call_index", [0, 1])
def test_every_call_goes_through_the_masking_boundary(call_index):
    _, client = run(SUMMARY, proposals(rightsizing_proposal()))

    # build_outbound_payload()가 만든 키 3종이 그대로 있어야 경계를 지난 것이다
    assert set(client.sent[call_index]) == {"system_prompt", "user_payload", "user_json"}


# ------------------------------------------------------------------------------
# 프롬프트 v1 — 판·해시·계약 사실 (Issue #243)
# ------------------------------------------------------------------------------

SNAPSHOT = Path(__file__).resolve().parents[1] / "evaluation" / "summary" / "summary_prompt_snapshot.json"


def test_prompt_fingerprint_matches_approved_snapshot():
    # 문구·필드명·제약 문구·출력 스키마 중 하나라도 바뀌면 여기서 선다. 고의로 바꿨다면
    # apps/core-api/ai/evaluation/summary/baseline.md의 재통과 절차를 거친 뒤 스냅샷을 갱신한다 — 이 테스트가
    # 있어야 "판 올리기를 잊어도 드러난다"가 말이 아니라 동작이다
    snapshot = json.loads(SNAPSHOT.read_text("utf-8"))

    assert snapshot["version"] == FINOPS_PROMPT_VERSION
    assert snapshot["prompt_sha256"] == finops_prompt_fingerprint(), (
        "프롬프트가 승인 스냅샷과 다릅니다 — apps/core-api/ai/evaluation/summary/baseline.md 절차로 재통과 후 갱신"
    )


def test_prompt_material_covers_every_instruction_surface():
    material = finops_prompt_material()

    assert _FINOPS_SUMMARY_SYSTEM_PROMPT in material
    assert _FINOPS_PROPOSAL_SYSTEM_PROMPT in material
    for texts in _PARAMETER_CONSTRAINTS.values():
        for text in texts:
            assert text in material
    # 출력 스키마 — 필드 이름이 모델에 나가므로 이름을 바꾸면 해시가 움직여야 한다
    for field in ("observation", "diagnosis", "rationale", "decision", "min_size"):
        assert f'"{field}"' in material
    # 실행 목표와 대상은 서버 몫이다. 단가 추정은 추천 요청과 분리한다.
    assert "target_instance_type" not in FinOpsProposedCandidate.model_fields
    assert '"target_arn"' not in material
    assert "ai_savings_estimate" not in FinOpsCandidateProposalOutput.model_json_schema()["properties"]


def test_finops_prompt_material_points_at_the_finops_schemas_not_the_legacy_ones():
    material = finops_prompt_material()

    assert json.dumps(FinOpsEvidenceDecisionOutput.model_json_schema(), ensure_ascii=False,
                      sort_keys=True) in material
    assert json.dumps(FinOpsCandidateProposalOutput.model_json_schema(), ensure_ascii=False,
                      sort_keys=True) in material
    assert json.dumps(ProposedCandidate.model_json_schema(), ensure_ascii=False,
                      sort_keys=True) not in material


def test_summary_output_fields_are_the_three_roles_then_the_decision():
    # 근거 3줄이 decision보다 먼저 온다 — 스키마 속성 순서가 곧 생성 순서다
    assert list(FinOpsEvidenceDecisionOutput.model_fields) == [
        "observation", "diagnosis", "rationale", "decision",
    ]
    assert list(FinOpsEvidenceDecisionOutput.model_json_schema()["properties"]) == [
        "observation", "diagnosis", "rationale", "decision",
    ]
    assert FinOpsEvidenceDecisionOutput.model_json_schema()["properties"]["decision"]["items"] == {
        "type": "integer"
    }


def test_finops_proposal_fields_are_the_legacy_ones_without_the_target():
    legacy = list(ProposedCandidate.model_fields)
    assert list(FinOpsProposedCandidate.model_fields) == [f for f in legacy if f != "target_arn"]
    assert list(FinOpsCandidateProposalOutput.model_fields) == ["candidates"]


def test_legacy_models_stay_the_secops_request_schemas():
    # SecOps 정상·대비책이 쓰는 모델은 FinOps 변경으로 움직이지 않는다
    assert list(EvidenceSummaryOutput.model_fields) == ["observation", "diagnosis", "rationale"]
    assert "target_arn" in ProposedCandidate.model_fields
    assert "decision" not in EvidenceSummaryOutput.model_json_schema()["properties"]
    assert list(CandidateProposalOutput.model_fields) == ["candidates"]


def test_summary_prompt_carries_the_multi_action_decision_criteria():
    # 다중 조치 기준은 요약 프롬프트의 몫이다 — 후보 프롬프트는 전사만 한다
    assert "decision" in _FINOPS_SUMMARY_SYSTEM_PROMPT
    assert "action_id" in _FINOPS_SUMMARY_SYSTEM_PROMPT
    assert "둘 이상" in _FINOPS_SUMMARY_SYSTEM_PROMPT
    assert "둘 이상" not in _FINOPS_PROPOSAL_SYSTEM_PROMPT
    assert "action_id" in _FINOPS_PROPOSAL_SYSTEM_PROMPT


def test_finops_prompt_version_is_v3():
    assert FINOPS_PROMPT_VERSION == "v3"


def test_summary_request_fingerprint_detects_schema_order(monkeypatch):
    before = finops_request_fingerprint()
    schema = FinOpsCandidateProposalOutput.model_json_schema()
    properties = schema["$defs"]["FinOpsProposedCandidate"]["properties"]
    properties["rule_number"] = properties.pop("rule_number")
    monkeypatch.setattr(FinOpsCandidateProposalOutput, "model_json_schema", lambda: schema)
    assert finops_request_fingerprint() != before


def test_summary_request_fingerprint_detects_decision_order(monkeypatch):
    before = finops_request_fingerprint()
    schema = FinOpsEvidenceDecisionOutput.model_json_schema()
    properties = schema["properties"]
    properties["observation"] = properties.pop("observation")
    monkeypatch.setattr(FinOpsEvidenceDecisionOutput, "model_json_schema", lambda: schema)
    assert finops_request_fingerprint() != before


@pytest.mark.parametrize("prompt", [
    _FINOPS_SUMMARY_SYSTEM_PROMPT,
    _FINOPS_PROPOSAL_SYSTEM_PROMPT,
], ids=["finops_summary", "finops_proposal"])
def test_finops_prompts_are_directive_not_prohibitive(prompt):
    # 금지가 쌓일수록 빈 후보가 가장 안전한 답이 된다(#243) — 금지형 표지를 잡는다
    # SecOps #324의 승인된 프롬프트는 의미 rubric·실측으로 평가한다.
    # 부정어 유무는 후보 강제/억제나 사용자 판단권 보존을 입증하지 않는다.
    # 대체 검증은 EITHER 답지·action_reason 의미 기준·라운드별 후보/무제안 관측이다.
    for marker in ("않는다", "마라", "금지"):
        assert marker not in prompt


def test_proposal_payload_carries_parameter_constraints_only_where_the_contract_has_them():
    autoscaling = {
        "runbook_id": "RUNBOOK_EC2_ENABLE_AUTOSCALING",
        "purpose": "고정 대수 EC2를 Auto Scaling 그룹으로 전환",
        "allowed_target_asset_types": ["EC2"],
    }
    graph_input = make_input(capabilities=[RIGHTSIZING_CAPABILITY, autoscaling])
    _, client = run(SUMMARY, proposals(rightsizing_proposal()), graph_input=graph_input)
    by_id = {c["runbook_id"]: c for c in client.sent[1]["user_payload"]["capabilities"]}

    # min ≤ max는 model_validator라 parameter_schema에 없다 — 문구로만 모델에 닿는다
    assert "min_size" in by_id["RUNBOOK_EC2_ENABLE_AUTOSCALING"]["parameter_schema"]
    assert by_id["RUNBOOK_EC2_ENABLE_AUTOSCALING"]["parameter_constraints"] == [
        "min_size는 max_size 이하로 정한다"
    ]
    assert by_id["RUNBOOK_EC2_RIGHTSIZING"]["parameter_constraints"] == []


def test_parameter_constraint_text_describes_a_live_contract_rule():
    # 문구의 원천은 계약의 model_validator다 — 계약에서 그 규칙이 사라지면 문구가 거짓이 된다
    for runbook_id in _PARAMETER_CONSTRAINTS:
        assert runbook_id in CANDIDATE_PARAMETER_MODELS
    with pytest.raises(ValidationError):
        Ec2EnableAutoscalingCandidateParameters(min_size=3, max_size=1)


# ------------------------------------------------------------------------------
# 실패 사유 로그 (#424) — FAILED가 된 자리와 이유가 incident_id와 함께 남는가
# ------------------------------------------------------------------------------
# 사유 코드로 "주입 시도로 거절된 제안"(메뉴 밖·대상 밖)과 형식 실패·모델 거절을 가른다.
# 모델이 쓴 문자열 중 남기는 것은 대상 밖으로 거절한 target_arn뿐이다(가드레일 ③ 거절 로그와
# 같이 512자로 자른다). 요약 등 그 밖의 값은 남지 않아야 한다.


class _RejectingClient:
    """모델이 응답을 거절한 경우(refusal) — 경계 예외만 낸다."""

    def complete(self, request, response_model):
        raise AIModelRejectedError("모델이 응답을 거절했습니다", phase="response")


class _BrokenAdapterClient:
    """어댑터가 응답 모양을 읽지 못한 경우 — 제공자 장애가 아니다."""

    def complete(self, request, response_model):
        raise AIModelAdapterError("응답에 choices가 없습니다", kind=FailureKind.RESPONSE_SHAPE)


def _failure_fields(caplog, *outputs, graph_input=None, client=None):
    """그래프를 1회 돌려 FAILED와 실패 로그 1건을 확인하고 그 필드를 돌려준다."""
    with caplog.at_level(logging.WARNING, logger="vigilantis.ai.agent"):
        output = run_finops_graph(
            graph_input or make_input(), client=client or FakeAIModelClient(list(outputs))
        )
    assert output.invocation_status == AgentInvocationStatus.FAILED
    records = [r for r in caplog.records if r.getMessage() == "agent_analysis_failed"]
    assert len(records) == 1
    fields = records[0].__dict__
    assert fields["incident_id"] == "inc-20260831-001"
    assert fields["domain"] == "FINOPS"
    return fields


def test_failure_log_names_model_refusal(caplog):
    fields = _failure_fields(caplog, client=_RejectingClient())

    assert fields["reason_code"] == "MODEL_REJECTED"
    assert fields["failed_step"] == "summarize_evidence"
    assert fields["error_class"] == "AIModelRejectedError"
    assert fields["phase"] == "response"


def test_failure_log_keeps_adapter_defect_apart_from_provider_outage(caplog):
    """어댑터 결함은 저장되는 사유에서 MODEL_UNAVAILABLE(제공자 장애)로 섞이지 않는다."""
    fields = _failure_fields(caplog, client=_BrokenAdapterClient())

    assert fields["reason_code"] == "MODEL_CONTRACT_VIOLATION"
    assert fields["error_class"] == "AIModelAdapterError"


@pytest.mark.parametrize(
    "outputs,step,reason",
    [
        # 준비한 응답이 없으면 경계가 일시 오류(재시도 소진과 같은 갈래)를 낸다
        ((), "summarize_evidence", "MODEL_UNAVAILABLE"),
        ((SUMMARY,), "propose_candidates", "MODEL_UNAVAILABLE"),
        # 요구한 구조가 아닌 응답 — 구조화 출력 파싱 실패와 같은 갈래
        ((SUMMARY, SUMMARY), "propose_candidates", "MODEL_CONTRACT_VIOLATION"),
    ],
)
def test_failure_log_names_the_model_call_that_failed(caplog, outputs, step, reason):
    fields = _failure_fields(caplog, *outputs)

    assert fields["reason_code"] == reason
    assert fields["failed_step"] == step


def test_failure_log_names_runbook_outside_menu_as_decision_mismatch(caplog):
    # 메뉴 밖 Runbook은 decision이 가리키는 집합과 달라 대응 검사에서 걸린다 — 후보 변환까지 가지 않는다
    proposal = rightsizing_proposal(
        runbook_id="RUNBOOK_NACL_ADD_DENY",
        rule_number=100,
        cidr_block="203.0.113.0/24",
        protocol="-1",
    )
    fields = _failure_fields(caplog, SUMMARY, proposals(proposal))

    assert fields["reason_code"] == "DECISION_MISMATCH"
    assert fields["failed_step"] == "validate_output_contract"
    assert "runbook_id" not in fields
    assert SUMMARY.observation not in json.dumps(fields, ensure_ascii=False, default=str)


def test_failure_log_names_missing_parameter_as_candidate_contract(caplog):
    autoscaling = {
        "runbook_id": "RUNBOOK_EC2_ENABLE_AUTOSCALING",
        "purpose": "고정 대수 EC2를 Auto Scaling 그룹으로 전환",
        "allowed_target_asset_types": ["EC2"],
    }
    proposal = rightsizing_proposal(runbook_id="RUNBOOK_EC2_ENABLE_AUTOSCALING", min_size=1)
    fields = _failure_fields(
        caplog, SUMMARY, proposals(proposal), graph_input=make_input(capabilities=[autoscaling])
    )

    assert fields["reason_code"] == "CANDIDATE_CONTRACT_VIOLATION"
    assert fields["runbook_id"] == "RUNBOOK_EC2_ENABLE_AUTOSCALING"
    # 위치·유형만 남긴다 — 입력값(input)·문구(msg)는 모델 출력을 되읽을 수 있다
    assert fields["errors"]
    assert all(set(error) == {"loc", "type"} for error in fields["errors"])


def test_failure_log_names_server_parameter_gap(caplog):
    graph_input = make_input(asset_context={**ASSET_CONTEXT, "spec": {"instance_type": None}})
    fields = _failure_fields(
        caplog, SUMMARY, proposals(rightsizing_proposal()), graph_input=graph_input
    )

    assert fields["reason_code"] == "SERVER_PARAMETER_UNAVAILABLE"


def test_failure_log_names_duplicate_runbooks_as_decision_mismatch(caplog):
    # 같은 runbook_id 중복은 출력 계약까지 가기 전에 decision 대응(개수·집합)에서 걸린다
    fields = _failure_fields(caplog, SUMMARY_EBS, proposals(ebs_proposal(), ebs_proposal()))

    assert fields["reason_code"] == "DECISION_MISMATCH"
    assert fields["failed_step"] == "validate_output_contract"


def test_no_failure_log_when_the_analysis_succeeds(caplog):
    with caplog.at_level(logging.WARNING, logger="vigilantis.ai.agent"):
        run(SUMMARY, proposals(rightsizing_proposal()))
        run(SUMMARY_BOTH, proposals(rightsizing_proposal(), ebs_proposal()))

    assert not [r for r in caplog.records if r.getMessage() == "agent_analysis_failed"]


# --- 모델 호출 로그 묶음 -----------------------------------------------------------
# 문맥 필드(incident_id·analysis_step)는 포매터가 합친다 — caplog 레코드에는 없으므로 실제
# 출력 형식(JsonLineFormatter)으로 받아 확인한다.


@pytest.fixture
def json_log_lines():
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonLineFormatter())
    root = logging.getLogger()
    previous_level = root.level
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    try:
        yield lambda: [json.loads(line) for line in stream.getvalue().splitlines()]
    finally:
        root.removeHandler(handler)
        root.setLevel(previous_level)


def _sdk_completion(parsed):
    usage = SimpleNamespace(prompt_tokens=10, completion_tokens=5, total_tokens=15)
    message = SimpleNamespace(parsed=parsed, refusal=None)
    return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=usage, model="m")


def test_model_call_logs_of_one_analysis_share_the_incident_and_name_the_step(json_log_lines):
    # 실제 경계 구현(OpenAIModelClient)에 SDK만 바꿔 낀다 — 재시도 1회를 섞어 retry 로그도 본다
    results = [
        APITimeoutError(httpx2.Request("POST", "https://api.openai.com/v1/chat/completions")),
        _sdk_completion(SUMMARY),
        _sdk_completion(proposals(rightsizing_proposal())),
    ]

    def parse(**kwargs):
        outcome = results.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    client = OpenAIModelClient(
        client=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(parse=parse))),
        model="m",
        policy=RetryPolicy(timeout_seconds=1.0, max_attempts=2, retry_backoff_seconds=0.0),
    )
    output = run_finops_graph(make_input(), client=client)
    assert output.invocation_status == AgentInvocationStatus.SUCCEEDED

    lines = [
        line for line in json_log_lines() if line["event"] in ("ai_model_call", "ai_model_retry")
    ]
    assert [(line["event"], line["analysis_step"]) for line in lines] == [
        ("ai_model_retry", "summarize_evidence"),
        ("ai_model_call", "summarize_evidence"),
        ("ai_model_call", "propose_candidates"),
    ]
    assert {line["incident_id"] for line in lines} == {"inc-20260831-001"}

    # 그래프 밖으로 나오면 문맥이 풀린다 — 같은 스레드의 다음 로그에 붙지 않는다
    logging.getLogger("vigilantis.test").info("after_graph")
    after = [line for line in json_log_lines() if line["event"] == "after_graph"]
    assert after and "incident_id" not in after[0] and "analysis_step" not in after[0]
