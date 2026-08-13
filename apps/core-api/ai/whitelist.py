# ==============================================================================
# [파일 설명]  담당: 안성일 / 김세혁
# 가드레일 2단계 Action Whitelist — 실행을 허용하는 Runbook ID의 확정 목록입니다.
# 목록은 ADR-0002(docs/adr/0002-runbook-whitelist-mvp-scope.md)의 7종 그대로이며,
# 여기 없는 ID는 실행 경로에 진입할 수 없습니다.
#
# Runbook별 필수 파라미터·허용 AWS 작업·호출 순서는 Master Registry(런북 명세서)
# 대조 후 별도 계약으로 추가합니다. 이 파일은 ID 허용 여부만 판정합니다.
# ==============================================================================

from __future__ import annotations

from enum import Enum


class RunbookId(str, Enum):
    """ADR-0002 확정 Action Whitelist 7종."""

    RUNBOOK_EC2_ISOLATE = "RUNBOOK_EC2_ISOLATE"
    RUNBOOK_NACL_ADD_DENY = "RUNBOOK_NACL_ADD_DENY"
    RUNBOOK_NACL_RESTORE = "RUNBOOK_NACL_RESTORE"
    RUNBOOK_SG_DELETE_ISOLATED = "RUNBOOK_SG_DELETE_ISOLATED"
    RUNBOOK_EC2_RIGHTSIZING = "RUNBOOK_EC2_RIGHTSIZING"
    RUNBOOK_EC2_ENABLE_AUTOSCALING = "RUNBOOK_EC2_ENABLE_AUTOSCALING"
    RUNBOOK_EBS_DELETE_UNATTACHED = "RUNBOOK_EBS_DELETE_UNATTACHED"


ALLOWED_RUNBOOK_IDS: frozenset[str] = frozenset(item.value for item in RunbookId)


def is_allowed_runbook(runbook_id: str) -> bool:
    """Action Whitelist 판정: 확정 7종에 정확히 일치할 때만 True."""
    return runbook_id in ALLOWED_RUNBOOK_IDS
