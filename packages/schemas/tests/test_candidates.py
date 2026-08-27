"""Candidate 상태·저장 계약 테스트 (Issue #49) — AI 추천 본편 7종만 허용."""

import pytest
from pydantic import ValidationError

from schemas.candidates import CandidateStatus, RunbookCandidateData
from schemas.runbooks import ROLLBACK_RUNBOOK_IDS


def test_statuses_match_contract_exactly():
    assert {s.value for s in CandidateStatus} == {
        "PENDING_VALIDATION", "EXECUTABLE", "REJECTED", "CLAIMED", "INVALIDATED",
    }


def make_data(**over):
    base = {
        "candidate_id": "cand-20260814-001",
        "incident_id": "inc-20260814-001",
        "runbook_id": "RUNBOOK_NACL_ADD_DENY",
        "target_arn": "arn:aws:ec2:ap-northeast-2:123456789012:network-acl/acl-0123",
        "parameters": {
            "rule_number": 100,
            "cidr_block": "203.0.113.10/32",
            "protocol": "-1",
        },
        "evidence_ids": ["ev-001"],
        "status": "PENDING_VALIDATION",
    }
    base.update(over)
    return base


def test_roundtrip():
    d = RunbookCandidateData.model_validate(make_data())
    assert RunbookCandidateData.model_validate_json(d.model_dump_json()) == d


@pytest.mark.parametrize("runbook_id", sorted(ROLLBACK_RUNBOOK_IDS))
def test_rejects_rollback_runbooks(runbook_id):
    with pytest.raises(ValidationError):
        RunbookCandidateData.model_validate(make_data(runbook_id=runbook_id))


@pytest.mark.parametrize("over", [
    {"evidence_ids": [""]},   # 빈 참조 금지
    {"evidence_ids": []},     # evidence_id(단수)를 뽑을 수 없다
    {"status": "APPROVED"},   # 등록 외 상태
    {"unknown_field": 1},     # extra 거부
    {"parameters": {}},                                   # 런북 필수 파라미터 누락
    {"parameters": {"target_instance_type": "t3.small"}},  # 다른 런북의 파라미터
])
def test_contract_violations(over):
    with pytest.raises(ValidationError):
        RunbookCandidateData.model_validate(make_data(**over))


def test_display_parameters_are_derived_from_parameters():
    """화면 표시본은 서버가 만든다 — 관제자가 승인한 값과 실행되는 값이 갈리지 않는다."""
    data = RunbookCandidateData.model_validate(make_data())
    assert data.display_parameters == {
        "rule_number": "100",
        "cidr_block": "203.0.113.10/32",
        "protocol": "-1",
    }


def test_hand_written_display_parameters_are_rejected():
    with pytest.raises(ValidationError):
        RunbookCandidateData.model_validate(
            make_data(display_parameters={"cidr_block": "0.0.0.0/0"})
        )
