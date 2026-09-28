# ==============================================================================
# [파일 설명]  담당: 안성일 / 김세혁
# Runbook 계약의 원천(Single Source) — 가드레일 2단계 Action Whitelist가 허용하는
# Runbook ID의 확정 목록입니다. 확정 10종 = 본편 7종(ADR-0002) + 롤백 3종(ADR-0004)이며,
# 여기 없는 ID는 실행 경로에 진입할 수 없습니다.
#
# 소비자: apps/core-api/ai/whitelist.py(가드레일 2단계 진입점, re-export) ·
#         실행 요청 DTO의 runbook_id 검증(PR #32 예정)
#
# 롤백 3종은 실행은 허용하되 AI 추천 대상이 아닙니다(ai_recommendable: false,
# ADR-0004 롤백 공통 정책 ②) — 트리거는 시스템·관제자만 가능합니다.
#
# 이 파일은 ID 수준 판정만 담당합니다. Runbook별 필수 파라미터는 runbook_parameters.py가
# 정의하고(#154, ADR-0007 §5), 허용 AWS 작업·호출 순서는 ADR-0007 §Context의 target_api
# 실측표가 갖습니다. Whitelist 확정본은 docs/PROJECT_STATUS.md §Action Whitelist 표이며,
# 이 파일은 그 표의 코드 소재입니다 — 표와 어긋나면 표를 기준으로 이 파일을 고칩니다.
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


# ------------------------------------------------------------------------------
# ID 수준 분류 (Issue #55) — 파라미터·호출 순서 같은 상세 계약은 여전히 별도 층이다.
# ------------------------------------------------------------------------------


@unique
class RunbookDomain(str, Enum):
    """Runbook Registry 분류 축 — Incident 분류(FINOPS·SECOPS)와 별개로 ROLLBACK을 가진다."""

    FINOPS = "FINOPS"
    SECOPS = "SECOPS"
    ROLLBACK = "ROLLBACK"


@unique
class TriggerSource(str, Enum):
    """실행을 시작한 사유. Execution 레코드에 개별 기록한다. (ADR-0004 1차 개정)"""

    USER_APPROVAL = "USER_APPROVAL"                    # 관제자 승인 실행
    PRE_MITIGATION_0_5S = "PRE_MITIGATION_0_5S"        # High 위협 즉시 선차단
    TIMEOUT_ISOLATION_1M = "TIMEOUT_ISOLATION_1M"      # 1분 미응답 만료 자동 격리
    AUTO_ON_FAILURE = "AUTO_ON_FAILURE"                # 주 조치 실패 시 자동 복구


@unique
class ApprovalMode(str, Enum):
    """Runbook별 승인 정책. 사람 승인 없이 실행될 수 있는지를 나타낸다. (ADR-0004 1차 개정)"""

    HUMAN_ONLY = "HUMAN_ONLY"              # 관제자 승인 필수
    SYSTEM_OR_HUMAN = "SYSTEM_OR_HUMAN"    # 시스템 자동 시작 허용


# Runbook → 도메인 분류 (본편 7종: SSOT Whitelist 표 / 롤백 3종: ADR-0004)
RUNBOOK_DOMAIN_BY_ID: dict[str, RunbookDomain] = {
    RunbookId.RUNBOOK_EC2_ISOLATE.value: RunbookDomain.SECOPS,
    RunbookId.RUNBOOK_NACL_ADD_DENY.value: RunbookDomain.SECOPS,
    RunbookId.RUNBOOK_NACL_RESTORE.value: RunbookDomain.SECOPS,
    RunbookId.RUNBOOK_SG_DELETE_ISOLATED.value: RunbookDomain.SECOPS,
    RunbookId.RUNBOOK_EC2_RIGHTSIZING.value: RunbookDomain.FINOPS,
    RunbookId.RUNBOOK_EC2_ENABLE_AUTOSCALING.value: RunbookDomain.FINOPS,
    RunbookId.RUNBOOK_EBS_DELETE_UNATTACHED.value: RunbookDomain.FINOPS,
    RunbookId.RUNBOOK_EC2_UNISOLATE.value: RunbookDomain.ROLLBACK,
    RunbookId.RUNBOOK_SG_RECREATE.value: RunbookDomain.ROLLBACK,
    RunbookId.RUNBOOK_EC2_REVERT_SIZE.value: RunbookDomain.ROLLBACK,
}

