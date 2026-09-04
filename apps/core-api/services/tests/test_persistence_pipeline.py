"""Persistence pipeline tests for Issue #67.

Verifies that:
1. `persist_inventory` correctly stores CollectionRun, Assets, MetricSummaries, and AssetRelationships.
2. `run_rule_engine` generates valid RuleEvaluation rows matching RuleEvaluationResult schema.
3. persisted assets pass GET /api/v1/assets schema validation cleanly.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import select

CORE_API = Path(__file__).resolve().parents[2]
REPO_ROOT = Path(__file__).resolve().parents[4]
for p in (str(CORE_API), str(REPO_ROOT / "packages")):
    if p not in sys.path:
        sys.path.insert(0, p)

from db import models  # noqa: E402
from schemas.api.assets import AssetItem, AssetType, SkipReasonCode  # noqa: E402
from schemas.assets import (  # noqa: E402
    AlbTargetGroupAsset,
    AssetInventory,
    AutoScalingGroupAsset,
    EbsAsset,
    Ec2Asset,
    LaunchTemplateAsset,
    MetricSummary,
    NaclAsset,
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


def _rt(rel) -> str:
    return getattr(rel.relation_type, "value", rel.relation_type)


def test_nacl_topology_relationship(db):
    """NACL 자산 적재 + EC2→NACL(PROTECTED_BY, subnet 연관) + EC2→SG(SECURED_BY) 동시 산출."""
    now = datetime.now(timezone.utc)
    sg_arn = "arn:aws:ec2:ap-northeast-2:123456789012:security-group/sg-t1"
    nacl_arn = "arn:aws:ec2:ap-northeast-2:123456789012:network-acl/acl-t1"
    inv = AssetInventory(
        account_id="123456789012", region="ap-northeast-2", mode="localstack",
        collected_at=now, lookback_days=14, period_seconds=3600,
        ec2_instances=[
            Ec2Asset(
                arn="arn:aws:ec2:ap-northeast-2:123456789012:instance/i-topo1",
                instance_id="i-topo1", name="topo-ec2", instance_type="t3.large",
                state="running", region="ap-northeast-2", subnet_id="subnet-aaa",
                security_group_ids=["sg-t1"], tags={"Environment": "dev"},
                metric_summary=MetricSummary(cpu_datapoints=336, cpu_avg=1.5, cpu_max=3.0),
            )
        ],
        security_groups=[
            SecurityGroupAsset(arn=sg_arn, group_id="sg-t1", name="sg-t1",
                               region="ap-northeast-2", attached=True, open_to_world=[])
        ],
        nacls=[
            NaclAsset(arn=nacl_arn, nacl_id="acl-t1", region="ap-northeast-2",
                      vpc_id="vpc-1", is_default=True, associated_subnet_ids=["subnet-aaa"])
        ],
    )
    res = persist_inventory(inv, db)
    assert res["nacl_count"] == 1
    assert res["total"] == 3

    # NACL 자산 적재
    nacl = db.execute(
        select(models.Asset).where(models.Asset.asset_type == AssetType.NACL)
    ).scalar_one()
    assert nacl.resource_id == "acl-t1"
    assert nacl.spec["associated_subnet_ids"] == ["subnet-aaa"]
    assert nacl.spec["is_default"] is True

    # EC2 관계 = SECURED_BY(sg) + PROTECTED_BY(nacl) 둘 다
    ec2 = db.execute(
        select(models.Asset).where(models.Asset.asset_type == AssetType.EC2)
    ).scalar_one()
    rels = db.execute(
        select(models.AssetRelationship).where(
            models.AssetRelationship.source_asset_id == ec2.asset_id
        )
    ).scalars().all()
    pairs = {(_rt(r), r.target_arn) for r in rels}
    assert ("SECURED_BY", sg_arn) in pairs
    assert ("PROTECTED_BY", nacl_arn) in pairs

    # AssetItem(NACL) 계약 라운드트립 — NaclSpec + RUNBOOK_SUPPORT + NOT_APPLICABLE
    item = AssetItem.model_validate(
        {
            "arn": nacl.arn, "resource_id": nacl.resource_id, "asset_type": nacl.asset_type,
            "resource_role": "RUNBOOK_SUPPORT", "account_id": nacl.account_id,
            "region": nacl.region, "spec": nacl.spec, "relationships": [],
            "evaluation_status": "NOT_APPLICABLE", "collected_at": nacl.collected_at,
        }
    )
    assert item.asset_type == AssetType.NACL


def test_skip_to_non_skip_clears_reason_code(db, skip_case_inventory):
    """SKIP→비SKIP 전이 시 update 경로가 skip_reason_code를 None으로 초기화하는지 (#109).

    rule_engine.py 의 `... if contract.skip_reason_code else None` 초기화 줄이 유일한
    방어 지점. 그 줄을 제거하면 이 테스트가 실패해야 한다(변이 확인).
    #99 테스트는 동일 입력 2회차라 이 전이 경로를 타지 않았다.
    """
    res = persist_inventory(skip_case_inventory, db)
    run_id = res["collection_run_id"]

    # 1) 1회차 — prod EC2는 SKIP_PROD_PROTECTED
    run_rule_engine(db, collection_run_id=run_id)
    db.expire_all()
    prod = db.execute(
        select(models.Asset).where(models.Asset.resource_id == "i-prod001")
    ).scalar_one()
    ev = db.execute(
        select(models.RuleEvaluation).where(models.RuleEvaluation.asset_id == prod.asset_id)
    ).scalar_one()
    assert ev.verdict == "SKIP"
    assert ev.skip_reason_code == "SKIP_PROD_PROTECTED"

    # 2) 운영 태그만 dev로 변경(같은 run_id 유지 → RuleEvaluation update 경로). 메트릭은 그대로.
    #    spec 을 새 dict 로 재바인딩하므로 SQLAlchemy 속성 계측이 dirty 로 잡는다
    #    (in-place 변경이 아니라 flag_modified 불필요).
    new_tags = {**prod.spec.get("tags", {}), "Environment": "dev"}
    prod.spec = {**prod.spec, "tags": new_tags}
    db.flush()

    # 3) 재판정(update 경로) 후 재조회. flush 로 update 분기의 대입을 DB 에 반영하고,
    #    expire_all 로 identity map 을 비워 ev2 를 DB 행에서 다시 읽는다(왕복 검증).
    #    (db 픽스처가 expire_on_commit=False + commit 없음이라, expire 없이는
    #     ev2 is ev 가 되어 메모리 속성만 읽게 된다 — #109 3단계 지시)
    run_rule_engine(db, collection_run_id=run_id)
    db.flush()
    db.expire_all()
    ev2 = db.execute(
        select(models.RuleEvaluation).where(models.RuleEvaluation.asset_id == prod.asset_id)
    ).scalar_one()

    # 4) 전이: 비SKIP + 이전 사유 코드 초기화(None) — 초기화 줄(else None)이 유일 방어
    assert ev2.verdict == "COST_CANDIDATE"
    assert ev2.skip_reason_code is None


def test_ebs_topology_and_verdict(db):
    """EBS 자산 적재 + EC2→EBS(ATTACHED_TO) 관계 + 판정(#149).

    - 부착 볼륨: rule_engine 이 SKIP/SKIP_ACTIVE, EC2 에 ATTACHED_TO 관계 산출
    - 미부착 볼륨: UNUSED(정리 후보) — EBS 는 NACL 과 달리 판정 대상(_RULE_TARGET_TYPES)
    """
    now = datetime.now(timezone.utc)
    vol_attached = "arn:aws:ec2:ap-northeast-2:123456789012:volume/vol-att1"
    vol_free = "arn:aws:ec2:ap-northeast-2:123456789012:volume/vol-free1"
    inv = AssetInventory(
        account_id="123456789012", region="ap-northeast-2", mode="localstack",
        collected_at=now, lookback_days=14, period_seconds=3600,
        ec2_instances=[
            Ec2Asset(
                arn="arn:aws:ec2:ap-northeast-2:123456789012:instance/i-ebs1",
                instance_id="i-ebs1", name="ebs-ec2", instance_type="t3.large",
                state="running", region="ap-northeast-2", tags={"Environment": "dev"},
                metric_summary=MetricSummary(cpu_datapoints=336, cpu_avg=1.5, cpu_max=3.0),
            )
        ],
        ebs_volumes=[
            EbsAsset(
                arn=vol_attached, volume_id="vol-att1", region="ap-northeast-2",
                volume_type="gp3", size_gib=20, availability_zone="ap-northeast-2a",
                encrypted=True, state="in-use", attached_instance_ids=["i-ebs1"],
            ),
            EbsAsset(
                arn=vol_free, volume_id="vol-free1", region="ap-northeast-2",
                volume_type="gp2", size_gib=8, availability_zone="ap-northeast-2a",
                encrypted=False, state="available", attached_instance_ids=[],
            ),
        ],
    )
    res = persist_inventory(inv, db)
    assert res["ebs_count"] == 2
    assert res["total"] == 3  # EC2 1 + EBS 2

    run_id = res["collection_run_id"]

    # EBS 자산 적재 + spec
    att = db.execute(
        select(models.Asset).where(models.Asset.resource_id == "vol-att1")
    ).scalar_one()
    assert att.asset_type == AssetType.EBS
    assert att.spec["volume_type"] == "gp3"
    assert att.spec["size_gib"] == 20
    assert att.spec["attached_instance_ids"] == ["i-ebs1"]

    # EC2→EBS ATTACHED_TO 관계 (부착 볼륨만)
    ec2 = db.execute(
        select(models.Asset).where(models.Asset.asset_type == AssetType.EC2)
    ).scalar_one()
    rels = db.execute(
        select(models.AssetRelationship).where(
            models.AssetRelationship.source_asset_id == ec2.asset_id
        )
    ).scalars().all()
    pairs = {(_rt(r), r.target_arn) for r in rels}
    assert ("ATTACHED_TO", vol_attached) in pairs
    assert ("ATTACHED_TO", vol_free) not in pairs

    # 판정: 미부착 → UNUSED, 부착 → SKIP/SKIP_ACTIVE
    run_rule_engine(db, collection_run_id=run_id)
    db.flush()

    free = db.execute(
        select(models.Asset).where(models.Asset.resource_id == "vol-free1")
    ).scalar_one()
    ev_free = db.execute(
        select(models.RuleEvaluation).where(models.RuleEvaluation.asset_id == free.asset_id)
    ).scalar_one()
    assert ev_free.evaluation_status == "COMPLETED"
    assert ev_free.verdict == "UNUSED"
    assert ev_free.skip_reason_code is None
    assert ev_free.health_score is None  # EBS 는 health_score 없음

    ev_att = db.execute(
        select(models.RuleEvaluation).where(models.RuleEvaluation.asset_id == att.asset_id)
    ).scalar_one()
    assert ev_att.verdict == "SKIP"
    assert ev_att.skip_reason_code == "SKIP_ACTIVE"

    # AssetItem(EBS) 계약 라운드트립 — EbsSpec + RUNBOOK_SUPPORT + COMPLETED/UNUSED
    item = AssetItem.model_validate(
        {
            "arn": free.arn, "resource_id": free.resource_id, "asset_type": free.asset_type,
            "resource_role": "RUNBOOK_SUPPORT", "account_id": free.account_id,
            "region": free.region, "state": free.state, "spec": free.spec, "relationships": [],
            "evaluation_status": ev_free.evaluation_status, "verdict": ev_free.verdict,
            "skip_reason_code": ev_free.skip_reason_code, "collected_at": free.collected_at,
        }
    )
    assert item.asset_type == AssetType.EBS
    assert item.verdict.value == "UNUSED"


def test_asg_launch_template_topology(db):
    """ASG·Launch Template 적재 + EC2→ASG(MEMBER_OF) + ASG→LT(USES) 관계 (#149).

    - MEMBER_OF: source 는 멤버 EC2 → EC2 관계 루프에서 산출
    - USES: source 는 ASG → ASG 적재 루프에서 산출(EC2 아님)
    - ASG/LT 는 판정 비대상 → NOT_APPLICABLE
    """
    now = datetime.now(timezone.utc)
    lt_arn = "arn:aws:ec2:ap-northeast-2:123456789012:launch-template/lt-t1"
    asg_arn = "arn:aws:autoscaling:ap-northeast-2:123456789012:autoScalingGroup:uuid:autoScalingGroupName/asg-t1"
    inv = AssetInventory(
        account_id="123456789012", region="ap-northeast-2", mode="localstack",
        collected_at=now, lookback_days=14, period_seconds=3600,
        ec2_instances=[
            Ec2Asset(
                arn="arn:aws:ec2:ap-northeast-2:123456789012:instance/i-asg1",
                instance_id="i-asg1", name="asg-member", instance_type="t3.large",
                state="running", region="ap-northeast-2", tags={"Environment": "dev"},
                metric_summary=MetricSummary(cpu_datapoints=336, cpu_avg=1.5, cpu_max=3.0),
            )
        ],
        launch_templates=[
            LaunchTemplateAsset(
                arn=lt_arn, launch_template_id="lt-t1", name="web-lt",
                region="ap-northeast-2", latest_version=3, default_version=1,
            )
        ],
        auto_scaling_groups=[
            AutoScalingGroupAsset(
                arn=asg_arn, name="asg-t1", region="ap-northeast-2",
                min_size=1, max_size=4, desired_capacity=2, health_check_type="EC2",
                instance_ids=["i-asg1"], launch_template_id="lt-t1", launch_template_name="web-lt",
            )
        ],
    )
    res = persist_inventory(inv, db)
    assert res["lt_count"] == 1
    assert res["asg_count"] == 1
    assert res["total"] == 3  # EC2 1 + LT 1 + ASG 1

    # LT/ASG 자산 적재 + spec
    lt = db.execute(
        select(models.Asset).where(models.Asset.asset_type == AssetType.LAUNCH_TEMPLATE)
    ).scalar_one()
    assert lt.resource_id == "lt-t1"
    assert lt.spec["latest_version"] == 3
    asg = db.execute(
        select(models.Asset).where(models.Asset.asset_type == AssetType.AUTO_SCALING_GROUP)
    ).scalar_one()
    assert asg.resource_id == "asg-t1"
    assert asg.spec["desired_capacity"] == 2

    # EC2→ASG MEMBER_OF (source 는 EC2)
    ec2 = db.execute(
        select(models.Asset).where(models.Asset.asset_type == AssetType.EC2)
    ).scalar_one()
    ec2_pairs = {
        (_rt(r), r.target_arn)
        for r in db.execute(
            select(models.AssetRelationship).where(
                models.AssetRelationship.source_asset_id == ec2.asset_id
            )
        ).scalars().all()
    }
    assert ("MEMBER_OF", asg_arn) in ec2_pairs

    # ASG→LT USES (source 는 ASG, EC2 아님)
    asg_pairs = {
        (_rt(r), r.target_arn)
        for r in db.execute(
            select(models.AssetRelationship).where(
                models.AssetRelationship.source_asset_id == asg.asset_id
            )
        ).scalars().all()
    }
    assert ("USES", lt_arn) in asg_pairs

    # AssetItem 계약 라운드트립 — ASG/LT 는 RUNBOOK_SUPPORT + NOT_APPLICABLE
    for asset in (lt, asg):
        item = AssetItem.model_validate(
            {
                "arn": asset.arn, "resource_id": asset.resource_id, "asset_type": asset.asset_type,
                "resource_role": "RUNBOOK_SUPPORT", "name": asset.name,
                "account_id": asset.account_id, "region": asset.region, "spec": asset.spec,
                "relationships": [], "evaluation_status": "NOT_APPLICABLE",
                "collected_at": asset.collected_at,
            }
        )
        assert item.evaluation_status.value == "NOT_APPLICABLE"


def test_degraded_collectors_finish_run_partial(db):
    """degrade(수집 실패 흡수)가 있으면 CollectionRun 이 PARTIAL 로 마감되고,
    LT 없는 ASG 는 USES 관계를 만들지 않는다 (#161 리뷰 ①②)."""
    now = datetime.now(timezone.utc)
    inv = AssetInventory(
        account_id="123456789012", region="ap-northeast-2", mode="aws",
        collected_at=now, lookback_days=14, period_seconds=3600,
        auto_scaling_groups=[
            AutoScalingGroupAsset(
                arn="arn:aws:autoscaling:ap-northeast-2:123456789012:autoScalingGroup:u:autoScalingGroupName/asg-nolt",
                name="asg-nolt", region="ap-northeast-2",
                min_size=0, max_size=2, desired_capacity=0, health_check_type="EC2",
                instance_ids=[], launch_template_id=None,  # LT 없음 → USES 없음
            )
        ],
        # 자산 단위 실패 사유(C4) — 예: 실 AWS AccessDenied 흡수 상황
        collector_failures={"auto_scaling_groups": "AccessDenied"},
    )
    res = persist_inventory(inv, db)
    run_id = res["collection_run_id"]
    assert res["collector_failures"] == {"auto_scaling_groups": "AccessDenied"}

    # 1) degrade(실패 사유)가 있으면 PARTIAL 로 마감 + error_summary(JSON)에 사유 적재 (C4)
    run = db.execute(
        select(models.CollectionRun).where(models.CollectionRun.collection_run_id == run_id)
    ).scalar_one()
    assert run.status.value == "PARTIAL"
    import json as _json
    assert _json.loads(run.error_summary) == {"auto_scaling_groups": "AccessDenied"}

    # 2) LT 없는 ASG 는 USES(및 어떤) 관계도 없다
    asg = db.execute(
        select(models.Asset).where(models.Asset.resource_id == "asg-nolt")
    ).scalar_one()
    rels = db.execute(
        select(models.AssetRelationship).where(
            models.AssetRelationship.source_asset_id == asg.asset_id
        )
    ).scalars().all()
    assert rels == []


def test_alb_target_group_topology(db):
    """ALB Target Group 적재 + EC2→TG(REGISTERED_IN) 관계 (#149).

    - REGISTERED_IN: source 는 등록된 EC2 → EC2 관계 루프에서 산출
    - TG 는 판정 비대상 → NOT_APPLICABLE
    """
    now = datetime.now(timezone.utc)
    tg_arn = "arn:aws:elasticloadbalancing:ap-northeast-2:123456789012:targetgroup/web-tg/abc123"
    inv = AssetInventory(
        account_id="123456789012", region="ap-northeast-2", mode="aws",
        collected_at=now, lookback_days=14, period_seconds=3600,
        ec2_instances=[
            Ec2Asset(
                arn="arn:aws:ec2:ap-northeast-2:123456789012:instance/i-tg1",
                instance_id="i-tg1", name="tg-member", instance_type="t3.large",
                state="running", region="ap-northeast-2", tags={"Environment": "dev"},
                metric_summary=MetricSummary(cpu_datapoints=336, cpu_avg=1.5, cpu_max=3.0),
            )
        ],
        alb_target_groups=[
            AlbTargetGroupAsset(
                arn=tg_arn, name="web-tg", region="ap-northeast-2",
                protocol="HTTP", port=80, target_type="instance",
                health_check_path="/health", target_instance_ids=["i-tg1"],
            )
        ],
    )
    res = persist_inventory(inv, db)
    assert res["tg_count"] == 1
    assert res["total"] == 2  # EC2 1 + TG 1

    # TG 자산 적재 + spec
    tg = db.execute(
        select(models.Asset).where(models.Asset.asset_type == AssetType.ALB_TARGET_GROUP)
    ).scalar_one()
    assert tg.resource_id == "web-tg"
    assert tg.spec["protocol"] == "HTTP"
    assert tg.spec["target_type"] == "instance"

    # EC2→TG REGISTERED_IN (source 는 EC2)
    ec2 = db.execute(
        select(models.Asset).where(models.Asset.asset_type == AssetType.EC2)
    ).scalar_one()
    ec2_pairs = {
        (_rt(r), r.target_arn)
        for r in db.execute(
            select(models.AssetRelationship).where(
                models.AssetRelationship.source_asset_id == ec2.asset_id
            )
        ).scalars().all()
    }
    assert ("REGISTERED_IN", tg_arn) in ec2_pairs

    # AssetItem 계약 라운드트립 — TG 는 RUNBOOK_SUPPORT + NOT_APPLICABLE
    item = AssetItem.model_validate(
        {
            "arn": tg.arn, "resource_id": tg.resource_id, "asset_type": tg.asset_type,
            "resource_role": "RUNBOOK_SUPPORT", "name": tg.name,
            "account_id": tg.account_id, "region": tg.region, "spec": tg.spec,
            "relationships": [], "evaluation_status": "NOT_APPLICABLE",
            "collected_at": tg.collected_at,
        }
    )
    assert item.evaluation_status.value == "NOT_APPLICABLE"


def test_registered_in_deduped_no_unique_violation(db):
    """같은 EC2가 한 TG 에 중복 등록돼도 REGISTERED_IN 은 1건만 적재된다 (#165 리뷰 ①).

    중복이 걸러지지 않으면 (source, relation, target) unique constraint 로 수집 전체가
    롤백된다. persist 의 rel_items 중복 제거가 방어선.
    """
    now = datetime.now(timezone.utc)
    tg_arn = "arn:aws:elasticloadbalancing:ap-northeast-2:123456789012:targetgroup/dup-tg/x1"
    inv = AssetInventory(
        account_id="123456789012", region="ap-northeast-2", mode="aws",
        collected_at=now, lookback_days=14, period_seconds=3600,
        ec2_instances=[
            Ec2Asset(
                arn="arn:aws:ec2:ap-northeast-2:123456789012:instance/i-dup",
                instance_id="i-dup", name="dup", instance_type="t3.large",
                state="running", region="ap-northeast-2", tags={"Environment": "dev"},
                metric_summary=MetricSummary(cpu_datapoints=336, cpu_avg=1.5, cpu_max=3.0),
            )
        ],
        alb_target_groups=[
            AlbTargetGroupAsset(
                arn=tg_arn, name="dup-tg", region="ap-northeast-2",
                protocol="HTTP", port=80, target_type="instance",
                target_instance_ids=["i-dup", "i-dup"],  # 멀티포트 중복 모사
            )
        ],
    )
    res = persist_inventory(inv, db)  # unique 위반 없이 통과해야 한다
    assert res["tg_count"] == 1

    ec2 = db.execute(
        select(models.Asset).where(models.Asset.resource_id == "i-dup")
    ).scalar_one()
    reg = db.execute(
        select(models.AssetRelationship).where(
            models.AssetRelationship.source_asset_id == ec2.asset_id,
            models.AssetRelationship.relation_type == "REGISTERED_IN",
        )
    ).scalars().all()
    assert len(reg) == 1
    assert reg[0].target_arn == tg_arn


@pytest.fixture
def six_skip_inventory() -> AssetInventory:
    """SkipReasonCode 6종을 한 배치로 유발 — DB 실적재 검증용 (C2 + #276).

    EC2 4종(INSUFFICIENT_DATA·PROD_PROTECTED·LOW_UTIL·ACTIVE) + SG 1종(WHITELISTED=default)
    + EBS 1종(UNSUPPORTED_STATE=creating·미부착).
    evaluate_ec2 우선순위: 데이터부족 → prod → (idle&spike)LOW_UTIL / idle→후보 → ACTIVE.
    """
    now = datetime.now(timezone.utc)
    return AssetInventory(
        account_id="123456789012", region="ap-northeast-2", mode="localstack",
        collected_at=now, lookback_days=14, period_seconds=3600,
        ec2_instances=[
            Ec2Asset(  # 관측치 부족(< MIN_DATAPOINTS 48) → SKIP_INSUFFICIENT_DATA
                arn="arn:aws:ec2:ap-northeast-2:123456789012:instance/i-insufficient",
                instance_id="i-insufficient", name="new-inst", instance_type="t3.large",
                state="running", region="ap-northeast-2", tags={"Environment": "dev"},
                metric_summary=MetricSummary(cpu_datapoints=10, cpu_avg=1.0, cpu_max=2.0),
            ),
            Ec2Asset(  # 운영 태그 → SKIP_PROD_PROTECTED
                arn="arn:aws:ec2:ap-northeast-2:123456789012:instance/i-prod",
                instance_id="i-prod", name="prod-api", instance_type="t3.large",
                state="running", region="ap-northeast-2", tags={"Environment": "production"},
                metric_summary=MetricSummary(cpu_datapoints=336, cpu_avg=1.5, cpu_max=3.0),
            ),
            Ec2Asset(  # 저활성 평균 + 스파이크 최대(≥ SPIKE_CPU_MAX 40) → SKIP_LOW_UTIL
                arn="arn:aws:ec2:ap-northeast-2:123456789012:instance/i-spike",
                instance_id="i-spike", name="spiky", instance_type="t3.large",
                state="running", region="ap-northeast-2", tags={"Environment": "dev"},
                metric_summary=MetricSummary(cpu_datapoints=336, cpu_avg=1.5, cpu_max=91.0),
            ),
            Ec2Asset(  # 정상 가동(평균 ≥ IDLE_CPU_AVG 5) → SKIP_ACTIVE
                arn="arn:aws:ec2:ap-northeast-2:123456789012:instance/i-active",
                instance_id="i-active", name="busy", instance_type="t3.large",
                state="running", region="ap-northeast-2", tags={"Environment": "dev"},
                metric_summary=MetricSummary(cpu_datapoints=336, cpu_avg=50.0, cpu_max=60.0),
            ),
        ],
        security_groups=[
            SecurityGroupAsset(  # default SG → SKIP_WHITELISTED
                arn="arn:aws:ec2:ap-northeast-2:123456789012:security-group/sg-default",
                group_id="sg-default", name="default", description="default VPC SG",
                region="ap-northeast-2", attached=True, open_to_world=[],
            ),
        ],
        ebs_volumes=[
            EbsAsset(  # available/in-use 아닌 전이 상태(creating·미부착) → SKIP_UNSUPPORTED_STATE (#276)
                arn="arn:aws:ec2:ap-northeast-2:123456789012:volume/vol-creating",
                volume_id="vol-creating", region="ap-northeast-2", volume_type="gp3",
                size_gib=100, state="creating", attached_instance_ids=[],
            ),
        ],
    )


def test_all_six_skip_reason_codes_persisted(db, six_skip_inventory):
    """SkipReasonCode 6종이 full 파이프라인(persist → run_rule_engine)으로 RuleEvaluation 에
    실제 적재되는지 (C2 + #276). rule_engine 판정 → DB 적재 경로로 검증된 건 기존엔 PROD_PROTECTED·
    ACTIVE 2종뿐이었다(나머지는 evaluate_* 직접 호출 golden 테스트로만 대조)."""
    res = persist_inventory(six_skip_inventory, db)
    run_rule_engine(db, collection_run_id=res["collection_run_id"])
    db.flush()
    db.expire_all()  # identity map 비우고 DB 행에서 다시 읽는다 — 왕복 검증(#109 선례)

    expected = {
        "i-insufficient": "SKIP_INSUFFICIENT_DATA",
        "i-prod": "SKIP_PROD_PROTECTED",
        "i-spike": "SKIP_LOW_UTIL",
        "i-active": "SKIP_ACTIVE",
        "sg-default": "SKIP_WHITELISTED",
        "vol-creating": "SKIP_UNSUPPORTED_STATE",
    }
    persisted = {}
    for resource_id, exp_code in expected.items():
        asset = db.execute(
            select(models.Asset).where(models.Asset.resource_id == resource_id)
        ).scalar_one()
        ev = db.execute(
            select(models.RuleEvaluation).where(models.RuleEvaluation.asset_id == asset.asset_id)
        ).scalar_one()
        assert ev.verdict == "SKIP", resource_id
        assert ev.skip_reason_code == exp_code, resource_id
        persisted[resource_id] = ev.skip_reason_code

    # 6종 전량 = SkipReasonCode enum 전량. 7번째 코드가 추가되면 깨져서 "6종" 전제가 낡았음을 알린다.
    assert set(persisted.values()) == {c.value for c in SkipReasonCode}


# --- #255: 메트릭 재사용 창·조회 -------------------------------------------------


def test_reused_summary_is_persisted_with_original_window(db, mock_inventory):
    """재사용한 요약은 이번 회차 시각이 아니라 **원본 창**으로 적재돼야 한다.

    collected_at 을 창의 끝으로 적으면 받아오지도 않은 최근 구간을 관측한 것처럼 남는다.
    """
    original_end = mock_inventory.collected_at - timedelta(minutes=45)
    inv = mock_inventory.model_copy(update={"metrics_window_end": original_end})

    persist_inventory(inv, db)

    asset = db.execute(
        select(models.Asset).where(models.Asset.resource_id == "i-idle001")
    ).scalar_one()
    row = db.execute(
        select(models.MetricSummary).where(models.MetricSummary.asset_id == asset.asset_id)
    ).scalar_one()
    assert row.window_end == original_end
    assert row.window_start == original_end - timedelta(days=inv.lookback_days)
    # 자산 자체의 수집 시각은 이번 회차 그대로 — 인벤토리는 방금 받았다
    assert asset.collected_at == inv.collected_at


def test_fresh_lookup_returns_recent_rows_and_skips_stale(db, mock_inventory):
    """조회는 입자 안(cutoff 이후)에 수집된 요약만 돌려준다 — 재사용 판단의 근거."""
    from db.repositories import assets as assets_repo

    persist_inventory(mock_inventory, db)

    cutoff = mock_inventory.collected_at - timedelta(seconds=mock_inventory.period_seconds)
    window_end, fresh = assets_repo.fresh_ec2_metric_summaries(
        db, region="ap-northeast-2", not_older_than=cutoff
    )
    assert set(fresh) == {"i-idle001"}
    assert fresh["i-idle001"].cpu_avg == 1.5
    assert fresh["i-idle001"].cpu_datapoints == 336
    assert window_end == mock_inventory.collected_at

    # 입자를 지난 시점을 기준으로 잡으면 같은 행이 더는 재사용 대상이 아니다
    stale_cutoff = mock_inventory.collected_at + timedelta(seconds=1)
    assert assets_repo.fresh_ec2_metric_summaries(
        db, region="ap-northeast-2", not_older_than=stale_cutoff
    ) == (None, {})


def test_fresh_lookup_is_scoped_to_region(db, mock_inventory):
    """다른 리전의 요약을 끌어다 쓰면 안 된다 — 리전별로 독립 수집한다(C4)."""
    from db.repositories import assets as assets_repo

    persist_inventory(mock_inventory, db)

    cutoff = mock_inventory.collected_at - timedelta(seconds=mock_inventory.period_seconds)
    assert assets_repo.fresh_ec2_metric_summaries(
        db, region="us-east-1", not_older_than=cutoff
    ) == (None, {})


def test_reuse_does_not_extend_freshness_across_cycles(db, mock_inventory):
    """직접 조회 → 재사용 → 최초 조회로부터 period 경과 → 재조회. (PR #257 리뷰)

    재사용한 요약도 회차마다 새 행으로 복사되고 collected_at 에는 그 회차 시각이 찍힌다.
    신선도를 collected_at 으로 재면 복사본이 매번 "방금 수집됨"이 되어 기준이 무한히
    연장되고, 최초 조회 이후 CloudWatch 를 영영 다시 부르지 않는다 — 메트릭이 그 시점에
    얼어붙는다. 기준은 재사용해도 보존되는 window_end 여야 한다.
    """
    from db.repositories import assets as assets_repo

    period = mock_inventory.period_seconds
    t0 = mock_inventory.collected_at

    def reusable_at(now):
        _, fresh = assets_repo.fresh_ec2_metric_summaries(
            db,
            region="ap-northeast-2",
            not_older_than=now - timedelta(seconds=period),
        )
        return set(fresh)

    # 1회차(t0) — 직접 조회. metrics_window_end 가 None 이므로 창의 끝은 t0
    persist_inventory(mock_inventory, db)
    assert reusable_at(t0 + timedelta(minutes=5)) == {"i-idle001"}

    # 2회차(t0+5분) — 재사용분 적재. 원본 창은 t0 그대로다
    persist_inventory(
        mock_inventory.model_copy(
            update={"collected_at": t0 + timedelta(minutes=5), "metrics_window_end": t0}
        ),
        db,
    )
    assert reusable_at(t0 + timedelta(minutes=10)) == {"i-idle001"}  # 입자 안 — 계속 재사용

    # 최초 조회로부터 period 경과 — 복사본이 몇 개든 재사용 대상이 사라져야 한다
    assert reusable_at(t0 + timedelta(seconds=period + 1)) == set()

    # 3회차 — 실제 재조회분(창의 끝 = 그 시각)을 적재하면 다시 재사용 가능해진다
    t3 = t0 + timedelta(seconds=period + 60)
    persist_inventory(mock_inventory.model_copy(update={"collected_at": t3}), db)
    assert reusable_at(t3 + timedelta(minutes=5)) == {"i-idle001"}
