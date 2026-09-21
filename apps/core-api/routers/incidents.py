# ==============================================================================
# [파일 설명]
# GET /api/v1/incidents(목록)·GET /api/v1/incidents/{id}(상세) 조회와
# POST /api/v1/incidents/{id}/resolve(관제자 종료 처리) 라우터입니다.
# (Issue #68·#199)
#
#   - 응답은 공개 계약 schemas.api.incidents로만 직렬화한다. 목록은 상세의
#     부분집합이며 created_at 내림차순으로 전체 반환한다 — SSOT §API 계약.
#   - SQL은 db.repositories 경유 — 라우터는 응답 조립만 한다.
#   - recommendations는 Guardrail PASS 제안(EXECUTABLE 후보)만 담는다.
#   - available_recovery_runbook_ids는 실행 이력에서 파생한다 — 짝(ADR-0004)이
#     있고, 원본이 복구 가능 상태이며, 아직 복구가 접수되지 않은 실행만 노출한다.
#     (Issue #126)
#   - verification_hold는 실행 행의 판정 불가 보류 기록을 그대로 싣는다 — 사유는 저장된
#     typed 코드이며 error_summary 문자열에서 뽑지 않는다. (Issue #249)
#   - 종료의 상태 전이·트랜잭션은 workflows.resolve_incident가 소유한다. 라우터는
#     commit 이후 INCIDENT_UPDATED를 발행하는 데까지만 한다 — 발행을 Workflow에
#     두면 그 계층이 앱 상태(app.state.realtime)를 알아야 한다.
# ==============================================================================

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Optional

from fastapi import APIRouter, Depends, Request
from pydantic import ValidationError
from sqlalchemy.orm import Session

from schemas.api.errors import ErrorCode
from schemas.api.analysis import AnalysisResult
from schemas.api.incidents import (
    IncidentCategory,
    IncidentListItem,
    IncidentResponse,
    IncidentsResponse,
    IncidentStatus,
    OpenIpThreatContext,
    ResolveIncidentRequest,
    SshBruteForceThreatContext,
    ThreatContext,
)
from schemas.api.ws import WsEventType
from schemas.candidates import CandidateStatus
from schemas.events import SshBruteForceThreatPayload
from schemas.executions import EXECUTION_RECOVERABLE_STATUSES
from schemas.runbooks import ROLLBACK_RUNBOOK_BY_MAIN_ID

import workflows
from db import mappers, models
from db.repositories import executions as executions_repo
from db.repositories import incidents as incidents_repo
from db.session import get_db
from exceptions import ApiError
from identifiers import canonical_id
from incident_analysis import load_analysis_results
from realtime import incident_event

router = APIRouter(prefix="/api/v1", tags=["incidents"])
logger = logging.getLogger("vigilantis.incidents")


def _recovery_ids(
    execution: models.ActionExecution, recovered_parent_ids: set[str]
) -> list[str]:
    """관제자에게 열어 줄 복구 조치(롤백 3종). 세 조건을 모두 만족할 때만 노출한다.

    조건은 접수 판정(workflows._recoverable_origin)과 같은 것이어야 한다 —
    목록에 보이는데 누르면 409가 되거나 그 반대가 되면 화면이 거짓말을 한다.
    """
    if execution.status not in EXECUTION_RECOVERABLE_STATUSES:
        return []
    if execution.execution_id in recovered_parent_ids:
        return []
    rollback_id = ROLLBACK_RUNBOOK_BY_MAIN_ID.get(execution.runbook_id.value)
    return [rollback_id] if rollback_id is not None else []


def _verification_hold(execution: models.ActionExecution) -> Optional[dict]:
    """판정 불가 보류 기록. 없으면 None — 보류가 없던 실행과 판정이 내려진 실행이다."""
    if not execution.verification_attempts:
        return None
    return {
        "reason_code": execution.verification_reason_code.value,
        "attempts": execution.verification_attempts,
        "first_failed_at": execution.verification_first_failed_at,
        "last_failed_at": execution.verification_last_failed_at,
    }


def _to_threat_context(
    row: models.Incident, event: models.ThreatEvent | None,
) -> ThreatContext | None:
    """영속 위협 관측만 노출한다. 누락·불일치는 다른 Incident 조회를 막지 않는다."""
    if row.category != IncidentCategory.SECOPS:
        return None
    if row.threat_event_id is None:
        reason = "missing_threat_event_id"
    elif event is None:
        reason = "threat_event_not_found"
    elif event.target_arn != row.subject_arn:
        reason = "target_mismatch"
    else:
        try:
            threat = mappers.to_threat_event(event)
            if isinstance(threat.payload, SshBruteForceThreatPayload):
                return SshBruteForceThreatContext(
                    event_type=threat.event_type.value,
                    source_ip=threat.payload.source_ip,
                )
            return OpenIpThreatContext(
                event_type=threat.event_type.value,
                exposed_cidr=threat.payload.source_cidr,
            )
        except ValidationError:
            reason = "invalid_threat_event"

    # 원문·ValidationError는 IP·payload를 포함할 수 있어 남기지 않는다.
    logger.warning(
        "incident_threat_context_unavailable",
        extra={
            "incident_id": row.incident_id,
            "threat_event_id": row.threat_event_id,
            "reason": reason,
        },
    )
    return None


