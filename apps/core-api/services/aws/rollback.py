# ==============================================================================
# [파일 설명]  담당: 김세혁 (Infra & DevSecOps)
# 자산 자동 원복(Auto-Rollback) 엔진입니다. get_waiter로 Status Check를 감시하고,
# 기동 실패/타임아웃 시 이전 스펙 스냅샷으로 자동 원복합니다.
#
# [수행해야 할 작업]
# 1. get_waiter('instance_status_ok') 기반 2/2 Status Check 감시 및 타임아웃 처리
# 2. 실패 시 SpecSnapshot 이전 스펙으로 자동 원복
# 3. ActionLog(status=ROLLBACK_INITIATED) 기록 및 알림
# ==============================================================================
