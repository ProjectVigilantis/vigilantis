# ==============================================================================
# [파일 설명]
# Guardrail 검증 결과 저장소 — GuardrailEvaluation. (Issue #60)
# 검증 수행·판정은 ai/guardrails 계층 몫이고 여기는 결과 보존·조회만 한다.
# ==============================================================================

from __future__ import annotations

from collections.abc import Sequence
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from schemas.guardrails import GuardrailValidationContext, GuardrailValidationResult
from schemas.runbooks import RunbookId

from .. import models


def candidate_evaluations_by_incident(
    db: Session, incident_ids: Sequence[str],
) -> dict[str, list[tuple[models.RunbookCandidate, models.GuardrailEvaluation | None]]]:
    """현재 후보 상태에 무관한 원래 분석 평가. 목록 전체를 한 쿼리로 읽는다.

    현재 SecOps의 NACL_RESTORE는 차단 뒤 서버가 만든 해제 제안이다. AI_CANDIDATE
    문맥을 공유하지만 최초 분석에는 포함하지 않는다(별도 문맥 분리는 #329 후속).
    """
    if not incident_ids:
        return {}
    rows = db.execute(
        select(models.RunbookCandidate, models.GuardrailEvaluation)
        .outerjoin(models.GuardrailEvaluation, (
            models.GuardrailEvaluation.candidate_id == models.RunbookCandidate.candidate_id
        ) & (
            models.GuardrailEvaluation.validation_context
            == GuardrailValidationContext.AI_CANDIDATE
        ))
        .where(
            models.RunbookCandidate.incident_id.in_(incident_ids),
            models.RunbookCandidate.runbook_id != RunbookId.RUNBOOK_NACL_RESTORE,
        )
        .order_by(
            models.RunbookCandidate.created_at,
            models.RunbookCandidate.candidate_id,
            models.GuardrailEvaluation.validated_at.desc(),
            models.GuardrailEvaluation.guardrail_evaluation_id.desc(),
        )
    )
    result: dict[str, list] = {}
    seen: set[str] = set()
    for candidate, evaluation in rows:
        if candidate.candidate_id not in seen:
            result.setdefault(candidate.incident_id, []).append((candidate, evaluation))
            seen.add(candidate.candidate_id)
    return result


def add_evaluation(
    db: Session,
    *,
    validation_context: GuardrailValidationContext,
    result: GuardrailValidationResult,
    candidate_id: Optional[str] = None,
    execution_id: Optional[str] = None,
    validated_command: Optional[dict] = None,
) -> models.GuardrailEvaluation:
    """candidate_id XOR execution_id는 DB CheckConstraint가 flush 시점에 강제한다."""
    row = models.GuardrailEvaluation(
        validation_context=validation_context,
        candidate_id=candidate_id,
        execution_id=execution_id,
        result=result.result,
        failed_step=result.failed_step,
        steps=[s.model_dump(mode="json") for s in result.steps],
        validated_command=validated_command,
        validated_at=result.validated_at,
    )
    db.add(row)
    db.flush()
    return row


def latest_for_candidate(
    db: Session, candidate_id: str
) -> Optional[models.GuardrailEvaluation]:
    return db.execute(
        select(models.GuardrailEvaluation)
        .where(models.GuardrailEvaluation.candidate_id == candidate_id)
        .order_by(models.GuardrailEvaluation.validated_at.desc())
        .limit(1)
    ).scalar_one_or_none()


def latest_for_execution(
    db: Session, execution_id: str
) -> Optional[models.GuardrailEvaluation]:
    return db.execute(
        select(models.GuardrailEvaluation)
        .where(models.GuardrailEvaluation.execution_id == execution_id)
        .order_by(models.GuardrailEvaluation.validated_at.desc())
        .limit(1)
    ).scalar_one_or_none()
