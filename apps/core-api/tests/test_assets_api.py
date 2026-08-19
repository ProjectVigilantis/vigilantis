# ==============================================================================
# [파일 설명]
# GET /api/v1/assets 통합 검증(PostgreSQL) — 수집 상태·판정 채움 규칙·연결관계.
# (Issue #68)
# ==============================================================================

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from schemas.api.assets import AssetType, RelationType
from schemas.collections import CollectionRunStatus
from schemas.rules import RuleEvaluationResult

from db.repositories import assets as assets_repo

NOW = datetime(2026, 8, 19, 6, 0, 0, tzinfo=timezone.utc)
ACCOUNT = "123456789012"
EC2_ARN = f"arn:aws:ec2:ap-northeast-2:{ACCOUNT}:instance/i-0aaa"
SG_ARN = f"arn:aws:ec2:ap-northeast-2:{ACCOUNT}:security-group/sg-0bbb"
NACL_ARN = f"arn:aws:ec2:ap-northeast-2:{ACCOUNT}:network-acl/acl-0ccc"


def test_no_collection_history_returns_not_collected(client_pg):
    response = client_pg.get("/api/v1/assets")
    assert response.status_code == 200
    body = response.json()
    assert body["collection_status"] == "NOT_COLLECTED"
    assert body["last_collected_at"] is None
    assert body["items"] == []


def _seed(db):
    run = assets_repo.start_collection_run(
        db,
        account_id=ACCOUNT,
        region="ap-northeast-2",
        mode="localstack",
        lookback_days=3,
        period_seconds=3600,
    )
    ec2 = assets_repo.upsert_asset(
        db,
        arn=EC2_ARN,
        asset_type=AssetType.EC2,
        resource_id="i-0aaa",
        account_id=ACCOUNT,
        region="ap-northeast-2",
        spec={"instance_type": "t3.xlarge", "availability_zone": "ap-northeast-2a"},
        collection_run_id=run.collection_run_id,
        collected_at=NOW,
        name="seed-idle",
        state="running",
    )
    assets_repo.upsert_asset(
        db,
        arn=SG_ARN,
        asset_type=AssetType.SG,
        resource_id="sg-0bbb",
        account_id=ACCOUNT,
        region="ap-northeast-2",
        spec={"attached": True, "open_to_world": []},
        collection_run_id=run.collection_run_id,
        collected_at=NOW,
    )
    assets_repo.upsert_asset(
        db,
        arn=NACL_ARN,
        asset_type=AssetType.NACL,
        resource_id="acl-0ccc",
        account_id=ACCOUNT,
        region="ap-northeast-2",
        spec={"is_default": False},
        collection_run_id=run.collection_run_id,
        collected_at=NOW,
    )
    assets_repo.replace_relationships(
        db,
        ec2.asset_id,
        [(RelationType.SECURED_BY, SG_ARN)],
        collection_run_id=run.collection_run_id,
    )
    assets_repo.add_rule_evaluation(
        db,
        RuleEvaluationResult(
            asset_arn=EC2_ARN,
            collection_run_id=run.collection_run_id,
            evaluation_status="COMPLETED",
            verdict="COST_CANDIDATE",
            health_score=35,
            skip_reason_code=None,
            reason="CPU 평균이 임계값 미만",
            evaluated_at=NOW,
        ),
    )
    assets_repo.finish_collection_run(
        db,
        run.collection_run_id,
        CollectionRunStatus.SUCCESS,
        finished_at=NOW + timedelta(minutes=1),
    )


def test_assets_assemble_latest_evaluation_and_relationships(client_pg, db):
    _seed(db)
    response = client_pg.get("/api/v1/assets")
    assert response.status_code == 200
    body = response.json()
    assert body["collection_status"] == "READY"
    assert body["last_collected_at"] == "2026-08-19T06:01:00Z"

    items = {item["arn"]: item for item in body["items"]}
    assert set(items) == {EC2_ARN, SG_ARN, NACL_ARN}

    ec2 = items[EC2_ARN]
    assert ec2["resource_role"] == "PRIMARY"
    assert ec2["evaluation_status"] == "COMPLETED"
    assert ec2["verdict"] == "COST_CANDIDATE"
    assert ec2["health_score"] == 35
    assert ec2["skip_reason_code"] is None
    assert ec2["spec"]["instance_type"] == "t3.xlarge"
    assert ec2["relationships"] == [
        {"relation_type": "SECURED_BY", "target_arn": SG_ARN}
    ]

    sg = items[SG_ARN]  # 판정 대상인데 판정 행 없음 → PENDING
    assert sg["resource_role"] == "PRIMARY"
    assert sg["evaluation_status"] == "PENDING"
    assert sg["verdict"] is None and sg["health_score"] is None

    nacl = items[NACL_ARN]  # 판정 비대상 → NOT_APPLICABLE
    assert nacl["resource_role"] == "RUNBOOK_SUPPORT"
    assert nacl["evaluation_status"] == "NOT_APPLICABLE"
    assert nacl["verdict"] is None and nacl["skip_reason_code"] is None


def test_assets_use_latest_evaluation_when_multiple_runs(client_pg, db):
    _seed(db)
    second = assets_repo.start_collection_run(
        db,
        account_id=ACCOUNT,
        region="ap-northeast-2",
        mode="localstack",
        lookback_days=3,
        period_seconds=3600,
    )
    assets_repo.add_rule_evaluation(
        db,
        RuleEvaluationResult(
            asset_arn=EC2_ARN,
            collection_run_id=second.collection_run_id,
            evaluation_status="COMPLETED",
            verdict="SKIP",
            health_score=None,
            skip_reason_code="SKIP_LOW_UTIL",
            reason="스파이크 관측",
            evaluated_at=NOW + timedelta(hours=1),
        ),
    )
    response = client_pg.get("/api/v1/assets")
    body = response.json()
    assert body["collection_status"] == "COLLECTING"  # 최신 실행이 IN_PROGRESS
    ec2 = next(item for item in body["items"] if item["arn"] == EC2_ARN)
    assert ec2["verdict"] == "SKIP"
    assert ec2["skip_reason_code"] == "SKIP_LOW_UTIL"
    assert ec2["health_score"] is None
