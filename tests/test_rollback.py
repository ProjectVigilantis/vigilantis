# ==============================================================================
# [파일 설명]  담당: 박지현 (QA & Scenario)
# 자산 자동 원복(Auto-Rollback) 회귀 테스트입니다.
#
# 2/2 Status Check 판정 자체(#240)는 services/tests/test_status_check.py가 3분기
# 전수로, 판정 → 상태 확정 라우팅은 apps/core-api/tests/test_dispatcher.py가 본다.
# 그 뒤의 **실제로 되돌리는 실행**은 #241에서 구현됐고, 회귀 테스트는 계층별로
# 아래 세 파일에 섰다. 여기 다시 쓰면 같은 것을 두 곳에서 보게 되므로 자리만 남긴다.
#
#   apps/core-api/services/tests/test_execute_revert_size.py
#     — 상태 대조 3분기(ADR-0008 §3-2)와 단계 기록 전수. AWS 불필요.
#   apps/core-api/services/tests/test_execute_revert_localstack.py
#     — 실물에서 타입이 백업 스펙 값으로 **되돌아가는가**. 호출 성공이 아니라 반영을 본다.
#   apps/core-api/tests/test_auto_rollback_workflow.py
#     — 발동과 확정: 원본당 1회, 원복 값의 출처, 자식↔원본↔Incident 상태 조합,
#       가드레일 거절이 자동 재시도로 이어지지 않는가(ADR-0004 정책 ④).
#
# [남은 작업] 전 구간 흐름(수집 → 판정 → 추천 → 승인 → 실행 → 실패 → 자동 원복)은
# test_e2e_scenario.py::test_t1_idle_ec2_downsize_and_auto_rollback_flow가 소유한다.
# 그 skip의 선행 조건(Status Check 실패 주입·자동 원복)은 #241로 해소됐다.
# ==============================================================================