# 주 조치 → 등록 롤백 Runbook 연결. Runbook 명세의 참조 관계 전체가 아니라 ROLLBACK
# 도메인 대상 연결만 추린 파생 맵이다(ADR-0004로 정식 등록된 3종). NACL 차단 해제는
# 주 조치 RUNBOOK_NACL_RESTORE 경로라 여기 없다.
ROLLBACK_RUNBOOK_BY_MAIN_ID: dict[str, str] = {
    RunbookId.RUNBOOK_EC2_ISOLATE.value: RunbookId.RUNBOOK_EC2_UNISOLATE.value,
    RunbookId.RUNBOOK_SG_DELETE_ISOLATED.value: RunbookId.RUNBOOK_SG_RECREATE.value,
    RunbookId.RUNBOOK_EC2_RIGHTSIZING.value: RunbookId.RUNBOOK_EC2_REVERT_SIZE.value,
}

# 롤백 3종의 승인 정책 — ADR-0004 §Decision 결정 표를 그대로 옮긴 것이다.
# `HUMAN_ONLY`는 **사람이 누르기 전에는 시작되지 않는다**는 뜻이고, 셋 중 그 반대는
# `REVERT_SIZE` 하나뿐이다(`trigger_source`에 `AUTO_ON_FAILURE`가 있다).
#
# 본편 7종을 여기 두지 않는 이유는 이 표를 읽는 질문이 하나이기 때문이다 — "주 조치가
# 실패했을 때 **시스템이 스스로** 그 짝을 발동해도 되는가"(dispatcher._AUTO_ROLLBACK_ON_
# ASSET_CHANGE). 본편의 승인 정책은 그 질문에 답하지 않는다.
APPROVAL_MODE_BY_ROLLBACK_ID: dict[str, ApprovalMode] = {
    RunbookId.RUNBOOK_EC2_UNISOLATE.value: ApprovalMode.HUMAN_ONLY,
    RunbookId.RUNBOOK_SG_RECREATE.value: ApprovalMode.HUMAN_ONLY,
    RunbookId.RUNBOOK_EC2_REVERT_SIZE.value: ApprovalMode.SYSTEM_OR_HUMAN,
}

# 주 조치 → **자동 발동이 허용된** 등록 롤백. 위 표에서 파생하므로 ADR-0004 결정 표
# 하나가 원천이다.
#
# ROLLBACK_RUNBOOK_BY_MAIN_ID와 갈라 두는 것이 이 상수의 존재 이유다. 저쪽은 "짝이
# 있는가"(관제자 복구 버튼이 무엇을 여는가)를 답하고, 이쪽은 "**사람 없이** 발동해도
# 되는가"를 답한다. 둘을 같은 표로 쓰면 짝이 있다는 이유만으로 `HUMAN_ONLY` 원복이
# 시스템 자동 실행으로 나가, ADR-0004가 사람에게 맡긴 판단을 스케줄러가 대신하게 된다.
AUTO_ROLLBACK_RUNBOOK_BY_MAIN_ID: dict[str, str] = {
    main_id: rollback_id
    for main_id, rollback_id in ROLLBACK_RUNBOOK_BY_MAIN_ID.items()
    if APPROVAL_MODE_BY_ROLLBACK_ID[rollback_id] is ApprovalMode.SYSTEM_OR_HUMAN
}


def domain_of(runbook_id: str) -> RunbookDomain | None:
    """등록 Runbook의 도메인 분류. 미등록 ID는 None."""
    return RUNBOOK_DOMAIN_BY_ID.get(runbook_id)
