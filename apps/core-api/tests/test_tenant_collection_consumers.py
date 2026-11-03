# ==============================================================================
# [파일 설명]
# 생산자 → 소비자 회귀 (#533 E): 실제 collect_and_store_tenants 의 적재 결과가 기존 범위 소비자
# (GET /api/v1/assets 의 목록 · 수집 상태, /metrics/timeseries 의 CloudWatch 대상 선택)에 그대로 먹힌다.
# test_asset_tenant_scope.py 는 tenant_id 를 손으로 붙여 시드하므로 수집 배선은 이 파일이 증명한다.
# AWS 는 테넌트 문맥을 보고 답하는 스텁이다(문맥 없는 호출은 실패). 같은 인스턴스 ID 를 두 고객사가 갖는다.
# ==============================================================================

from __future__ import annotations

import pytest
from db import models
from schemas.api.auth import PlatformRole, UserRole
from services import collector as C
from services.aws.client import current_tenant

REGION = "ap-northeast-2"


class _Pages:
    def __init__(self, pages):
        self._pages = pages

    def paginate(self, **_kw):
        return iter(self._pages)


class _Aws:
    def __init__(self, instance_ids):
        self.instance_ids = instance_ids

    def get_paginator(self, name):
        instances = [
            {
                "InstanceId": i, "InstanceType": "t3.micro", "State": {"Name": "running"}, "VpcId": "vpc-1",
                "SubnetId": "s", "SecurityGroups": [], "Placement": {"AvailabilityZone": "x"},
            }
            for i in self.instance_ids
        ]
        pages = {
            "describe_instances": [{"Reservations": [{"Instances": instances}]}],
            "describe_volumes": [{"Volumes": []}],
            "describe_launch_templates": [{"LaunchTemplates": []}],
            "describe_auto_scaling_groups": [{"AutoScalingGroups": []}],
            "describe_target_groups": [{"TargetGroups": []}],
        }
        return _Pages(pages[name])

    def describe_security_groups(self, **_kw):
        return {"SecurityGroups": []}

    def describe_network_interfaces(self, **_kw):
        return {"NetworkInterfaces": []}

    def describe_network_acls(self, **_kw):
        return {"NetworkAcls": []}

    def get_metric_data(self, **_kw):
        return {"MetricDataResults": []}


@pytest.fixture()
def collected(db, make_tenant, monkeypatch):
    a, b = make_tenant(), make_tenant()
    accounts = {t: db.get(models.Tenant, t).aws_account_id for t in (a, b)}
    clients = {a: _Aws(["i-same", "i-a"]), b: _Aws(["i-same"])}

    def client(_service, _region):
        access = current_tenant()
        assert access is not None, "문맥 없는 AWS 호출"
        return clients[access.tenant_id]

    monkeypatch.setattr(C, "aws_client", client)
    monkeypatch.setattr(C, "_account_id", lambda _region: accounts[current_tenant().tenant_id])
    import db.session as db_session

    monkeypatch.setattr(db_session, "get_session_factory", lambda: (lambda: db))
    monkeypatch.setattr("routers.assets._configured_regions", lambda: [REGION])

    results = C.collect_and_store_tenants()
    assert {r["tenant_id"] for r in results} == {a, b}
    return {"a": a, "b": b, "accounts": accounts}


def _arn(account: str, iid: str) -> str:
    return f"arn:aws:ec2:{REGION}:{account}:instance/{iid}"


@pytest.mark.parametrize("who", ["a", "b"])
def test_collected_assets_and_status_reach_the_owning_tenant_only(client_pg, login_as, collected, who):
    login_as(UserRole.VIEWER, tenant_id=collected[who])
    body = client_pg.get("/api/v1/assets").json()

    mine = collected["accounts"][collected[who]]
    assert all(f":{mine}:" in item["arn"] for item in body["items"])
    assert {item["arn"] for item in body["items"]} == (
        {_arn(mine, "i-same"), _arn(mine, "i-a")} if who == "a" else {_arn(mine, "i-same")}
    )
    assert body["collection_status"] == "READY"
    assert body["last_collected_at"] is not None


