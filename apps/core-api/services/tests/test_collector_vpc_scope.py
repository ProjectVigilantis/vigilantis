# ==============================================================================
# [파일 설명]  담당: 김승철 (Data & Rule Engine)
# 수집 범위 VPC(COLLECT_VPC_IDS) 필터를 회귀로 고정한다.
#
# 단일 계정·같은 리전에 플랫폼 자신(ECS·RDS·ALB 가 든 별도 VPC)을 배포하면 플랫폼 자원이
# 관제 대상으로 수집·판정된다. 포함 목록으로 계정 경계를 흉내 내는 설정이며, 지키려는 것은
# 다음과 같다.
#   1. 빈 값이면 지금과 완전히 같다(리전 전체 수집).
#   2. 값이 있으면 범위 밖 EC2·SG·NACL·ALB TG 가 빠지고, 붙은 EBS 는 인스턴스를 따라가며
#      미연결 EBS 는 남는다. ASG 는 서브넷 → VPC, LT 는 범위 밖 ASG 전용일 때만 빠진다.
#   3. 걸러진 자산은 메트릭·target health 조회 대상에서도 빠진다(호출 비용).
#   4. (DB) 전에 적재된 범위 밖 자산은 소멸 표시되고, 자산 수 스냅샷·관계는 걸러진 결과 기준이다.
#   5. 범위 VPC 가 설정 리전 어디에도 없으면(형식만 맞는 오타) 그 회차는 소멸 표시·자산 수
#      스냅샷을 건너뛰고 PARTIAL + vpc_scope 라벨로 남는다(PR #440 리뷰).
#   6. VpcId 가 빠진 EC2(종료 인스턴스)는 근거 없음이라 유지한다.
#
# AWS 는 스텁 클라이언트로 대신한다 — LocalStack 은 autoscaling·elbv2 가 없어(ADR-0006 §4)
# ASG·TG 경로를 재현할 수 없다.
# ==============================================================================

from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy import select

