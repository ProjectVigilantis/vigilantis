"""다운사이징 목표 타입 규칙 테스트 (Issue #251).

규칙 셋(같은 패밀리 · 현재 메모리의 1/4 이상 · 메모리 2 GiB 이상)을 AND로 건 결과를 표로
고정한다. 표의 값이 바뀌면 방침이 바뀐 것이다 — #251 합의 없이 기대값을 고치지 않는다.
"""

import pytest

from schemas.rightsizing_policy import (
    FAMILY_MEMORY_MIB,
    FLOOR_MEMORY_MIB,
    KNOWN_INSTANCE_TYPES,
    MEMORY_RATIO_LIMIT,
    rightsizing_target_type,
)


@pytest.mark.parametrize(
    "current,expected",
    [
        ("t3.2xlarge", "t3.large"),
        # 비율 규칙이 이긴다 — 하한만 보면 small인데, 그러면 메모리가 16→2 GiB(8배 감소)다
        ("t3.xlarge", "t3.medium"),
        ("t3.large", "t3.small"),
        # 하한이 이긴다 — 비율만 보면 micro(1 GiB)까지 내려간다
        ("t3.medium", "t3.small"),
        ("t3.small", None),
        ("t3.micro", None),
        ("t3.nano", None),
        # m5에는 small이 없다 — 하한을 메모리로 적어 둔 이유다
        ("m5.24xlarge", "m5.8xlarge"),
        ("m5.2xlarge", "m5.large"),
        ("m5.xlarge", "m5.large"),
        ("m5.large", None),
    ],
)
def test_target_table(current, expected):
    assert rightsizing_target_type(current) == expected


@pytest.mark.parametrize("family", ["t2", "t3", "t3a", "t4g"])
def test_burstable_family_is_kept(family):
    assert rightsizing_target_type(f"{family}.xlarge") == f"{family}.medium"


@pytest.mark.parametrize(
    "current", [None, "", "c5.large", "c7g.xlarge", "t3.mega", "T3.XLARGE", "t3", " t3.large"]
)
def test_types_outside_the_spec_table_have_no_target(current):
    """표 밖 패밀리의 메모리를 추측하지 않는다 — 추측한 값으로 내리면 그것이 실행된다."""
    assert rightsizing_target_type(current) is None


@pytest.mark.parametrize("current", sorted(KNOWN_INSTANCE_TYPES))
def test_every_target_keeps_the_three_rules(current):
    target = rightsizing_target_type(current)
    if target is None:
        return
    current_family, current_size = current.split(".")
    target_family, target_size = target.split(".")
    sizes = FAMILY_MEMORY_MIB[current_family]

    assert target_family == current_family  # ① 같은 패밀리
    assert sizes[target_size] < sizes[current_size]  # 실제로 내린다
    assert sizes[target_size] * MEMORY_RATIO_LIMIT >= sizes[current_size]  # ②
    assert sizes[target_size] >= FLOOR_MEMORY_MIB  # ③


def test_golden_cost_candidates_split_into_two_targets():
    """골든 COST_CANDIDATE 6건(A1·A7 = t3.xlarge, A11·A12·A14·A16 = t3.large)의 목표.

    하한만 두면 6건이 전부 t3.small로 모인다(#251 박지현 코멘트). 비율 규칙이 xlarge를
    medium에 멈춰 세운다.
    """
    assert rightsizing_target_type("t3.xlarge") == "t3.medium"
    assert rightsizing_target_type("t3.large") == "t3.small"


def test_localstack_seed_cost_candidate_has_a_target():
    """LocalStack 시드의 다운사이징 후보(idle-dev, m5.2xlarge)가 메뉴에서 빠지지 않는다.

    10/1 중간 발표 시연이 LocalStack 기반이라, 이 한 대가 목표를 잃으면 시연의 다운사이징
    카드가 사라진다(scripts/seed_localstack.py — 절감 후보는 이 한 대뿐이다).
    """
    assert rightsizing_target_type("m5.2xlarge") == "m5.large"
