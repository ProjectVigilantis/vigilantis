# ==============================================================================
# [파일 설명]  담당: 안성일 (AI/Guardrail · Architect)
# Mock 위협 입력·정규화·초기 위험 판정의 내부 계약입니다. (Issue #49)
# 구 계약(SeverityLevel·MitigationAction·ThreatEvent — EventBridge/GuardDuty 전제)은
# 저장소 내 소비처가 없어 이 계약으로 교체했다.
#
# 계약 원칙
#   - Mock 입력은 severity·response_mode를 받지 않는다. Pydantic은 형식만 검증하고
#     초기 위험도·대응 경로는 Risk Evaluator가 생성한다(extra=forbid로 거부).
#   - 입력 event_id는 외부 원본 식별자다. 정규화 이후에는 서버가 발급한
#     threat_event_id를 쓰고, 입력 event_id는 source_event_id로 옮겨 보존한다.
#     중복 판정은 source_event_id·deduplication_key로 한다.
#   - 초기 판정 High→PRE_MITIGATION_0_5S, Medium·Low→AGENT_WAIT.
#     TIMEOUT_ISOLATION_1M은 초기 판정 결과가 아니다(응답 기한 만료 시 전환).
#   - reason_codes 는 RiskReasonCode(2026-08-31 안성일 결정, PR #206) 제한·최소 1개.
# ==============================================================================

from __future__ import annotations

from enum import Enum, unique
from ipaddress import ip_address, ip_network
from typing import Annotated, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .api.assets import UtcDateTime
from .api.incidents import ResponseMode, RiskLevel


@unique
class ThreatEventType(str, Enum):
    OPEN_IP = "OPEN_IP"
    SSH_BRUTE_FORCE = "SSH_BRUTE_FORCE"
    # 고객 계정 GuardDuty 탐지 결과(#588 C). Mock 입력이 아니라 수집 주기가 읽어 정규화한다
    # (services/guardduty.py · guardduty_ingress.py) — 그래서 MockThreatEventInput 에는 없다.
    GUARDDUTY_FINDING = "GUARDDUTY_FINDING"


class OpenIpThreatInput(BaseModel):
    """SG 인그레스 전체개방 Mock 이벤트 — 비공개 Threat Ingress 입력."""

    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(min_length=1)
    event_type: Literal[ThreatEventType.OPEN_IP] = ThreatEventType.OPEN_IP
    target_arn: str = Field(min_length=1)  # 대상 SG ARN
    occurred_at: UtcDateTime
    protocol: str = Field(min_length=1)
    from_port: Optional[int] = Field(None, ge=0, le=65535)
    to_port: Optional[int] = Field(None, ge=0, le=65535)
    source_cidr: str = Field(min_length=1)

    @field_validator("source_cidr")
    @classmethod
    def _valid_source_cidr(cls, value: str) -> str:
        if "/" not in value:
            raise ValueError("source_cidr는 CIDR 형식이어야 합니다")
        ip_network(value)
        return value


class SshBruteForceThreatInput(BaseModel):
    """SSH 브루트포스 Mock 이벤트 — 비공개 Threat Ingress 입력."""

    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(min_length=1)
    event_type: Literal[ThreatEventType.SSH_BRUTE_FORCE] = ThreatEventType.SSH_BRUTE_FORCE
    target_arn: str = Field(min_length=1)  # 대상 EC2 ARN
    source_ip: str = Field(min_length=1)   # 공격 IP — 이 유형에서는 필수
    occurred_at: UtcDateTime
    failed_attempt_count: int = Field(ge=1)
    window_seconds: int = Field(ge=1)

    @field_validator("source_ip")
    @classmethod
    def _valid_source_ip(cls, value: str) -> str:
        ip_address(value)
        return value


MockThreatEventInput = Annotated[
    Union[OpenIpThreatInput, SshBruteForceThreatInput],
    Field(discriminator="event_type"),
]


