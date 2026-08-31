"""Risk Evaluator 단위 테스트 — SecOps Golden 10케이스(S1~S10) 기준. DB·LocalStack 불필요.

⚠️ 아래 기대값은 **잠정 판정안(2026-08-31)** 을 인코딩한다 — 안성일 승인 시 임계·매핑이
바뀌면 이 표와 `security/risk_evaluator.py` 상수를 함께 갱신한다. SecOps Golden expected
정답지(datasets/golden/secops/expected)는 규칙 확정 전까지 채우지 않는다(추측 금지 원칙).
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
from security.risk_evaluator import evaluate_threat  # noqa: E402

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


# (파일, 케이스, 기대 risk_level, 기대 response_mode, 기대 reason_codes 집합) — 잠정안
CASES = [
    ("evt_open_ip_001.json", "S1", "MEDIUM", "AGENT_WAIT",
     {"RISK_OPEN_INGRESS_WORLD", "RISK_SENSITIVE_PORT_EXPOSED"}),
    ("evt_open_ip_002.json", "S2", "MEDIUM", "AGENT_WAIT",
     {"RISK_OPEN_INGRESS_WORLD", "RISK_ALL_PROTOCOL_OPEN", "RISK_SENSITIVE_PORT_EXPOSED"}),
    ("evt_open_ip_003.json", "S5", "MEDIUM", "AGENT_WAIT",
     {"RISK_OPEN_INGRESS_WORLD", "RISK_SENSITIVE_PORT_EXPOSED"}),
    ("evt_open_ip_004.json", "S6", "MEDIUM", "AGENT_WAIT",
     {"RISK_OPEN_INGRESS_WORLD", "RISK_SENSITIVE_PORT_EXPOSED"}),
    ("evt_open_ip_005.json", "S7", "MEDIUM", "AGENT_WAIT",
     {"RISK_OPEN_INGRESS_WORLD", "RISK_SENSITIVE_PORT_EXPOSED"}),
    ("evt_ssh_bruteforce_001.json", "S3", "HIGH", "PRE_MITIGATION_0_5S", {"RISK_SSH_BRUTEFORCE"}),
    ("evt_ssh_bruteforce_002.json", "S4", "LOW", "AGENT_WAIT", {"RISK_LOW_SIGNAL"}),
    ("evt_ssh_bruteforce_003.json", "S8", "LOW", "AGENT_WAIT", {"RISK_LOW_SIGNAL"}),
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


def test_ssh_medium_band_provisional():
    # 중간대(시도 10~99 & 분당 20 미만) — Golden 10건엔 없는 밴드. 구현 3분기(LOW/MEDIUM/HIGH)를 고정.
    ev = NormalizedThreatEvent(
        threat_event_id="te-mid", source_event_id="mid",
        event_type=ThreatEventType.SSH_BRUTE_FORCE,
        target_arn="arn:aws:ec2:ap-northeast-2:123456789012:instance/i-mid",
        occurred_at=datetime.now(timezone.utc),
        payload=SshBruteForceThreatPayload(
            source_ip="203.0.113.9", failed_attempt_count=60, window_seconds=600,  # 6/min
        ),
        deduplication_key="mid", collected_at=datetime.now(timezone.utc),
    )
    r = evaluate_threat(ev)
    assert r.initial_risk_level.value == "MEDIUM"
    assert r.response_mode.value == "AGENT_WAIT"
    assert set(r.reason_codes) == {"RISK_SSH_BRUTEFORCE"}


def test_asset_context_arg_is_optional_and_ignored_for_now():
    # 결정 ②(자산 문맥 의존) 확정 전 — asset_context 를 받되 결과가 바뀌지 않아야 한다
    ev = _load("evt_ssh_bruteforce_005.json")  # S10 (prod EC2)
    base = evaluate_threat(ev)
    with_ctx = evaluate_threat(ev, asset_context={"is_prod": True, "actionable": False})
    assert base.initial_risk_level == with_ctx.initial_risk_level
    assert set(base.reason_codes) == set(with_ctx.reason_codes)
