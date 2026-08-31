# ==============================================================================
# [파일 설명]
# 접수된 조치 실행을 AWS 실행으로 넘기고, 진행 중인 채로 남은 실행을 회수하는
# 모듈입니다. workflows.reserve_execution이 예약까지만 하므로, 예약과 실제 실행이
# 갈라지는 시점에 이 자리가 필요합니다. 필요한 조각은 이미 있습니다 — 비종료
# 상태 집합(packages/schemas/executions.py, Issue #55), 회수 스캔과 부분 인덱스
# (db/repositories/executions.py·db/models.py, Issue #60), 행 잠금(lock_execution
# — Issue #126이 관제자 복구 접수 경쟁을 막으려 만든 것을 재사용).
#
# 계층 경계 — 비종료 실행 회수 스캔은 이 모듈 하나가 소유합니다. 스캔이 둘이면
# 같은 실행 행을 두 주체가 만집니다. 개별 실행의 Status Check 확인과 자동 원복
# 판단은 services/aws/rollback.py 몫이라 여기서 다시 돌지 않습니다(executor.py
# [남은 작업] 2번의 "트리거 판단·감시는 rollback.py 담당"이 그것입니다).
# 실제 일은 아래로 내려보냅니다.
#   dispatcher → workflows.py              상태 전이·트랜잭션
#              → services/aws/executor.py  조치 실행
#              → services/aws/rollback.py  자동 원복 동작
# workflows.store_instance_spec_backup()과 services/aws/backup.py가 나눈 것과 같은
# 경계입니다 — AWS 호출은 services/aws/**, 커밋 순서는 workflows.py.
#
# [수행해야 할 작업]
# 1. 예약된 실행을 executor로 넘기고 결과를 종료 상태로 기록
# 2. 비종료 실행 회수 스캔 — list_non_terminal()
#    (부분 인덱스 ix_action_executions_non_terminal 대상)
# 3. 회수 대상 선점 — lock_execution() 행 잠금 후 상태 재확인(동시 회수 방지)
# 4. 진행하던 프로세스가 사라진 실행을 종료 상태로 정리
# 5. 실행 종료와 Incident 전이를 한 트랜잭션에 — ACTION_IN_PROGRESS인데 진행 중
#    실행이 없으면 상세 조회가 500입니다(schemas/api/incidents.py 응답 계약).
#    실행 결과별 목적 상태는 별도 결정이 필요합니다 — 관제자 종료 경로(#199)는
#    ACTION_IN_PROGRESS를 409로 거절하므로 이 매핑을 대신 정해 주지 않습니다.
# 6. 상태 전이 commit 이후 EXECUTION_UPDATED·INCIDENT_UPDATED 발행(realtime.py 규약)
#
# 회수 주기·프로세스 소실 판정 기준·기동 worker 개수는 미정입니다. 마지막 항목은
# ADR-0005가 다중 worker·replica 실행 토폴로지를 별도 결정 대상으로 남긴 것입니다.
# ==============================================================================
