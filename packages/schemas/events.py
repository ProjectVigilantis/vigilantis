from enum import Enum
from typing import Any, Dict, Optional
from datetime import datetime, timezone
from pydantic import BaseModel, Field


class EventSource(str, Enum):
    GUARDDUTY = "GUARDDUTY"
    CLOUDTRAIL = "CLOUDTRAIL"
    EVENTBRIDGE = "EVENTBRIDGE"
    LAMBDA_PRE_MITIGATION = "LAMBDA_PRE_MITIGATION"
    MANUAL = "MANUAL"


class ThreatSeverity(str, Enum):
    CRITICAL = "CRITICAL"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    INFORMATIONAL = "INFORMATIONAL"


class GuardDutyFinding(BaseModel):
    finding_id: str = Field(..., description="AWS GuardDuty Finding ID")
    title: str = Field(..., description="Finding title")
    description: str = Field(..., description="Finding detail description")
    severity: ThreatSeverity = Field(..., description="Normalized threat severity level")
    raw_severity: float = Field(..., description="Original GuardDuty numerical severity (0.0 to 10.0)")
    account_id: str = Field(..., description="AWS Account ID where finding occurred")
    region: str = Field(..., description="AWS Region")
    finding_type: str = Field(..., description="GuardDuty finding type (e.g. UnauthorizedAccess:EC2/SSHBruteForce)")
    resource_type: str = Field(..., description="Target AWS resource type")
    resource_id: str = Field(..., description="Target AWS resource ID / ARN")
    created_at: datetime = Field(..., description="Finding creation timestamp")
    raw_finding: Dict[str, Any] = Field(default_factory=dict, description="Full unparsed GuardDuty finding JSON")


class CloudTrailEvent(BaseModel):
    event_id: str = Field(..., description="CloudTrail Event ID")
    event_name: str = Field(..., description="AWS API call event name (e.g. AuthorizeSecurityGroupIngress)")
    event_source: str = Field(..., description="AWS service source (e.g. ec2.amazonaws.com)")
    event_time: datetime = Field(..., description="Timestamp of the API call")
    user_identity: Dict[str, Any] = Field(default_factory=dict, description="CloudTrail UserIdentity dictionary")
    aws_region: str = Field(..., description="AWS Region")
    source_ip_address: Optional[str] = Field(default=None, description="Source IP address of requester")
    request_parameters: Dict[str, Any] = Field(default_factory=dict, description="Request input parameters")
    response_elements: Optional[Dict[str, Any]] = Field(default=None, description="Response payload elements")


class ThreatEvent(BaseModel):
    event_id: str = Field(..., description="Unified event tracking ID")
    source: EventSource = Field(..., description="Originating event source")
    severity: ThreatSeverity = Field(..., description="Threat severity level")
    title: str = Field(..., description="Threat summary title")
    description: str = Field(..., description="Detailed description")
    affected_asset_id: str = Field(..., description="ID / ARN of affected resource")
    account_id: str = Field(..., description="AWS Account ID")
    region: str = Field(..., description="AWS Region")
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc), description="Event timestamp")
    trace_id: Optional[str] = Field(default=None, description="W3C Trace Context ID for OpenTelemetry")
    raw_payload: Dict[str, Any] = Field(default_factory=dict, description="Original event payload")
