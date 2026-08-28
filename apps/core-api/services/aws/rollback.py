# ==============================================================================
# [파일 설명]  담당: 김세혁 (Infra & DevSecOps)
# 자산 자동 원복(Auto-Rollback) 동작입니다. get_waiter로 개별 실행의 Status Check
# 결과를 보고, 기동 실패·타임아웃이면 BackupRecord의 이전 스펙으로 되돌립니다.
# 비종료 실행 회수 스캔은 dispatcher.py가, ActionExecution 상태 전이·커밋은
# workflows.py가 소유합니다 — services/aws/backup.py와
# workflows.store_instance_spec_backup()이 나눈 것과 같은 경계입니다.
#
# [수행해야 할 작업]
# 1. get_waiter('instance_status_ok') 기반 2/2 Status Check 확인 및 타임아웃 판정
# 2. 실패 시 BackupRecord 이전 스펙으로 자동 원복
# 3. 원복 결과를 반환 — ActionExecution 상태 기록·알림은 호출부 소유
# ==============================================================================
