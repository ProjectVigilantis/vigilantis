# ==============================================================================
# [파일 설명]
# PostgreSQL ORM 모델 정의입니다. 자산/인시던트/조치로그/스펙 스냅샷/스킵 사유를
# 저장합니다. (API 응답 DTO는 packages/schemas의 Pydantic 모델을 사용)
#
# [수행해야 할 작업]
# 1. Asset(arn, asset_type=EC2|SG, state, health_score, skip_reason)
# 2. Incident(cot_summary, evidence_id, recommended_runbook_id)
# 3. ActionLog(incident_id, runbook_id, idempotency_key, status)
# 4. SpecSnapshot(arn, spec_json) — 다운사이징 백업/자동 원복용
# ==============================================================================