def test_adopted_legacy_asset_reaches_scoped_assets_status_and_metric_target(
    db, make_tenant, client_pg, login_as, monkeypatch
):
    from datetime import datetime, timezone

    from schemas.assets import AssetInventory, Ec2Asset, MetricSummary

    a = make_tenant()
    account = db.get(models.Tenant, a).aws_account_id
    # 소속 없이 수집돼 있던 같은 계정의 자산 — 테넌트가 다시 관측하면 그 고객사 것이 된다
    C.persist_inventory(
        AssetInventory(
            account_id=account, region=REGION, mode="localstack",
            collected_at=datetime(2026, 10, 28, tzinfo=timezone.utc), lookback_days=14, period_seconds=3600,
            ec2_instances=[Ec2Asset(
                arn=_arn(account, "i-old"), instance_id="i-old", name="i-old", instance_type="t3.micro",
                state="running", region=REGION, availability_zone=f"{REGION}a", vpc_id="vpc-1", subnet_id="s",
                security_group_ids=[],
                metric_summary=MetricSummary(cpu_datapoints=1, cpu_avg=1.0, cpu_max=1.0, net_in_avg=1.0, net_out_avg=1.0),
            )],
        ),
        db,
    )
    db.commit()
    assert db.query(models.Asset).filter_by(arn=_arn(account, "i-old")).one().tenant_id is None

    monkeypatch.setattr(C, "aws_client", lambda _s, _r: _Aws(["i-old"]))
    monkeypatch.setattr(C, "_account_id", lambda _r: account)
    import db.session as db_session

    monkeypatch.setattr(db_session, "get_session_factory", lambda: (lambda: db))
    monkeypatch.setattr("routers.assets._configured_regions", lambda: [REGION])
    C.collect_and_store_tenants()

    login_as(UserRole.VIEWER, tenant_id=a)
    body = client_pg.get("/api/v1/assets").json()
    assert [i["arn"] for i in body["items"]] == [_arn(account, "i-old")]
    assert body["collection_status"] == "READY"

    seen: list = []
    monkeypatch.setattr("routers.metrics.cpu_timeseries", lambda refs, **_k: seen.extend(refs) or [])
    monkeypatch.setattr("routers.metrics.network_timeseries", lambda *a_, **k: [])
    login_as(UserRole.ADMIN, platform_role=PlatformRole.CENTRAL_ADMIN)
    client_pg.get("/api/v1/metrics/timeseries")
    assert [r.access.tenant_id for r in seen if r.access is not None] == [a]


def test_central_admin_sees_both_owners_collections(client_pg, login_as, collected):
    login_as(UserRole.ADMIN, platform_role=PlatformRole.CENTRAL_ADMIN)
    body = client_pg.get("/api/v1/assets").json()
    assert len(body["items"]) == 3
    assert body["collection_status"] == "READY"


def test_metrics_targets_use_the_collected_owner_credentials(client_pg, login_as, collected, monkeypatch):
    seen: list = []

    def capture(refs, **_kw):
        seen.extend(refs)
        return []

    monkeypatch.setattr("routers.metrics.cpu_timeseries", capture)
    monkeypatch.setattr("routers.metrics.network_timeseries", lambda *a, **k: [])
    login_as(UserRole.ADMIN, platform_role=PlatformRole.CENTRAL_ADMIN)

    client_pg.get("/api/v1/metrics/timeseries")

    assert len(seen) == 3
    for ref in seen:
        account = ref.arn.split(":")[4]
        owner = next(t for t in (collected["a"], collected["b"]) if collected["accounts"][t] == account)
        # 같은 인스턴스 ID 라도 대상의 자격은 그 자산을 수집한 고객사의 것이다
        assert ref.access is not None and ref.access.tenant_id == owner