def _load_threat_contexts(
    db: Session, rows: Sequence[models.Incident],
) -> dict[str, ThreatContext | None]:
    events = incidents_repo.get_threat_events_by_ids(
        db, [
            row.threat_event_id for row in rows
            if row.category == IncidentCategory.SECOPS and row.threat_event_id is not None
        ],
    )
    return {
        row.incident_id: _to_threat_context(row, events.get(row.threat_event_id))
        for row in rows
    }


def _to_list_item(
    row: models.Incident, context: ThreatContext | None, analysis_result: AnalysisResult | None,
) -> IncidentListItem:
    return IncidentListItem.model_validate(
        {
            "incident_id": row.incident_id,
            "title": row.title,
            "subject_arn": row.subject_arn,
            "category": row.category,
            "status": row.status,
            "initial_risk_level": row.initial_risk_level,
            "reviewed_risk_level": row.reviewed_risk_level,
            "response_mode": row.response_mode,
            "threat_context": context,
            "analysis_result": analysis_result,
            "created_at": row.created_at,
            "updated_at": row.updated_at,
        }
    )


@router.get("/incidents", response_model=IncidentsResponse)
def list_incidents(
    status: Optional[IncidentStatus] = None,
    category: Optional[IncidentCategory] = None,
    db: Session = Depends(get_db),
) -> IncidentsResponse:
    rows = incidents_repo.list_incidents(db, status=status, category=category)
    contexts = _load_threat_contexts(db, rows)
    analyses = load_analysis_results(db, rows)
    return IncidentsResponse(items=[
        _to_list_item(row, contexts[row.incident_id], analyses[row.incident_id]) for row in rows
    ])


def _load_incident(db: Session, incident_id: str) -> models.Incident:
    """형식이 어긋난 식별자도 404다 — 계약이 UUID를 요구하지 않으므로 계약 위반이
    아니라 없는 인시던트로 본다. 조회 전 변환은 DB 캐스트 오류(500)를 막는다."""
    stored_id = canonical_id(incident_id)
    if stored_id is None:
        raise ApiError(ErrorCode.INCIDENT_NOT_FOUND)
    row = incidents_repo.get_incident(db, stored_id)
    if row is None:
        raise ApiError(ErrorCode.INCIDENT_NOT_FOUND)
    return row


def _to_detail(db: Session, row: models.Incident) -> IncidentResponse:
    evidence_ids = [
        item.evidence_id for item in incidents_repo.list_evidence(db, row.incident_id)
    ]
    executable = incidents_repo.list_candidates(
        db, row.incident_id, status=CandidateStatus.EXECUTABLE
    )
    recommendations = [
        {
            "runbook_id": candidate.runbook_id,
            "target_arn": candidate.target_arn,
            "display_parameters": candidate.display_parameters,
            "ai_savings_estimate": candidate.ai_savings_estimate,
        }
        for candidate in sorted(executable, key=lambda c: c.created_at)
    ]
    execution_rows = executions_repo.list_by_incident(db, row.incident_id)
    recovered_parent_ids = {
        execution.parent_execution_id
        for execution in execution_rows
        if execution.parent_execution_id is not None
    }
    executions = [
        {
            "execution_id": execution.execution_id,
            "runbook_id": execution.runbook_id,
            "status": execution.status,
            "available_recovery_runbook_ids": _recovery_ids(
                execution, recovered_parent_ids
            ),
            "verification_hold": _verification_hold(execution),
            "updated_at": execution.updated_at,
        }
        for execution in execution_rows
    ]
    return IncidentResponse.model_validate(
        {
            "incident_id": row.incident_id,
            "title": row.title,
            "subject_arn": row.subject_arn,
            "category": row.category,
            "status": row.status,
            "initial_risk_level": row.initial_risk_level,
            "reviewed_risk_level": row.reviewed_risk_level,
            "response_mode": row.response_mode,
            "threat_context": _load_threat_contexts(db, [row])[row.incident_id],
            "summary_lines": row.summary_lines,
            "analysis_result": load_analysis_results(db, [row])[row.incident_id],
            "evidence_ids": evidence_ids,
            "recommendations": recommendations,
            "executions": executions,
            "resolution": row.resolution,
            "resolution_note": row.resolution_note,
            "resolved_at": row.resolved_at,
            "created_at": row.created_at,
            "updated_at": row.updated_at,
        }
    )


@router.get("/incidents/{incident_id}", response_model=IncidentResponse)
def get_incident(incident_id: str, db: Session = Depends(get_db)) -> IncidentResponse:
    return _to_detail(db, _load_incident(db, incident_id))


@router.post("/incidents/{incident_id}/resolve", response_model=IncidentResponse)
def resolve_incident(
    incident_id: str,
    payload: ResolveIncidentRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> IncidentResponse:
    """이미 종료된 건의 재요청도 200이며, 처음 저장된 판단을 그대로 돌려준다."""
    row = _load_incident(db, incident_id)
    changed = workflows.resolve_incident(
        db, row.incident_id, payload.resolution, resolution_note=payload.resolution_note,
    )
    response = _to_detail(db, row)
    if changed:
        # 발행은 commit 이후에만 한다(realtime.py 규약). 재요청은 상태가 그대로라
        # 발행하지 않는다 — 받는 쪽이 바뀐 것 없는 재조회를 반복하게 된다
        request.app.state.realtime.publish(
            incident_event(
                WsEventType.INCIDENT_UPDATED,
                incident_id=row.incident_id,
                occurred_at=row.updated_at,
            )
        )
    return response
