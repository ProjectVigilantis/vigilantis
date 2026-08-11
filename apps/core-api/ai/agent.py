# ==============================================================================
# [파일 설명]  담당: 안성일 (AI / Guardrail)
# OpenAI GPT-4o 기반 추론기입니다. CoT 3줄 요약과 추천 Runbook ID를 Pydantic
# Structured Output으로 산출합니다. (LangGraph 도입 여부는 Post-MVP 미확정)
#
# [수행해야 할 작업]
# 1. GPT-4o 호출 및 Pydantic Structured Output 파싱
# 2. Evidence ID 대조 기반 Decision Trace 생성
# 3. 추천 Runbook ID + 파라미터를 guardrails 단계로 전달
# ==============================================================================
