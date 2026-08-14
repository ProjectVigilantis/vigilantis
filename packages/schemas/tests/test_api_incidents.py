"""GET /api/v1/incidents/{id} 외부 DTO 계약 테스트 (확정 설계 4.3).

핵심 불변식: FINOPS↔위험 대응 축 null, summary_lines 0|3, 추천은 본편 7종만,
복구 조치는 롤백 3종만, 상태↔목록 정합, 같은 runbook_id 제안 중복 금지.
"""

import pytest
from pydantic import ValidationError

from schemas.api.incidents import (
    IncidentCategory,
    IncidentResponse,
    IncidentStatus,
    RecommendationItem,
    ResponseMode,
    RiskLevel,
)
from schemas.runbooks import AI_RECOMMENDABLE_RUNBOOK_IDS, ROLLBACK_RUNBOOK_IDS


def make_secops_incident(**over):
    """설계 4.3 예시 JSON 기반의 유효 SECOPS Incident."""
    base = {
        "incident_id": "inc-20260812-001",
        "subject_arn": "arn:aws:ec2:ap-northeast-2:123456789012:instance/i-0123",
        "category": "SECOPS",
        "status": "AWAITING_APPROVAL",
        "initial_risk_level": "HIGH",
        "reviewed_risk_level": "HIGH",
        "response_mode": "PRE_MITIGATION_0_5S",
        "summary_lines": [
            "SSH 브루트포스 Mock 이벤트가 감지되었습니다.",
            "대상 EC2는 사전 승인 Runbook으로 격리되었습니다.",
            "공격 IP의 NACL 차단 제안을 검토할 수 있습니다.",
        ],
        "evidence_ids": ["ev-001", "ev-002"],
        "recommendations": [
            {
                "runbook_id": "RUNBOOK_NACL_ADD_DENY",
                "target_arn": "arn:aws:ec2:ap-northeast-2:123456789012:network-acl/acl-0123",
                "display_parameters": {"source_cidr": "203.0.113.10/32"},
            }
        ],
        "executions": [
            {
                "execution_id": "exec-20260812-isolate-001",
                "runbook_id": "RUNBOOK_EC2_ISOLATE",
                "status": "SUCCESS",
                "available_recovery_runbook_ids": ["RUNBOOK_EC2_UNISOLATE"],
                "updated_at": "2026-08-12T09:01:01Z",
            }
        ],
        "created_at": "2026-08-12T09:01:00Z",
        "updated_at": "2026-08-12T09:01:02Z",
    }
    base.update(over)
    return base


def make_finops_incident(**over):
    base = make_secops_incident(
        incident_id="inc-20260812-002",
        category="FINOPS",
        status="AWAITING_APPROVAL",
        initial_risk_level=None,
        reviewed_risk_level=None,
        response_mode=None,
        summary_lines=[
            "3일간 CPU 이용률이 5% 미만입니다.",
            "t3.large는 현재 부하 대비 과대 스펙입니다.",
            "t3.small로의 다운사이징을 제안합니다.",
        ],
        evidence_ids=["ev-010"],
        recommendations=[{
            "runbook_id": "RUNBOOK_EC2_RIGHTSIZING",
            "target_arn": "arn:aws:ec2:ap-northeast-2:123456789012:instance/i-0456",
            "display_parameters": {"target_instance_type": "t3.small"},
        }],
        executions=[],
    )
    base.update(over)
    return base


def test_enums_match_contract_exactly():
    assert {s.value for s in IncidentStatus} == {
        "ANALYZING", "AWAITING_APPROVAL", "ACTION_IN_PROGRESS", "RESOLVED", "FAILED",
    }
    assert {c.value for c in IncidentCategory} == {"FINOPS", "SECOPS"}
    assert {r.value for r in RiskLevel} == {"HIGH", "MEDIUM", "LOW"}
    assert {m.value for m in ResponseMode} == {
        "PRE_MITIGATION_0_5S", "AGENT_WAIT", "TIMEOUT_ISOLATION_1M",
    }


def test_secops_roundtrip_and_z_serialization():
    inc = IncidentResponse.model_validate(make_secops_incident())
    assert inc.status == IncidentStatus.AWAITING_APPROVAL
    dumped = inc.model_dump_json()
    assert '"2026-08-12T09:01:01Z"' in dumped
    assert IncidentResponse.model_validate_json(dumped) == inc


