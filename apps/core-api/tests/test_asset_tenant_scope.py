# ==============================================================================
# [파일 설명]  담당: 김승철 (API 계약 · BE)
# 자산 목록의 테넌트 범위 (Post-MVP T-4 2단계 C · #533) — 실제 PostgreSQL · 실제 라우터.
#
# 계약: packages/schemas/api/tenants.py 머리말 — 자산 목록도 범위 밖은 빠진다(ID 로 여는 경로가 없어 404 자리는
# 없다). 세 필드(collection_status · items · last_collected_at)는 같은 범위로 좁힌다 — 남의 테넌트 수집 회차가
# 내 수집 상태 · 마지막 수집 시각을 바꾸지 않는다. 테넌트 없는 MVP 기록은 플랫폼만 본다.
# ==============================================================================

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from db.repositories import assets as assets_repo
from schemas.api.assets import AssetType
from schemas.api.auth import UserRole
from schemas.collections import CollectionRunStatus
from tenancy import TenantScope

REGION = "ap-northeast-2"
ACCOUNT = "123456789012"
T0 = datetime(2026, 10, 28, 1, 0, tzinfo=UTC)


def _collected(db, *, tenant_id: str | None, instance: str, status: CollectionRunStatus, finished_at: datetime):
    """수집 회차 하나와 그 회차가 본 EC2 자산 하나 — tenant_id 를 그 테넌트로 둔다."""
    run = assets_repo.start_collection_run(
        db, account_id=ACCOUNT, region=REGION, mode="localstack", lookback_days=3, period_seconds=3600,
    )
    run.tenant_id = tenant_id
    run.started_at = finished_at - timedelta(minutes=1)
    asset = assets_repo.upsert_asset(
        db, arn=f"arn:aws:ec2:{REGION}:{ACCOUNT}:instance/{instance}", asset_type=AssetType.EC2,
        resource_id=instance, account_id=ACCOUNT, region=REGION, spec={"instance_type": "t3.micro"},
        collection_run_id=run.collection_run_id, collected_at=finished_at, name=instance, state="running",
    )
    asset.tenant_id = tenant_id
    assets_repo.finish_collection_run(db, run.collection_run_id, status, finished_at=finished_at)
    return asset


@pytest.fixture()
def collected(db, make_tenant, monkeypatch):
    """A 는 성공 회차, B 는 더 늦은 실패 회차, MVP 는 가장 늦은 성공 회차 — 범위가 새면 A 의 상태 · 시각이 바뀐다."""
    monkeypatch.setattr("routers.assets._configured_regions", lambda: [REGION])
    a, b, empty = make_tenant(), make_tenant(), make_tenant()
    seeded = {
        "a": a, "b": b, "empty": empty,
        "mine": _collected(db, tenant_id=a, instance="i-0a", status=CollectionRunStatus.SUCCESS, finished_at=T0),
        "theirs": _collected(db, tenant_id=b, instance="i-0b", status=CollectionRunStatus.FAILED,
                             finished_at=T0 + timedelta(minutes=5)),
        "mvp": _collected(db, tenant_id=None, instance="i-0c", status=CollectionRunStatus.SUCCESS,
                          finished_at=T0 + timedelta(minutes=10)),
    }
    db.commit()
    return seeded


def _arns(resp) -> set[str]:
    return {item["arn"] for item in resp.json()["items"]}


@pytest.mark.parametrize("role", list(UserRole))
def test_tenant_account_sees_only_its_own_assets_and_collection(client_pg, login_as, collected, role):
    login_as(role, tenant_id=collected["a"])
    resp = client_pg.get("/api/v1/assets")
    assert resp.status_code == 200
    body = resp.json()
    assert _arns(resp) == {collected["mine"].arn}
    # B 의 실패 · MVP 의 더 늦은 회차가 A 의 수집 상태와 마지막 수집 시각에 섞이지 않는다
    assert body["collection_status"] == "READY"
    assert datetime.fromisoformat(body["last_collected_at"].replace("Z", "+00:00")) == T0


def test_tenant_without_collection_is_not_collected_even_when_others_are(client_pg, login_as, collected):
    login_as(UserRole.VIEWER, tenant_id=collected["empty"])
    body = client_pg.get("/api/v1/assets").json()
    assert (body["collection_status"], body["items"], body["last_collected_at"]) == ("NOT_COLLECTED", [], None)


@pytest.mark.parametrize("regions", [None, [REGION]], ids=["distinct-on", "lateral"])
def test_latest_run_per_region_is_narrowed_by_scope_on_both_paths(db, collected, regions):
    # 리전 미지정(DISTINCT ON)과 지정(LATERAL) 두 경로가 같은 범위로 좁힌다 — 라우터는 뒤쪽만 탄다
    runs = assets_repo.latest_collection_run_per_region(db, regions, scope=TenantScope(tenant_id=collected["a"]))
    assert [(run.tenant_id, run.status) for run in runs] == [(collected["a"], CollectionRunStatus.SUCCESS)]
    assert assets_repo.latest_collection_run_per_region(db, regions, scope=TenantScope(collected["empty"])) == []


def test_platform_account_sees_every_asset(client_pg, collected):
    # client_pg 기본 세션 = PLATFORM ADMIN — 리전별 최신 회차(MVP 성공)와 전 자산이 그대로다
    resp = client_pg.get("/api/v1/assets")
    assert {collected[k].arn for k in ("mine", "theirs", "mvp")} <= _arns(resp)
    assert resp.json()["collection_status"] == "READY"
