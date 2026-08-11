# ==============================================================================
# [파일 설명]  담당: 박지현 (QA & Scenario)
# End-to-End 시연 시나리오 테스트입니다.
# 감지 → Rule Engine → AI CoT → 4단계 가드레일 → 원클릭 실행 → (실패 시) 자동 원복.
# ==============================================================================
import pytest


@pytest.mark.skip(reason="TODO: E2E 파이프라인 연동 후 작성")
def test_idle_ec2_downsize_flow():
    # Golden Dataset의 Idle EC2 케이스가 전 구간을 통과하는지 검증
    ...


@pytest.mark.skip(reason="TODO: E2E 파이프라인 연동 후 작성")
def test_open_ssh_ip_block_flow():
    # 0.0.0.0/0 SSH 개방 케이스가 선제 차단 → 원클릭 해제까지 동작하는지 검증
    ...
