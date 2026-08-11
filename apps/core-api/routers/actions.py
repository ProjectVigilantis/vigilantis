# ==============================================================================
# [파일 설명]
# POST /api/v1/actions/execute — 원클릭 조치 실행 라우터입니다. Idempotency Key로
# 중복 실행을 막고, 4단계 가드레일 통과 후 Boto3 조치를 실행합니다.
#
# [수행해야 할 작업]
# 1. 요청 검증: {incident_id, runbook_id, idempotency_key}
# 2. ai.guardrails 4단계 검증 통과 후 services.aws.executor 호출
# 3. 응답 상태 반환: SUCCESS / FAILED / IN_PROGRESS / ROLLBACK_INITIATED
# ==============================================================================
