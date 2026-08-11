# ==============================================================================
# [파일 설명]  담당: 박지현 (QA & Scenario)
# 4단계 Execution Guardrail 통과/차단 회귀 테스트입니다.
# ==============================================================================
import pytest


@pytest.mark.skip(reason="TODO: ai.guardrails 구현 후 작성")
def test_guardrail_blocks_unlisted_runbook():
    # 미등록 Runbook ID는 Action Whitelist 단계에서 차단되어야 함
    ...


@pytest.mark.skip(reason="TODO: ai.guardrails 구현 후 작성")
def test_guardrail_blocks_arn_scope_escalation():
    # DB에 없는 ARN 타겟은 ARN Match 단계에서 차단되어야 함
    ...
