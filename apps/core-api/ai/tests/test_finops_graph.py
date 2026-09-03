"""LangGraph FinOps 그래프 테스트 (Issue #209).

모델은 FakeAIModelClient로만 부른다 — 실제 API 호출 0회다. 확인하는 것은 셋이다.
① 최종 상태 3갈래(SUCCEEDED·NO_PROPOSAL·FAILED)가 준비한 응답만으로 재현되는가
② 모델이 지어낼 수 없는 값(메뉴 밖 Runbook·대상 밖 ARN)이 FAILED로 막히는가
③ 모델로 나간 값이 마스킹 경로를 지났는가

프롬프트 문구의 품질은 여기서 보지 않는다. 문구·필드명·출력 스키마가 바뀌었는데 승인
스냅샷이 갱신되지 않은 것만 잡는다(#243 — 재통과 절차는 docs/AI_SUMMARY_BASELINE.md).
"""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from ai.agent import (
    _PARAMETER_CONSTRAINTS,
    _PROPOSAL_SYSTEM_PROMPT,
    _SUMMARY_SYSTEM_PROMPT,
    PROMPT_VERSION,
    CandidateProposalOutput,
    EvidenceSummaryOutput,
    ProposedCandidate,
    prompt_fingerprint,
    prompt_material,
    run_finops_graph,
)
from ai.model_client import FakeAIModelClient
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
        "target_instance_type": "t3.medium",
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
    # 노드 2개가 각각 1회씩 부른다
    assert len(client.sent) == 2


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
        target_instance_type=None,
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
    # 다운사이징 목표 타입은 AI가 정해야 하는 값이다 — 서버가 대신 채우지 않는다
    proposal = rightsizing_proposal(target_instance_type=None)
    output, _ = run(SUMMARY, proposals(proposal))

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
    rule = dict(RULE_RESULT, reason="수집 계정 키 AKIAIOSFODNN7EXAMPLE 로 조회함")
    graph_input = make_input(rule_evaluation=rule)
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
    _, client = run(SUMMARY, proposals(rightsizing_proposal()))
    by_id = {c["runbook_id"]: c for c in client.sent[1]["user_payload"]["capabilities"]}

    rightsizing = by_id["RUNBOOK_EC2_RIGHTSIZING"]
    assert rightsizing["required_parameters"] == ["target_instance_type"]
    assert rightsizing["parameter_schema"]["target_instance_type"]["type"] == "string"

    # AI가 정할 값이 0개인 Runbook은 빈 목록이다 — 채울 자리가 없다는 것도 정보다
    assert by_id["RUNBOOK_EBS_DELETE_UNATTACHED"]["required_parameters"] == []
    assert by_id["RUNBOOK_EBS_DELETE_UNATTACHED"]["parameter_schema"] == {}


def test_rule_evaluation_is_not_sent_twice():
    # RuleEvidence는 RuleEvaluationResult를 그대로 감싼 모델이라, RULE 근거가 있으면
    # 최상위 rule_evaluation은 그 복사본이다. 후보 호출이 이 페이로드를 다시 보내므로
    # 그대로 두면 같은 판정이 한 실행에서 네 번 나간다
    _, client = run(SUMMARY, proposals(rightsizing_proposal()))

    for sent in client.sent:
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

SNAPSHOT = Path(__file__).resolve().parents[1] / "evaluation" / "summary_prompt_snapshot.json"


def test_prompt_fingerprint_matches_approved_snapshot():
    # 문구·필드명·제약 문구·출력 스키마 중 하나라도 바뀌면 여기서 선다. 고의로 바꿨다면
    # docs/AI_SUMMARY_BASELINE.md의 재통과 절차를 거친 뒤 스냅샷을 갱신한다 — 이 테스트가
    # 있어야 "판 올리기를 잊어도 드러난다"가 말이 아니라 동작이다
    snapshot = json.loads(SNAPSHOT.read_text("utf-8"))

    assert snapshot["version"] == PROMPT_VERSION
    assert snapshot["prompt_sha256"] == prompt_fingerprint(), (
        "프롬프트가 승인 스냅샷과 다릅니다 — docs/AI_SUMMARY_BASELINE.md 절차로 재통과 후 갱신"
    )


def test_prompt_material_covers_every_instruction_surface():
    material = prompt_material()

    assert _SUMMARY_SYSTEM_PROMPT in material
    assert _PROPOSAL_SYSTEM_PROMPT in material
    for texts in _PARAMETER_CONSTRAINTS.values():
        for text in texts:
            assert text in material
    # 출력 스키마 — 필드 이름이 모델에 나가므로 이름을 바꾸면 해시가 움직여야 한다
    for field in ("observation", "diagnosis", "rationale", "target_instance_type"):
        assert f'"{field}"' in material


def test_summary_output_fields_are_the_three_roles():
    assert list(EvidenceSummaryOutput.model_fields) == ["observation", "diagnosis", "rationale"]


@pytest.mark.parametrize("prompt", [_SUMMARY_SYSTEM_PROMPT, _PROPOSAL_SYSTEM_PROMPT])
def test_prompts_are_directive_not_prohibitive(prompt):
    # 금지가 쌓일수록 빈 후보가 가장 안전한 답이 된다(#243) — 금지형 표지를 잡는다
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
