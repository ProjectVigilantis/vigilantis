"""CollectionRunStatus 내부 계약 테스트 (Issue #48) — 값 4종 고정.

수집 실패 라벨 지도(#364)도 여기 계약이다 — 수집기의 소멸 표시와 조인 점검의 관계
분류가 같은 지도를 읽으므로, 라벨을 늘린 쪽이 여기를 고치게 한다.
"""

import pytest

from schemas.api.assets import AssetType
from schemas.collections import (
    UNOBSERVED_TYPES_BY_FAILURE,
    CollectionRunStatus,
    unobserved_asset_types,
)


def test_values_match_contract_exactly():
    assert {s.value for s in CollectionRunStatus} == {
        "IN_PROGRESS", "SUCCESS", "PARTIAL", "FAILED",
    }


def test_map_covers_only_absorbable_lookups():
    """흡수 가능한 조회 4종뿐이다 — EC2·SG·NACL·EBS 는 실패하면 리전이 FAILED 라 없다."""
    assert set(UNOBSERVED_TYPES_BY_FAILURE) == {
        "launch_templates", "auto_scaling_groups", "alb_target_groups", "alb_target_health",
    }
    assert UNOBSERVED_TYPES_BY_FAILURE["alb_target_health"] == ()


@pytest.mark.parametrize("error_summary, expected", [
    ('{"launch_templates":"AccessDenied"}', {AssetType.LAUNCH_TEMPLATE}),
    ('{"auto_scaling_groups":"InternalFailure","alb_target_groups":"InternalFailure"}',
     {AssetType.AUTO_SCALING_GROUP, AssetType.ALB_TARGET_GROUP}),
    # 못 보게 만드는 유형이 없는 라벨 — 빈 집합이지만 '모른다'와 결과가 같다
    ('{"alb_target_health":"Throttling"}', set()),
])
def test_known_labels_map_to_their_types(error_summary, expected):
    assert unobserved_asset_types(error_summary) == expected


@pytest.mark.parametrize("error_summary", [
    None,                                              # 요약 없음(성공 회차)
    "",
    "not json",                                        # 파싱 실패
    "[]",                                              # 객체가 아님
    "{}",                                              # 빈 객체
    '{"_truncated":"7"}',                              # 상한 초과 표식
    '{"brand_new_lookup":"InternalFailure"}',          # 지도에 없는 라벨
    '{"launch_templates":"X","brand_new_lookup":"Y"}',  # 아는 라벨과 섞여도 판단하지 않는다
])
def test_unknown_or_unreadable_summary_is_empty(error_summary):
    """모르면 빈 집합이다 — 부르는 쪽이 '못 봤다'로 접지 않게 한다(#364)."""
    assert unobserved_asset_types(error_summary) == frozenset()
