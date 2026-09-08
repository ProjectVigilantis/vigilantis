# ==============================================================================
# [파일 설명]  담당: 박지현 (QA & Scenario)
# 골든 자산 가드가 쓰는 **계약 파생식**을 한자리에 둡니다. (Issue #271)
#
# ── 왜 공용 모듈인가 ───────────────────────────────────────────────────────
# 골든 입력 자산에 거는 가드가 둘이고, 둘 다 "이 자산 리스트는 무슨 유형인가"를
# 알아야 한다.
#
#   tests/test_golden_dataset.py  판정 대상 자산에 정답이 1:1로 있는가        (#270)
#   tests/test_guardrails.py      골든 자산 종류마다 짝지을 런북이 있는가      (#271)
#
# 파생식을 두 벌로 두면 **한쪽만 고쳐져 가드가 조용히 헐거워진다.** #270 이 정확히
# 그런 구멍(fail-open)을 닫은 자리라, 그 파생식을 복제하지 않고 여기로 모은다.
#
# ── 이름을 conftest 로 두지 않는 이유 ─────────────────────────────────────
# CI 는 여러 디렉터리를 한 세션으로 돌리고(.github/workflows/ci.yml), 그때
# `conftest` 라는 최상위 이름은 apps/core-api/tests/conftest.py 가 먼저 차지한다.
# `tests/execution_harness.py` 헤더가 적은 것과 같은 이유다.
#
# 여기에는 pytest 픽스처를 두지 않는다. 순수 함수만 둔다.
# ==============================================================================

from __future__ import annotations

import sys
from pathlib import Path
from typing import get_args

ROOT = Path(__file__).resolve().parents[1]
for _path in (ROOT / "apps" / "core-api", ROOT / "packages"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from ai.capabilities import target_asset_type  # noqa: E402
from schemas.api.assets import _RULE_TARGET_TYPES, AssetType  # noqa: E402
from schemas.assets import AssetInventory  # noqa: E402
from schemas.runbooks import RunbookId  # noqa: E402

GOLDEN_FINOPS_INPUT = ROOT / "datasets" / "golden" / "finops" / "input"


def asset_list_fields() -> dict[str, AssetType]:
    """AssetInventory 의 '자산 리스트' 필드 → 그 리스트가 담는 자산 유형.

    필드 이름을 손으로 적지 않는다. 리스트 항목 모델의 asset_type 기본값에서 읽으므로
    (Ec2Asset.asset_type = AssetType.EC2, frozen), 자산 유형이 늘면 여기도 함께 는다.

    **유형이 실제 AssetType 일 때만 담는다.** `asset_type` 필드가 있기만 하면 담으면,
    기본값 없는 모델(`asset_type: AssetType` 만 선언)의 default 는 None 이 아니라
    PydanticUndefined 라 어떤 집합에도 없고, 그 리스트가 **면제로** 분류된다 — 세지도
    대조하지도 않은 채 골든을 통과한다. PydanticUndefined 는 "비대상임의 증명"이 아니라
    **증명 실패**이므로, 증명에 실패한 리스트는 fields 에 넣지 않아 호출부가 계속 세게
    둔다(닫히는 쪽으로 틀린다). SG 누락이 그렇게 지나간 적이 있다(#134).
    """
    fields: dict[str, AssetType] = {}
    for name, field in AssetInventory.model_fields.items():
        args = get_args(field.annotation)  # list[Ec2Asset] → (Ec2Asset,)
        if not args:
            continue
        declared = getattr(args[0], "model_fields", {}).get("asset_type")
        if isinstance(getattr(declared, "default", None), AssetType):
            fields[name] = declared.default
    return fields


def judgement_free_list_fields() -> frozenset[str]:
    """판정이 붙지 않는 자산 리스트. 계약(_RULE_TARGET_TYPES)에서 **파생**시킨다.

    키 이름을 하드코딩하면 판정 대상이 늘어날 때 면제가 함께 좁아지지 않아, 가드가
    조용히 헐거워진다. 어떤 유형이 _RULE_TARGET_TYPES 에 들어가는 순간 이 집합에서
    자동으로 빠진다. (Issue #271 ① · PR #270)
    """
    return frozenset(
        name
        for name, asset_type in asset_list_fields().items()
        if asset_type not in _RULE_TARGET_TYPES
    )


def runbook_target_asset_types() -> frozenset[AssetType]:
    """어떤 런북이든 조치 대상으로 삼는 자산 유형의 합집합.

    원천은 파라미터 계약이다 — `target_asset_type()` 이 런북의 **대상 자원 ID 파라미터**
    에서 유형을 읽는다(ai/capabilities.py). 대상 자원 ID 파라미터가 없는 런북은 None 을
    돌려주므로 합집합에서 빠진다.

    런북이 늘거나 파라미터 계약이 바뀌면 이 집합이 **자동으로** 따라 움직인다.
    """
    return frozenset(
        asset_type
        for asset_type in (target_asset_type(runbook_id) for runbook_id in RunbookId)
        if asset_type is not None
    )


def runbook_free_list_fields() -> frozenset[str]:
    """**어떤 런북도 대상으로 삼지 않는** 자산 리스트. 계약에서 파생시킨다.

    지금은 NACL 을 뺀 토폴로지 자산 3종(Launch Template · ASG · ALB Target Group)이다 —
    NACL 은 RUNBOOK_NACL_ADD_DENY 의 대상이라 여기 들어오지 않는다. 어떤 유형을 대상으로
    삼는 런북이 생기는 순간 이 집합에서 자동으로 빠진다.

    **판정 대상 여부와는 다른 축이다.** EBS 는 판정 대상이면서(_RULE_TARGET_TYPES)
    RUNBOOK_EBS_DELETE_UNATTACHED 의 대상이라 양쪽 어디에도 면제되지 않는다.
    """
    targeted = runbook_target_asset_types()
    return frozenset(
        name
        for name, asset_type in asset_list_fields().items()
        if asset_type not in targeted
    )
