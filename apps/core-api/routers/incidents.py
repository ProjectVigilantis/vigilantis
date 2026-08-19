# ==============================================================================
# [파일 설명]
# GET /api/v1/incidents(목록)·GET /api/v1/incidents/{id}(상세) 조회 라우터입니다.
# (Issue #68)
#
#   - 응답은 공개 계약 schemas.api.incidents로만 직렬화한다. 목록은 상세의
#     부분집합 10필드, created_at 내림차순 전체 반환 — SSOT §API 계약.
#   - SQL은 db.repositories 경유 — 라우터는 응답 조립만 한다.
#   - recommendations는 Guardrail PASS 제안(EXECUTABLE 후보)만 담는다.
# ==============================================================================

from __future__ import annotations

import uuid
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

from db import models
from db.repositories import executions as executions_repo
from db.repositories import incidents as incidents_repo
from db.session import get_db
from exceptions import ApiError

router = APIRouter(prefix="/api/v1", tags=["incidents"])


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
def get_incident(incident_id: uuid.UUID, db: Session = Depends(get_db)) -> IncidentResponse:
    """경로 인자를 UUID로 선언해 형식 오류는 422 봉투로 떨어진다(DB 오류 방지)."""
    row = incidents_repo.get_incident(db, str(incident_id))
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
    executions = [
        {
            "execution_id": execution.execution_id,
            "runbook_id": execution.runbook_id,
            "status": execution.status,
            # 복구 가능 목록은 Backup 결속 등 실행 흐름 데이터로 구성한다 —
            # 그 구현은 조치 실행 작업으로 유예, 조회 단계는 빈 목록(Issue #68 범위 밖)
            "available_recovery_runbook_ids": [],
            "updated_at": execution.updated_at,
        }
        for execution in executions_repo.list_by_incident(db, row.incident_id)
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
