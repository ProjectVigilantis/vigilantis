"""4단계 Guardrail 검증 계약 테스트 (Issue #55).

핵심: 검증 요청은 candidate_id XOR execution_id, 단계 결과는 고정 순서 4개,
FAIL이면 실패 단계 이전 PASS·이후 NOT_RUN.
"""

import pytest
from pydantic import ValidationError

from schemas.guardrails import (
    GUARDRAIL_STEP_ORDER,
    GuardrailDecision,
    GuardrailStep,
    GuardrailStepResult,
    GuardrailStepStatus,
    GuardrailValidationContext,
    GuardrailValidationRequest,
    GuardrailValidationResult,
)


def test_enums_match_contract_exactly():
    assert [s.value for s in GUARDRAIL_STEP_ORDER] == [
        "SCHEMA_CHECK", "ACTION_WHITELIST", "ARN_MATCH", "AWS_DRY_RUN",
    ]
    assert {c.value for c in GuardrailValidationContext} == {
        "AI_CANDIDATE", "AUTO_ISOLATION", "ROLLBACK_EXECUTION",
    }
    assert {d.value for d in GuardrailDecision} == {"PASS", "FAIL"}
    assert {s.value for s in GuardrailStepStatus} == {"PASS", "FAIL", "NOT_RUN"}


def make_request(**over):
    base = {
        "validation_context": "AI_CANDIDATE",
        "candidate_id": "cand-20260814-001",
        "execution_id": None,
        "command_payload": {"runbook_id": "RUNBOOK_NACL_ADD_DENY"},
    }
    base.update(over)
    return base


def test_request_candidate_context_valid():
    req = GuardrailValidationRequest.model_validate(make_request())
    assert req.candidate_id is not None


def test_request_execution_context_valid():
    req = GuardrailValidationRequest.model_validate(make_request(
        validation_context="ROLLBACK_EXECUTION",
        candidate_id=None, execution_id="exec-20260814-001",
    ))
    assert req.execution_id is not None


@pytest.mark.parametrize("over", [
    {"execution_id": "exec-1"},                            # 둘 다 참조
    {"candidate_id": None},                                # 둘 다 없음
    {"validation_context": "AUTO_ISOLATION"},              # 문맥↔참조 불일치(candidate만)
    {"validation_context": "AI_CANDIDATE",
     "candidate_id": None, "execution_id": "exec-1"},      # AI 검증인데 execution 참조
])
def test_request_violations(over):
    with pytest.raises(ValidationError):
        GuardrailValidationRequest.model_validate(make_request(**over))


def steps(*results, reasons=None):
    reasons = reasons or {}
    return [
        {"step": step.value, "result": result,
         "reason_code": reasons.get(step.value)}
        for step, result in zip(GUARDRAIL_STEP_ORDER, results)
    ]


def make_result(**over):
    base = {
        "result": "PASS",
        "failed_step": None,
        "steps": steps("PASS", "PASS", "PASS", "PASS"),
        "validated_at": "2026-08-14T09:00:00Z",
    }
    base.update(over)
    return base


def test_pass_result_roundtrip():
    r = GuardrailValidationResult.model_validate(make_result())
    assert GuardrailValidationResult.model_validate_json(r.model_dump_json()) == r


def test_fail_result_matches_ssot_example():
    # SSOT 예시 재현: ARN_MATCH 실패 → 이전 PASS·이후 NOT_RUN
    r = GuardrailValidationResult.model_validate(make_result(
        result="FAIL", failed_step="ARN_MATCH",
        steps=steps("PASS", "PASS", "FAIL", "NOT_RUN",
                    reasons={"ARN_MATCH": "TARGET_NOT_MANAGED"}),
    ))
    assert r.steps[2].reason_code == "TARGET_NOT_MANAGED"


@pytest.mark.parametrize("over", [
    {"failed_step": "ARN_MATCH"},                                    # PASS인데 실패 단계
    {"result": "FAIL"},                                              # FAIL인데 failed_step 없음
    {"result": "FAIL", "failed_step": "ARN_MATCH"},                  # FAIL인데 단계 전부 PASS
    {"result": "FAIL", "failed_step": "ARN_MATCH",
     "steps": steps("PASS", "PASS", "FAIL", "PASS")},                # 실패 이후가 NOT_RUN 아님
    {"result": "FAIL", "failed_step": "AWS_DRY_RUN",
     "steps": steps("PASS", "PASS", "FAIL", "NOT_RUN")},             # failed_step↔실제 단계 불일치
    {"steps": steps("PASS", "PASS", "PASS")},                        # 단계 3개
    {"steps": steps("PASS", "PASS", "PASS", "PASS")[::-1]},          # 순서 역전
])
def test_result_violations(over):
    with pytest.raises(ValidationError):
        GuardrailValidationResult.model_validate(make_result(**over))


def test_step_reason_only_on_fail():
    with pytest.raises(ValidationError):
        GuardrailStepResult.model_validate({
            "step": "SCHEMA_CHECK", "result": "PASS", "reason_code": "SOME_CODE",
        })


def test_verification_summary_only_on_dry_run():
    """검증 방식·한계 요약은 AWS 사전검증 단계에만 남긴다.
    다른 단계에 붙으면 어느 단계가 실제 AWS를 확인했는지 감사에서 구분되지 않는다."""
    ok = GuardrailStepResult(
        step=GuardrailStep.AWS_DRY_RUN,
        result=GuardrailStepStatus.PASS,
        verification_summary="DryRun=True 지원 API로 사전검증",
    )
    assert ok.verification_summary is not None

    with pytest.raises(ValidationError):
        GuardrailStepResult(
            step=GuardrailStep.ARN_MATCH,
            result=GuardrailStepStatus.PASS,
            verification_summary="여기 쓰면 안 됨",
        )
