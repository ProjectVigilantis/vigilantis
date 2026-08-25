"""EC2/SG Raw Data 수집 — 기본(통합) 테스트.

services.collector.collect_region() 을 실제로 호출해 반환된 AssetInventory 를 검증한다.
(boto3 직접 호출이 아니라 우리 수집기를 거치므로, collector 가 깨지면 이 테스트가 잡는다.)

LocalStack(4566) 대상 통합 테스트라 미기동 시 전체 skip 한다(CI 안전).
로컬 실행 전제: LocalStack 기동 + EC2/SG 시드(scripts/seed_localstack) 완료.
"""

import os
import sys
import urllib.request
from pathlib import Path

import pytest

# import 경로: services(core-api) + schemas(packages)
CORE_API = Path(__file__).resolve().parents[2]
REPO_ROOT = Path(__file__).resolve().parents[4]
for p in (str(CORE_API), str(REPO_ROOT / "packages")):
    if p not in sys.path:
        sys.path.insert(0, p)

ENDPOINT = os.getenv("AWS_ENDPOINT_URL", "http://localhost:4566")
REGION = os.getenv("AWS_REGIONS", os.getenv("AWS_REGION", "ap-northeast-2")).split(",")[0]

# collector 가 LocalStack 으로 붙도록 보장(엔드포인트 없으면 실 AWS 로 감)
os.environ.setdefault("AWS_ENDPOINT_URL", ENDPOINT)
os.environ.setdefault("AWS_ACCESS_KEY_ID", "test")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "test")
os.environ.setdefault("AWS_REGIONS", REGION)


def _localstack_up() -> bool:
    try:
        with urllib.request.urlopen(f"{ENDPOINT}/_localstack/health", timeout=2) as r:
            return r.status == 200
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _localstack_up(), reason="LocalStack(4566) 미기동 — 통합 테스트 skip"
)


@pytest.fixture(scope="module")
def inventory():
    from services.collector import collect_region

    return collect_region(REGION)


def test_collect_region_ec2_raw(inventory):
    assert len(inventory.ec2_instances) >= 1, "EC2 0대 — LocalStack 시드 필요"
    a = inventory.ec2_instances[0]
    assert a.instance_id and a.instance_type   # 스키마 필드 기준 검증
    assert a.arn.startswith("arn:aws:ec2:")


def test_collect_region_sg_raw(inventory):
    assert len(inventory.security_groups) >= 1, "SG 0개 — 시드 필요"
    assert all(g.group_id for g in inventory.security_groups)


def test_open_to_world_detected_by_collector(inventory):
    # collector 의 정형화 결과(open_to_world/OpenPort)를 그대로 검증 — 판정 로직 중복 제거
    assert any(g.open_to_world for g in inventory.security_groups), (
        "전체개방(0.0.0.0/0) SG 없음 — 시드 확인"
    )


def test_collect_region_ebs_raw(inventory):
    # EBS 는 ec2 네임스페이스라 LocalStack Community 에서 수집 가능(ADR-0006 §4).
    # 시드(_ensure_volume)는 미부착(available) 볼륨을 만든다 → attached_instance_ids 비어야 한다.
    assert len(inventory.ebs_volumes) >= 1, "EBS 0개 — LocalStack 시드(_ensure_volume) 필요"
    v = inventory.ebs_volumes[0]
    assert v.volume_id and v.arn.startswith("arn:aws:ec2:")
    assert ":volume/" in v.arn
    assert any(not vol.attached_instance_ids for vol in inventory.ebs_volumes), (
        "미부착(available) 볼륨 없음 — 시드 확인"
    )


def test_collect_region_launch_template_raw(inventory):
    # Launch Template 은 ec2 네임스페이스라 Community 에서 수집 가능(ADR-0006 §4).
    # 시드(_ensure_launch_template)가 1개 만든다.
    assert len(inventory.launch_templates) >= 1, "LT 0개 — 시드(_ensure_launch_template) 필요"
    lt = inventory.launch_templates[0]
    assert lt.launch_template_id.startswith("lt-")
    assert ":launch-template/" in lt.arn


def test_asg_degrades_on_pro_only_service(inventory):
    # autoscaling 은 LocalStack Community 미포함(Pro 전용, ADR-0006 §4) → InternalFailure.
    # collector 의 _safe_describe 가 이를 흡수해 빈 목록으로 degrade 하고, 나머지 수집(EC2/SG/
    # EBS/NACL/LT)은 정상 완료되어야 한다(위 테스트들이 통과하는 것이 곧 그 증거).
    # 실 AWS 에서의 ASG 수집·MEMBER_OF/USES 검증은 6~7주차 스모크로 이월(§4).
    assert inventory.auto_scaling_groups == [], (
        "로컬에서 ASG 가 수집됨 — LocalStack 이 autoscaling(Pro)을 지원하게 되었거나 "
        "_safe_describe degrade 가 동작하지 않음"
    )
