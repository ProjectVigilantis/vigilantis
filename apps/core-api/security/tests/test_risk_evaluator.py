"""Risk Evaluator 단위 테스트 — SecOps Golden 10케이스(S1~S10) 기준. DB·LocalStack 불필요.

아래 기대값은 확정 판정 규칙(2026-08-31 안성일 결정, PR #206)을 인코딩한다 — 임계·매핑이
바뀌면 이 표와 `security/risk_evaluator.py` 상수를 함께 갱신한다. SecOps Golden expected
정답지(datasets/golden/secops/expected)는 J3(박지현)에서 이 규칙대로 채운다.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

CORE_API = Path(__file__).resolve().parents[2]
REPO_ROOT = Path(__file__).resolve().parents[4]
for _p in (str(CORE_API), str(REPO_ROOT / "packages")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from schemas.events import (  # noqa: E402
    NormalizedThreatEvent,
    OpenIpThreatPayload,
    SshBruteForceThreatPayload,
    ThreatEventType,
)
from security.risk_evaluator import _hits_sensitive_port, evaluate_threat  # noqa: E402

GOLDEN_INPUT = REPO_ROOT / "datasets" / "golden" / "secops" / "input"


def _normalized_from_input(raw: dict) -> NormalizedThreatEvent:
    """Golden Mock 입력 → NormalizedThreatEvent (정규화 단계 대체)."""
    raw = {k: v for k, v in raw.items() if k != "$schema"}
    etype = ThreatEventType(raw["event_type"])
    now = datetime.now(timezone.utc)
    if etype == ThreatEventType.OPEN_IP:
        payload = OpenIpThreatPayload(
            protocol=raw["protocol"],
            from_port=raw.get("from_port"),
            to_port=raw.get("to_port"),
            source_cidr=raw["source_cidr"],
        )
    else:
        payload = SshBruteForceThreatPayload(
            source_ip=raw["source_ip"],
            failed_attempt_count=raw["failed_attempt_count"],
            window_seconds=raw["window_seconds"],
        )
    return NormalizedThreatEvent(
        threat_event_id=f"te-{raw['event_id']}",
        source_event_id=raw["event_id"],
        event_type=etype,
        target_arn=raw["target_arn"],
        occurred_at=raw["occurred_at"],
        payload=payload,
        deduplication_key=raw["event_id"],
        collected_at=now,
    )


def _load(name: str) -> NormalizedThreatEvent:
    with (GOLDEN_INPUT / name).open(encoding="utf-8") as fp:
        return _normalized_from_input(json.load(fp))


# (파일, 케이스, 기대 risk_level, 기대 response_mode, 기대 reason_codes 집합)
# — 판정 규칙 확정: 2026-08-31 안성일(PR #206). 임계 변경 시 이 표와 risk_evaluator 상수를 함께 갱신.
CASES = [
    ("evt_open_ip_001.json", "S1", "MEDIUM", "AGENT_WAIT",
     {"RISK_OPEN_INGRESS_WORLD", "RISK_SENSITIVE_PORT_EXPOSED"}),
    ("evt_open_ip_002.json", "S2", "MEDIUM", "AGENT_WAIT",
     {"RISK_OPEN_INGRESS_WORLD", "RISK_ALL_PROTOCOL_OPEN"}),
    ("evt_open_ip_003.json", "S5", "MEDIUM", "AGENT_WAIT",
     {"RISK_OPEN_INGRESS_WORLD", "RISK_ALL_PORTS_EXPOSED"}),
    ("evt_open_ip_004.json", "S6", "MEDIUM", "AGENT_WAIT",
     {"RISK_OPEN_INGRESS_WORLD", "RISK_SENSITIVE_PORT_EXPOSED"}),
    ("evt_open_ip_005.json", "S7", "MEDIUM", "AGENT_WAIT",
     {"RISK_OPEN_INGRESS_WORLD", "RISK_SENSITIVE_PORT_EXPOSED"}),
    ("evt_ssh_bruteforce_001.json", "S3", "HIGH", "PRE_MITIGATION_0_5S", {"RISK_SSH_BRUTEFORCE"}),
    ("evt_ssh_bruteforce_002.json", "S4", "LOW", "AGENT_WAIT", {"RISK_SSH_LOW_SIGNAL"}),
    ("evt_ssh_bruteforce_003.json", "S8", "LOW", "AGENT_WAIT", {"RISK_SSH_LOW_SIGNAL"}),
    ("evt_ssh_bruteforce_004.json", "S9", "HIGH", "PRE_MITIGATION_0_5S", {"RISK_SSH_BRUTEFORCE"}),
    ("evt_ssh_bruteforce_005.json", "S10", "HIGH", "PRE_MITIGATION_0_5S", {"RISK_SSH_BRUTEFORCE"}),
]


@pytest.mark.parametrize("fname,case_id,exp_level,exp_mode,exp_codes", CASES,
                         ids=[c[1] for c in CASES])
def test_golden_case_provisional(fname, case_id, exp_level, exp_mode, exp_codes):
    result = evaluate_threat(_load(fname))
    assert result.initial_risk_level.value == exp_level, case_id
    assert result.response_mode.value == exp_mode, case_id
    assert set(result.reason_codes) == exp_codes, case_id


@pytest.mark.parametrize("fname", [c[0] for c in CASES], ids=[c[1] for c in CASES])
def test_reason_codes_non_empty(fname):
    # DB CHECK(category_risk_shape): SECOPS 는 reason_codes 배열 길이 ≥1
    assert len(evaluate_threat(_load(fname)).reason_codes) >= 1


def test_ipv6_world_open_treated_like_ipv4():
    # S7 논점: ::/0 를 0.0.0.0/0 와 동급으로 잡는가(문자열 IPv4 한정 방지)
    r6 = evaluate_threat(_load("evt_open_ip_005.json"))       # tcp22 ::/0
    r4 = evaluate_threat(_load("evt_open_ip_001.json"))       # tcp22 0.0.0.0/0
    assert r6.initial_risk_level == r4.initial_risk_level
    assert set(r6.reason_codes) == set(r4.reason_codes)


def test_rdp_same_tier_as_ssh_port():
    # S6 논점: 3389(RDP)이 22(SSH)와 같은 민감 포트 등급인가
    rdp = evaluate_threat(_load("evt_open_ip_004.json"))
    assert "RISK_SENSITIVE_PORT_EXPOSED" in rdp.reason_codes


def _ssh_event(count: int, window: int) -> NormalizedThreatEvent:
    return NormalizedThreatEvent(
        threat_event_id=f"te-{count}-{window}", source_event_id=f"{count}-{window}",
        event_type=ThreatEventType.SSH_BRUTE_FORCE,
        target_arn="arn:aws:ec2:ap-northeast-2:123456789012:instance/i-syn",
        occurred_at=datetime.now(timezone.utc),
        payload=SshBruteForceThreatPayload(
            source_ip="203.0.113.9", failed_attempt_count=count, window_seconds=window,
        ),
        deduplication_key=f"{count}-{window}", collected_at=datetime.now(timezone.utc),
    )


def test_ssh_medium_band_sustained_below_rate():
    # 지속적이나 발동선 미만(시도 60 & 분당 6) → MEDIUM. Golden 10건엔 없는 밴드.
    r = evaluate_threat(_ssh_event(60, 600))
    assert r.initial_risk_level.value == "MEDIUM"
    assert set(r.reason_codes) == {"RISK_SSH_BRUTEFORCE"}


def test_ssh_short_burst_is_medium_not_low():
    # 🔵 해소: 9회/1초=540/min 짧은 버스트 — 단발(1회)이 아니고 속도가 높아 LOW 로 떨어지지 않는다.
    r = evaluate_threat(_ssh_event(9, 1))
    assert r.initial_risk_level.value == "MEDIUM"


def test_non_world_open_ip_rejected():
    # ③: 전체 공개가 아닌 OPEN_IP 는 접수 단계에서 거부 — 평가기 도달 시 방어적으로 거절
    raw = {
        "event_id": "evt-narrow", "event_type": "OPEN_IP",
        "target_arn": "arn:aws:ec2:ap-northeast-2:123456789012:security-group/sg-x",
        "occurred_at": "2026-08-20T06:10:00Z",
        "protocol": "tcp", "from_port": 22, "to_port": 22, "source_cidr": "10.0.0.0/8",
    }
    with pytest.raises(ValueError):
        evaluate_threat(_normalized_from_input(raw))


# --- _hits_sensitive_port 가드 방어 테스트 (#282) --------------------------------
# S15(역순 범위)·S18(to=null)이 골든에서 빠지면 아래 두 가드가 골든 회귀 보호를 잃는다.
# min/max 정규화·None 가드를 순수 함수 단위로 직접 고정한다(#272/#273 연계).


@pytest.mark.parametrize(
    "from_port,to_port,expected",
    [
        (22, 22, True),          # 단일 포트 22
        (3389, 3389, True),      # 단일 포트 3389(RDP)
        (20, 25, True),          # 범위가 22 포함
        (22, 3389, True),        # 범위가 둘 다 포함
        (23, 23, False),         # 경계 바로 옆 — 미포함
        (0, 65535, True),        # 전 포트는 22 포함(단, 분기 순서상 실제 판정은 ALL_PORTS 우선)
        (80, 443, False),        # 민감 포트 없는 범위
    ],
)
def test_hits_sensitive_port_ranges(from_port, to_port, expected):
    assert _hits_sensitive_port(from_port, to_port) is expected


@pytest.mark.parametrize("from_port,to_port", [(25, 20), (3389, 22)])
def test_hits_sensitive_port_normalizes_reversed_range(from_port, to_port):
    # S15 가드: AWS 는 from>to 를 거부하므로 실입력엔 없지만, 역순이 들어와도 min/max 로
    # 정규화해 잡는다(수집단 버그 방어층). 이 테스트가 없으면 정규화 제거 회귀를 못 잡는다.
    assert _hits_sensitive_port(from_port, to_port) is True


@pytest.mark.parametrize("from_port,to_port", [(22, None), (None, 22), (None, None)])
def test_hits_sensitive_port_none_guard_returns_false(from_port, to_port):
    # S18 가드: 반쪽만 적힌 포트쌍(from 또는 to 가 null)은 민감으로 보지 않는다 — 수집 경로에서
    # 나올 수 없는 malformed 입력이라 의도된 미탐(#282). 이 가드가 없으면 min/max 가 TypeError 를
    # 낸다. 정책이 바뀌면 이 테스트와 #282 를 함께 갱신한다.
    assert _hits_sensitive_port(from_port, to_port) is False


def test_half_specified_port_pair_not_flagged_sensitive_end_to_end():
    # #282 미탐을 계약 레벨에서 고정 — from=22·to=null 세계개방은 WORLD 만 붙고 SENSITIVE 는 안 붙는다.
    raw = {
        "event_id": "evt-half", "event_type": "OPEN_IP",
        "target_arn": "arn:aws:ec2:ap-northeast-2:123456789012:security-group/sg-half",
        "occurred_at": "2026-08-20T06:10:00Z",
        "protocol": "tcp", "from_port": 22, "source_cidr": "0.0.0.0/0",  # to_port 없음(null)
    }
    result = evaluate_threat(_normalized_from_input(raw))
    assert set(result.reason_codes) == {"RISK_OPEN_INGRESS_WORLD"}
    assert result.initial_risk_level.value == "MEDIUM"
