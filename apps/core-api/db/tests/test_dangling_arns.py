# ==============================================================================
# [파일 설명]  담당: 김승철 (Data & Rule Engine)
# assets_repo.find_dangling_arns 회귀 — 조인 키가 assets.arn 과 어긋나거나 자산 리전이
# 자기 ARN 과 다르면 잡는다. (Issue #342)
# ==============================================================================

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

CORE_API = Path(__file__).resolve().parents[2]
REPO_ROOT = Path(__file__).resolve().parents[4]
for _p in (str(CORE_API), str(REPO_ROOT / "packages")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from db import models  # noqa: E402
from db.repositories import assets as assets_repo  # noqa: E402
from schemas.api.assets import AssetType, RelationType  # noqa: E402

NOW = datetime(2026, 9, 16, 6, 0, tzinfo=timezone.utc)


def _asset(db, arn, region):
    run = assets_repo.start_collection_run(
        db, account_id="1", region=region, mode="localstack",
        lookback_days=14, period_seconds=3600,
    )
    return assets_repo.upsert_asset(
        db, arn=arn, asset_type=AssetType.EC2, resource_id=arn.rsplit("/", 1)[-1],
        account_id="1", region=region, spec={}, collection_run_id=run.collection_run_id,
        collected_at=NOW,
    )


def test_consistent_assets_and_relationships_have_no_findings(db):
    """리전 정합 자산 + 그 자산을 정확히 가리키는 관계 → 0건."""
    a = _asset(db, "arn:aws:ec2:ap-northeast-2:1:instance/i-ok", "ap-northeast-2")
    sg = _asset(db, "arn:aws:ec2:ap-northeast-2:1:security-group/sg-ok", "ap-northeast-2")
    db.add(models.AssetRelationship(
        source_asset_id=a.asset_id, relation_type=RelationType.SECURED_BY.value,
        target_arn=sg.arn,
    ))
    db.flush()
    assert assets_repo.find_dangling_arns(db) == []


def test_dangling_target_arn_is_flagged(db):
    """관계 target_arn 이 assets.arn 에 없으면 (dangling) 으로 잡힌다 — 한 곳에서 ARN 을
    다르게 조립했을 때의 증상."""
    a = _asset(db, "arn:aws:ec2:ap-northeast-2:1:instance/i-ok", "ap-northeast-2")
    db.add(models.AssetRelationship(
        source_asset_id=a.asset_id, relation_type=RelationType.SECURED_BY.value,
        target_arn="arn:aws:ec2:ap-northeast-2:1:security-group/sg-missing",
    ))
    db.flush()
    found = assets_repo.find_dangling_arns(db)
    assert [f for f in found if f.kind == "dangling" and "sg-missing" in f.value]
    assert all(f.table == "asset_relationships" for f in found if f.kind == "dangling")


def test_region_mismatch_is_flagged(db):
    """자산 region 컬럼이 자기 ARN 의 리전과 다르면 (region_mismatch) 으로 잡힌다 —
    #261 리전 스코프가 둘을 따로 읽어 어긋나면 필터에서 새거나 빠진다."""
    _asset(db, "arn:aws:ec2:us-east-1:1:instance/i-bad", "ap-northeast-2")  # region≠arn
    found = assets_repo.find_dangling_arns(db)
    mism = [f for f in found if f.kind == "region_mismatch"]
    assert mism and "i-bad" in mism[0].value