class OpenIpThreatPayload(BaseModel):
    """정규화 후 유형별 세부 필드 — 공통 필드는 NormalizedThreatEvent 명시 컬럼."""

    model_config = ConfigDict(extra="forbid")

    protocol: str = Field(min_length=1)
    from_port: Optional[int] = Field(None, ge=0, le=65535)
    to_port: Optional[int] = Field(None, ge=0, le=65535)
    source_cidr: str = Field(min_length=1)


class SshBruteForceThreatPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_ip: str = Field(min_length=1)
    failed_attempt_count: int = Field(ge=1)
    window_seconds: int = Field(ge=1)


#: GuardDuty 심각도 범위. Critical(9.0–10.0)까지 담는다.
GUARDDUTY_SEVERITY_MAX = 10.0


class GuardDutyFindingThreatPayload(BaseModel):
    """GuardDuty 탐지 1건(#588 C). 대상 자원 ARN 은 공통 칸 target_arn 이다.

    대상이 EC2 인스턴스면 인스턴스 ARN, 그 밖의 자원(IAM 키 · S3 등)은 탐지 자체의 ARN 이다 —
    resource_type 이 어느 쪽인지 알린다(security/threat_normalizer.py).

    region 은 읽은 탐지기의 리전, finding_region 은 탐지가 표시한 리전이다 — 글로벌 서비스(IAM · STS) 이벤트의
    탐지는 다른 리전에서 탐지해도 us-east-1 로 표시돼 둘이 다를 수 있다."""

    model_config = ConfigDict(extra="forbid")

    finding_id: str = Field(min_length=1)
    finding_type: str = Field(min_length=1)    # 예: UnauthorizedAccess:EC2/SSHBruteForce
    severity: float = Field(ge=0, le=GUARDDUTY_SEVERITY_MAX)
    resource_type: str = Field(min_length=1)   # GuardDuty Resource.ResourceType (Instance · AccessKey …)
    region: str = Field(min_length=1)          # 읽은 탐지기의 리전
    finding_region: str = Field(min_length=1)  # 탐지의 Region
    first_seen_at: UtcDateTime
    last_seen_at: UtcDateTime

    @model_validator(mode="after")
    def _seen_in_order(self):
        if self.last_seen_at < self.first_seen_at:
            raise ValueError("last_seen_at 은 first_seen_at 보다 앞설 수 없습니다")
        return self


_PAYLOAD_BY_TYPE: dict[ThreatEventType, type[BaseModel]] = {
    ThreatEventType.OPEN_IP: OpenIpThreatPayload,
    ThreatEventType.SSH_BRUTE_FORCE: SshBruteForceThreatPayload,
    ThreatEventType.GUARDDUTY_FINDING: GuardDutyFindingThreatPayload,
}


class NormalizedThreatEvent(BaseModel):
    """검증·정규화된 위협 이벤트 — Threat Ingress → PostgreSQL·Security Workflow."""

    model_config = ConfigDict(extra="forbid")

    threat_event_id: str = Field(min_length=1)  # 서버 발급 식별자
    source_event_id: str = Field(min_length=1)  # 입력 event_id 보존
    event_type: ThreatEventType
    target_arn: str = Field(min_length=1)
    occurred_at: UtcDateTime
    payload: Union[OpenIpThreatPayload, SshBruteForceThreatPayload, GuardDutyFindingThreatPayload]
    deduplication_key: str = Field(min_length=1)
    collected_at: UtcDateTime

    @model_validator(mode="before")
    @classmethod
    def _bind_payload_to_event_type(cls, data):
        # smart-union 오매칭 방지: event_type이 지정한 payload 모델로만 검증한다
        if isinstance(data, dict) and isinstance(data.get("payload"), dict):
            try:
                event_type = ThreatEventType(data.get("event_type"))
            except (ValueError, TypeError):
                return data  # event_type 오류는 필드 검증이 보고한다
            data = dict(data)
            data["payload"] = _PAYLOAD_BY_TYPE[event_type].model_validate(data["payload"])
        return data

    @model_validator(mode="after")
    def _enforce_payload_shape(self):
        expected = _PAYLOAD_BY_TYPE[self.event_type]
        if not isinstance(self.payload, expected):
            raise ValueError(
                f"{self.event_type.value} 이벤트의 payload는 {expected.__name__}이어야 합니다"
            )
        return self


