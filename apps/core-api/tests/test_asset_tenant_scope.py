# ==============================================================================
# [파일 설명]
# GET /api/v1/assets 의 테넌트 범위 — 실제 PostgreSQL · 실제 라우터.
#
# 고객사 계정은 자기 테넌트의 자산 · 수집 회차 · 연결만 본다. collection_status · last_collected_at ·
# uncollected 는 같은 테넌트의 회차에서만 산출하므로 다른 테넌트나 소속 없는 기록의 더 새 회차가
# 영향을 주지 못한다. 소속 없는 기록은 플랫폼 범위에서만 보인다.
# 연결의 target_arn 은 자산 FK 가 아닌 문자열이라, 대상이 같은 테넌트의 자산으로 해석될 때만 싣는다.
# ==============================================================================

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from db import models
from db.repositories import assets as assets_repo
from schemas.api.assets import AssetType, RelationType
from schemas.api.auth import UserRole
from schemas.collections import CollectionRunStatus
from tenancy import TenantScope

REGION = "ap-northeast-2"
# 소속 없는(MVP) 기록의 계정 — 테넌트의 AWS 계정과 겹치지 않는다
MVP_ACCOUNT = "000000000001"
T0 = datetime(2026, 10, 28, 1, 0, tzinfo=UTC)
# 수집 계약의 실패 라벨 → 미관측 유형 (schemas.collections.UNOBSERVED_TYPES_BY_FAILURE)
ASG_FAILURE = '{"auto_scaling_groups": "AccessDenied"}'  # → AUTO_SCALING_GROUP
TARGET_GROUP_FAILURE = '{"alb_target_groups": "Throttling"}'  # → ALB_TARGET_GROUP


def _arn(account: str, instance: str) -> str:
    return f"arn:aws:ec2:{REGION}:{account}:instance/{instance}"


def _collected(
    db,
    *,
    tenant_id: str | None,
    account: str,
    instance: str,
    status: CollectionRunStatus,
    finished_at: datetime,
    error_summary: str | None = None,
):
    """수집 회차 하나와 그 회차가 본 EC2 자산 하나 — 둘 다 같은 tenant_id 로 둔다."""
    run = assets_repo.start_collection_run(
        db, account_id=account, region=REGION, mode="localstack", lookback_days=3, period_seconds=3600,
    )
    run.tenant_id = tenant_id
    run.started_at = finished_at - timedelta(minutes=1)
    asset = assets_repo.upsert_asset(
        db, arn=_arn(account, instance), asset_type=AssetType.EC2, resource_id=instance, account_id=account,
        region=REGION, spec={"instance_type": "t3.micro"}, collection_run_id=run.collection_run_id,
        collected_at=finished_at, name=instance, state="running",
    )
    asset.tenant_id = tenant_id
    assets_repo.finish_collection_run(
        db, run.collection_run_id, status, finished_at=finished_at, error_summary=error_summary
    )
    return asset


@pytest.fixture()
def collected(db, make_tenant, monkeypatch):
    """A 는 가장 오래된 성공 회차, B 와 소속 없는 기록은 더 새 회차 — 범위가 새면 A 의 상태 · 시각 · uncollected 가 바뀐다."""
    monkeypatch.setattr("routers.assets._configured_regions", lambda: [REGION])
    a, b, empty = make_tenant(), make_tenant(), make_tenant()
    acct_a, acct_b = (db.get(models.Tenant, t).aws_account_id for t in (a, b))
    seeded = {
        "a": a,
        "b": b,
        "empty": empty,
        "mine": _collected(
            db, tenant_id=a, account=acct_a, instance="i-0a",
            status=CollectionRunStatus.SUCCESS, finished_at=T0,
        ),
        "mine2": _collected(
            db, tenant_id=a, account=acct_a, instance="i-0a2",
            status=CollectionRunStatus.SUCCESS, finished_at=T0 - timedelta(minutes=5),
        ),
        "theirs": _collected(
            db, tenant_id=b, account=acct_b, instance="i-0b", status=CollectionRunStatus.FAILED,
            finished_at=T0 + timedelta(minutes=5), error_summary=ASG_FAILURE,
        ),
        "mvp": _collected(
            db, tenant_id=None, account=MVP_ACCOUNT, instance="i-0c", status=CollectionRunStatus.PARTIAL,
            finished_at=T0 + timedelta(minutes=10), error_summary=TARGET_GROUP_FAILURE,
        ),
    }
    missing_arn = _arn(acct_a, "i-missing")
    seeded["missing_arn"] = missing_arn
    assets_repo.replace_relationships(
        db,
        seeded["mine"].asset_id,
        [
            (RelationType.ATTACHED_TO, seeded["mine2"].arn),
            (RelationType.SECURED_BY, seeded["theirs"].arn),
            (RelationType.PROTECTED_BY, seeded["mvp"].arn),
            (RelationType.USES, missing_arn),
        ],
        collection_run_id=seeded["mine"].last_collection_run_id,
    )
    db.commit()
    return seeded


