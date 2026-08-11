# ==============================================================================
# [파일 설명]
# GET /api/v1/incidents/{id} — AI의 CoT 3줄 요약, Evidence ID, 추천 Runbook ID를
# 담은 위협/최적화 진단 보고서를 조회하는 라우터입니다.
#
# [수행해야 할 작업]
# 1. GET /incidents/{id} 엔드포인트 구현
# 2. packages/schemas의 guardrails/runbooks 모델로 응답 직렬화
# 3. Evidence ID 기반 Decision Trace 및 추천 Runbook 반환
# ==============================================================================
