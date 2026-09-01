# ==============================================================================
# [파일 설명]  담당: 박지현 (QA & Scenario)
# 자산 자동 원복(Auto-Rollback) 회귀 테스트입니다.
# ==============================================================================
import pytest


# 2/2 Status Check 판정 자체(#240)는 services/tests/test_status_check.py가 3분기
# 전수로, 판정 → 상태 확정 라우팅은 apps/core-api/tests/test_dispatcher.py가 본다.
# 여기 남은 것은 그 뒤 — 실제로 되돌리는 실행이다.
@pytest.mark.skip(reason="TODO: RUNBOOK_EC2_REVERT_SIZE 자동 발동(#241) 구현 후 작성")
def test_rollback_on_status_check_fail():
    # ROLLBACK_INITIATED 원본이 이전 스펙 스냅샷으로 원복되어야 함
    ...
