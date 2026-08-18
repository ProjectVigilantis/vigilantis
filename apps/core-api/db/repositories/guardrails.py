# ==============================================================================
# [파일 설명]  담당: 안성일 (AI/Guardrail · Architect)
# Guardrail 검증 결과 저장소 — GuardrailEvaluation. (Issue #60)
# 검증 수행·판정은 ai/guardrails 계층 몫이고 여기는 결과 보존·조회만 한다.
# ==============================================================================

from __future__ import annotations

from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from schemas.guardrails import GuardrailValidationContext, GuardrailValidationResult

from .. import models


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
