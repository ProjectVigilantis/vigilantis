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
    ProposedCandidate,
    finops_prompt_fingerprint,
    finops_prompt_material,
    finops_request_fingerprint,
    run_finops_graph,
)
from ai.model_client import AIModelRejectedError, FakeAIModelClient
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

SUMMARY = EvidenceSummaryOutput(
    observation="t3.xlarge 인스턴스의 3일 평균 CPU가 3%다.",
    diagnosis="현재 스펙에 비해 사용률이 낮아 과대 스펙으로 보인다.",
    rationale="3일 내내 낮은 사용률이라 다운사이징으로 비용을 줄일 수 있다.",
)


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


def rightsizing_proposal(**over) -> ProposedCandidate:
    base = {
        "runbook_id": "RUNBOOK_EC2_RIGHTSIZING",
        "target_arn": EC2_ARN,
        "evidence_ids": ["ev-0001"],
    }
    base.update(over)
    return ProposedCandidate.model_validate(base)


def proposals(*candidates) -> CandidateProposalOutput:
    return CandidateProposalOutput(candidates=list(candidates))


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
    proposal = rightsizing_proposal(
        runbook_id="RUNBOOK_EBS_DELETE_UNATTACHED",
        target_arn=VOLUME_ARN,
    )
    output, client = run(SUMMARY, proposals(proposal))
    assert len(client.sent) == 2

    assert output.invocation_status is AgentInvocationStatus.SUCCEEDED



def test_no_proposal_when_model_returns_empty_candidates():
    output, client = run(SUMMARY, proposals())

    assert output.invocation_status == AgentInvocationStatus.NO_PROPOSAL
    assert len(output.summary_lines) == 3
    assert output.candidates == []
    assert len(client.sent) == 2


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


def test_failed_when_target_arn_is_outside_asset_and_relationships():
    proposal = rightsizing_proposal(target_arn=OTHER_ARN)
    output, _ = run(SUMMARY, proposals(proposal))

    assert output.invocation_status == AgentInvocationStatus.FAILED


def test_relationship_arn_is_an_allowed_target():
    proposal = ProposedCandidate.model_validate(
        {
            "runbook_id": "RUNBOOK_EBS_DELETE_UNATTACHED",
            "target_arn": VOLUME_ARN,
            "evidence_ids": ["ev-0001"],
        }
    )
    output, _ = run(SUMMARY, proposals(proposal))

    assert output.invocation_status == AgentInvocationStatus.SUCCEEDED
    assert output.candidates[0].target_arn == VOLUME_ARN


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
    good = ProposedCandidate.model_validate(
        {
            "runbook_id": "RUNBOOK_EBS_DELETE_UNATTACHED",
            "target_arn": VOLUME_ARN,
            "evidence_ids": ["ev-0001"],
        }
    )
    output, _ = run(SUMMARY, proposals(good, rightsizing_proposal(target_arn=OTHER_ARN)))

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
    _, client = run(SUMMARY, proposals(), graph_input=graph_input)

    for sent in client.sent:
        assert "AKIAIOSFODNN7EXAMPLE" not in sent["user_json"]
        assert "[REDACTED]" in sent["user_json"]


def test_proposal_payload_carries_the_menu_and_summary():
    _, client = run(SUMMARY, proposals(rightsizing_proposal()))
    proposal_payload = client.sent[1]["user_payload"]

    assert proposal_payload["allowed_target_arns"] == [EC2_ARN, VOLUME_ARN]
    assert [c["runbook_id"] for c in proposal_payload["capabilities"]] == [
        "RUNBOOK_EC2_RIGHTSIZING",
        "RUNBOOK_EBS_DELETE_UNATTACHED",
    ]
    assert proposal_payload["summary_lines"] == [
        SUMMARY.observation,
        SUMMARY.diagnosis,
        SUMMARY.rationale,
    ]


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


