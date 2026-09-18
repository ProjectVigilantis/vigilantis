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

import json
from enum import Enum, unique

from .api.assets import AssetType


@unique
class CollectionRunStatus(str, Enum):
    IN_PROGRESS = "IN_PROGRESS"
    SUCCESS = "SUCCESS"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"


# 수집 실패 라벨 → 그 실패가 **못 보게 만드는** 자산 유형. 수집기(services/collector.py)가
# 소멸 표시에서, 조인 점검(db/repositories/assets.py)이 관계 분류에서 같은 판단을 쓰므로
# 원천을 여기 하나로 둔다(#364). 종전에는 수집기에 두고 점검 쪽이 유형 집합만 복사한 뒤
# 드리프트 테스트로 묶었는데, 라벨 단위 판단에는 지도 전체가 필요하다.
# EC2·SG·NACL·EBS 조회는 degrade 를 흡수하지 않아(실패하면 리전이 FAILED) 여기 없다.
UNOBSERVED_TYPES_BY_FAILURE: dict[str, tuple[AssetType, ...]] = {
    "launch_templates": (AssetType.LAUNCH_TEMPLATE,),
    "auto_scaling_groups": (AssetType.AUTO_SCALING_GROUP,),
    "alb_target_groups": (AssetType.ALB_TARGET_GROUP,),
    "alb_target_health": (),
}


def unobserved_asset_types(error_summary: str | None) -> frozenset[AssetType]:
    """회차의 `error_summary`(실패 라벨 JSON)가 말하는 '이 회차가 못 본 유형'.

    **모르면 빈 집합을 준다.** 부르는 쪽이 판단을 미관측으로 접지 않고 조사 대상으로
    남기게 하려는 것이다 — 소멸 표시는 "잘못 지우기보다 안 지우기"로 넘어지지만(#332),
    조인 점검은 **"놓치기보다 경고하기"** 가 안전한 방향이다(#364).

    빈 집합을 주는 경우: 요약이 없거나(성공 회차) · JSON 이 아니거나 · 객체가 아니거나 ·
    모르는 라벨이 하나라도 섞였을 때. 상한 초과로 항목을 버리고 남긴 `_truncated` 표식도
    (collector.`_failures_summary`) 모르는 라벨이라 여기에 든다.
    """
    if not error_summary:
        return frozenset()
    try:
        failures = json.loads(error_summary)
    except (TypeError, ValueError):
        return frozenset()
    if not isinstance(failures, dict) or not failures:
        return frozenset()
    types: set[AssetType] = set()
    for label in failures:
        if label not in UNOBSERVED_TYPES_BY_FAILURE:
            return frozenset()
        types.update(UNOBSERVED_TYPES_BY_FAILURE[label])
    return frozenset(types)
