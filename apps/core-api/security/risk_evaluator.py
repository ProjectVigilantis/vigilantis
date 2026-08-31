# ==============================================================================
# [파일 설명]  담당: 김승철 (Data & Rule Engine)
# 결정적 초기 위험 판정(Risk Evaluator). 정규화된 위협 이벤트(NormalizedThreatEvent)를
# 받아 initial_risk_level·response_mode·reason_codes(InitialRiskEvaluationResult)를 낸다.
# rule_engine(FinOps 자산 판정)과 짝이 되는 SecOps 판정 축이다.
#
# ⚠️ 임계값·위험도 매핑은 **잠정(2026-08-31)** — 안성일 승인 대기(스코핑 결정 ②③④,
#    #회의 Canvas). 확정 전까지 SecOps Golden expected 정답은 채우지 않는다(추측 금지).
#    - 결정 ②(자산 문맥 의존): evaluate_threat 은 asset_context 를 선택 인자로 받되
#      현재는 사용하지 않는다. "판정이 DB 자산을 조인하나"가 확정되면 여기서 소비한다
#      (S5=default SG 조치불가, S10=prod EC2 위험↑ 논점).
#    - 임계값 상수는 이 파일 상단 한 곳에 모아 두어 확정 시 교체가 국소적이다.
#
# 접지 근거(잠정): 위험도를 대응 런북의 위험도에 맞춘다 — OpenIP(SG 전체개방)의 조치는
#   NACL_ADD_DENY·SG_DELETE(Medium/관제자 승인)라 MEDIUM, SSH 브루트포스의 조치는
#   EC2_ISOLATE(High/0.5초 선차단)라 강도 임계 이상이면 HIGH.
# ==============================================================================

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

# import 경로: packages(schemas) — services/collector.py 등과 동일 관례
_PACKAGES = Path(__file__).resolve().parents[3] / "packages"
if str(_PACKAGES) not in sys.path:
    sys.path.insert(0, str(_PACKAGES))

from schemas.api.incidents import RiskLevel  # noqa: E402
from schemas.events import (  # noqa: E402
    NormalizedThreatEvent,
    OpenIpThreatPayload,
    RiskReasonCode,
    SshBruteForceThreatPayload,
    ThreatEventType,
    _EXPECTED_MODE_BY_RISK,
)
from schemas.events import InitialRiskEvaluationResult  # noqa: E402

# ----- 잠정 임계값 (안성일 승인 대기 — 확정 시 이 블록만 교체) -----
WORLD_CIDRS = ("0.0.0.0/0", "::/0")          # IPv4·IPv6 전체개방 (S7 IPv6 누락 방지)
ALL_PROTOCOL = "-1"                          # 전 프로토콜 개방 표기
SENSITIVE_PORTS = (22, 3389)                 # SSH·RDP — 노출 시 민감(S1·S6)
SSH_LOW_ATTEMPT_MAX = 10                     # 미만이면 LOW(오탐·오타) — S4(5)·S8(1)
SSH_HIGH_ATTEMPT_MIN = 100                   # 이상이면 HIGH — S3(120)·S9(1000)·S10(120)
SSH_HIGH_RATE_PER_MIN = 20.0                 # 분당 시도 이 값 이상이면 HIGH


def _hits_sensitive_port(from_port: Optional[int], to_port: Optional[int]) -> bool:
    """개방 포트 범위가 민감 포트(22/3389)를 포함하는가. 단일 포트는 from==to."""
    if from_port is None or to_port is None:
        return False
    lo, hi = min(from_port, to_port), max(from_port, to_port)
    return any(lo <= p <= hi for p in SENSITIVE_PORTS)


def _evaluate_open_ip(payload: OpenIpThreatPayload) -> tuple[RiskLevel, list[RiskReasonCode]]:
    world_open = payload.source_cidr in WORLD_CIDRS
    all_proto = payload.protocol == ALL_PROTOCOL
    sensitive = all_proto or _hits_sensitive_port(payload.from_port, payload.to_port)

    if not world_open:
        # 전체개방이 아니면 잠정 하한 — 특정 CIDR 대상 개방 규칙은 미확정
        return RiskLevel.LOW, [RiskReasonCode.RISK_LOW_SIGNAL]

    codes = [RiskReasonCode.RISK_OPEN_INGRESS_WORLD]
    if all_proto:
        codes.append(RiskReasonCode.RISK_ALL_PROTOCOL_OPEN)
    if sensitive:
        codes.append(RiskReasonCode.RISK_SENSITIVE_PORT_EXPOSED)
    # 잠정: SG 전체개방의 대응 런북이 Medium(관제자 승인)이라 MEDIUM 으로 접지.
    return RiskLevel.MEDIUM, codes


def _evaluate_ssh_bruteforce(
    payload: SshBruteForceThreatPayload,
) -> tuple[RiskLevel, list[RiskReasonCode]]:
    count = payload.failed_attempt_count
    rate_per_min = count * 60.0 / payload.window_seconds

    if count < SSH_LOW_ATTEMPT_MAX:
        return RiskLevel.LOW, [RiskReasonCode.RISK_LOW_SIGNAL]
    if count >= SSH_HIGH_ATTEMPT_MIN or rate_per_min >= SSH_HIGH_RATE_PER_MIN:
        return RiskLevel.HIGH, [RiskReasonCode.RISK_SSH_BRUTEFORCE]
    return RiskLevel.MEDIUM, [RiskReasonCode.RISK_SSH_BRUTEFORCE]


def evaluate_threat(
    event: NormalizedThreatEvent,
    asset_context: Optional[dict] = None,
) -> InitialRiskEvaluationResult:
    """정규화 위협 이벤트 → 결정적 초기 위험 판정.

    asset_context 는 결정 ②(자산 문맥 의존) 확정 전까지 받되 사용하지 않는다.
    response_mode 는 initial_risk_level 에서 파생한다(_EXPECTED_MODE_BY_RISK — validator 가
    동일 매핑을 재검증). reason_codes 는 항상 ≥1개(DB CHECK: SECOPS 배열 길이 ≥1).
    """
    if event.event_type == ThreatEventType.OPEN_IP:
        level, codes = _evaluate_open_ip(event.payload)  # type: ignore[arg-type]
    elif event.event_type == ThreatEventType.SSH_BRUTE_FORCE:
        level, codes = _evaluate_ssh_bruteforce(event.payload)  # type: ignore[arg-type]
    else:  # 방어적 — ThreatEventType 확장 시 명시적으로 실패
        raise ValueError(f"지원하지 않는 위협 유형: {event.event_type}")

    return InitialRiskEvaluationResult(
        threat_event_id=event.threat_event_id,
        initial_risk_level=level,
        response_mode=_EXPECTED_MODE_BY_RISK[level],
        reason_codes=[c.value for c in codes],
    )
