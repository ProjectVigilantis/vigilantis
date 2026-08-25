"""4단계 Guardrail 검증 계약 테스트 (Issue #55).

핵심: 검증 요청은 candidate_id XOR execution_id, 단계 결과는 고정 순서 4개,
FAIL이면 실패 단계 이전 PASS·이후 NOT_RUN.
"""

import pytest
from pydantic import ValidationError

from schemas.guardrails import (
    GUARDRAIL_STEP_ORDER,
    STEP_REASON_CODES,
    ActionWhitelistReasonCode,
    ArnMatchReasonCode,
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
                    reasons={"ARN_MATCH": "ARN_TARGET_NOT_MANAGED"}),
    ))
    assert r.steps[2].reason_code is ArnMatchReasonCode.ARN_TARGET_NOT_MANAGED


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
    # 정의된 코드를 쓴다 — 미정의 문자열이면 Enum 거절과 구분되지 않아
    # "PASS에는 사유를 남기지 않는다"는 규칙을 실제로 검증하지 못한다.
    with pytest.raises(ValidationError, match="FAIL 단계에만"):
        GuardrailStepResult.model_validate({
            "step": "SCHEMA_CHECK", "result": "PASS",
            "reason_code": "SCHEMA_INVALID_PAYLOAD",
        })


# ---------------------------------------------------------------- 사유 코드 어휘
# 네 단계의 코드가 한 곳(packages/schemas/guardrails.py)에 정의되고, 단계와 맞지 않는
# 코드는 계약이 거절한다. (#125)


def test_reason_codes_cover_four_steps_with_step_prefix():
    """단계별 목록이 4단계 전부를 덮고, 접두가 단계를 표시한다.

    접두가 흔들리면 거절 기록만 보고 어느 단계가 막았는지 역산할 수 없다.
    """
    assert set(STEP_REASON_CODES) == set(GUARDRAIL_STEP_ORDER)

    prefixes = {
        GuardrailStep.SCHEMA_CHECK: "SCHEMA_",
        GuardrailStep.ACTION_WHITELIST: "WHITELIST_",
        GuardrailStep.ARN_MATCH: "ARN_",
        GuardrailStep.AWS_DRY_RUN: "PRECHECK_",
    }
    for step, enum_cls in STEP_REASON_CODES.items():
        assert list(enum_cls), f"{step.value} 단계에 사유 코드가 없다"
        for code in enum_cls:
            assert code.value.startswith(prefixes[step]), (
                f"{code.value}는 {step.value} 접두 {prefixes[step]}를 따라야 한다"
            )


def test_reason_code_values_are_globally_unique():
    """단계가 달라도 값은 겹치지 않는다 — 겹치면 역산이 성립하지 않는다."""
    values = [c.value for cls in STEP_REASON_CODES.values() for c in cls]
    assert len(values) == len(set(values))


def test_precheck_reason_code_is_the_shared_definition():
    """④는 별도 정의가 아니라 공용 목록의 ④ 자리다 — executor가 쓰는 import 경로도 같다."""
    from schemas.precheck import PrecheckReasonCode as ReExported

    assert STEP_REASON_CODES[GuardrailStep.AWS_DRY_RUN] is ReExported


@pytest.mark.parametrize("step, code", [
    ("ACTION_WHITELIST", "PRECHECK_AWS_ERROR"),        # ②에 ④ 코드
    ("SCHEMA_CHECK", "WHITELIST_UNKNOWN_RUNBOOK"),     # ①에 ② 코드
    ("AWS_DRY_RUN", "ARN_TARGET_NOT_MANAGED"),         # ④에 ③ 코드
    ("ARN_MATCH", "SCHEMA_INVALID_PAYLOAD"),           # ③에 ① 코드
])
def test_reason_code_must_belong_to_its_step(step, code):
    with pytest.raises(ValidationError, match="reason_code는"):
        GuardrailStepResult.model_validate({
            "step": step, "result": "FAIL", "reason_code": code,
        })


def test_unknown_reason_code_is_rejected():
    """목록에 없는 값은 계약이 받지 않는다 — 앱이 임의 문자열을 남길 수 없다."""
    with pytest.raises(ValidationError):
        GuardrailStepResult.model_validate({
            "step": "SCHEMA_CHECK", "result": "FAIL", "reason_code": "SOME_CODE",
        })


def test_reason_code_serializes_to_plain_string():
    """DB 저장 포맷 불변 — GuardrailEvaluation.steps는 이 형태로 적재된다
    (apps/core-api/db/repositories/guardrails.py). Enum 이름이 새면 기존 레코드와
    다른 값이 남는다."""
    dumped = GuardrailStepResult(
        step=GuardrailStep.ACTION_WHITELIST,
        result=GuardrailStepStatus.FAIL,
        reason_code=ActionWhitelistReasonCode.WHITELIST_NOT_AI_RECOMMENDABLE,
    ).model_dump(mode="json")

    assert dumped["reason_code"] == "WHITELIST_NOT_AI_RECOMMENDABLE"


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
