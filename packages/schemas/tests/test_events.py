"""Mock 위협 입력·정규화·초기 위험 판정 계약 테스트 (Issue #49).

핵심: 입력은 severity·response_mode를 받지 않고, 초기 판정 High→PRE_MITIGATION_0_5S·
Medium/Low→AGENT_WAIT 짝만 허용(TIMEOUT_ISOLATION_1M은 초기 판정 결과가 아님).
"""

import pytest
from pydantic import TypeAdapter, ValidationError

from schemas.events import (
    InitialRiskEvaluationResult,
    MockThreatEventInput,
    NormalizedThreatEvent,
    OpenIpThreatInput,
    SshBruteForceThreatInput,
    ThreatEventType,
)

MOCK_INPUT = TypeAdapter(MockThreatEventInput)

EC2_ARN = "arn:aws:ec2:ap-northeast-2:123456789012:instance/i-0123"
SG_ARN = "arn:aws:ec2:ap-northeast-2:123456789012:security-group/sg-0123"


def make_ssh_input(**over):
    base = {
        "event_id": "evt-mock-001",
        "event_type": "SSH_BRUTE_FORCE",
        "target_arn": EC2_ARN,
        "source_ip": "203.0.113.10",
        "occurred_at": "2026-08-14T09:00:00Z",
        "failed_attempt_count": 42,
        "window_seconds": 60,
    }
    base.update(over)
    return base


def make_open_ip_input(**over):
    base = {
        "event_id": "evt-mock-002",
        "event_type": "OPEN_IP",
        "target_arn": SG_ARN,
        "occurred_at": "2026-08-14T09:00:00Z",
        "protocol": "tcp",
        "from_port": 22,
        "to_port": 22,
        "source_cidr": "0.0.0.0/0",
    }
    base.update(over)
    return base


def test_threat_event_types_match_contract_exactly():
    assert {t.value for t in ThreatEventType} == {"OPEN_IP", "SSH_BRUTE_FORCE"}


def test_union_discriminates_by_event_type():
    assert isinstance(MOCK_INPUT.validate_python(make_ssh_input()), SshBruteForceThreatInput)
    assert isinstance(MOCK_INPUT.validate_python(make_open_ip_input()), OpenIpThreatInput)


@pytest.mark.parametrize("over", [
    {"severity": "HIGH"},              # 입력은 위험도를 받지 않는다 — Risk Evaluator가 생성
    {"response_mode": "AGENT_WAIT"},   # 대응 경로도 서버 판정 결과
    {"source_ip": None},               # SSH 유형에서는 필수
    {"failed_attempt_count": 0},
])
def test_ssh_input_violations(over):
    with pytest.raises(ValidationError):
        MOCK_INPUT.validate_python(make_ssh_input(**over))


def make_normalized(**over):
    base = {
        "threat_event_id": "thr-20260814-001",
        "source_event_id": "evt-mock-001",
        "event_type": "SSH_BRUTE_FORCE",
        "target_arn": EC2_ARN,
        "occurred_at": "2026-08-14T09:00:00Z",
        "payload": {
            "source_ip": "203.0.113.10",
            "failed_attempt_count": 42,
            "window_seconds": 60,
        },
        "deduplication_key": "SSH_BRUTE_FORCE:i-0123:203.0.113.10",
        "collected_at": "2026-08-14T09:00:01Z",
    }
    base.update(over)
    return base


def test_normalized_roundtrip():
    ev = NormalizedThreatEvent.model_validate(make_normalized())
    assert NormalizedThreatEvent.model_validate_json(ev.model_dump_json()) == ev


def test_normalized_rejects_mismatched_payload():
    # SSH 이벤트에 OPEN_IP payload — event_type↔payload 정합 위반
    with pytest.raises(ValidationError):
        NormalizedThreatEvent.model_validate(make_normalized(
            payload={"protocol": "tcp", "source_cidr": "0.0.0.0/0"},
        ))


@pytest.mark.parametrize("risk,mode,ok", [
    ("HIGH", "PRE_MITIGATION_0_5S", True),
    ("MEDIUM", "AGENT_WAIT", True),
    ("LOW", "AGENT_WAIT", True),
    ("HIGH", "AGENT_WAIT", False),
    ("MEDIUM", "PRE_MITIGATION_0_5S", False),
    ("LOW", "TIMEOUT_ISOLATION_1M", False),
])
def test_initial_risk_mode_pairing(risk, mode, ok):
    data = {
        "threat_event_id": "thr-20260814-001",
        "initial_risk_level": risk,
        "response_mode": mode,
        "reason_codes": ["RISK_SSH_BRUTEFORCE"],
    }
    if ok:
        assert InitialRiskEvaluationResult.model_validate(data).response_mode.value == mode
    else:
        with pytest.raises(ValidationError):
            InitialRiskEvaluationResult.model_validate(data)
