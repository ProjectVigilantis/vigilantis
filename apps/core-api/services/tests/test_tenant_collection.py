# ==============================================================================
# [파일 설명]
# 테넌트별 수집과 소유 기록 — collect_and_store_tenants · persist_inventory(tenant_id). (#533 E)
#
# 지키려는 것:
#   1. ACTIVE 테넌트마다 그 테넌트 문맥(tenant_scope) · 등록 리전 · collect_vpc_ids(빈 목록 = 전체)로 수집한다.
#      ACTIVE 가 없거나 자격을 못 얻어도 문맥 없는(프로세스 자격) 호출로 대신하지 않는다.
#   2. 회차 · 자산에 실제 tenant_id 와 테넌트 등록 계정이 적힌다. 자격의 계정이 다르면 적재하지 않는다.
#   3. 한 테넌트의 실패는 다음 테넌트를 막지 않는다. 설정 · 상태는 회차와 새 리전 시작 전에 다시 읽는다.
#      수집 중 정지돼도 이미 시작한 리전의 관측은 적재하고, 아직 시작하지 않은 리전은 수집하지 않는다.
#   4. 소멸 표시 · 메트릭 재사용 · 업서트가 (소유 × 리전) 안에서만 일어난다 — 같은 인스턴스 ID 가 있어도.
#   5. 소속 없는(NULL) legacy 수집은 그대로 NULL 로 적재하고 고객사 행을 건드리지 않는다. 다른 고객사 소유의 같은 ARN 은
#      덮어쓰지 않고(OwnershipConflict), 소속 없던 같은 계정의 행은 테넌트가 이번에 관측했을 때만 이어받는다
#      (못 본 자산 · 과거 회차는 소급하지 않는다).
#   6. 테넌트 문맥은 회차가 끝나면(실패 포함) 복원된다.
# AWS 는 문맥(current_tenant)을 보고 답하는 스텁이다 — 문맥 없는 호출이 오면 테스트가 실패한다.
# ==============================================================================

from __future__ import annotations

import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from botocore.exceptions import ClientError
from sqlalchemy import select