_EXPECTED_MODE_BY_RISK: dict[RiskLevel, ResponseMode] = {
    RiskLevel.HIGH: ResponseMode.PRE_MITIGATION_0_5S,
    RiskLevel.MEDIUM: ResponseMode.AGENT_WAIT,
    RiskLevel.LOW: ResponseMode.AGENT_WAIT,
}


def expected_mode_for(risk_level: RiskLevel) -> ResponseMode:
    """초기 판정 위험도 → 대응 경로(response_mode) 매핑의 공개 접점.

    Risk Evaluator 등 외부 소비자가 비공개 `_EXPECTED_MODE_BY_RISK` 를 넘어 import 하지
    않도록 공개 헬퍼로 노출한다. InitialRiskEvaluationResult validator 가 이 매핑을 재검증한다.
    """
    return _EXPECTED_MODE_BY_RISK[risk_level]


@unique
class RiskReasonCode(str, Enum):
    """Risk Evaluator 판정 근거 코드 (2026-08-31 안성일 결정, PR #206).

    가드레일 reason code 관례를 계승한다(`packages/schemas/guardrails.py`):
    접두어(`RISK_`)로 축을 표시하고, DB(`initial_risk_reason_codes` JSONB)에 값이
    남으므로 **이름은 늘리되 기존 값은 바꾸지 않는다**. 판정당 최소 1개(DB CHECK
    `category_risk_shape`: SECOPS면 reason_codes 배열 길이 ≥1).

    ② 자산 문맥 미의존 판정이라 자산 기반 코드(prod·조치불가)는 두지 않는다.
    """

    RISK_OPEN_INGRESS_WORLD = "RISK_OPEN_INGRESS_WORLD"          # 0.0.0.0/0·::/0 전체개방 인그레스
    RISK_SENSITIVE_PORT_EXPOSED = "RISK_SENSITIVE_PORT_EXPOSED"  # 22(SSH)·3389(RDP) 노출
    RISK_ALL_PROTOCOL_OPEN = "RISK_ALL_PROTOCOL_OPEN"            # protocol -1 전 프로토콜 개방
    RISK_ALL_PORTS_EXPOSED = "RISK_ALL_PORTS_EXPOSED"            # 단일 프로토콜 전 포트(0–65535) 개방
    RISK_SSH_BRUTEFORCE = "RISK_SSH_BRUTEFORCE"                  # SSH 브루트포스(횟수·속도 임계)
    RISK_SSH_LOW_SIGNAL = "RISK_SSH_LOW_SIGNAL"                  # SSH 임계 미만(오탐·오타 가능)
    RISK_GUARDDUTY_FINDING = "RISK_GUARDDUTY_FINDING"            # GuardDuty 탐지 — 위험도는 심각도 구간(#588 Q2)


class InitialRiskEvaluationResult(BaseModel):
    """결정적 초기 위험 판정 — Risk Evaluator → PostgreSQL·Security Workflow.

    initial_risk_level은 생성 후 불변이며 AI 사후 평가(reviewed_risk_level)와
    분리된다 — 서로 덮어쓰지 않는다.
    """

    model_config = ConfigDict(extra="forbid")

    threat_event_id: str = Field(min_length=1)
    initial_risk_level: RiskLevel
    response_mode: ResponseMode
    # ③(2026-08-31 안성일): RiskReasonCode 로 제한하고 최소 1개를 강제한다
    # (DB CHECK category_risk_shape: SECOPS reason_codes 배열 길이 ≥1과 정렬).
    reason_codes: list[RiskReasonCode] = Field(min_length=1)

    @model_validator(mode="after")
    def _mode_matches_risk(self):
        expected = _EXPECTED_MODE_BY_RISK[self.initial_risk_level]
        if self.response_mode != expected:
            raise ValueError(
                f"초기 판정 {self.initial_risk_level.value}의 response_mode는 "
                f"{expected.value}이어야 합니다 (TIMEOUT_ISOLATION_1M은 초기 판정 결과가 아님)"
            )
        return self
