"""Persistence pipeline tests for Issue #67.

Verifies that:
1. `persist_inventory` correctly stores CollectionRun, Assets, MetricSummaries, and AssetRelationships.
2. `run_rule_engine` generates valid RuleEvaluation rows matching RuleEvaluationResult schema.
3. persisted assets pass GET /api/v1/assets schema validation cleanly.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import select

CORE_API = Path(__file__).resolve().parents[2]
REPO_ROOT = Path(__file__).resolve().parents[4]
for p in (str(CORE_API), str(REPO_ROOT / "packages")):
    if p not in sys.path:
        sys.path.insert(0, p)

from db import models  # noqa: E402
from schemas.api.assets import AssetItem  # noqa: E402
from schemas.assets import (  # noqa: E402
    AssetInventory,
    Ec2Asset,
    MetricSummary,
    OpenPort,
    SecurityGroupAsset,
)
from services.collector import persist_inventory  # noqa: E402
from services.rule_engine import run_rule_engine  # noqa: E402

# db/tests/conftest.py 의 픽스처 재사용
from db.tests.conftest import db, pg_engine  # noqa: F401, E402


@pytest.fixture
def mock_inventory() -> AssetInventory:
    now = datetime.now(timezone.utc)
    return AssetInventory(
        account_id="123456789012",
        region="ap-northeast-2",
        mode="localstack",
        collected_at=now,
        lookback_days=14,
        period_seconds=3600,
        ec2_instances=[
            Ec2Asset(
                arn="arn:aws:ec2:ap-northeast-2:123456789012:instance/i-idle001",
                instance_id="i-idle001",
                name="dev-idle-ec2",
                instance_type="t3.xlarge",
                state="running",
                region="ap-northeast-2",
                availability_zone="ap-northeast-2a",
                vpc_id="vpc-001",
                subnet_id="subnet-001",
                private_ip="10.0.1.10",
                security_group_ids=["sg-open001"],
                tags={"Name": "dev-idle-ec2", "Environment": "dev"},
                metric_summary=MetricSummary(
                    cpu_datapoints=336,
                    cpu_avg=1.5,
                    cpu_max=3.2,
                    net_in_avg=100.0,
                    net_out_avg=200.0,
                ),
            )
        ],
        security_groups=[
            SecurityGroupAsset(
                arn="arn:aws:ec2:ap-northeast-2:123456789012:security-group/sg-open001",
                group_id="sg-open001",
                name="open-ssh-sg",
                description="SSH open group",
                region="ap-northeast-2",
                vpc_id="vpc-001",
                attached=True,
                open_to_world=[
                    OpenPort(protocol="tcp", from_port=22, to_port=22, ipv6=False)
                ],
            )
        ],
    )


def test_persist_inventory_and_run_rule_engine(db, mock_inventory):
    # 1. persist_inventory 실행
    res = persist_inventory(mock_inventory, db)
    assert res["total"] == 2
    assert res["ec2_count"] == 1
    assert res["sg_count"] == 1

    run_id = res["collection_run_id"]
    assert run_id is not None

    # DB 적재 결과 확인
    assets = db.execute(select(models.Asset)).scalars().all()
    assert len(assets) == 2

    ec2 = next(a for a in assets if a.asset_type.value == "EC2")
    assert ec2.resource_id == "i-idle001"
    # spec에 metric_summary가 섞여 들지 않았는지 확인
    assert "metric_summary" not in ec2.spec
    assert "attributes" not in ec2.spec
    # EC2 tags가 spec에 올바르게 저장되었는지 확인
    assert "tags" in ec2.spec
    assert ec2.spec["tags"] == {"Name": "dev-idle-ec2", "Environment": "dev"}

    # MetricSummary 적재 확인
    ms = db.execute(
        select(models.MetricSummary).where(models.MetricSummary.asset_id == ec2.asset_id)
    ).scalar_one_or_none()
    assert ms is not None
    assert ms.cpu_avg == 1.5
    assert ms.cpu_datapoints == 336

    # AssetRelationship 적재 확인
    rels = db.execute(
        select(models.AssetRelationship).where(
            models.AssetRelationship.source_asset_id == ec2.asset_id
        )
    ).scalars().all()
    assert len(rels) == 1
    assert rels[0].target_arn == "arn:aws:ec2:ap-northeast-2:123456789012:security-group/sg-open001"

    # 2. run_rule_engine 실행
    rule_res = run_rule_engine(db, collection_run_id=run_id)
    assert rule_res["counts"]["COST_CANDIDATE"] == 1
    assert rule_res["counts"]["THREAT"] == 1

    # RuleEvaluation 테이블 저장 확인
    evals = db.execute(select(models.RuleEvaluation)).scalars().all()
    assert len(evals) == 2

    ec2_eval = next(e for e in evals if e.asset_id == ec2.asset_id)
    assert ec2_eval.verdict == "COST_CANDIDATE"
    assert ec2_eval.health_score == 2  # round(1.5) = 2 int
    assert ec2_eval.evaluation_status == "COMPLETED"

    # 3. AssetItem 변환 검증 (routers/assets.py 에서의 schema validation 안전성)
    item = AssetItem.model_validate(
        {
            "arn": ec2.arn,
            "resource_id": ec2.resource_id,
            "asset_type": ec2.asset_type,
            "resource_role": "PRIMARY",
            "name": ec2.name,
            "account_id": ec2.account_id,
            "region": ec2.region,
            "state": ec2.state,
            "spec": ec2.spec,
            "relationships": [
                {"relation_type": r.relation_type, "target_arn": r.target_arn} for r in rels
            ],
            "evaluation_status": ec2_eval.evaluation_status,
            "verdict": ec2_eval.verdict,
            "health_score": ec2_eval.health_score,
            "skip_reason_code": ec2_eval.skip_reason_code,
            "collected_at": ec2.collected_at,
        }
    )
    assert item.arn == ec2.arn
    assert item.verdict.value == "COST_CANDIDATE"


@pytest.fixture
def skip_case_inventory() -> AssetInventory:
    """SKIP 판정(운영 보호)과 비SKIP(후보)을 한 배치에 담아 skip_reason_code 적재를 검증."""
    now = datetime.now(timezone.utc)
    idle = dict(
        cpu_datapoints=336, cpu_avg=1.5, cpu_max=3.2, net_in_avg=100.0, net_out_avg=200.0
    )
    return AssetInventory(
        account_id="123456789012",
        region="ap-northeast-2",
        mode="localstack",
        collected_at=now,
        lookback_days=14,
        period_seconds=3600,
        ec2_instances=[
            Ec2Asset(
                arn="arn:aws:ec2:ap-northeast-2:123456789012:instance/i-prod001",
                instance_id="i-prod001",
                name="prod-api",
                instance_type="t3.large",
                state="running",
                region="ap-northeast-2",
                tags={"Name": "prod-api", "Environment": "production"},  # → SKIP_PROD_PROTECTED
                metric_summary=MetricSummary(**idle),
            ),
            Ec2Asset(
                arn="arn:aws:ec2:ap-northeast-2:123456789012:instance/i-cand001",
                instance_id="i-cand001",
                name="dev-idle",
                instance_type="t3.large",
                state="running",
                region="ap-northeast-2",
                tags={"Name": "dev-idle", "Environment": "dev"},  # → COST_CANDIDATE
                metric_summary=MetricSummary(**idle),
            ),
        ],
        security_groups=[],
    )


def test_skip_reason_code_persisted(db, skip_case_inventory):
    """SKIP 판정은 skip_reason_code가 DB에 적재되고, 비SKIP은 null이어야 한다."""
    res = persist_inventory(skip_case_inventory, db)
    run_id = res["collection_run_id"]

    run_rule_engine(db, collection_run_id=run_id)

    by_rid = {
        a.resource_id: a for a in db.execute(select(models.Asset)).scalars().all()
    }
    evals = {
        e.asset_id: e for e in db.execute(select(models.RuleEvaluation)).scalars().all()
    }

    prod_eval = evals[by_rid["i-prod001"].asset_id]
    assert prod_eval.verdict == "SKIP"
    assert prod_eval.skip_reason_code == "SKIP_PROD_PROTECTED"   # 사유 적재 확인
    assert prod_eval.evaluation_status == "COMPLETED"

    cand_eval = evals[by_rid["i-cand001"].asset_id]
    assert cand_eval.verdict == "COST_CANDIDATE"
    assert cand_eval.skip_reason_code is None                    # 비SKIP은 null

    # 계약(SKIP→코드 필수) 위반 없이 AssetItem 변환되는지 라운드트립 검증
    prod_asset = by_rid["i-prod001"]
    item = AssetItem.model_validate(
        {
            "arn": prod_asset.arn,
            "resource_id": prod_asset.resource_id,
            "asset_type": prod_asset.asset_type,
            "resource_role": "PRIMARY",
            "name": prod_asset.name,
            "account_id": prod_asset.account_id,
            "region": prod_asset.region,
            "state": prod_asset.state,
            "spec": prod_asset.spec,
            "relationships": [],
            "evaluation_status": prod_eval.evaluation_status,
            "verdict": prod_eval.verdict,
            "health_score": prod_eval.health_score,
            "skip_reason_code": prod_eval.skip_reason_code,
            "collected_at": prod_asset.collected_at,
        }
    )
    assert item.verdict.value == "SKIP"
    assert item.skip_reason_code.value == "SKIP_PROD_PROTECTED"

    # update 경로(2회차 run_rule_engine) — 기존 RuleEvaluation 갱신 분기 고정.
    # SKIP→skip_reason_code 유지, 비SKIP→None 유지(비SKIP 시 코드 지우는 줄 회귀 방어).
    run_rule_engine(db, collection_run_id=run_id)
    db.expire_all()
    evals2 = {
        e.asset_id: e for e in db.execute(select(models.RuleEvaluation)).scalars().all()
    }
    assert len(evals2) == len(evals)  # 중복 insert 아님 = update 경로
    assert evals2[by_rid["i-prod001"].asset_id].skip_reason_code == "SKIP_PROD_PROTECTED"
    assert evals2[by_rid["i-cand001"].asset_id].skip_reason_code is None
