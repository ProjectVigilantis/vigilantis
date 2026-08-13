# ==============================================================================
# [파일 설명]  담당: 안성일 / 김세혁
# 가드레일 2단계 Action Whitelist — 실행을 허용하는 Runbook ID의 확정 목록입니다.
# 목록은 확정 10종 = 본편 7종(ADR-0002) + 롤백 3종(ADR-0004)이며,
# 여기 없는 ID는 실행 경로에 진입할 수 없습니다.
#
# 롤백 3종은 실행은 허용하되 AI 추천 대상이 아닙니다(ai_recommendable: false,
# ADR-0004 롤백 공통 정책 ②) — 트리거는 시스템·관제자만 가능합니다.
#
# Runbook별 필수 파라미터·허용 AWS 작업·호출 순서는 런북 명세서
# (vigilantis-docs/런북 명세서.md — 저장소 밖 확정본, ADR-0002 참조) 대조 후
# 별도 계약으로 추가합니다. 이 파일은 ID 허용 여부만 판정합니다.
# ==============================================================================

from __future__ import annotations

from enum import Enum, unique


@unique
class RunbookId(str, Enum):
    """확정 Action Whitelist 10종 = 본편 7종(ADR-0002) + 롤백 3종(ADR-0004)."""

    # 본편 7종 (ADR-0002)
    RUNBOOK_EC2_ISOLATE = "RUNBOOK_EC2_ISOLATE"
    RUNBOOK_NACL_ADD_DENY = "RUNBOOK_NACL_ADD_DENY"
    RUNBOOK_NACL_RESTORE = "RUNBOOK_NACL_RESTORE"
    RUNBOOK_SG_DELETE_ISOLATED = "RUNBOOK_SG_DELETE_ISOLATED"
    RUNBOOK_EC2_RIGHTSIZING = "RUNBOOK_EC2_RIGHTSIZING"
    RUNBOOK_EC2_ENABLE_AUTOSCALING = "RUNBOOK_EC2_ENABLE_AUTOSCALING"
    RUNBOOK_EBS_DELETE_UNATTACHED = "RUNBOOK_EBS_DELETE_UNATTACHED"
    # 롤백 3종 (ADR-0004) — 실행 허용, AI 추천 불가
    RUNBOOK_EC2_UNISOLATE = "RUNBOOK_EC2_UNISOLATE"
    RUNBOOK_SG_RECREATE = "RUNBOOK_SG_RECREATE"
    RUNBOOK_EC2_REVERT_SIZE = "RUNBOOK_EC2_REVERT_SIZE"


ALLOWED_RUNBOOK_IDS: frozenset[str] = frozenset(item.value for item in RunbookId)

ROLLBACK_RUNBOOK_IDS: frozenset[str] = frozenset({
    RunbookId.RUNBOOK_EC2_UNISOLATE.value,
    RunbookId.RUNBOOK_SG_RECREATE.value,
    RunbookId.RUNBOOK_EC2_REVERT_SIZE.value,
})

# ADR-0004 정책 ②: 롤백은 AI 추천 목록에서 제외 — 트리거는 시스템·관제자만
AI_RECOMMENDABLE_RUNBOOK_IDS: frozenset[str] = ALLOWED_RUNBOOK_IDS - ROLLBACK_RUNBOOK_IDS


def is_allowed_runbook(runbook_id: str) -> bool:
    """Action Whitelist 판정: 확정 10종에 정확히 일치할 때만 True."""
    return runbook_id in ALLOWED_RUNBOOK_IDS


def is_ai_recommendable(runbook_id: str) -> bool:
    """AI 추천 가능 여부: 본편 7종만 True, 롤백 3종·미등록 ID는 False."""
    return runbook_id in AI_RECOMMENDABLE_RUNBOOK_IDS
