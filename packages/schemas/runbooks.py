from enum import Enum
from typing import Any, Dict, List, Optional
from datetime import datetime, timezone
from pydantic import BaseModel, Field


class RunbookRiskLevel(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class ExecutionMode(str, Enum):
    GITOPS_PR = "GITOPS_PR"
    BOTO3_DIRECT = "BOTO3_DIRECT"
    PRE_MITIGATION_LAMBDA = "PRE_MITIGATION_LAMBDA"


class ExecutionStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    ROLLED_BACK = "ROLLED_BACK"
    ROLLBACK_FAILED = "ROLLBACK_FAILED"


class RunbookParameter(BaseModel):
    name: str = Field(..., description="Parameter name")
    type: str = Field(..., description="Parameter data type (string, int, bool, list)")
    required: bool = Field(default=True, description="Whether parameter is mandatory")
    default: Optional[Any] = Field(default=None, description="Default value if not provided")
    description: str = Field(..., description="Description of the parameter")


class RunbookDefinition(BaseModel):
    runbook_id: str = Field(..., description="Registered Runbook ID (e.g. RB-EC2-ISOLATE-001)")
    title: str = Field(..., description="Runbook title")
    description: str = Field(..., description="Detailed functionality description")
    risk_level: RunbookRiskLevel = Field(..., description="Inherent execution risk level")
    allowed_target_types: List[str] = Field(..., description="Allowed AWS resource types (e.g. ['aws_instance', 'EC2'])")
    allowed_actions: List[str] = Field(..., description="Whitelisted API actions performed by runbook")
    parameters: List[RunbookParameter] = Field(default_factory=list, description="Runbook execution parameters")


class RunbookExecutionRequest(BaseModel):
    execution_id: str = Field(..., description="Unique ID for runbook execution instance")
    runbook_id: str = Field(..., description="ID of runbook being executed")
    target_arn: str = Field(..., description="Target AWS Resource ARN")
    parameters: Dict[str, Any] = Field(default_factory=dict, description="Execution parameters")
    execution_mode: ExecutionMode = Field(..., description="Execution path (GitOps PR vs Boto3 direct)")
    requester_id: str = Field(..., description="User or service ID initiating execution")
    trace_id: Optional[str] = Field(default=None, description="OpenTelemetry Trace ID")


class RunbookExecutionResult(BaseModel):
    execution_id: str = Field(..., description="Runbook execution instance ID")
    runbook_id: str = Field(..., description="Executed runbook ID")
    status: ExecutionStatus = Field(..., description="Final execution status")
    execution_mode: ExecutionMode = Field(..., description="Execution mode used")
    output: Dict[str, Any] = Field(default_factory=dict, description="Execution output details")
    error_message: Optional[str] = Field(default=None, description="Error message if execution failed")
    rollback_status: Optional[ExecutionStatus] = Field(default=None, description="Auto-rollback status if triggered")
    evidence_id: Optional[str] = Field(default=None, description="Evidence ID for audit trace")
    trace_id: Optional[str] = Field(default=None, description="OpenTelemetry Trace ID")
    started_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc), description="Start time")
    completed_at: Optional[datetime] = Field(default=None, description="Completion time")
