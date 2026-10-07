"""분석 결과 조회. 종료·실행 때문에 바뀌는 후보 상태에서 원인을 추측하지 않는다.

공개 응답(analysis_result)에는 SecOps만 싣는다(api/incidents.py 계약). 판정 자체는
도메인과 무관해 분석 종결 로그(workflows.record_agent_analysis)도 같은 판정을 쓴다(#424).
"""

from collections.abc import Sequence

from sqlalchemy.orm import Session

from schemas.api.analysis import AnalysisResult, AnalysisResultStatus, GuardrailRejection
from schemas.api.incidents import IncidentCategory
from schemas.guardrails import STEP_REASON_CODES, GuardrailDecision
from schemas.incidents import AgentInvocationStatus

from db import models
from db.repositories import guardrails as guardrails_repo


def _rejection(
    candidate: models.RunbookCandidate, evaluation: models.GuardrailEvaluation,
) -> GuardrailRejection:
    # 공개된 코드만 내보낸다. 저장된 예외·검증 설명·명령 원문은 직렬화하지 않는다.
    code = None
    for step in evaluation.steps:
        if step.get("step") == evaluation.failed_step.value:
            try:
                code = STEP_REASON_CODES[evaluation.failed_step](step.get("reason_code"))
            except ValueError:
                pass
            break
    return GuardrailRejection(
        runbook_id=candidate.runbook_id, failed_step=evaluation.failed_step, reason_code=code,
    )


def analysis_result_from(
    invocation_status: AgentInvocationStatus,
    candidates: Sequence[tuple[models.RunbookCandidate, models.GuardrailEvaluation | None]],
) -> AnalysisResult:
    """분석 호출 상태와 저장된 후보별 원래 평가 → 분석이 어떻게 끝났는지."""
    if invocation_status is not AgentInvocationStatus.SUCCEEDED:
        return AnalysisResult(status=AnalysisResultStatus(invocation_status.value))
    rejected = [
        _rejection(candidate, evaluation) for candidate, evaluation in candidates
        if evaluation is not None and evaluation.result is GuardrailDecision.FAIL
        and evaluation.failed_step is not None
    ]
    if any(
        evaluation is not None and evaluation.result is GuardrailDecision.PASS
        for _, evaluation in candidates
    ):
        status = AnalysisResultStatus.PROPOSALS_GENERATED
    elif candidates and len(rejected) == len(candidates):
        status = AnalysisResultStatus.GUARDRAIL_REJECTED
    else:
        status = AnalysisResultStatus.UNAVAILABLE
    return AnalysisResult(status=status, guardrail_rejections=rejected)


def load_analysis_results(
    db: Session, incidents: Sequence[models.Incident],
) -> dict[str, AnalysisResult | None]:
    evaluations = guardrails_repo.candidate_evaluations_by_incident(db, [
        row.incident_id for row in incidents
        if row.category is IncidentCategory.SECOPS
        and row.agent_invocation_status is AgentInvocationStatus.SUCCEEDED
    ])
    return {
        row.incident_id: (
            analysis_result_from(row.agent_invocation_status, evaluations.get(row.incident_id, []))
            if row.category is IncidentCategory.SECOPS
            else None
        )
        for row in incidents
    }
