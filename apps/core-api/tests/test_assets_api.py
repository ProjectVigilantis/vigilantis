# ==============================================================================
# [파일 설명]
# GET /api/v1/assets 통합 검증(PostgreSQL) — 수집 상태·판정 채움 규칙·연결관계.
# (Issue #68)
# ==============================================================================

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

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


# --- #231: collection_status 를 리전별 최신 run 의 최악 상태로 산출 ---------------

US_EAST = "us-east-1"


def _run(db, region: str, status: CollectionRunStatus | None, started_at=None):
    """리전에 수집 실행을 1건 남긴다. status 를 주면 그 상태로 마감한다."""
    run = assets_repo.start_collection_run(
        db,
        account_id=ACCOUNT,
        region=region,
        mode="localstack",
        lookback_days=3,
        period_seconds=3600,
    )
    if started_at is not None:
        run.started_at = started_at
    if status is not None:
        assets_repo.finish_collection_run(
            db,
            collection_run_id=run.collection_run_id,
            status=status,
            finished_at=NOW,
        )
    db.flush()
    return run


@pytest.fixture
def two_regions(monkeypatch):
    """수집 대상 리전을 2개로 — 라우터가 설정된 리전만 보므로 함께 지정해야 한다."""
    from config import get_aws_settings

    monkeypatch.setenv("AWS_REGIONS", f"ap-northeast-2,{US_EAST}")
    get_aws_settings.cache_clear()
    yield
    get_aws_settings.cache_clear()


def test_failed_region_is_not_hidden_by_later_success(client_pg, db, two_regions):
    """리전1 FAILED → 리전2 SUCCESS 순서. 전역 최신 1행만 보면 READY 로 실패가 사라졌다."""
    _run(db, "ap-northeast-2", CollectionRunStatus.FAILED, started_at=NOW)
    _run(db, US_EAST, CollectionRunStatus.SUCCESS, started_at=NOW + timedelta(minutes=1))

    body = client_pg.get("/api/v1/assets").json()
    assert body["collection_status"] == "FAILED"


def test_failed_region_surfaces_regardless_of_order(client_pg, db, two_regions):
    """반대 순서(SUCCESS 가 먼저)에서도 같은 답이어야 한다 — 순서에 의존하지 않는다."""
    _run(db, "ap-northeast-2", CollectionRunStatus.SUCCESS, started_at=NOW)
    _run(db, US_EAST, CollectionRunStatus.FAILED, started_at=NOW + timedelta(minutes=1))

    body = client_pg.get("/api/v1/assets").json()
    assert body["collection_status"] == "FAILED"


def test_partial_region_outranks_success(client_pg, db, two_regions):
    _run(db, "ap-northeast-2", CollectionRunStatus.PARTIAL, started_at=NOW)
    _run(db, US_EAST, CollectionRunStatus.SUCCESS, started_at=NOW + timedelta(minutes=1))

    body = client_pg.get("/api/v1/assets").json()
    assert body["collection_status"] == "PARTIAL"


def test_all_regions_success_is_ready(client_pg, db, two_regions):
    _run(db, "ap-northeast-2", CollectionRunStatus.SUCCESS, started_at=NOW)
    _run(db, US_EAST, CollectionRunStatus.SUCCESS, started_at=NOW + timedelta(minutes=1))

    body = client_pg.get("/api/v1/assets").json()
    assert body["collection_status"] == "READY"


def test_only_latest_run_per_region_counts(client_pg, db, two_regions):
    """같은 리전의 옛 FAILED 는 그 리전이 이후 성공하면 더는 화면을 잡지 않는다."""
    _run(db, "ap-northeast-2", CollectionRunStatus.FAILED, started_at=NOW)
    _run(db, "ap-northeast-2", CollectionRunStatus.SUCCESS, started_at=NOW + timedelta(minutes=5))
    _run(db, US_EAST, CollectionRunStatus.SUCCESS, started_at=NOW + timedelta(minutes=6))

    body = client_pg.get("/api/v1/assets").json()
    assert body["collection_status"] == "READY"


def test_unconfigured_region_is_ignored(client_pg, db):
    """수집 대상에서 빠진 리전의 옛 FAILED 가 화면을 영구히 붙잡지 않아야 한다.

    이 테스트는 AWS_REGIONS 를 건드리지 않는다 — 기본값(ap-northeast-2)만 대상이므로
    us-east-1 행은 조회 범위 밖이다.
    """
    _run(db, US_EAST, CollectionRunStatus.FAILED, started_at=NOW)
    _run(db, "ap-northeast-2", CollectionRunStatus.SUCCESS, started_at=NOW + timedelta(minutes=1))

    body = client_pg.get("/api/v1/assets").json()
    assert body["collection_status"] == "READY"


def test_no_run_in_configured_regions_is_not_collected(client_pg, db):
    _run(db, US_EAST, CollectionRunStatus.SUCCESS, started_at=NOW)

    body = client_pg.get("/api/v1/assets").json()
    assert body["collection_status"] == "NOT_COLLECTED"


# --- 순수 함수: 심각도 순서 (DB 불필요) ---


class _Run:
    def __init__(self, status):
        self.status = status


@pytest.mark.parametrize(
    "statuses, expected",
    [
        ([CollectionRunStatus.SUCCESS, CollectionRunStatus.FAILED], CollectionRunStatus.FAILED),
        ([CollectionRunStatus.SUCCESS, CollectionRunStatus.PARTIAL], CollectionRunStatus.PARTIAL),
        ([CollectionRunStatus.PARTIAL, CollectionRunStatus.FAILED], CollectionRunStatus.FAILED),
        # 아직 안 끝난 리전이 있으면 READY 로 확정하지 않는다
        ([CollectionRunStatus.SUCCESS, CollectionRunStatus.IN_PROGRESS], CollectionRunStatus.IN_PROGRESS),
        # 실패는 진행 중보다 위 — 늦게 보여줄 이유가 없다
        ([CollectionRunStatus.IN_PROGRESS, CollectionRunStatus.FAILED], CollectionRunStatus.FAILED),
        ([CollectionRunStatus.SUCCESS], CollectionRunStatus.SUCCESS),
    ],
)
def test_worst_status_picks_the_worst(statuses, expected):
    from routers.assets import _worst_status

    assert _worst_status([_Run(s) for s in statuses]) == expected
