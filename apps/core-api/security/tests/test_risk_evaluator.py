"""Risk Evaluator 단위 테스트 — SecOps Golden 10케이스(S1~S10) 기준. DB·LocalStack 불필요.

아래 기대값은 확정 판정 규칙(2026-08-31 안성일 결정, PR #206)을 인코딩한다 — 임계·매핑이
바뀌면 이 표와 `security/risk_evaluator.py` 상수를 함께 갱신한다. SecOps Golden expected
정답지(datasets/golden/secops/expected)는 J3(박지현)에서 이 규칙대로 채운다.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

CORE_API = Path(__file__).resolve().parents[2]
REPO_ROOT = Path(__file__).resolve().parents[4]
for _p in (str(CORE_API), str(REPO_ROOT / "packages")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from schemas.events import NormalizedThreatEvent  # noqa: E402
from security.risk_evaluator import evaluate_threat  # noqa: E402
from security.threat_normalizer import normalize_mock_input  # noqa: E402

GOLDEN_INPUT = REPO_ROOT / "datasets" / "golden" / "secops" / "input"


def _normalized_from_input(raw: dict) -> NormalizedThreatEvent:
    """Golden Mock 입력 → NormalizedThreatEvent.

    정형화는 프로덕션 코드(security/threat_normalizer.py)가 한다 — 여기서는 호출만
    한다. 종전에는 이 함수가 변환을 직접 들고 있어 tests/test_golden_dataset.py 의
    같은 헬퍼와 collected_at 이 갈렸고(now vs occurred_at), 둘 다 threat_event_id 를
    DB 에 넣을 수 없는 형식으로 만들고 있었다(#268).
    """
    return normalize_mock_input(raw)


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
    """골든에 없는 밴드를 합성한다. 골든 케이스와 같은 정형화 경로를 지난다 — 합성만
    다른 변환을 쓰면 이 테스트가 지키는 것이 프로덕션 동작이 아니게 된다(#268).
    occurred_at 은 판정에 쓰이지 않아 고정값으로 둔다."""
    return normalize_mock_input(
        {
            "event_id": f"syn-ssh-{count}-{window}",
            "event_type": "SSH_BRUTE_FORCE",
            "target_arn": "arn:aws:ec2:ap-northeast-2:123456789012:instance/i-syn",
            "source_ip": "203.0.113.9",
            "occurred_at": "2026-08-31T00:00:00Z",
            "failed_attempt_count": count,
            "window_seconds": window,
        }
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