CORE_API = Path(__file__).resolve().parents[2]
REPO_ROOT = Path(__file__).resolve().parents[4]
for _p in (str(CORE_API), str(REPO_ROOT / "packages")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import services.collector as C  # noqa: E402
from config import CollectorSettings  # noqa: E402

# db·pg_engine 픽스처는 services/tests/conftest.py 가 등록한다

ACCOUNT = "123456789012"
REGION = "ap-northeast-2"
APP_VPC = "vpc-app"
PLAT_VPC = "vpc-plat"
CFG = {"lookback_days": 14, "period_seconds": 3600}


# --- 스텁 AWS --------------------------------------------------------------------


def _instance(iid, vpc, subnet, sg):
    return {
        "InstanceId": iid, "InstanceType": "t3.micro", "State": {"Name": "running"},
        "VpcId": vpc, "SubnetId": subnet, "SecurityGroups": [{"GroupId": sg}],
        "Placement": {"AvailabilityZone": f"{REGION}a"},
    }


def _sg(gid, vpc, world=False):
    perms = (
        [{"IpProtocol": "tcp", "FromPort": 443, "ToPort": 443, "IpRanges": [{"CidrIp": "0.0.0.0/0"}]}]
        if world else []
    )
    return {"GroupId": gid, "GroupName": gid, "Description": gid, "VpcId": vpc, "IpPermissions": perms}


def _volume(vid, attached_to=None):
    return {
        "VolumeId": vid, "VolumeType": "gp3", "Size": 8, "State": "in-use" if attached_to else "available",
        "AvailabilityZone": f"{REGION}a",
        "Attachments": [{"InstanceId": attached_to}] if attached_to else [],
    }


def _asg(name, subnets="", lt=None, members=()):
    g = {
        "AutoScalingGroupARN": f"arn:aws:autoscaling:{REGION}:{ACCOUNT}:autoScalingGroup:u:autoScalingGroupName/{name}",
        "AutoScalingGroupName": name, "MinSize": 1, "MaxSize": 2, "DesiredCapacity": 1,
        "Instances": [{"InstanceId": i} for i in members],
        "VPCZoneIdentifier": subnets,
    }
    if lt:
        # 이름을 ID 와 다르게 둔다 — 같으면 _asg_launch_template 의 [0]/[1] 뒤바뀜이 통과한다(PR #440 리뷰).
        g["LaunchTemplate"] = {"LaunchTemplateId": lt, "LaunchTemplateName": f"name-{lt}"}
    return g


def _tg(name, vpc):
    return {
        "TargetGroupArn": f"arn:aws:elasticloadbalancing:{REGION}:{ACCOUNT}:targetgroup/{name}/abc",
        "TargetGroupName": name, "Protocol": "HTTP", "Port": 80, "TargetType": "instance", "VpcId": vpc,
    }


INSTANCES = [
    _instance("i-app", APP_VPC, "subnet-app", "sg-app"),
    _instance("i-plat", PLAT_VPC, "subnet-plat", "sg-plat"),
]
SGS = [_sg("sg-app", APP_VPC), _sg("sg-plat", PLAT_VPC, world=True)]
NACLS = [
    {"NetworkAclId": "acl-app", "VpcId": APP_VPC, "IsDefault": True,
     "Associations": [{"SubnetId": "subnet-app"}]},
    {"NetworkAclId": "acl-plat", "VpcId": PLAT_VPC, "IsDefault": True,
     "Associations": [{"SubnetId": "subnet-plat"}, {"SubnetId": "subnet-plat-empty"}]},
]
VOLUMES = [_volume("vol-app", "i-app"), _volume("vol-plat", "i-plat"), _volume("vol-free")]
LTS = [
    {"LaunchTemplateId": lt, "LaunchTemplateName": f"name-{lt}", "LatestVersionNumber": 1, "DefaultVersionNumber": 1}
    for lt in ("lt-app", "lt-plat", "lt-orphan")
]
ASGS = [
    _asg("asg-app", "subnet-app", lt="lt-app", members=["i-app"]),
    # 인스턴스 0대여도 서브넷(NACL 연결)으로 VPC 를 안다
    _asg("asg-plat", "subnet-plat-empty", lt="lt-plat"),
    _asg("asg-unknown"),  # 서브넷·소속 인스턴스 모두 없음 — 근거 없음 → 유지
]
TGS = [_tg("tg-app", APP_VPC), _tg("tg-plat", PLAT_VPC)]
TARGETS = {TGS[0]["TargetGroupArn"]: "i-app", TGS[1]["TargetGroupArn"]: "i-plat"}


class _Pages:
    def __init__(self, pages):
        self._pages = pages

    def paginate(self, **_kw):
        return iter(self._pages)


class _FakeAws:
    """collect_region 이 부르는 ec2·cloudwatch·autoscaling·elbv2 호출을 한 객체로 흉내 낸다.
    메트릭·target health 를 누구에 대해 물었는지 남겨 범위 밖 조회가 없는지 본다."""

    def __init__(self):
        self.metric_instance_ids: set[str] = set()
        self.health_queried: list[str] = []

    def get_paginator(self, name):
        pages = {
            "describe_instances": [{"Reservations": [{"Instances": INSTANCES}]}],
            "describe_volumes": [{"Volumes": VOLUMES}],
            "describe_launch_templates": [{"LaunchTemplates": LTS}],
            "describe_auto_scaling_groups": [{"AutoScalingGroups": ASGS}],
            "describe_target_groups": [{"TargetGroups": TGS}],
        }
        return _Pages(pages[name])

    def describe_security_groups(self, **_kw):
        return {"SecurityGroups": SGS}

    def describe_network_interfaces(self, **_kw):
        return {"NetworkInterfaces": []}

    def describe_network_acls(self, **_kw):
        return {"NetworkAcls": NACLS}

    def describe_vpcs(self, Filters=(), **_kw):
        wanted = set(Filters[0]["Values"]) if Filters else None
        return {"Vpcs": [{"VpcId": v} for v in (APP_VPC, PLAT_VPC) if wanted is None or v in wanted]}

    def get_metric_data(self, MetricDataQueries, **_kw):
        for q in MetricDataQueries:
            dims = q["MetricStat"]["Metric"]["Dimensions"]
            self.metric_instance_ids.add(dims[0]["Value"])
        return {"MetricDataResults": []}

    def describe_target_health(self, TargetGroupArn):
        self.health_queried.append(TargetGroupArn)
        return {"TargetHealthDescriptions": [{"Target": {"Id": TARGETS[TargetGroupArn], "Port": 80}}]}


@pytest.fixture
def aws(monkeypatch):
    fake = _FakeAws()
    monkeypatch.setattr(C, "aws_client", lambda _svc, _region: fake)
    monkeypatch.setattr(C, "_account_id", lambda _region: ACCOUNT)
    return fake


def _ids(inv):
    return {
        "ec2": {a.instance_id for a in inv.ec2_instances},
        "sg": {a.group_id for a in inv.security_groups},
        "nacl": {a.nacl_id for a in inv.nacls},
        "ebs": {a.volume_id for a in inv.ebs_volumes},
        "lt": {a.launch_template_id for a in inv.launch_templates},
        "asg": {a.name for a in inv.auto_scaling_groups},
        "tg": {a.name for a in inv.alb_target_groups},
    }


# --- 1) 설정 ---------------------------------------------------------------------


def test_setting_empty_means_no_scope():
    assert CollectorSettings(COLLECT_VPC_IDS="").collect_vpc_ids() is None
    assert CollectorSettings(COLLECT_VPC_IDS=" , ").collect_vpc_ids() is None


def test_setting_parses_comma_list():
    s = CollectorSettings(COLLECT_VPC_IDS=" vpc-a , vpc-b,,")
    assert s.collect_vpc_ids() == frozenset({"vpc-a", "vpc-b"})


def test_setting_rejects_non_vpc_id():
    # 오타가 통과하면 그 VPC 자산이 전부 범위 밖으로 걸러져 소멸 표시된다 — 기동에서 막는다.
    with pytest.raises(ValidationError):
        CollectorSettings(COLLECT_VPC_IDS="vpc-a,subnet-b")


# --- 2) 수집 ---------------------------------------------------------------------


def test_no_scope_collects_everything(aws):
    # cfg 에 vpc_scope 가 없거나 None 이면 지금과 같다.
    for cfg in (CFG, {**CFG, "vpc_scope": None}):
        inv = C.collect_region(REGION, cfg=cfg)
        assert _ids(inv) == {
            "ec2": {"i-app", "i-plat"},
            "sg": {"sg-app", "sg-plat"},
            "nacl": {"acl-app", "acl-plat"},
            "ebs": {"vol-app", "vol-plat", "vol-free"},
            "lt": {"lt-app", "lt-plat", "lt-orphan"},
            "asg": {"asg-app", "asg-plat", "asg-unknown"},
            "tg": {"tg-app", "tg-plat"},
        }


def test_apply_vpc_scope_none_is_identity():
    raw = C._RawInventory(INSTANCES, SGS, NACLS, VOLUMES, LTS, ASGS, TGS)
    assert C._apply_vpc_scope(raw, None) is raw


def test_scope_excludes_out_of_vpc_assets(aws):
    inv = C.collect_region(REGION, cfg={**CFG, "vpc_scope": frozenset({APP_VPC})})
    assert _ids(inv) == {
        "ec2": {"i-app"},
        "sg": {"sg-app"},  # 플랫폼 ALB SG(0.0.0.0/0)가 빠져 THREAT 판정에 닿지 않는다
        "nacl": {"acl-app"},
        "ebs": {"vol-app", "vol-free"},  # 붙은 볼륨은 인스턴스를 따라가고, 미연결은 남는다
        "lt": {"lt-app", "lt-orphan"},  # 범위 밖 ASG 전용 LT 만 빠진다
        "asg": {"asg-app", "asg-unknown"},  # 근거 없는 ASG 는 유지
        "tg": {"tg-app"},
    }
    # 범위 밖 자산은 메트릭·target health 조회도 하지 않는다
    assert aws.metric_instance_ids == {"i-app"}
    assert aws.health_queried == [TGS[0]["TargetGroupArn"]]


def test_asg_falls_back_to_member_instances():
    # 서브넷 지도로 못 가르는 ASG 는 소속 인스턴스로 가른다.
    raw = C._RawInventory(
        INSTANCES, SGS, [], [], [],
        [_asg("asg-x", "subnet-unknown", members=["i-plat"]), _asg("asg-y", members=["i-app"])],
        [],
    )
    scoped = C._apply_vpc_scope(raw, frozenset({APP_VPC}))
    assert [g["AutoScalingGroupName"] for g in scoped.asgs] == ["asg-y"]


def test_scope_absent_from_region_yields_empty(aws):
    # 리전별 부재는 정상이다(복수 리전에서 다른 리전에 있음) — 이 리전의 범위 안 자산은 0건이다.
    # cfg 를 직접 넘겨 사전 확인(_check_vpc_scope)을 타지 않는다. 오타 판정은 아래 별도 테스트.
    inv = C.collect_region(REGION, cfg={**CFG, "vpc_scope": frozenset({"vpc-elsewhere"})})
    ids = _ids(inv)
    assert ids["ec2"] == ids["sg"] == ids["nacl"] == ids["tg"] == set()
    assert ids["ebs"] == {"vol-free"}
    assert C.VPC_SCOPE_FAILURE_LABEL not in inv.collector_failures


def test_scope_keeps_instance_without_vpc_id():
    # 종료된 인스턴스는 AWS 응답에서 VpcId·SubnetId 가 빠진다. 범위 밖으로 거르면 범위 안 VPC 에서
    # 종료된 인스턴스가 그 회차에 바로 소멸 표시되어, 설정 유무에 따라 동작이 갈린다 — 유지한다.
    terminated = {k: v for k, v in _instance("i-term", APP_VPC, "subnet-app", "sg-app").items()
                  if k not in ("VpcId", "SubnetId")}
    terminated["State"] = {"Name": "terminated"}
    vol = _volume("vol-term", "i-term")
    raw = C._RawInventory([*INSTANCES, terminated], SGS, NACLS, [*VOLUMES, vol], [], [], [])
    scoped = C._apply_vpc_scope(raw, frozenset({APP_VPC}))
    assert {i["InstanceId"] for i in scoped.instances} == {"i-app", "i-term"}
    assert "vol-term" in {v["VolumeId"] for v in scoped.volumes}


def test_check_vpc_scope(aws):
    assert C._check_vpc_scope(None, [REGION]) is None
    assert C._check_vpc_scope(frozenset({APP_VPC}), [REGION]) is None
    # 하나라도 없으면 문제 — 둘 중 하나만 오타여도 그 VPC 자산이 전량 소멸 표시되기 때문이다
    assert C._check_vpc_scope(frozenset({APP_VPC, "vpc-typo"}), [REGION]) == "VpcScopeNotFound"
    # 리전별 부재는 정상 — 전 리전을 합쳐 본다
    assert C._check_vpc_scope(frozenset({APP_VPC}), [REGION, "us-east-1"]) is None


def test_check_vpc_scope_lookup_failure_fails_closed(monkeypatch):
    # 확인 조회 자체가 실패(AccessDenied 등)해도 "확인 못 함"으로 넘어져 소멸 표시를 막는다.
    from botocore.exceptions import ClientError

    class _Denied:
        def describe_vpcs(self, **_kw):
            raise ClientError({"Error": {"Code": "AccessDenied", "Message": "no"}}, "DescribeVpcs")

    monkeypatch.setattr(C, "aws_client", lambda _svc, _region: _Denied())
    assert C._check_vpc_scope(frozenset({APP_VPC}), [REGION]) == "AccessDenied"


# --- 3) 적재 — 소멸 표시·자산 수·관계 (DB) ---------------------------------------


def test_scope_marks_out_of_scope_rows_absent_and_counts_filtered(aws, db):
    from db import models
    from db.repositories import assets as assets_repo
    from schemas.api.assets import AssetType

    before = C.collect_region(REGION, cfg=CFG)
    C.persist_inventory(before, db, prune_absent=True)

    after = C.collect_region(REGION, cfg={**CFG, "vpc_scope": frozenset({APP_VPC})})
    after = after.model_copy(update={"collected_at": before.collected_at + timedelta(minutes=5)})
    summary = C.persist_inventory(after, db, prune_absent=True)
    db.flush()

    rows = {a.resource_id: a for a in db.execute(select(models.Asset)).scalars()}
    absent = {rid for rid, a in rows.items() if a.absent_since is not None}
    assert absent == {"i-plat", "sg-plat", "acl-plat", "vol-plat", "lt-plat", "asg-plat", "tg-plat"}
    assert summary["absent_marked"] == 7

    # 판정·화면이 읽는 목록에서 빠진다
    live = {a.resource_id for a in assets_repo.list_assets(db, regions=[REGION])}
    assert "sg-plat" not in live and "sg-app" in live

    # 자산 수 스냅샷은 걸러진 결과 기준
    counts = {
        AssetType(c.asset_type): c.count
        for c in db.execute(
            select(models.AssetInventoryCount).where(
                models.AssetInventoryCount.collection_run_id == summary["collection_run_id"]
            )
        ).scalars()
    }
    assert counts == {
        AssetType.EC2: 1, AssetType.SG: 1, AssetType.NACL: 1, AssetType.EBS: 2,
        AssetType.LAUNCH_TEMPLATE: 2, AssetType.AUTO_SCALING_GROUP: 2, AssetType.ALB_TARGET_GROUP: 1,
    }

    # 범위 안 EC2 의 관계는 범위 안 자산만 가리킨다
    targets = set(
        db.execute(
            select(models.AssetRelationship.target_arn).where(
                models.AssetRelationship.source_asset_id == rows["i-app"].asset_id
            )
        ).scalars()
    )
    assert targets and all("plat" not in arn for arn in targets)

    # 범위를 다시 비우면 다음 회차 upsert 가 소멸 표시를 지운다
    again = C.collect_region(REGION, cfg=CFG)
    again = again.model_copy(update={"collected_at": after.collected_at + timedelta(minutes=5)})
    C.persist_inventory(again, db, prune_absent=True)
    db.flush()
    assert all(a.absent_since is None for a in db.execute(select(models.Asset)).scalars())


def test_scope_typo_skips_absence_marking_and_counts(aws, db):
    """형식은 맞고 존재하지 않는 VPC 가 적힌 회차 — 소멸 표시·자산 수 스냅샷을 건너뛰고
    PARTIAL + vpc_scope 라벨로 남는다. 누가 "경고 후 진행"으로 바꾸면 전 인벤토리가 소멸 표시돼
    여기서 걸린다(PR #440 리뷰: 김승철)."""
    from db import models
    from schemas.collections import CollectionRunStatus

    before = C.collect_region(REGION, cfg=CFG)
    C.persist_inventory(before, db, prune_absent=True)

    scope = frozenset({"vpc-0123456789abcdef1"})
    cfg = {**CFG, "vpc_scope": scope, "vpc_scope_error": C._check_vpc_scope(scope, [REGION])}
    after = C.collect_region(REGION, cfg=cfg)
    after = after.model_copy(update={"collected_at": before.collected_at + timedelta(minutes=5)})
    summary = C.persist_inventory(after, db, prune_absent=True)
    db.flush()

    assert summary["collector_failures"] == {C.VPC_SCOPE_FAILURE_LABEL: "VpcScopeNotFound"}
    assert summary["absent_marked"] == 0
    assert all(a.absent_since is None for a in db.execute(select(models.Asset)).scalars())
    run = db.get(models.CollectionRun, summary["collection_run_id"])
    assert run.status == CollectionRunStatus.PARTIAL
    assert not db.execute(
        select(models.AssetInventoryCount).where(
            models.AssetInventoryCount.collection_run_id == summary["collection_run_id"]
        )
    ).scalars().all()