def test_finops_valid_without_risk_axis():
    inc = IncidentResponse.model_validate(make_finops_incident())
    assert inc.initial_risk_level is None and inc.response_mode is None


def test_analyzing_with_empty_lists_valid():
    inc = IncidentResponse.model_validate(make_secops_incident(
        status="ANALYZING", summary_lines=[], recommendations=[], executions=[],
        reviewed_risk_level=None,
    ))
    assert inc.summary_lines == [] and inc.recommendations == []


def test_action_in_progress_valid():
    inc = IncidentResponse.model_validate(make_secops_incident(
        status="ACTION_IN_PROGRESS",
        recommendations=[],
        executions=[{
            "execution_id": "exec-1", "runbook_id": "RUNBOOK_NACL_ADD_DENY",
            "status": "IN_PROGRESS", "available_recovery_runbook_ids": [],
            "updated_at": "2026-08-12T09:02:00Z",
        }],
    ))
    assert inc.status == IncidentStatus.ACTION_IN_PROGRESS


@pytest.mark.parametrize("runbook_id", sorted(AI_RECOMMENDABLE_RUNBOOK_IDS))
def test_recommendations_accept_all_main_runbooks(runbook_id):
    item = RecommendationItem.model_validate({
        "runbook_id": runbook_id, "target_arn": "arn:aws:ec2:r:a:x/y",
    })
    assert item.runbook_id.value == runbook_id


@pytest.mark.parametrize("runbook_id", sorted(ROLLBACK_RUNBOOK_IDS))
def test_recommendations_reject_rollback_runbooks(runbook_id):
    # ADR-0004 정책 ②: 롤백 3종은 AI 추천 경로에 못 온다
    with pytest.raises(ValidationError):
        RecommendationItem.model_validate({
            "runbook_id": runbook_id, "target_arn": "arn:aws:ec2:r:a:x/y",
        })


@pytest.mark.parametrize("over", [
    # FINOPS인데 위험 대응 축 잔존
    {"category": "FINOPS", "initial_risk_level": "HIGH"},
    {"category": "FINOPS", "response_mode": "AGENT_WAIT"},
    # summary_lines 개수 위반 (0 또는 3만 허용)
    {"summary_lines": ["한 줄", "두 줄"]},
    # 같은 runbook_id 제안 중복
    {"recommendations": [
        {"runbook_id": "RUNBOOK_NACL_ADD_DENY", "target_arn": "arn:a"},
        {"runbook_id": "RUNBOOK_NACL_ADD_DENY", "target_arn": "arn:b"},
    ]},
    # 상태 ↔ 목록 정합 위반 (케이스마다 위반 1개씩)
    {"status": "AWAITING_APPROVAL", "recommendations": []},   # 제안 0개
    {"status": "ANALYZING", "recommendations": []},           # summary_lines 잔존
    {"status": "ANALYZING", "summary_lines": []},             # recommendations 잔존
    {"status": "ACTION_IN_PROGRESS", "recommendations": []},  # 진행 중 실행 없음
    {"status": "RESOLVED"},                                   # 제안 잔존
    # extra 거부
    {"unknown_field": 1},
])
def test_incident_contract_violations(over):
    with pytest.raises(ValidationError):
        IncidentResponse.model_validate(make_secops_incident(**over))


def test_awaiting_approval_rejects_in_progress_execution():
    with pytest.raises(ValidationError):
        IncidentResponse.model_validate(make_secops_incident(executions=[{
            "execution_id": "exec-2", "runbook_id": "RUNBOOK_EC2_ISOLATE",
            "status": "IN_PROGRESS", "available_recovery_runbook_ids": [],
            "updated_at": "2026-08-12T09:03:00Z",
        }]))


def test_recovery_ids_reject_main_runbooks():
    # 설계 4.3 명시 사례: NACL_RESTORE는 주 조치라 recommendations 경로 — 복구 목록에 못 온다
    with pytest.raises(ValidationError):
        IncidentResponse.model_validate(make_secops_incident(executions=[{
            "execution_id": "exec-3", "runbook_id": "RUNBOOK_NACL_ADD_DENY",
            "status": "SUCCESS",
            "available_recovery_runbook_ids": ["RUNBOOK_NACL_RESTORE"],
            "updated_at": "2026-08-12T09:04:00Z",
        }]))
