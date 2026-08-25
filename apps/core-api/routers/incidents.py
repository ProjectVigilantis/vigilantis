# ==============================================================================
# [파일 설명]
# GET /api/v1/incidents(목록)·GET /api/v1/incidents/{id}(상세) 조회 라우터입니다.
# (Issue #68)
#
#   - 응답은 공개 계약 schemas.api.incidents로만 직렬화한다. 목록은 상세의
#     부분집합 10필드, created_at 내림차순 전체 반환 — SSOT §API 계약.
#   - SQL은 db.repositories 경유 — 라우터는 응답 조립만 한다.
#   - recommendations는 Guardrail PASS 제안(EXECUTABLE 후보)만 담는다.
#   - available_recovery_runbook_ids는 실행 이력에서 파생한다 — 짝(ADR-0004)이
#     있고, 원본이 복구 가능 상태이며, 아직 복구가 접수되지 않은 실행만 노출한다.
#     (Issue #126)
# ==============================================================================

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from schemas.api.errors import ErrorCode
from schemas.api.incidents import (
    IncidentCategory,
    IncidentListItem,
    IncidentResponse,
    IncidentsResponse,
    IncidentStatus,
)
from schemas.candidates import CandidateStatus
from schemas.executions import EXECUTION_RECOVERABLE_STATUSES
from schemas.runbooks import ROLLBACK_RUNBOOK_BY_MAIN_ID

from db import models
from db.repositories import executions as executions_repo
from db.repositories import incidents as incidents_repo
from db.session import get_db
from exceptions import ApiError
from identifiers import canonical_id

router = APIRouter(prefix="/api/v1", tags=["incidents"])


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


def _to_list_item(row: models.Incident) -> IncidentListItem:
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
    return IncidentsResponse(items=[_to_list_item(row) for row in rows])


@router.get("/incidents/{incident_id}", response_model=IncidentResponse)
def get_incident(incident_id: str, db: Session = Depends(get_db)) -> IncidentResponse:
    """형식이 어긋난 식별자도 404다 — 계약이 UUID를 요구하지 않으므로 계약 위반이
    아니라 없는 인시던트로 본다. 조회 전 변환은 DB 캐스트 오류(500)를 막는다."""
    stored_id = canonical_id(incident_id)
    if stored_id is None:
        raise ApiError(ErrorCode.INCIDENT_NOT_FOUND)
    row = incidents_repo.get_incident(db, stored_id)
    if row is None:
        raise ApiError(ErrorCode.INCIDENT_NOT_FOUND)

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
            "summary_lines": row.summary_lines,
            "evidence_ids": evidence_ids,
            "recommendations": recommendations,
            "executions": executions,
            "created_at": row.created_at,
            "updated_at": row.updated_at,
        }
    )
