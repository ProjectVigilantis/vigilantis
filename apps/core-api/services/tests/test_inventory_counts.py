# ==============================================================================
# [파일 설명]  담당: 김세혁 (PM · Infra & DevSecOps)
# 수집 회차가 마감 때 남기는 유형별 자산 수(asset_inventory_counts — 시계열 축 5의 원천)를
# 회귀로 고정한다(2026-09-29).
#
# 지키려는 것은 넷이다.
#   1. 관측한 유형은 0건이어도 행이 남는다 — "없다"도 추이의 값이다.
#   2. 조회가 실패한 유형(degrade)은 행을 남기지 않는다 — 못 본 것을 0건으로 적으면 추이가
#      자산이 사라진 것처럼 떨어진다(소멸 표시 #332와 같은 경계).
#   3. 리전 전체를 관측한 호출(prune_absent)만 남긴다 — 모르는 실패 라벨이 섞인 회차,
#      회차를 남이 연 호출, 골든 적재처럼 리전의 일부만 넘기는 호출은 남기지 않는다.
#   4. 조회(asset_inventory_history)는 리전 × 칸마다 **스냅샷을 남긴** 마지막 회차만 센다
#      — 스냅샷 없는 회차가 칸 끝에 와도 앞선 관측값이 사라지지 않는다(PR #411 리뷰).
# ==============================================================================

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select, update

CORE_API = Path(__file__).resolve().parents[2]
REPO_ROOT = Path(__file__).resolve().parents[4]
for _p in (str(CORE_API), str(REPO_ROOT / "packages")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from db import models  # noqa: E402
from db.repositories import assets as assets_repo  # noqa: E402
from schemas.api.assets import AssetType  # noqa: E402
from schemas.assets import AssetInventory, AutoScalingGroupAsset, Ec2Asset  # noqa: E402
from schemas.collections import CollectionRunStatus  # noqa: E402
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
    res = persist_inventory(
        _inventory(["i-a", "i-b"], collected_at=_at(), asg_names=["g1"]), db, prune_absent=True
    )

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
        prune_absent=True,
    )

    counts = _counts(db, res["collection_run_id"])
    assert counts[AssetType.EC2] == 1
    assert AssetType.AUTO_SCALING_GROUP not in counts
    assert AssetType.ALB_TARGET_GROUP not in counts


def test_unknown_failure_label_records_nothing(db):
    res = persist_inventory(
        _inventory(["i-a"], collected_at=_at(), failures={"brand_new_service": "Boom"}),
        db,
        prune_absent=True,
    )

    assert _counts(db, res["collection_run_id"]) == {}


def test_run_opened_by_caller_records_nothing(db):
    """회차를 연 쪽이 마감도 한다 — 이 함수는 남의 회차에 스냅샷을 남기지 않는다."""
    run = assets_repo.start_collection_run(
        db, account_id=ACCOUNT, region=REGION, mode="localstack", lookback_days=14,
        period_seconds=3600,
    )
    persist_inventory(
        _inventory(["i-a"], collected_at=_at()), db, run.collection_run_id, prune_absent=True
    )

    assert _counts(db, run.collection_run_id) == {}


def test_partial_view_call_records_nothing(db):
    """골든 적재 모양 — 파일 하나(리전의 일부)를 넘기는 호출은 리전 스냅샷을 남기지 않는다.

    ``scripts/load_golden_assets.py`` 는 골든 파일마다 이 함수를 ``prune_absent`` 없이
    부른다. 남기면 마지막 파일의 건수가 리전 전체로 그려진다(PR #411 리뷰: 8건 ≠ 32건).
    """
    res = persist_inventory(_inventory(["i-a", "i-b"], collected_at=_at()), db)

    assert _counts(db, res["collection_run_id"]) == {}


# --- 조회: asset_inventory_history ------------------------------------------------

_BUCKET = 300


def _bucket_start() -> datetime:
    """_BUCKET_ORIGIN(정시) 기준으로 정렬된 최근 칸의 시작 — 칸 경계에 걸리지 않게 한다."""
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    return now.replace(minute=now.minute - now.minute % 5) - timedelta(hours=1)


def _run(db, started_at: datetime, *, status=CollectionRunStatus.SUCCESS, counts=None) -> str:
    """회차 1건을 started_at 으로 박아 만든다. counts 가 None 이면 스냅샷 없는 회차."""
    run = assets_repo.start_collection_run(
        db, account_id=ACCOUNT, region=REGION, mode="localstack", lookback_days=14,
        period_seconds=3600,
    )
    db.execute(
        update(models.CollectionRun)
        .where(models.CollectionRun.collection_run_id == run.collection_run_id)
        .values(started_at=started_at)
    )
    assets_repo.finish_collection_run(
        db, run.collection_run_id, status, finished_at=started_at + timedelta(seconds=5)
    )
    if counts is not None:
        assets_repo.record_inventory_counts(
            db, collection_run_id=run.collection_run_id, counts=counts
        )
    db.flush()
    return run.collection_run_id


def _history(db, since: datetime) -> dict[AssetType, int]:
    rows = assets_repo.asset_inventory_history(
        db, regions=[REGION], since=since, bucket_seconds=_BUCKET
    )
    assert len({r.observed_at for r in rows}) <= 1, rows
    return {r.asset_type: r.count for r in rows}


def test_history_keeps_prior_value_when_failed_run_follows(db):
    t0 = _bucket_start()
    _run(db, t0 + timedelta(seconds=10), counts={AssetType.EC2: 4, AssetType.SG: 4})
    _run(db, t0 + timedelta(seconds=70), status=CollectionRunStatus.FAILED)

    assert _history(db, t0) == {AssetType.EC2: 4, AssetType.SG: 4}


def test_history_keeps_prior_value_when_snapshot_skipped_run_follows(db):
    """모르는 실패 라벨로 스냅샷을 건너뛴 회차(PARTIAL)가 칸 끝에 와도 앞선 값이 남는다."""
    t0 = _bucket_start()
    _run(db, t0 + timedelta(seconds=10), counts={AssetType.EC2: 4, AssetType.SG: 4})
    _run(db, t0 + timedelta(seconds=70), status=CollectionRunStatus.PARTIAL)

    assert _history(db, t0) == {AssetType.EC2: 4, AssetType.SG: 4}


def test_history_uses_latest_snapshot_and_keeps_zero(db):
    """스냅샷 있는 회차가 둘이면 마지막 것만 센다(합산 금지) — 관측값 0도 행으로 남는다."""
    t0 = _bucket_start()
    _run(db, t0 + timedelta(seconds=10), counts={AssetType.EC2: 4, AssetType.SG: 4})
    _run(db, t0 + timedelta(seconds=70), counts={AssetType.EC2: 3, AssetType.SG: 0})

    assert _history(db, t0) == {AssetType.EC2: 3, AssetType.SG: 0}
