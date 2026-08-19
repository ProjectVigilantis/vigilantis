# ==============================================================================
# [파일 설명]
# ORM 행 ↔ 내부 Pydantic 계약(#48·#49·#55) 변환입니다. (Issue #60)
#
#   - 변환 대상은 계약이 이미 확정된 쌍만이다. 계약이 없는 엔티티(Asset·Incident
#     집계 등)는 억지로 새 계약을 만들지 않고 소비 계층이 생길 때 추가한다.
#   - 외부 API DTO 변환은 Router 몫이다 — 여기서 다루지 않는다.
#   - to_*: ORM → 계약(조회 방향) / new_*: 계약 → ORM 인스턴스(저장 방향).
#     검증은 Pydantic 계약이 수행하고, 이 모듈은 필드 이동만 한다.
# ==============================================================================

from __future__ import annotations

from schemas.candidates import RunbookCandidateData
from schemas.events import NormalizedThreatEvent
from schemas.evidence import EvidenceItem
from schemas.executions import ExecutionStepResult
from schemas.guardrails import GuardrailValidationResult
from schemas.incidents import AgentWaitSchedule
from schemas.rules import RuleEvaluationResult

from . import models

# --- 위협 이벤트 ---------------------------------------------------------------


def to_threat_event(row: models.ThreatEvent) -> NormalizedThreatEvent:
    return NormalizedThreatEvent.model_validate(
        {
            "threat_event_id": row.threat_event_id,
            "source_event_id": row.source_event_id,
            "event_type": row.event_type,
            "target_arn": row.target_arn,
            "occurred_at": row.occurred_at,
            "payload": row.payload,
            "deduplication_key": row.deduplication_key,
            "collected_at": row.collected_at,
        }
    )


def new_threat_event(contract: NormalizedThreatEvent) -> models.ThreatEvent:
    return models.ThreatEvent(
        threat_event_id=contract.threat_event_id,
        source_event_id=contract.source_event_id,
        event_type=contract.event_type,
        target_arn=contract.target_arn,
        payload=contract.payload.model_dump(mode="json"),
        deduplication_key=contract.deduplication_key,
        occurred_at=contract.occurred_at,
        collected_at=contract.collected_at,
    )


# --- Rule 판정 -----------------------------------------------------------------


def to_rule_evaluation_result(
    row: models.RuleEvaluation, asset_arn: str, collection_run_id: str
) -> RuleEvaluationResult:
    """asset_arn·collection_run_id는 계약 필드 — 호출부가 조인으로 확보해 전달한다."""
    return RuleEvaluationResult.model_validate(
        {
            "asset_arn": asset_arn,
            "collection_run_id": collection_run_id,
            "evaluation_status": row.evaluation_status,
            "verdict": row.verdict,
            "health_score": row.health_score,
            "skip_reason_code": row.skip_reason_code,
            "reason": row.reason,
            "evaluated_at": row.evaluated_at,
        }
    )


def new_rule_evaluation(
    contract: RuleEvaluationResult, asset_id: str
) -> models.RuleEvaluation:
    """계약의 asset_arn은 호출부가 asset_id로 해석(resolve)해 전달한다."""
    return models.RuleEvaluation(
        asset_id=asset_id,
        collection_run_id=contract.collection_run_id,
        evaluation_status=contract.evaluation_status.value,
        verdict=contract.verdict.value if contract.verdict else None,
        health_score=contract.health_score,
        skip_reason_code=(
            contract.skip_reason_code.value if contract.skip_reason_code else None
        ),
        reason=contract.reason,
        evaluated_at=contract.evaluated_at,
    )


# --- Evidence ------------------------------------------------------------------


def to_evidence_item(row: models.Evidence) -> EvidenceItem:
    return EvidenceItem.model_validate(
        {
            "evidence_id": row.evidence_id,
            "incident_id": row.incident_id,
            "evidence_type": row.evidence_type,
            "source_type": row.source_type,
            "source_id": row.source_id,
            "content": row.content,
            "occurred_at": row.occurred_at,
            "collected_at": row.collected_at,
        }
    )


def new_evidence(contract: EvidenceItem) -> models.Evidence:
    return models.Evidence(
        evidence_id=contract.evidence_id,
        incident_id=contract.incident_id,
        evidence_type=contract.evidence_type,
        source_type=contract.source_type,
        source_id=contract.source_id,
        content=contract.content.model_dump(mode="json"),
        occurred_at=contract.occurred_at,
        collected_at=contract.collected_at,
    )


# --- AI 제안 후보 --------------------------------------------------------------


def to_candidate_data(row: models.RunbookCandidate) -> RunbookCandidateData:
    return RunbookCandidateData.model_validate(
        {
            "candidate_id": row.candidate_id,
            "incident_id": row.incident_id,
            "runbook_id": row.runbook_id,
            "target_arn": row.target_arn,
            "display_parameters": row.display_parameters,
            "evidence_ids": row.evidence_ids,
            "status": row.status,
        }
    )


def new_candidate(contract: RunbookCandidateData) -> models.RunbookCandidate:
    return models.RunbookCandidate(
        candidate_id=contract.candidate_id,
        incident_id=contract.incident_id,
        runbook_id=contract.runbook_id,
        target_arn=contract.target_arn,
        display_parameters=dict(contract.display_parameters),
        evidence_ids=list(contract.evidence_ids),
        status=contract.status,
    )


# --- 승인 대기 일정 ------------------------------------------------------------


def to_agent_wait_schedule(row: models.Incident) -> AgentWaitSchedule | None:
    """대기 필드가 둘 다 있을 때만 계약으로 변환한다(없으면 대기 미시작)."""
    if row.agent_wait_started_at is None or row.response_deadline_at is None:
        return None
    return AgentWaitSchedule.model_validate(
        {
            "incident_id": row.incident_id,
            "started_at": row.agent_wait_started_at,
            "response_deadline_at": row.response_deadline_at,
        }
    )


# --- Guardrail 검증 결과 -------------------------------------------------------


def to_guardrail_result(row: models.GuardrailEvaluation) -> GuardrailValidationResult:
    return GuardrailValidationResult.model_validate(
        {
            "result": row.result,
            "failed_step": row.failed_step,
            "steps": row.steps,
            "validated_at": row.validated_at,
        }
    )


# --- 실행 단계 -----------------------------------------------------------------


def to_step_result(row: models.ExecutionStep) -> ExecutionStepResult:
    return ExecutionStepResult.model_validate(
        {
            "sequence": row.sequence,
            "affected_arn": row.affected_arn,
            "step_type": row.step_type,
            "aws_operation": row.aws_operation,
            "status": row.status,
            "effect": row.effect,
            "aws_request_id": row.aws_request_id,
            "result_summary": row.result_summary,
            "error_summary": row.error_summary,
            "occurred_at": row.occurred_at,
        }
    )


def new_execution_step(
    contract: ExecutionStepResult, execution_id: str
) -> models.ExecutionStep:
    return models.ExecutionStep(
        execution_id=execution_id,
        sequence=contract.sequence,
        affected_arn=contract.affected_arn,
        step_type=contract.step_type,
        aws_operation=contract.aws_operation,
        status=contract.status,
        effect=contract.effect,
        aws_request_id=contract.aws_request_id,
        result_summary=contract.result_summary,
        error_summary=contract.error_summary,
        occurred_at=contract.occurred_at,
    )
