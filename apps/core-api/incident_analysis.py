"""분석 결과 조회. 종료·실행 때문에 바뀌는 후보 상태에서 원인을 추측하지 않는다.

공개 응답(analysis_result)에는 SecOps만 싣는다(api/incidents.py 계약). 판정 자체는
도메인과 무관해 분석 종결 로그(workflows.record_agent_analysis)도 같은 판정을 쓴다(#424).
FAILED 호출은 저장된 사유 코드로 가른다 — 제공할 자동 조치가 없어 닫힌 건(빈 메뉴·IPv6
SSH 출발지)은 NO_ACTION_AVAILABLE, 그 밖은 FAILED이며 둘 다 사유를 싣는다(#434).
"""

from collections.abc import Sequence

from sqlalchemy.orm import Session

from schemas.api.analysis import (
    NO_ACTION_FAILURE_REASONS,
    AnalysisFailureReason,
    AnalysisResult,
    AnalysisResultStatus,
    GuardrailRejection,
)
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


def _stored_failure_reason(value: str | None) -> AnalysisFailureReason | None:
    # 칸은 DB에서 자유 문자열이다(어휘가 분석 단계와 함께 는다). 이 판이 모르는 코드는
    # 공개하지 않는다 — 목록 조회 전체가 한 행의 값 때문에 실패하지 않게 한다.
    try:
        return None if value is None else AnalysisFailureReason(value)
    except ValueError:
        return None


def analysis_result_from(
    invocation_status: AgentInvocationStatus,
    candidates: Sequence[tuple[models.RunbookCandidate, models.GuardrailEvaluation | None]],
    *,
    failure_reason: AnalysisFailureReason | None,
) -> AnalysisResult:
    """분석 호출 상태와 저장된 후보별 원래 평가·실패 사유 → 분석이 어떻게 끝났는지."""
    if invocation_status is AgentInvocationStatus.FAILED:
        status = (AnalysisResultStatus.NO_ACTION_AVAILABLE
                  if failure_reason in NO_ACTION_FAILURE_REASONS else AnalysisResultStatus.FAILED)
        return AnalysisResult(status=status, failure_reason=failure_reason)
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
            analysis_result_from(
                row.agent_invocation_status, evaluations.get(row.incident_id, []),
                failure_reason=_stored_failure_reason(row.agent_failure_reason),
            )
            if row.category is IncidentCategory.SECOPS
            else None
        )
        for row in incidents
    }
