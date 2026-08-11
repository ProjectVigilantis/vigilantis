# ==============================================================================
# [파일 설명]  담당: 안성일 (AI / Guardrail)
# 4단계 Execution Guardrail입니다. LLM 출력을 순서대로 검증해 프롬프트 인젝션/RCE를
# 차단합니다: Schema → Action Whitelist → ARN Match → AWS Dry-Run.
#
# [수행해야 할 작업]
# 1. Schema Check: Pydantic 규격 검증
# 2. Action Whitelist: whitelist.ALLOWED_RUNBOOKS 대조
# 3. ARN Match: DB 수집 ARN과 대조(Scope Escalation 차단)
# 4. AWS Dry-Run: Boto3 DryRun=True 사전 검증
# ==============================================================================
