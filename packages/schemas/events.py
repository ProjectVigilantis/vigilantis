from pydantic import BaseModel, Field
from datetime import datetime, timezone
from typing import Optional, Dict, Any
from enum import Enum


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)

class SeverityLevel(str, Enum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"

class MitigationAction(str, Enum):
    PRE_MITIGATION_0_5S = "PRE_MITIGATION_0_5S"  # 0.5초 즉시 선 차단
    AGENT_WAIT = "AGENT_WAIT"                    # AI 분석 & 관제자 승인 대기
    TIMEOUT_ISOLATION_1M = "TIMEOUT_ISOLATION_1M" # 1분 미응답 시 자동 격리

class ThreatEvent(BaseModel):
    event_id: str = Field(..., description="EventBridge 고유 이벤트 ID")
    rule_arn: str = Field(..., description="감지된 GuardDuty/EventBridge 규칙 ARN")
    severity: SeverityLevel = Field(..., description="위험도 (HIGH, MEDIUM, LOW)")
    action_type: MitigationAction = Field(..., description="대응 방식 유형")
    target_arn: str = Field(..., description="격리/차단 대상 AWS 리소스 ARN")
    source_ip: Optional[str] = Field(None, description="공격자 IP 주소 (있는 경우)")
    timestamp: datetime = Field(default_factory=_utcnow)
    raw_payload: Optional[Dict[str, Any]] = Field(None, description="CloudTrail/GuardDuty Raw JSON")
