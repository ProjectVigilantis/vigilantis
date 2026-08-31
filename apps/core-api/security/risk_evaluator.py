# ==============================================================================
# [파일 설명]  담당: 김승철 (Data & Rule Engine) · 디렉터리 오너 SEC(김세혁)
# 결정적 초기 위험 판정(Risk Evaluator). 정규화된 위협 이벤트(NormalizedThreatEvent)를
# 받아 initial_risk_level·response_mode·reason_codes(InitialRiskEvaluationResult)를 낸다.
# rule_engine(FinOps 자산 판정)과 짝이 되는 SecOps 판정 축이다.
#
# 판정 규칙 확정: 2026-08-31 안성일(아키텍트) 결정, PR #206 리뷰.
#   ② 자산 문맥 미의존 — 들어온 위협 정보(NormalizedThreatEvent)만 본다. 조치 가능성
#      (S5 default SG)·운영 자산 여부(S10 prod)는 초기 위험도에 반영하지 않고 가드레일·
#      실행 단계가 판단한다.
#   ③ reason_codes 는 RiskReasonCode 로 제한(최소 1개). 전 포트 개방(RISK_ALL_PORTS_EXPOSED)과
#      전 프로토콜 개방(RISK_ALL_PROTOCOL_OPEN)을 구분한다. 전체 공개가 아닌 OPEN_IP 는
#      위협 접수 단계에서 거부되므로 이 판정기에 도달하지 않는다(도달 시 방어적으로 거절).
#   ④ OpenIP 전체 공개 → MEDIUM(관제자 확인). SSH → 실패 횟수와 발생 속도를 함께 본다.
#      횟수 단독(예: 100회)으로 HIGH 가 되지는 않는다(관측 창이 일정하지 않음).
# ==============================================================================

from __future__ import annotations

from typing import Optional

# schemas 는 진입점(main.py·conftest)이 sys.path 에 올린다 — collector.py·rule_engine.py 와
# 동일하게 여기서는 부트스트랩하지 않는다. RiskLevel 은 events 가 재노출하므로 원천을 하나로 모은다.
from schemas.events import (
    InitialRiskEvaluationResult,
    NormalizedThreatEvent,
    OpenIpThreatPayload,
    RiskLevel,
    RiskReasonCode,
    SshBruteForceThreatPayload,
    ThreatEventType,
    expected_mode_for,
)

# ----- 판정 임계값 (2026-08-31 안성일 결정) -----
WORLD_CIDRS = ("0.0.0.0/0", "::/0")     # IPv4·IPv6 전체개방 (S7 IPv6 누락 방지)
ALL_PROTOCOL = "-1"                     # 전 프로토콜 개방 표기
ALL_PORTS = (0, 65535)                  # 단일 프로토콜 전 포트 개방 (S5)
SENSITIVE_PORTS = (22, 3389)           # SSH·RDP — 노출 시 민감 (S1·S6)
SSH_SINGLE_ATTEMPT = 1                 # 단발 시도는 발생 속도와 무관하게 LOW (S8)
SSH_HIGH_ATTEMPT_MIN = 10              # HIGH 하한(횟수) — 발생 속도 조건과 AND
SSH_HIGH_RATE_PER_MIN = 20.0           # HIGH 하한(분당 시도)


def _hits_sensitive_port(from_port: Optional[int], to_port: Optional[int]) -> bool:
    """개방 포트 범위가 민감 포트(22/3389)를 포함하는가. 단일 포트는 from==to."""
    if from_port is None or to_port is None:
        return False
    lo, hi = min(from_port, to_port), max(from_port, to_port)
    return any(lo <= p <= hi for p in SENSITIVE_PORTS)


def _is_all_ports(from_port: Optional[int], to_port: Optional[int]) -> bool:
    """단일 프로토콜의 전 포트(0–65535) 개방 여부."""
    return (from_port, to_port) == ALL_PORTS


def _evaluate_open_ip(payload: OpenIpThreatPayload) -> tuple[RiskLevel, list[RiskReasonCode]]:
    if payload.source_cidr not in WORLD_CIDRS:
        # ③: 전체 공개가 아닌 OPEN_IP 는 접수 단계에서 거부된다 — 여기 도달하면 계약 위반.
        raise ValueError(
            f"전체 공개(0.0.0.0/0·::/0)가 아닌 OPEN_IP 는 접수 단계에서 거부되어야 한다: "
            f"{payload.source_cidr}"
        )

    codes = [RiskReasonCode.RISK_OPEN_INGRESS_WORLD]
    if payload.protocol == ALL_PROTOCOL:
        codes.append(RiskReasonCode.RISK_ALL_PROTOCOL_OPEN)          # 전 프로토콜 (S2)
    elif _is_all_ports(payload.from_port, payload.to_port):
        codes.append(RiskReasonCode.RISK_ALL_PORTS_EXPOSED)         # 단일 프로토콜 전 포트 (S5)
    elif _hits_sensitive_port(payload.from_port, payload.to_port):
        codes.append(RiskReasonCode.RISK_SENSITIVE_PORT_EXPOSED)    # 22·3389 (S1·S6·S7)
    # SG 전체개방의 대응 런북이 Medium(관제자 승인)이라 MEDIUM 으로 접지.
    return RiskLevel.MEDIUM, codes


def _evaluate_ssh_bruteforce(
    payload: SshBruteForceThreatPayload,
) -> tuple[RiskLevel, list[RiskReasonCode]]:
    count = payload.failed_attempt_count
    rate_per_min = count * 60.0 / payload.window_seconds

    if count <= SSH_SINGLE_ATTEMPT:
        # 단발 — 발생 속도 무관 LOW (S8: 1회/1초)
        return RiskLevel.LOW, [RiskReasonCode.RISK_SSH_LOW_SIGNAL]
    if count >= SSH_HIGH_ATTEMPT_MIN and rate_per_min >= SSH_HIGH_RATE_PER_MIN:
        # 횟수·속도 동시 충족 (S3·S9·S10)
        return RiskLevel.HIGH, [RiskReasonCode.RISK_SSH_BRUTEFORCE]
    if count < SSH_HIGH_ATTEMPT_MIN and rate_per_min < SSH_HIGH_RATE_PER_MIN:
        # 저강도·저속 — 오탐·오타 (S4: 5회/3600초)
        return RiskLevel.LOW, [RiskReasonCode.RISK_SSH_LOW_SIGNAL]
    # 한쪽만 충족(지속적이나 발동선 미만, 또는 짧은 버스트) → MEDIUM(관제자 확인)
    return RiskLevel.MEDIUM, [RiskReasonCode.RISK_SSH_BRUTEFORCE]


def evaluate_threat(event: NormalizedThreatEvent) -> InitialRiskEvaluationResult:
    """정규화 위협 이벤트 → 결정적 초기 위험 판정.

    입력은 NormalizedThreatEvent 하나뿐이다(② 자산 문맥 미의존). response_mode 는
    initial_risk_level 에서 파생하고(expected_mode_for — validator 가 재검증), reason_codes 는
    항상 ≥1개다(DB CHECK: SECOPS 배열 길이 ≥1).
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
        response_mode=expected_mode_for(level),
        reason_codes=codes,
    )
