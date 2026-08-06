"""
Vigilantis Common Pydantic Schemas Package.
Exporting all data transfer objects (DTOs) for assets, events, guardrails, and runbooks.
"""

try:
    from packages.schemas.assets import (
        AssetMetadata,
        AssetType,
        DriftStatus,
        TerraformDrift,
        TerraformResource,
    )
    from packages.schemas.events import (
        CloudTrailEvent,
        EventSource,
        GuardDutyFinding,
        ThreatEvent,
        ThreatSeverity,
    )
    from packages.schemas.guardrails import (
        GuardrailEvaluation,
        GuardrailRequest,
        GuardrailResponse,
        GuardrailStatus,
        GuardrailStep,
    )
    from packages.schemas.runbooks import (
        ExecutionMode,
        ExecutionStatus,
        RunbookDefinition,
        RunbookExecutionRequest,
        RunbookExecutionResult,
        RunbookParameter,
        RunbookRiskLevel,
    )
except ImportError:
    from assets import (
        AssetMetadata,
        AssetType,
        DriftStatus,
        TerraformDrift,
        TerraformResource,
    )
    from events import (
        CloudTrailEvent,
        EventSource,
        GuardDutyFinding,
        ThreatEvent,
        ThreatSeverity,
    )
    from guardrails import (
        GuardrailEvaluation,
        GuardrailRequest,
        GuardrailResponse,
        GuardrailStatus,
        GuardrailStep,
    )
    from runbooks import (
        ExecutionMode,
        ExecutionStatus,
        RunbookDefinition,
        RunbookExecutionRequest,
        RunbookExecutionResult,
        RunbookParameter,
        RunbookRiskLevel,
    )

__all__ = [
    # Assets
    "AssetType",
    "DriftStatus",
    "AssetMetadata",
    "TerraformResource",
    "TerraformDrift",
    # Events
    "EventSource",
    "ThreatSeverity",
    "GuardDutyFinding",
    "CloudTrailEvent",
    "ThreatEvent",
    # Guardrails
    "GuardrailStep",
    "GuardrailStatus",
    "GuardrailEvaluation",
    "GuardrailRequest",
    "GuardrailResponse",
    # Runbooks",
    "RunbookRiskLevel",
    "ExecutionMode",
    "ExecutionStatus",
    "RunbookParameter",
    "RunbookDefinition",
    "RunbookExecutionRequest",
    "RunbookExecutionResult",
]
