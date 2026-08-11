# ==============================================================================
# [파일 설명]  담당: 박지현 (QA & Scenario)
# 자산 자동 원복(Auto-Rollback) 회귀 테스트입니다.
# ==============================================================================
import pytest


@pytest.mark.skip(reason="TODO: services.aws.rollback 구현 후 작성")
def test_rollback_on_status_check_fail():
    # 다운사이징 후 Status Check 실패 시 이전 스펙 스냅샷으로 원복되어야 함
    ...