CORE_API = Path(__file__).resolve().parents[2]
REPO_ROOT = Path(__file__).resolve().parents[4]
for _p in (str(CORE_API), str(REPO_ROOT / "packages")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import services.collector as C  # noqa: E402
from db import models  # noqa: E402
from db.repositories import assets as assets_repo  # noqa: E402
from db.repositories import tenants as tenants_repo  # noqa: E402
from schemas.api.tenants import TenantVerificationResult  # noqa: E402
from schemas.assets import AssetInventory, Ec2Asset, MetricSummary  # noqa: E402
from schemas.collections import CollectionRunStatus  # noqa: E402
from services.aws.client import current_tenant, tenant_scope  # noqa: E402

# db·pg_engine 픽스처는 services/tests/conftest.py 가 등록한다

SEOUL = "ap-northeast-2"
TOKYO = "ap-northeast-1"
ACCT_A = "111111111111"
ACCT_B = "222222222222"
LEGACY_ACCT = "000000000001"
NOW = datetime(2026, 10, 28, 1, 0, tzinfo=timezone.utc)


# --- 스텁 AWS --------------------------------------------------------------------


def _instance(iid: str, vpc: str = "vpc-1"):
    return {
        "InstanceId": iid, "InstanceType": "t3.micro", "State": {"Name": "running"},
        "VpcId": vpc, "SubnetId": f"subnet-{vpc}", "SecurityGroups": [],
        "Placement": {"AvailabilityZone": "x"},
    }


class _Pages:
    def __init__(self, pages):
        self._pages = pages

    def paginate(self, **_kw):
        return iter(self._pages)


class _TenantAws:
    """한 테넌트 계정의 AWS — 인스턴스만 가진다. region 별로 같은 목록을 답한다."""

    def __init__(self, world: "_World", account: str):
        self.world, self.account = world, account
        self.instances: list[dict] = []
        self.fail_with: ClientError | None = None
        self.on_call = None  # 호출 중 끼어드는 훅(수집 중 정지 모사)

    def get_paginator(self, name):
        self.world.record("paginate:" + name)
        if self.fail_with is not None:
            raise self.fail_with
        pages = {
            "describe_instances": [{"Reservations": [{"Instances": self.instances}]}],
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
        if self.on_call:
            self.on_call()
        return {"NetworkAcls": []}

    def describe_vpcs(self, Filters=(), **_kw):
        vpcs = {i["VpcId"] for i in self.instances} | {"vpc-1", "vpc-2", "vpc-3"}
        wanted = set(Filters[0]["Values"]) if Filters else None
        return {"Vpcs": [{"VpcId": v} for v in sorted(vpcs) if wanted is None or v in wanted]}

    def get_metric_data(self, MetricDataQueries, **_kw):
        for q in MetricDataQueries:
            self.world.metric_queries.append(
                (current_tenant().tenant_id, q["MetricStat"]["Metric"]["Dimensions"][0]["Value"])
            )
        return {"MetricDataResults": []}


class _World:
    """tenant_id → 그 테넌트 계정의 AWS. 문맥 없는 호출은 실패시킨다(프로세스 자격 대체 금지)."""

    def __init__(self):
        self.by_tenant: dict[str, _TenantAws] = {}
        self.calls: list[tuple[str, str, str]] = []  # (tenant_id, service, region)
        self.contexts: list[str] = []
        self.metric_queries: list[tuple[str, str]] = []
        self.denied: dict[str, ClientError] = {}  # 자격 획득(AssumeRole) 실패를 모사
        self.reported_account: dict[str, str] = {}
        self.sts_errors: dict[tuple[str, str], list[Exception]] = {}  # (tenant, region) → 앞에서부터 소진
        self.sts_attempts: list[tuple[str, str]] = []

    def record(self, what: str):
        self.contexts.append(what)

    def client(self, service: str, region: str):
        access = current_tenant()
        assert access is not None, "테넌트 문맥 없이 AWS 를 불렀다 — 프로세스 자격으로 새어 나갔다"
        if access.tenant_id in self.denied:
            raise self.denied[access.tenant_id]
        self.calls.append((access.tenant_id, service, region))
        return self.by_tenant[access.tenant_id]

    def account_id(self, region: str):
        access = current_tenant()
        assert access is not None
        self.sts_attempts.append((access.tenant_id, region))
        pending = self.sts_errors.get((access.tenant_id, region))
        if pending:
            raise pending.pop(0)
        if access.tenant_id in self.denied:
            raise self.denied[access.tenant_id]
        return self.reported_account.get(access.tenant_id) or self.by_tenant[access.tenant_id].account


def _denied() -> ClientError:
    return ClientError({"Error": {"Code": "AccessDenied", "Message": "assume"}}, "AssumeRole")


@pytest.fixture
def world(monkeypatch, db):
    w = _World()
    monkeypatch.setattr(C, "aws_client", w.client)
    monkeypatch.setattr(C, "_account_id", w.account_id)
    import db.session as db_session

    # 수집은 회차 · 리전마다 세션을 열고 닫는다 — 테스트 트랜잭션 세션 하나로 고정한다(commit 은 savepoint 해제)
    monkeypatch.setattr(db_session, "get_session_factory", lambda: (lambda: db))
    return w


def _tenant(db, world, account, *, regions=(SEOUL,), vpcs=(), active=True, offset=0):
    row = tenants_repo.create_tenant(
        db, name=f"t-{account}", aws_account_id=account, regions=list(regions),
        collect_vpc_ids=list(vpcs), now=NOW + timedelta(seconds=offset),
    )
    if active:
        tenants_repo.record_verification(
            db, row.tenant_id, result=TenantVerificationResult.OK, reason_code=None, now=NOW
        )
    db.commit()
    world.by_tenant[row.tenant_id] = _TenantAws(world, account)
    return row.tenant_id


def _assets(db, tenant_id):
    return {
        a.arn: a
        for a in db.scalars(select(models.Asset).where(models.Asset.tenant_id == tenant_id))
    }


def _runs(db, tenant_id):
    return list(db.scalars(
        select(models.CollectionRun).where(models.CollectionRun.tenant_id == tenant_id)
        .order_by(models.CollectionRun.started_at)
    ))


def _arn(account, iid, region=SEOUL):
    return f"arn:aws:ec2:{region}:{account}:instance/{iid}"


# --- 1) 소유가 적힌다 · 두 고객사 ----------------------------------------------------


def test_two_tenants_persist_own_runs_and_assets_with_identical_resource_ids(db, world):
    a = _tenant(db, world, ACCT_A)
    b = _tenant(db, world, ACCT_B, offset=1)
    world.by_tenant[a].instances = [_instance("i-same"), _instance("i-a-only")]
    world.by_tenant[b].instances = [_instance("i-same")]

    results = C.collect_and_store_tenants()

    assert {r["tenant_id"] for r in results} == {a, b}
    assert set(_assets(db, a)) == {_arn(ACCT_A, "i-same"), _arn(ACCT_A, "i-a-only")}
    assert set(_assets(db, b)) == {_arn(ACCT_B, "i-same")}
    for tid, acct in ((a, ACCT_A), (b, ACCT_B)):
        runs = _runs(db, tid)
        assert [(r.account_id, r.region, r.status) for r in runs] == [
            (acct, SEOUL, CollectionRunStatus.SUCCESS)
        ]
        assert all(x.account_id == acct and x.region == SEOUL for x in _assets(db, tid).values())
    # 회차의 소유와 자산의 마지막 회차가 같은 테넌트다
    for asset in _assets(db, a).values():
        run = db.get(models.CollectionRun, asset.last_collection_run_id)
        assert run.tenant_id == a
    assert db.scalars(select(models.Asset).where(models.Asset.tenant_id.is_(None))).first() is None


def test_each_tenant_is_collected_in_its_own_regions_only(db, world):
    a = _tenant(db, world, ACCT_A, regions=(SEOUL, TOKYO))
    b = _tenant(db, world, ACCT_B, regions=(TOKYO,), offset=1)
    world.by_tenant[a].instances = [_instance("i-1")]
    world.by_tenant[b].instances = [_instance("i-1")]

    C.collect_and_store_tenants()

    regions_by_tenant = {
        t: {r for tid, _s, r in world.calls if tid == t} for t in (a, b)
    }
    assert regions_by_tenant == {a: {SEOUL, TOKYO}, b: {TOKYO}}
    assert {(r.region) for r in _runs(db, a)} == {SEOUL, TOKYO}
    assert {(r.region) for r in _runs(db, b)} == {TOKYO}


def test_collect_vpc_ids_filter_and_empty_means_all(db, world):
    scoped = _tenant(db, world, ACCT_A, vpcs=("vpc-1",))
    open_ = _tenant(db, world, ACCT_B, vpcs=(), offset=1)
    for t in (scoped, open_):
        world.by_tenant[t].instances = [_instance("i-1", "vpc-1"), _instance("i-2", "vpc-2")]

    C.collect_and_store_tenants()

    assert {a.resource_id for a in _assets(db, scoped).values()} == {"i-1"}
    assert {a.resource_id for a in _assets(db, open_).values()} == {"i-1", "i-2"}


# --- 2) 대상 없음 · 자격 실패 · 실패 격리 · 계정 불일치 ----------------------------------


def test_no_active_tenant_collects_nothing_and_never_uses_process_credentials(db, world):
    _tenant(db, world, ACCT_A, active=False)  # PENDING_CONNECTION

    assert C.collect_and_store_tenants() == []

    assert world.calls == []  # 문맥 없는 호출이었다면 world.client 의 assert 가 먼저 터졌다
    assert db.scalars(select(models.CollectionRun)).first() is None


def test_acquisition_failure_is_recorded_for_that_tenant_and_next_tenant_continues(db, world):
    a = _tenant(db, world, ACCT_A)
    b = _tenant(db, world, ACCT_B, offset=1)
    world.by_tenant[b].instances = [_instance("i-b")]
    world.denied[a] = _denied()

    results = C.collect_and_store_tenants()

    by_tenant = {r["tenant_id"]: r for r in results}
    assert by_tenant[a]["status"] == "FAILED"
    assert by_tenant[b].get("status") != "FAILED"
    # 실패한 테넌트의 회차는 그 테넌트 · 등록 계정으로 남고 자산은 없다
    failed = _runs(db, a)
    assert [(r.tenant_id, r.account_id, r.status) for r in failed] == [
        (a, ACCT_A, CollectionRunStatus.FAILED)
    ]
    assert _assets(db, a) == {}
    assert set(_assets(db, b)) == {_arn(ACCT_B, "i-b")}


def test_region_failure_in_one_tenant_does_not_stop_the_next(db, world):
    a = _tenant(db, world, ACCT_A)
    b = _tenant(db, world, ACCT_B, offset=1)
    world.by_tenant[a].fail_with = ClientError({"Error": {"Code": "AccessDenied"}}, "DescribeInstances")
    world.by_tenant[b].instances = [_instance("i-b")]

    C.collect_and_store_tenants()

    assert [r.status for r in _runs(db, a)] == [CollectionRunStatus.FAILED]
    assert [r.status for r in _runs(db, b)] == [CollectionRunStatus.SUCCESS]
    assert set(_assets(db, b)) == {_arn(ACCT_B, "i-b")}


def test_credentials_for_another_account_are_not_stored_as_this_tenant(db, world):
    a = _tenant(db, world, ACCT_A, vpcs=("vpc-1",))
    world.by_tenant[a].instances = [_instance("i-1")]
    world.reported_account[a] = ACCT_B  # 문맥의 자격이 등록 계정이 아닌 곳으로 나갔다

    results = C.collect_and_store_tenants()

    assert results[0]["error"] == "AccountMismatch"
    assert world.calls == []  # 다른 계정의 VPC · 자원을 한 건도 읽지 않았다
    assert _assets(db, a) == {}
    assert [(r.account_id, r.status) for r in _runs(db, a)] == [(ACCT_A, CollectionRunStatus.FAILED)]


# --- 3) 설정 · 상태는 회차마다 새로 읽는다 -------------------------------------------


def test_settings_and_status_are_read_fresh_every_round(db, world):
    a = _tenant(db, world, ACCT_A, regions=(SEOUL,))
    b = _tenant(db, world, ACCT_B, offset=1)
    for t in (a, b):
        world.by_tenant[t].instances = [_instance("i-1")]
    C.collect_and_store_tenants()
    world.calls.clear()

    from schemas.api.tenants import TenantUpdateRequest

    tenants_repo.update_tenant(db, a, TenantUpdateRequest(regions=[TOKYO]), now=NOW)
    tenants_repo.suspend_tenant(db, b, reason="x", now=NOW)
    db.commit()
    C.collect_and_store_tenants()

    assert {(t, r) for t, _s, r in world.calls} == {(a, TOKYO)}  # B 는 정지라 부르지 않는다


def test_suspension_mid_round_keeps_finished_region_and_stops_unstarted_ones(db, world):
    a = _tenant(db, world, ACCT_A, regions=(SEOUL, TOKYO))
    world.by_tenant[a].instances = [_instance("i-1")]

    def suspend():
        tenants_repo.suspend_tenant(db, a, reason="x", now=NOW)
        db.commit()
        world.by_tenant[a].on_call = None

    world.by_tenant[a].on_call = suspend  # 첫 리전 수집 중 정지

    results = C.collect_and_store_tenants()

    # 이미 관측을 마친 리전은 적재되고(ADR-0012: 새 수집만 막는다), 아직 시작하지 않은 리전은 부르지 않는다
    assert [(r["region"], r["status"] if "status" in r else "OK") for r in results] == [
        (SEOUL, "OK"), (TOKYO, "SKIPPED"),
    ]
    assert set(_assets(db, a)) == {_arn(ACCT_A, "i-1")}
    assert {r for _t, _s, r in world.calls} == {SEOUL}
    assert [r.region for r in _runs(db, a)] == [SEOUL]


def test_region_removed_mid_round_is_not_started(db, world):
    from schemas.api.tenants import TenantUpdateRequest

    a = _tenant(db, world, ACCT_A, regions=(SEOUL, TOKYO))
    world.by_tenant[a].instances = [_instance("i-1")]

    def drop_tokyo():
        tenants_repo.update_tenant(db, a, TenantUpdateRequest(regions=[SEOUL]), now=NOW)
        db.commit()
        world.by_tenant[a].on_call = None

    world.by_tenant[a].on_call = drop_tokyo

    results = C.collect_and_store_tenants()

    assert [(r["region"], r.get("error")) for r in results] == [(SEOUL, None), (TOKYO, "REGION_REMOVED")]
    assert {r for _t, _s, r in world.calls} == {SEOUL}


def test_changed_vpc_scope_applies_to_regions_started_after_the_change(db, world):
    from schemas.api.tenants import TenantUpdateRequest

    a = _tenant(db, world, ACCT_A, regions=(SEOUL, TOKYO))
    world.by_tenant[a].instances = [_instance("i-1", "vpc-1"), _instance("i-2", "vpc-2")]

    def narrow():
        tenants_repo.update_tenant(db, a, TenantUpdateRequest(collect_vpc_ids=["vpc-1"]), now=NOW)
        db.commit()
        world.by_tenant[a].on_call = None

    world.by_tenant[a].on_call = narrow

    C.collect_and_store_tenants()

    by_region = {
        r: {x.resource_id for x in _assets(db, a).values() if x.region == r} for r in (SEOUL, TOKYO)
    }
    assert by_region == {SEOUL: {"i-1", "i-2"}, TOKYO: {"i-1"}}


def test_settings_read_failure_of_one_tenant_does_not_stop_the_next(db, world, monkeypatch):
    a = _tenant(db, world, ACCT_A)
    b = _tenant(db, world, ACCT_B, offset=1)
    world.by_tenant[b].instances = [_instance("i-b")]
    original = C._read_tenant_round

    def flaky(tenant_id, session_factory):
        if tenant_id == a:
            raise RuntimeError("db hiccup")
        return original(tenant_id, session_factory)

    monkeypatch.setattr(C, "_read_tenant_round", flaky)

    results = C.collect_and_store_tenants()

    assert {r["tenant_id"]: r.get("status") for r in results}[a] == "FAILED"
    assert set(_assets(db, b)) == {_arn(ACCT_B, "i-b")}


def test_settings_reread_failure_keeps_completed_region_summaries(db, world, monkeypatch):
    a = _tenant(db, world, ACCT_A, regions=(SEOUL, TOKYO))
    b = _tenant(db, world, ACCT_B, offset=1)
    world.by_tenant[a].instances = [_instance("i-a")]
    world.by_tenant[b].instances = [_instance("i-b")]
    original = C._read_tenant_round
    reads = []

    def fail_on_reread(tenant_id, session_factory):
        reads.append(tenant_id)
        if tenant_id == a and reads.count(a) == 2:
            raise RuntimeError("settings reread failed")
        return original(tenant_id, session_factory)

    monkeypatch.setattr(C, "_read_tenant_round", fail_on_reread)

    results = C.collect_and_store_tenants()

    assert [(r["tenant_id"], r.get("region"), r.get("error")) for r in results] == [
        (a, SEOUL, None), (a, None, "RuntimeError"), (b, SEOUL, None),
    ]
    assert results[1] == {"tenant_id": a, "status": "FAILED", "error": "RuntimeError"}
    assert reads == [a, a, b]
    assert [run.region for run in _runs(db, a)] == [SEOUL]
    assert set(_assets(db, a)) == {_arn(ACCT_A, "i-a")}
    assert set(_assets(db, b)) == {_arn(ACCT_B, "i-b")}
    assert {(tenant_id, region) for tenant_id, _service, region in world.calls} == {
        (a, SEOUL), (b, SEOUL),
    }


def test_acquisition_failure_leaves_owned_failed_run_for_every_region(db, world):
    a = _tenant(db, world, ACCT_A, regions=(SEOUL, TOKYO))
    world.denied[a] = _denied()

    C.collect_and_store_tenants()

    assert sorted((r.region, r.tenant_id, r.account_id, r.status) for r in _runs(db, a)) == [
        (TOKYO, a, ACCT_A, CollectionRunStatus.FAILED),
        (SEOUL, a, ACCT_A, CollectionRunStatus.FAILED),
    ]


def _sts_blip() -> Exception:
    from botocore.exceptions import EndpointConnectionError

    return EndpointConnectionError(endpoint_url="https://sts.example")


def test_transient_account_lookup_is_retried_once_per_region_and_isolated(db, world):
    a = _tenant(db, world, ACCT_A, regions=(SEOUL, TOKYO))
    world.by_tenant[a].instances = [_instance("i-1")]
    world.sts_errors[(a, SEOUL)] = [_sts_blip()]  # 서울: 한 번 흔들리고 재시도에서 성공
    world.sts_errors[(a, TOKYO)] = [_sts_blip(), _sts_blip()]  # 도쿄: 재시도까지 실패

    results = C.collect_and_store_tenants()

    assert world.sts_attempts == [(a, SEOUL), (a, SEOUL), (a, TOKYO), (a, TOKYO)]
    assert [(r["region"], r["status"] if r.get("status") == "FAILED" else "OK") for r in results] == [
        (SEOUL, "OK"), (TOKYO, "FAILED"),
    ]
    assert {r.region: (r.tenant_id, r.account_id, r.status) for r in _runs(db, a)} == {
        SEOUL: (a, ACCT_A, CollectionRunStatus.SUCCESS),
        TOKYO: (a, ACCT_A, CollectionRunStatus.FAILED),
    }
    assert set(_assets(db, a)) == {_arn(ACCT_A, "i-1")}


# --- 4) 소멸 · 메트릭 재사용 · 소유 충돌 --------------------------------------------------


def _legacy_inventory(iids, *, account=LEGACY_ACCT, collected_at=NOW):
    return AssetInventory(
        account_id=account, region=SEOUL, mode="localstack", collected_at=collected_at,
        lookback_days=14, period_seconds=3600,
        ec2_instances=[
            Ec2Asset(
                arn=_arn(account, i), instance_id=i, name=i, instance_type="t3.micro", state="running",
                region=SEOUL, availability_zone=f"{SEOUL}a", vpc_id="vpc-1", subnet_id="s", security_group_ids=[],
                metric_summary=MetricSummary(cpu_datapoints=1, cpu_avg=1.0, cpu_max=1.0, net_in_avg=1.0, net_out_avg=1.0),
            )
            for i in iids
        ],
    )


def test_absence_is_marked_only_inside_the_owner(db, world):
    a = _tenant(db, world, ACCT_A)
    b = _tenant(db, world, ACCT_B, offset=1)
    for t in (a, b):
        world.by_tenant[t].instances = [_instance("i-same"), _instance("i-gone")]
    C.persist_inventory(_legacy_inventory(["i-same", "i-legacy"]), db, prune_absent=True)
    db.commit()
    C.collect_and_store_tenants()

    world.by_tenant[a].instances = [_instance("i-same")]  # A 에서만 i-gone 이 사라졌다
    C.collect_and_store_tenants()

    a_assets, b_assets = _assets(db, a), _assets(db, b)
    assert a_assets[_arn(ACCT_A, "i-gone")].absent_since is not None
    assert a_assets[_arn(ACCT_A, "i-same")].absent_since is None
    assert all(x.absent_since is None for x in b_assets.values())  # 같은 ID · 같은 리전의 B
    legacy = db.scalar(select(models.Asset).where(models.Asset.arn == _arn(LEGACY_ACCT, "i-legacy")))
    assert legacy.tenant_id is None and legacy.absent_since is None


def test_legacy_prune_does_not_mark_tenant_assets_absent(db, world):
    a = _tenant(db, world, ACCT_A)
    world.by_tenant[a].instances = [_instance("i-1")]
    C.collect_and_store_tenants()

    C.persist_inventory(_legacy_inventory(["i-x"]), db, prune_absent=True)  # legacy 회차는 A 의 i-1 을 못 본다
    db.commit()

    assert _assets(db, a)[_arn(ACCT_A, "i-1")].absent_since is None
    legacy = db.scalar(select(models.Asset).where(models.Asset.arn == _arn(LEGACY_ACCT, "i-x")))
    assert legacy.tenant_id is None
    run = db.get(models.CollectionRun, legacy.last_collection_run_id)
    assert run.tenant_id is None  # null 호환: 소속 없는 수집은 그대로 NULL


def test_metric_reuse_is_isolated_by_owner(db, world):
    a = _tenant(db, world, ACCT_A)
    b = _tenant(db, world, ACCT_B, active=False, offset=1)
    for t in (a, b):
        world.by_tenant[t].instances = [_instance("i-same")]
    C.collect_and_store_tenants()  # A 만 ACTIVE — A 가 i-same 의 신선한 요약을 갖는다
    assert set(world.metric_queries) == {(a, "i-same")}

    tenants_repo.record_verification(db, b, result=TenantVerificationResult.OK, reason_code=None, now=NOW)
    db.commit()
    world.metric_queries.clear()
    C.collect_and_store_tenants()

    # B 는 A 의 같은 ID 요약을 물려받지 않고 직접 조회한다. A 는 자기 요약을 재사용해 다시 묻지 않는다
    assert set(world.metric_queries) == {(b, "i-same")}


def test_reobserved_legacy_asset_is_adopted_by_the_collecting_tenant(db, world):
    a = _tenant(db, world, ACCT_A)
    world.by_tenant[a].instances = [_instance("i-1")]
    # 소속 없던 과거 수집이 같은 계정의 i-1 · i-unseen 을 남겼다. 이번 테넌트 수집은 i-1 만 본다
    C.persist_inventory(_legacy_inventory(["i-1", "i-unseen"], account=ACCT_A), db)
    db.commit()
    old = {x.arn: x for x in db.scalars(select(models.Asset))}
    old_ids = {arn: x.asset_id for arn, x in old.items()}
    old_run_ids = {x.last_collection_run_id for x in old.values()}

    results = C.collect_and_store_tenants()

    assert results[0].get("status") != "FAILED"
    db.expire_all()
    adopted = _assets(db, a)
    assert set(adopted) == {_arn(ACCT_A, "i-1")}
    assert adopted[_arn(ACCT_A, "i-1")].asset_id == old_ids[_arn(ACCT_A, "i-1")]  # 같은 자산 — 새 행이 아니다
    new_run = db.get(models.CollectionRun, adopted[_arn(ACCT_A, "i-1")].last_collection_run_id)
    assert (new_run.tenant_id, new_run.account_id) == (a, ACCT_A)
    assert [r.status for r in _runs(db, a)] == [CollectionRunStatus.SUCCESS]
    # 못 본 자산 · 과거 회차는 소급되지 않는다
    unseen = db.scalar(select(models.Asset).where(models.Asset.arn == _arn(ACCT_A, "i-unseen")))
    assert unseen.tenant_id is None and unseen.absent_since is None
    for run_id in old_run_ids:
        assert db.get(models.CollectionRun, run_id).tenant_id is None


def test_legacy_asset_of_another_account_is_not_adopted(db, world):
    a = _tenant(db, world, ACCT_A)
    world.by_tenant[a].instances = [_instance("i-1")]
    C.persist_inventory(_legacy_inventory(["i-1"], account=ACCT_A), db)
    db.commit()
    legacy = db.scalar(select(models.Asset).where(models.Asset.arn == _arn(ACCT_A, "i-1")))
    legacy.account_id = ACCT_B  # 저장된 계정이 관측 계정과 다르다 — 채택 근거 없음
    db.commit()

    results = C.collect_and_store_tenants()

    assert results[0]["error"] == "OwnershipConflict"
    db.expire_all()
    assert db.get(models.Asset, legacy.asset_id).tenant_id is None


def test_same_arn_owned_by_another_tenant_is_a_conflict_not_an_overwrite(db, world):
    a = _tenant(db, world, ACCT_A)
    b = _tenant(db, world, ACCT_B, offset=1)
    world.by_tenant[a].instances = [_instance("i-1")]
    C.persist_inventory(_legacy_inventory(["i-1"], account=ACCT_A), db, tenant_id=b)  # 다른 고객사 소유로 저장돼 있다
    db.commit()

    results = C.collect_and_store_tenants()

    assert {r["tenant_id"]: r.get("error") for r in results}[a] == "OwnershipConflict"
    db.expire_all()
    row = db.scalar(select(models.Asset).where(models.Asset.arn == _arn(ACCT_A, "i-1")))
    assert row.tenant_id == b
    assert [r.status for r in _runs(db, a)] == [CollectionRunStatus.FAILED]


def test_upsert_asset_rejects_other_owner(db, make_tenant_rows):
    owner, other = make_tenant_rows
    run = assets_repo.start_collection_run(
        db, account_id=ACCT_A, region=SEOUL, mode="localstack", lookback_days=1, period_seconds=60, tenant_id=owner
    )
    from schemas.api.assets import AssetType

    kw = dict(
        arn=_arn(ACCT_A, "i-1"), asset_type=AssetType.EC2, resource_id="i-1", account_id=ACCT_A, region=SEOUL,
        spec={}, collection_run_id=run.collection_run_id, collected_at=NOW,
    )
    assets_repo.upsert_asset(db, tenant_id=owner, **kw)
    with pytest.raises(assets_repo.OwnershipConflict):
        assets_repo.upsert_asset(db, tenant_id=other, **kw)
    with pytest.raises(assets_repo.OwnershipConflict):
        assets_repo.upsert_asset(db, tenant_id=None, **kw)  # 고객사 행을 소속 없음으로 되돌리지 않는다


@pytest.fixture
def make_tenant_rows(db, world):
    return _tenant(db, world, ACCT_A), _tenant(db, world, ACCT_B, offset=1)


# --- 5) 문맥 복원 ---------------------------------------------------------------------


def test_tenant_context_is_restored_after_rounds_and_failures(db, world):
    a = _tenant(db, world, ACCT_A)
    b = _tenant(db, world, ACCT_B, offset=1)
    world.denied[a] = _denied()
    world.by_tenant[b].instances = [_instance("i-b")]
    assert current_tenant() is None

    C.collect_and_store_tenants()

    assert current_tenant() is None


def test_outer_tenant_scope_is_restored_and_worker_thread_does_not_inherit_it(db, world):
    a = _tenant(db, world, ACCT_A)
    world.by_tenant[a].instances = [_instance("i-a")]
    seen: list[str | None] = []
    original = world.client

    def spy(service, region):
        access = current_tenant()
        seen.append(access.tenant_id if access else None)
        return original(service, region)

    C.aws_client = spy  # world 픽스처가 monkeypatch 로 되돌린다

    from services.aws.client import TenantAccess

    outer = TenantAccess(tenant_id="outer", access_role_arn="arn:aws:iam::000000000009:role/x", external_id="e" * 16)
    with tenant_scope(outer):
        # 스레드 풀 잡은 호출자의 문맥을 물려받지 않는다 — 잡 안에서 세운 테넌트 문맥만 본다
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(C.collect_and_store_tenants).result()
        assert current_tenant() is outer  # 호출자 문맥은 그대로

    assert set(seen) == {a}
