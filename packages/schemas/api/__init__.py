# ==============================================================================
# [파일 설명]  담당: 안성일 (AI/Guardrail · Architect)
# Dashboard(FE)와의 외부 API DTO 네임스페이스입니다.
# 수집·정형화 계층(packages/schemas/*.py 최상위 모듈)과 계약 계층을 분리합니다.
#   - assets:    GET /api/v1/assets 응답 (이슈 #31)
#   - incidents: GET /api/v1/incidents/{id} 응답 (이슈 #32)
#   - actions:   POST /api/v1/actions/execute 요청·응답 (이슈 #32)
#   - ws:        WebSocket(/api/v1/ws) 공통 이벤트 봉투 (이슈 #32)
#   - errors:    REST 공통 오류 봉투 (이슈 #32)
# ==============================================================================

from .actions import (
    ExecuteActionRequest,
    ExecuteActionResponse,
    ExecutionStatus,
)
from .errors import ErrorCode, ErrorDetail, ErrorResponse
from .incidents import (
    ExecutionSummaryItem,
    IncidentCategory,
    IncidentListItem,
    IncidentResponse,
    IncidentsResponse,
    IncidentStatus,
    RecommendationItem,
    ResponseMode,
    RiskLevel,
)
from .ws import ExecutionEventData, IncidentEventData, WsEvent, WsEventType
from .assets import (
    AlbTargetGroupSpec,
    AsgSpec,
    AssetItem,
    AssetRelationship,
    AssetSpec,
    AssetsResponse,
    AssetType,
    CollectionStatus,
    EbsSpec,
    Ec2Spec,
    EvaluationStatus,
    LaunchTemplateSpec,
    NaclSpec,
    OpenPortRule,
    RelationType,
    ResourceRole,
    SgSpec,
    SkipReasonCode,
    UtcDateTime,
    Verdict,
)

__all__ = [
    "ErrorCode",
    "ErrorDetail",
    "ErrorResponse",
    "ExecuteActionRequest",
    "ExecuteActionResponse",
    "ExecutionEventData",
    "ExecutionStatus",
    "ExecutionSummaryItem",
    "IncidentCategory",
    "IncidentEventData",
    "IncidentListItem",
    "IncidentResponse",
    "IncidentsResponse",
    "IncidentStatus",
    "RecommendationItem",
    "ResponseMode",
    "RiskLevel",
    "WsEvent",
    "WsEventType",
    "AlbTargetGroupSpec",
    "AsgSpec",
    "AssetItem",
    "AssetRelationship",
    "AssetSpec",
    "AssetsResponse",
    "AssetType",
    "CollectionStatus",
    "EbsSpec",
    "Ec2Spec",
    "EvaluationStatus",
    "LaunchTemplateSpec",
    "NaclSpec",
    "OpenPortRule",
    "RelationType",
    "ResourceRole",
    "SgSpec",
    "SkipReasonCode",
    "UtcDateTime",
    "Verdict",
]
