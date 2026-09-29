# ==============================================================================
# [파일 설명]  담당: 김세혁 (PM · Infra & DevSecOps)
# 수집 회차가 마감 때 남기는 유형별 자산 수(asset_inventory_counts — 시계열 축 5의 원천)를
# 회귀로 고정한다(2026-09-29).
#
# 지키려는 것은 셋이다.
#   1. 관측한 유형은 0건이어도 행이 남는다 — "없다"도 추이의 값이다.
#   2. 조회가 실패한 유형(degrade)은 행을 남기지 않는다 — 못 본 것을 0건으로 적으면 추이가
#      자산이 사라진 것처럼 떨어진다(소멸 표시 #332와 같은 경계).
#   3. 모르는 실패 라벨이 섞인 회차, 회차를 남이 연 호출은 남기지 않는다.
# ==============================================================================

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select

CORE_API = Path(__file__).resolve().parents[2]
REPO_ROOT = Path(__file__).resolve().parents[4]
for _p in (str(CORE_API), str(REPO_ROOT / "packages")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from db import models  # noqa: E402
from db.repositories import assets as assets_repo  # noqa: E402
from schemas.api.assets import AssetType  # noqa: E402
from schemas.assets import AssetInventory, AutoScalingGroupAsset, Ec2Asset  # noqa: E402
from services.collector import persist_inventory  # noqa: E402

# db·pg_engine 픽스처는 services/tests/conftest.py 가 등록한다

ACCOUNT = "123456789012"
REGION = "ap-northeast-2"


def _inventory(
    instance_ids: list[str],
    *,
    collected_at: datetime,
    failures: dict[str, str] | None = None,
    asg_names: list[str] | None = None,
) -> AssetInventory:
    """EC2·ASG 만 채운 인벤토리 — 나머지 유형은 관측했지만 0건이다."""
    return AssetInventory(
        account_id=ACCOUNT,
        region=REGION,
        mode="localstack",
        collected_at=collected_at,
        lookback_days=14,
        period_seconds=3600,
        ec2_instances=[
            Ec2Asset(
                arn=f"arn:aws:ec2:{REGION}:{ACCOUNT}:instance/{iid}",
                instance_id=iid,
                name=iid,
                instance_type="t3.micro",
                state="running",
                region=REGION,
                availability_zone=f"{REGION}a",
                vpc_id="vpc-001",
                subnet_id="subnet-001",
                private_ip="10.0.1.10",
                security_group_ids=[],
                tags={},
            )
            for iid in instance_ids
        ],
        auto_scaling_groups=[
            AutoScalingGroupAsset(
                arn=f"arn:aws:autoscaling:{REGION}:{ACCOUNT}:autoScalingGroup:g1:autoScalingGroupName/{n}",
                name=n,
                region=REGION,
                min_size=1,
                max_size=3,
                desired_capacity=2,
            )
            for n in (asg_names or [])
        ],
        collector_failures=failures or {},
    )


def _counts(db, run_id: str) -> dict[AssetType, int]:
    rows = db.execute(
        select(models.AssetInventoryCount).where(
            models.AssetInventoryCount.collection_run_id == run_id
        )
    ).scalars()
    return {AssetType(r.asset_type): r.count for r in rows}


def _at() -> datetime:
    return datetime.now(timezone.utc) - timedelta(hours=1)


def test_observed_types_are_recorded_including_zero(db):
    res = persist_inventory(_inventory(["i-a", "i-b"], collected_at=_at(), asg_names=["g1"]), db)

    counts = _counts(db, res["collection_run_id"])
    assert counts[AssetType.EC2] == 2
    assert counts[AssetType.AUTO_SCALING_GROUP] == 1
    # 관측했지만 없는 유형도 0건으로 남는다
    assert counts[AssetType.NACL] == 0
    assert set(counts) == set(AssetType)


def test_degraded_type_leaves_no_row(db):
    """LocalStack Community 모양 — autoscaling·elbv2 가 막혀 그 유형은 못 봤다."""
    res = persist_inventory(
        _inventory(
            ["i-a"],
            collected_at=_at(),
            failures={"auto_scaling_groups": "AccessDenied", "alb_target_groups": "AccessDenied"},
        ),
        db,
    )

    counts = _counts(db, res["collection_run_id"])
    assert counts[AssetType.EC2] == 1
    assert AssetType.AUTO_SCALING_GROUP not in counts
    assert AssetType.ALB_TARGET_GROUP not in counts


def test_unknown_failure_label_records_nothing(db):
    res = persist_inventory(
        _inventory(["i-a"], collected_at=_at(), failures={"brand_new_service": "Boom"}), db
    )

    assert _counts(db, res["collection_run_id"]) == {}


def test_run_opened_by_caller_records_nothing(db):
    """회차를 연 쪽이 마감도 한다 — 이 함수는 남의 회차에 스냅샷을 남기지 않는다."""
    run = assets_repo.start_collection_run(
        db, account_id=ACCOUNT, region=REGION, mode="localstack", lookback_days=14,
        period_seconds=3600,
    )
    persist_inventory(_inventory(["i-a"], collected_at=_at()), db, run.collection_run_id)

    assert _counts(db, run.collection_run_id) == {}
