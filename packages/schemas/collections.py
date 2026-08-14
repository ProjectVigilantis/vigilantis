# ==============================================================================
# [파일 설명]  담당: 안성일 (AI/Guardrail · Architect)
# 수집 실행(CollectionRun) 내부 상태 Enum의 단일 원천입니다. (Issue #48)
# Collector 실행 단위·DB CollectionRun.status·Asset Workflow가 소비하며,
# 공개 API의 CollectionStatus(api/assets.py — 조회 시점 표현)와는 별개 계약입니다.
#
#   - FAILED  : 이번 수집 실패 — 새 Rule 판정·AI 분석을 시작하지 않는다
#   - PARTIAL : 일부만 확보 — 필수 입력이 확보된 자산만 판정한다
# ==============================================================================

from __future__ import annotations

from enum import Enum, unique


@unique
class CollectionRunStatus(str, Enum):
    IN_PROGRESS = "IN_PROGRESS"
    SUCCESS = "SUCCESS"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"