def test_summary_payload_carries_action_purposes_but_not_runbook_ids():
    # rationale이 카드의 조치를 설명하려면 메뉴에 무엇이 있는지 알아야 한다(#243). 다만
    # 요약 노드는 조치를 고르지 않으므로 Runbook 이름·파라미터 명세·허용 대상은 싣지 않는다
    # — 이름을 주면 문장이 이름을 되읽고, 메뉴 전체를 열거한다(v1 2차 실측)
    _, client = run(SUMMARY, proposals())
    summary_payload = client.sent[0]["user_payload"]

    assert summary_payload["available_actions"] == ["과대 스펙 EC2 다운사이징", "미연결 EBS 볼륨 삭제"]
    assert "capabilities" not in summary_payload
    assert "RUNBOOK_EC2_RIGHTSIZING" not in client.sent[0]["user_json"]
    assert "allowed_target_arns" not in summary_payload
    assert summary_payload["incident_id"] == "inc-20260831-001"
    assert [e["evidence_id"] for e in summary_payload["evidences"]] == ["ev-0001"]


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
    for field in ("observation", "diagnosis", "rationale", "min_size"):
        assert f'"{field}"' in material
    # 실행 목표는 서버 몫이다. 단가 추정은 추천 요청과 분리한다.
    assert "target_instance_type" not in ProposedCandidate.model_fields
    assert "ai_savings_estimate" not in CandidateProposalOutput.model_json_schema()["properties"]


def test_summary_output_fields_are_the_three_roles():
    assert list(EvidenceSummaryOutput.model_fields) == ["observation", "diagnosis", "rationale"]


def test_summary_request_fingerprint_detects_schema_order(monkeypatch):
    before = finops_request_fingerprint()
    schema = CandidateProposalOutput.model_json_schema()
    properties = schema["$defs"]["ProposedCandidate"]["properties"]
    properties["rule_number"] = properties.pop("rule_number")
    monkeypatch.setattr(CandidateProposalOutput, "model_json_schema", lambda: schema)
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

# 모델이 지어낸 대상 — 주입 시도가 지목한 대상이라 거절 로그에 남아야 한다
INJECTED_ARN = f"arn:aws:ec2:{REGION}:{ACCOUNT}:instance/i-ignore-previous-instructions"


class _RejectingClient:
    """모델이 응답을 거절한 경우(refusal) — 경계 예외만 낸다."""

    def complete(self, request, response_model):
        raise AIModelRejectedError("모델이 응답을 거절했습니다", phase="response")


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


def test_failure_log_separates_runbook_outside_menu(caplog):
    proposal = rightsizing_proposal(
        runbook_id="RUNBOOK_NACL_ADD_DENY",
        rule_number=100,
        cidr_block="203.0.113.0/24",
        protocol="-1",
    )
    fields = _failure_fields(caplog, SUMMARY, proposals(proposal))

    assert fields["reason_code"] == "RUNBOOK_NOT_OFFERED"
    assert fields["failed_step"] == "validate_output_contract"
    assert fields["runbook_id"] == "RUNBOOK_NACL_ADD_DENY"


@pytest.mark.parametrize("target_arn", [INJECTED_ARN, INJECTED_ARN + "x" * 1000])
def test_failure_log_keeps_the_target_outside_asset_cut_to_512(caplog, target_arn):
    fields = _failure_fields(
        caplog, SUMMARY, proposals(rightsizing_proposal(target_arn=target_arn))
    )

    assert fields["reason_code"] == "TARGET_NOT_ALLOWED"
    assert fields["runbook_id"] == "RUNBOOK_EC2_RIGHTSIZING"
    assert fields["target_arn"] == target_arn[:512]
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


def test_failure_log_names_output_contract_violation(caplog):
    # 후보 하나하나는 계약을 지키지만 출력 전체가 같은 runbook_id 중복으로 거절된다
    ebs = ProposedCandidate.model_validate(
        {
            "runbook_id": "RUNBOOK_EBS_DELETE_UNATTACHED",
            "target_arn": VOLUME_ARN,
            "evidence_ids": ["ev-0001"],
        }
    )
    fields = _failure_fields(caplog, SUMMARY, proposals(ebs, ebs))

    assert fields["reason_code"] == "OUTPUT_CONTRACT_VIOLATION"
    assert fields["failed_step"] == "validate_output_contract"


def test_no_failure_log_when_the_analysis_succeeds(caplog):
    with caplog.at_level(logging.WARNING, logger="vigilantis.ai.agent"):
        run(SUMMARY, proposals(rightsizing_proposal()))
        run(SUMMARY, proposals())

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
        timeout_seconds=1.0,
        max_attempts=2,
        retry_backoff_seconds=0.0,
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
