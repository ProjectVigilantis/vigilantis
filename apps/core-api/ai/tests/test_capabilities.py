"""FinOps 조치 메뉴 빌더 테스트 — 축 ③ 서버 계산 가능 여부 (Issue #251).

축 ①②는 골든 케이스로 test_evaluation_cases.py가 본다. 여기서는 다운사이징 목표 타입을
규칙이 계산할 수 없는 자산에 다운사이징이 메뉴로 올라가지 않는지만 본다 — 올라가면 모델이
고르는 순간 그래프가 값을 채우지 못해 호출 전체가 FAILED가 된다.
"""

import pytest

from ai.capabilities import build_finops_capabilities
from schemas.api.assets import AssetType, Verdict
from schemas.runbooks import RunbookId


def _offered(instance_type):
    return [
        capability.runbook_id
        for capability in build_finops_capabilities(
            asset_type=AssetType.EC2, verdict=Verdict.COST_CANDIDATE, instance_type=instance_type
        )
    ]


@pytest.mark.parametrize(
    # m5.2xlarge = LocalStack 시드의 다운사이징 후보(scripts/seed_localstack.py idle-dev)
    "instance_type", ["t3.xlarge", "t3.large", "t3.medium", "t4g.2xlarge", "m5.2xlarge"]
)
def test_rightsizing_is_offered_when_the_rule_has_a_target(instance_type):
    assert _offered(instance_type) == [
        RunbookId.RUNBOOK_EC2_RIGHTSIZING,
        RunbookId.RUNBOOK_EC2_ENABLE_AUTOSCALING,
    ]


@pytest.mark.parametrize("instance_type", ["t3.small", "t3.micro", "m5.large", "c5.large", None])
def test_rightsizing_is_withheld_when_the_rule_has_no_target(instance_type):
    # 이미 하한 근처(더 내릴 크기가 없음)이거나 사양 표 밖 패밀리 — 나머지 메뉴는 그대로 남는다
    assert _offered(instance_type) == [RunbookId.RUNBOOK_EC2_ENABLE_AUTOSCALING]


def test_non_ec2_menus_do_not_depend_on_instance_type():
    offered = build_finops_capabilities(
        asset_type=AssetType.EBS, verdict=Verdict.UNUSED, instance_type=None
    )
    assert [capability.runbook_id for capability in offered] == [
        RunbookId.RUNBOOK_EBS_DELETE_UNATTACHED
    ]