def _arns(resp) -> set[str]:
    return {item["arn"] for item in resp.json()["items"]}


def _item(resp, arn: str) -> dict:
    return next(item for item in resp.json()["items"] if item["arn"] == arn)


@pytest.mark.parametrize("role", list(UserRole))
def test_tenant_account_sees_only_its_own_assets_and_collection(client_pg, login_as, collected, role):
    login_as(role, tenant_id=collected["a"])
    resp = client_pg.get("/api/v1/assets")
    assert resp.status_code == 200
    body = resp.json()
    assert _arns(resp) == {collected["mine"].arn, collected["mine2"].arn}
    # 다른 테넌트 · 소속 없는 더 새 회차(실패 · 부분 실패)가 내 상태 · 시각 · 미수집 유형에 섞이지 않는다
    assert body["collection_status"] == "READY"
    assert datetime.fromisoformat(body["last_collected_at"].replace("Z", "+00:00")) == T0
    assert body["uncollected"] == []


def test_tenant_without_collection_is_not_collected_even_when_others_are(client_pg, login_as, collected):
    login_as(UserRole.VIEWER, tenant_id=collected["empty"])
    body = client_pg.get("/api/v1/assets").json()
    assert body["collection_status"] == "NOT_COLLECTED"
    assert (body["items"], body["last_collected_at"], body["uncollected"]) == ([], None, [])


def test_tenant_uncollected_comes_from_its_own_runs(client_pg, login_as, collected):
    login_as(UserRole.VIEWER, tenant_id=collected["b"])
    body = client_pg.get("/api/v1/assets").json()
    assert body["collection_status"] == "FAILED"
    assert body["uncollected"] == [{"asset_type": "AUTO_SCALING_GROUP", "reason_code": "AccessDenied"}]
    assert [item["arn"] for item in body["items"]] == [collected["theirs"].arn]


def test_tenant_relationships_keep_only_targets_resolving_to_its_own_assets(client_pg, login_as, collected):
    login_as(UserRole.VIEWER, tenant_id=collected["a"])
    resp = client_pg.get("/api/v1/assets")
    relationships = _item(resp, collected["mine"].arn)["relationships"]
    assert relationships == [{"relation_type": "ATTACHED_TO", "target_arn": collected["mine2"].arn}]


def test_platform_account_sees_every_asset_and_relationship(client_pg, collected):
    # client_pg 기본 세션 = PLATFORM ADMIN — 소속 없는 기록과 해석되지 않는 대상까지 그대로다
    resp = client_pg.get("/api/v1/assets")
    assert {collected[k].arn for k in ("mine", "mine2", "theirs", "mvp")} <= _arns(resp)
    assert resp.json()["collection_status"] == "PARTIAL"
    assert resp.json()["uncollected"] == [{"asset_type": "ALB_TARGET_GROUP", "reason_code": "Throttling"}]
    assert {rel["target_arn"] for rel in _item(resp, collected["mine"].arn)["relationships"]} == {
        collected["mine2"].arn, collected["theirs"].arn, collected["mvp"].arn, collected["missing_arn"],
    }


@pytest.mark.parametrize("regions", [None, [REGION]], ids=["distinct-on", "lateral"])
def test_latest_run_per_region_is_narrowed_before_selection_on_both_paths(db, collected, regions):
    runs = assets_repo.latest_collection_run_per_region(db, regions, scope=TenantScope(collected["a"]))
    assert [(run.tenant_id, run.status) for run in runs] == [(collected["a"], CollectionRunStatus.SUCCESS)]
    assert assets_repo.latest_collection_run_per_region(db, regions, scope=TenantScope(collected["empty"])) == []
    platform = assets_repo.latest_collection_run_per_region(db, regions)
    assert [(run.tenant_id, run.status) for run in platform] == [(None, CollectionRunStatus.PARTIAL)]
