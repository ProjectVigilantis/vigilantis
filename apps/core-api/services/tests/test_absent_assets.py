# ==============================================================================
# [파일 설명]  담당: 김승철 (Data & Rule Engine)
# 소멸 자산 표시·해제와 그 경계를 회귀로 고정한다. (Issue #332)
#
# 지키려는 것은 넷이다.
#   1. SUCCESS 로 마감된 회차가 못 본 그 리전의 자산에 소멸 표시가 찍힌다.
#   2. PARTIAL 회차에서는 아무것도 표시되지 않는다 — 못 본 것과 사라진 것은 다르다(#221).
#   3. 소멸 자산은 판정 대상에서 빠진다 — 낡은 메트릭으로 후보가 서지 않는다.
#   4. 이력(MetricSummary·RuleEvaluation)은 남는다 — 표시이지 삭제가 아니다.
#
# prune 이 기본으로 꺼져 있는 것도 함께 잠근다. scripts/load_golden_assets.py 가 골든
# 파일 1건마다 persist_inventory 를 부르고 그 파일들이 전부 같은 리전이라, 기본이 켜지면
# 두 번째 파일이 첫 번째 파일의 자산을 통째로 소멸 처리한다.
# ==============================================================================

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import select

CORE_API = Path(__file__).resolve().parents[2]
REPO_ROOT = Path(__file__).resolve().parents[4]
for _p in (str(CORE_API), str(REPO_ROOT / "packages")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from db import models  # noqa: E402
from schemas.assets import AssetInventory, Ec2Asset, MetricSummary  # noqa: E402
from services.collector import persist_inventory  # noqa: E402
from services.rule_engine import run_rule_engine  # noqa: E402

# db·pg_engine 픽스처는 services/tests/conftest.py 가 등록한다

ACCOUNT = "123456789012"
REGION = "ap-northeast-2"


def _ec2(instance_id: str, region: str = REGION) -> Ec2Asset:
    """저활성 EC2 1대 — cpu_avg 가 낮아 판정이 COST_CANDIDATE 로 선다."""
    return Ec2Asset(
        arn=f"arn:aws:ec2:{region}:{ACCOUNT}:instance/{instance_id}",
        instance_id=instance_id,
        name=instance_id,
        instance_type="t3.xlarge",
        state="running",
        region=region,
        availability_zone=f"{region}a",
        vpc_id="vpc-001",
        subnet_id="subnet-001",
        private_ip="10.0.1.10",
        security_group_ids=[],
        tags={"Name": instance_id, "Environment": "dev"},
        metric_summary=MetricSummary(
            cpu_datapoints=336,
            cpu_avg=1.5,
            cpu_max=3.2,
            net_in_avg=100.0,
            net_out_avg=200.0,
        ),
    )


def _inventory(
    instance_ids: list[str],
    *,
    collected_at: datetime,
    region: str = REGION,
    failures: dict[str, str] | None = None,
) -> AssetInventory:
    return AssetInventory(
        account_id=ACCOUNT,
        region=region,
        mode="localstack",
        collected_at=collected_at,
        lookback_days=14,
        period_seconds=3600,
        ec2_instances=[_ec2(iid, region) for iid in instance_ids],
        collector_failures=failures or {},
    )


def _asset(db, instance_id: str, region: str = REGION) -> models.Asset:
    arn = f"arn:aws:ec2:{region}:{ACCOUNT}:instance/{instance_id}"
    return db.execute(
        select(models.Asset).where(models.Asset.arn == arn)
    ).scalar_one()


@pytest.fixture
def t0() -> datetime:
    return datetime.now(timezone.utc) - timedelta(hours=1)


# ------------------------------------------------------------------ 표시와 해제


def test_unobserved_asset_is_marked_absent(db, t0):
    """다음 회차가 못 본 자산에 소멸 표시가 찍히고, 본 자산은 그대로다."""
    persist_inventory(_inventory(["i-aaa", "i-bbb"], collected_at=t0), db, prune_absent=True)
    db.commit()

    later = t0 + timedelta(minutes=30)
    res = persist_inventory(_inventory(["i-aaa"], collected_at=later), db, prune_absent=True)
    db.commit()

    assert res["absent_marked"] == 1
    assert _asset(db, "i-bbb").absent_since == later
    assert _asset(db, "i-aaa").absent_since is None


def test_reobserved_asset_clears_the_mark(db, t0):
    """지워졌다 같은 ARN 으로 돌아오면 표시가 풀린다."""
    persist_inventory(_inventory(["i-aaa", "i-bbb"], collected_at=t0), db, prune_absent=True)
    db.commit()
    persist_inventory(
        _inventory(["i-aaa"], collected_at=t0 + timedelta(minutes=30)), db, prune_absent=True
    )
    db.commit()
    assert _asset(db, "i-bbb").absent_since is not None

    persist_inventory(
        _inventory(["i-aaa", "i-bbb"], collected_at=t0 + timedelta(hours=1)),
        db,
        prune_absent=True,
    )
    db.commit()

    assert _asset(db, "i-bbb").absent_since is None


def test_absent_since_keeps_the_first_time(db, t0):
    """언제부터 없었나가 남아야 한다 — 매 회차 값이 갱신되면 그 정보가 사라진다."""
    persist_inventory(_inventory(["i-aaa", "i-bbb"], collected_at=t0), db, prune_absent=True)
    db.commit()
    first_miss = t0 + timedelta(minutes=30)
    persist_inventory(_inventory(["i-aaa"], collected_at=first_miss), db, prune_absent=True)
    db.commit()

    res = persist_inventory(
        _inventory(["i-aaa"], collected_at=t0 + timedelta(hours=2)), db, prune_absent=True
    )
    db.commit()

    assert res["absent_marked"] == 0   # 이미 표시된 자산은 다시 세지 않는다
    assert _asset(db, "i-bbb").absent_since == first_miss


# ------------------------------------------------------------------ 경계


def test_partial_run_marks_nothing(db, t0):
    """PARTIAL 회차는 판단하지 않는다 — 못 본 것을 사라진 것으로 읽으면 멀쩡한 자산이 사라진다."""
    persist_inventory(_inventory(["i-aaa", "i-bbb"], collected_at=t0), db, prune_absent=True)
    db.commit()

    res = persist_inventory(
        _inventory(
            ["i-aaa"],
            collected_at=t0 + timedelta(minutes=30),
            failures={"auto_scaling_groups": "InternalFailure"},
        ),
        db,
        prune_absent=True,
    )
    db.commit()

    assert res["absent_marked"] == 0
    assert _asset(db, "i-bbb").absent_since is None


def test_prune_is_off_by_default(db, t0):
    """골든 적재 경로 보호 — 같은 리전 파일을 이어 넣어도 앞 파일의 자산이 죽지 않는다."""
    persist_inventory(_inventory(["i-aaa", "i-bbb"], collected_at=t0), db)
    db.commit()

    res = persist_inventory(_inventory(["i-ccc"], collected_at=t0 + timedelta(minutes=30)), db)
    db.commit()

    assert res["absent_marked"] == 0
    assert _asset(db, "i-aaa").absent_since is None
    assert _asset(db, "i-bbb").absent_since is None


def test_other_region_is_untouched(db, t0):
    """수집 단위가 리전이므로 다른 리전 자산은 이번 회차의 판단 범위 밖이다."""
    other = "us-east-1"
    persist_inventory(_inventory(["i-aaa"], collected_at=t0), db, prune_absent=True)
    persist_inventory(_inventory(["i-zzz"], collected_at=t0, region=other), db, prune_absent=True)
    db.commit()

    persist_inventory(
        _inventory([], collected_at=t0 + timedelta(minutes=30)), db, prune_absent=True
    )
    db.commit()

    assert _asset(db, "i-aaa").absent_since is not None
    assert _asset(db, "i-zzz", other).absent_since is None


# ------------------------------------------------------------------ 판정·이력


def test_absent_asset_is_not_evaluated(db, t0):
    """소멸 자산은 판정 대상에서 빠진다 — 낡은 메트릭으로 후보가 서지 않는다."""
    persist_inventory(_inventory(["i-aaa", "i-bbb"], collected_at=t0), db, prune_absent=True)
    db.commit()
    run_rule_engine(db)
    db.commit()

    gone = _asset(db, "i-bbb")
    before = db.execute(
        select(models.RuleEvaluation).where(models.RuleEvaluation.asset_id == gone.asset_id)
    ).scalars().all()
    assert before, "선행 조건: 소멸 전에는 판정이 있어야 한다"

    persist_inventory(
        _inventory(["i-aaa"], collected_at=t0 + timedelta(minutes=30)), db, prune_absent=True
    )
    db.commit()
    judged = run_rule_engine(db)
    db.commit()

    after = db.execute(
        select(models.RuleEvaluation).where(models.RuleEvaluation.asset_id == gone.asset_id)
    ).scalars().all()
    assert len(after) == len(before), "소멸 자산에 새 판정 행이 생기면 안 된다"
    assert gone.arn not in {e.asset_arn for e in judged["evaluations"]}


def test_absent_asset_keeps_its_history(db, t0):
    """표시이지 삭제가 아니다 — 메트릭·판정 이력이 남는다(FK 가 asset_id 를 문다)."""
    persist_inventory(_inventory(["i-aaa", "i-bbb"], collected_at=t0), db, prune_absent=True)
    db.commit()
    run_rule_engine(db)
    db.commit()

    gone = _asset(db, "i-bbb")
    asset_id = gone.asset_id
    metrics_before = db.execute(
        select(models.MetricSummary).where(models.MetricSummary.asset_id == asset_id)
    ).scalars().all()
    evals_before = db.execute(
        select(models.RuleEvaluation).where(models.RuleEvaluation.asset_id == asset_id)
    ).scalars().all()

    persist_inventory(
        _inventory(["i-aaa"], collected_at=t0 + timedelta(minutes=30)), db, prune_absent=True
    )
    db.commit()

    assert db.get(models.Asset, asset_id) is not None
    assert len(
        db.execute(
            select(models.MetricSummary).where(models.MetricSummary.asset_id == asset_id)
        ).scalars().all()
    ) == len(metrics_before)
    assert len(
        db.execute(
            select(models.RuleEvaluation).where(models.RuleEvaluation.asset_id == asset_id)
        ).scalars().all()
    ) == len(evals_before)
