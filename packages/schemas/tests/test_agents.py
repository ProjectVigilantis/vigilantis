"""LangGraph 입출력 계약 테스트 (Issue #49).

핵심: 입력은 domain으로 분기하는 typed snapshot, 출력은 Terminal 상태별
요약 3줄·후보 개수 불변식 + AI 추천 가능 본편 7종만 허용(롤백 3종 차단).
"""

import pytest
from pydantic import ValidationError

from schemas.agents import (
    AGENT_GRAPH_INPUT_ADAPTER,
    AgentAssetContext,
    AgentGraphOutput,
    FinOpsGraphInput,
    RunbookCandidateDraft,
    RunbookCapability,
    SecOpsGraphInput,
)
from schemas.api.assets import AssetItem
from schemas.runbooks import AI_RECOMMENDABLE_RUNBOOK_IDS, ROLLBACK_RUNBOOK_IDS

EC2_ARN = "arn:aws:ec2:ap-northeast-2:123456789012:instance/i-0123"

ASSET_CONTEXT = {
    "arn": EC2_ARN,
    "resource_id": "i-0123",
    "asset_type": "EC2",
    "resource_role": "PRIMARY",
    "account_id": "123456789012",
    "region": "ap-northeast-2",
    "state": "running",
    "spec": {"instance_type": "t3.large"},
    "relationships": [],
    "evaluation_status": "COMPLETED",
    "health_score": 3,
    "verdict": "COST_CANDIDATE",
    "collected_at": "2026-08-14T09:00:00Z",
}

RULE_RESULT = {
    "asset_arn": EC2_ARN,
    "collection_run_id": "run-20260814-001",
    "evaluation_status": "COMPLETED",
    "verdict": "COST_CANDIDATE",
    "health_score": 3,
    "skip_reason_code": None,
    "reason": "3일 평균 CPU 3%",
    "evaluated_at": "2026-08-14T09:00:00Z",
}

CAPABILITY = {
    "runbook_id": "RUNBOOK_EC2_RIGHTSIZING",
    "purpose": "과대 스펙 EC2 다운사이징",
    "allowed_target_asset_types": ["EC2"],
}


def make_finops_input(**over):
    base = {
        "domain": "FINOPS",
        "incident_id": "inc-20260814-001",
        "asset_context": ASSET_CONTEXT,
        "rule_evaluation": RULE_RESULT,
        "evidences": [],
        "capabilities": [CAPABILITY],
    }
    base.update(over)
    return base


def make_secops_input(**over):
    base = {
        "domain": "SECOPS",
        "incident_id": "inc-20260814-002",
        "asset_context": ASSET_CONTEXT,
        "initial_risk": {
            "threat_event_id": "thr-20260814-001",
            "initial_risk_level": "HIGH",
            "response_mode": "PRE_MITIGATION_0_5S",
        },
        "evidences": [],
        "isolation_execution": {
            "execution_id": "exec-isolate-001",
            "runbook_id": "RUNBOOK_EC2_ISOLATE",
            "status": "SUCCESS",
            "affected_arns": [EC2_ARN],
        },
        "capabilities": [{
            "runbook_id": "RUNBOOK_NACL_ADD_DENY",
            "purpose": "공격 IP의 NACL 차단",
            "allowed_target_asset_types": ["NACL"],
        }],
    }
    base.update(over)
    return base


def test_asset_context_is_public_asset_item():
    # 발명 금지 원칙: 자산 문맥은 공개 AssetItem 재사용(동일 객체)
    assert AgentAssetContext is AssetItem


def test_input_union_discriminates_by_domain():
    fin = AGENT_GRAPH_INPUT_ADAPTER.validate_python(make_finops_input())
    sec = AGENT_GRAPH_INPUT_ADAPTER.validate_python(make_secops_input())
    assert isinstance(fin, FinOpsGraphInput)
    assert isinstance(sec, SecOpsGraphInput)
    assert sec.isolation_execution is not None


def test_secops_isolation_context_nullable():
    sec = SecOpsGraphInput.model_validate(make_secops_input(isolation_execution=None))
    assert sec.isolation_execution is None


@pytest.mark.parametrize("runbook_id", sorted(ROLLBACK_RUNBOOK_IDS))
def test_capability_and_draft_reject_rollback(runbook_id):
    # ADR-0004: 롤백 3종은 AI 추천 경로(입력 capability·출력 Draft)에 못 온다
    with pytest.raises(ValidationError):
        RunbookCapability.model_validate({
            "runbook_id": runbook_id, "purpose": "x",
            "allowed_target_asset_types": ["EC2"],
        })
    with pytest.raises(ValidationError):
        RunbookCandidateDraft.model_validate({
            "runbook_id": runbook_id, "target_arn": EC2_ARN,
        })


def test_capabilities_reject_duplicates():
    with pytest.raises(ValidationError):
        FinOpsGraphInput.model_validate(
            make_finops_input(capabilities=[CAPABILITY, CAPABILITY])
        )


SUMMARY3 = ["저활성 EC2입니다.", "현재 부하 대비 과대 스펙입니다.", "다운사이징을 제안합니다."]
DRAFT = {
    "runbook_id": "RUNBOOK_EC2_RIGHTSIZING",
    "target_arn": EC2_ARN,
    "display_parameters": {"target_instance_type": "t3.small"},
    "evidence_ids": ["ev-001"],
}


def test_output_succeeded_valid():
    out = AgentGraphOutput.model_validate({
        "invocation_status": "SUCCEEDED",
        "summary_lines": SUMMARY3,
        "reviewed_risk_level": "HIGH",
        "candidates": [DRAFT],
    })
    assert out.candidates[0].runbook_id.value in AI_RECOMMENDABLE_RUNBOOK_IDS


def test_output_no_proposal_valid():
    out = AgentGraphOutput.model_validate({
        "invocation_status": "NO_PROPOSAL",
        "summary_lines": SUMMARY3,
        "reviewed_risk_level": "LOW",
        "candidates": [],
    })
    assert out.candidates == []


def test_output_failed_valid():
    out = AgentGraphOutput.model_validate({"invocation_status": "FAILED"})
    assert out.summary_lines == [] and out.candidates == []
    assert out.reviewed_risk_level is None


@pytest.mark.parametrize("payload", [
    {"invocation_status": "PENDING"},        # Terminal 아님 — Workflow 저장용 상태
    {"invocation_status": "IN_PROGRESS"},
    {"invocation_status": "SUCCEEDED", "summary_lines": SUMMARY3, "candidates": []},
    {"invocation_status": "SUCCEEDED", "summary_lines": ["한 줄"], "candidates": [DRAFT]},
    {"invocation_status": "NO_PROPOSAL", "summary_lines": SUMMARY3, "candidates": [DRAFT]},
    {"invocation_status": "FAILED", "summary_lines": SUMMARY3},
    {"invocation_status": "FAILED", "reviewed_risk_level": "LOW"},
    {"invocation_status": "SUCCEEDED", "summary_lines": SUMMARY3, "candidates": [DRAFT, DRAFT]},
])
def test_output_violations(payload):
    with pytest.raises(ValidationError):
        AgentGraphOutput.model_validate(payload)
