from enum import Enum
from typing import Any, Dict, List, Optional
from datetime import datetime, timezone
from pydantic import BaseModel, Field


class GuardrailStep(str, Enum):
    STEP1_INPUT_SANITIZATION = "STEP1_INPUT_SANITIZATION"
    STEP2_SCHEMA_VALIDATION = "STEP2_SCHEMA_VALIDATION"
    STEP3_ACTION_WHITELIST = "STEP3_ACTION_WHITELIST"
    STEP4_ARN_DRYRUN_MATCH = "STEP4_ARN_DRYRUN_MATCH"


class GuardrailStatus(str, Enum):
    PASSED = "PASSED"
    BLOCKED = "BLOCKED"
    WARNING = "WARNING"


class GuardrailEvaluation(BaseModel):
    step: GuardrailStep = Field(..., description="Guardrail pipeline evaluation step")
    status: GuardrailStatus = Field(..., description="Result status for this step")
    details: str = Field(..., description="Evaluation logs or reasoning details")
    blocked_reason: Optional[str] = Field(default=None, description="Reason if step was blocked")
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc), description="Evaluation timestamp")


class GuardrailRequest(BaseModel):
    request_id: str = Field(..., description="Unique ID for this guardrail evaluation request")
    event_id: Optional[str] = Field(default=None, description="Associated threat event ID")
    runbook_id: str = Field(..., description="Runbook ID proposed by AI Agent")
    target_arn: str = Field(..., description="Target AWS Resource ARN to execute runbook against")
    parameters: Dict[str, Any] = Field(default_factory=dict, description="Proposed execution arguments")
    requester: str = Field(default="ai-engine", description="Initiator of execution request")
    trace_id: Optional[str] = Field(default=None, description="OpenTelemetry Trace ID")


class GuardrailResponse(BaseModel):
    request_id: str = Field(..., description="Matching guardrail evaluation request ID")
    is_allowed: bool = Field(..., description="Whether execution is fully approved across all 4 guardrail steps")
    overall_status: GuardrailStatus = Field(..., description="Overall evaluation status")
    evaluations: List[GuardrailEvaluation] = Field(default_factory=list, description="Step-by-step evaluation results")
    blocked_step: Optional[GuardrailStep] = Field(default=None, description="Step where block occurred, if any")
    blocked_reason: Optional[str] = Field(default=None, description="Summary reason for blocking execution")
    trace_id: Optional[str] = Field(default=None, description="OpenTelemetry Trace ID")
    evaluated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc), description="Completion timestamp")
