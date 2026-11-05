"""T7 — 테넌트별 protected_tags 평가. 순수 판정 + run_rule_engine 소속 연결."""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
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
from services import rule_engine  # noqa: E402
from services.collector import persist_inventory  # noqa: E402
from services.rule_engine import SkipReason, Verdict, evaluate_ec2, run_rule_engine  # noqa: E402

# db 픽스처는 services/tests/conftest.py 가 등록한다

ACCOUNT_A = "123456789012"
ACCOUNT_B = "210987654321"
REGION = "ap-northeast-2"


def _idle(tags, policy="unset"):
    kwargs = {} if policy == "unset" else {"protected_tag_policy": policy}
    return evaluate_ec2(1.0, 3.0, 336, tags, state="running", **kwargs)[:2]


PROTECTED = (Verdict.SKIP, SkipReason.SKIP_PROD_PROTECTED)
CANDIDATE = (Verdict.COST_CANDIDATE, None)


def test_no_policy_keeps_legacy_defaults():
    assert _idle({"Environment": "Prod"}) == PROTECTED
    assert _idle({"Environment": "Prod"}, None) == PROTECTED
    assert _idle({"Team": "payments"}) == CANDIDATE


def test_empty_policy_disables_tag_protection_only():
    assert _idle({"Environment": "prod"}, []) == CANDIDATE
    # 다른 가드는 그대로: 관측치 부족은 여전히 보류
    assert evaluate_ec2(1.0, 3.0, 3, {"Environment": "prod"}, state="running",
                        protected_tag_policy=[])[1] == SkipReason.SKIP_INSUFFICIENT_DATA


def test_custom_rules_replace_defaults_with_or_semantics():
    policy = [{"key": "Team", "values": ["Payments", " core "]}, {"key": "tier", "values": ["gold"]}]
    assert _idle({"TEAM": " PAYMENTS "}, policy) == PROTECTED
    assert _idle({"team": "core"}, policy) == PROTECTED
    assert _idle({"Tier": "gold"}, policy) == PROTECTED
    assert _idle({"Team": "pay"}, policy) == CANDIDATE
    assert _idle({"Team ": "payments"}, policy) == CANDIDATE   # 키는 strip 하지 않는다
    assert _idle({"Environment": "prod"}, policy) == CANDIDATE  # 기본 규칙은 대체된다


def test_non_string_asset_value_is_stringified():
    assert _idle({"Level": 1}, [{"key": "level", "values": ["1"]}]) == PROTECTED


# ------------------------------------------------------------- run_rule_engine


def _inv(ids):
    return AssetInventory(
        account_id=ACCOUNT_A, region=REGION, mode="localstack",
        collected_at=datetime.now(UTC) - timedelta(hours=1),
        lookback_days=14, period_seconds=3600,
        ec2_instances=[
            Ec2Asset(
                arn=f"arn:aws:ec2:{REGION}:{ACCOUNT_A}:instance/{i}", instance_id=i, name=i,
                instance_type="t3.xlarge", state="running", region=REGION,
                availability_zone=f"{REGION}a", vpc_id="vpc-1", subnet_id="subnet-1",
                private_ip="10.0.0.1", security_group_ids=[], tags={"Team": "payments"},
                metric_summary=MetricSummary(cpu_datapoints=336, cpu_avg=1.5, cpu_max=3.2,
                                             net_in_avg=1.0, net_out_avg=1.0),
            )
            for i in ids
        ],
        collector_failures={},
    )


def _tenant(db, name, account):
    t = models.Tenant(
        name=name, aws_account_id=account,
        access_role_arn=f"arn:aws:iam::{account}:role/r", external_id=f"external-id-{name}-0123456789",
        regions=[REGION],
    )
    db.add(t)
    db.flush()
    return t


def _skips(out):
    return {r["name"]: r["skip_reason"] for r in out["results"]}


@pytest.fixture
def owned(db):
    """i-a: 테넌트 A 소유 · i-b: 테넌트 B 소유 · i-x: B 를 가리키지만 계정 불일치 · i-n: 소속 없음."""
    persist_inventory(_inv(["i-a", "i-b", "i-x", "i-n"]), db)
    ta, tb = _tenant(db, "a", ACCOUNT_A), _tenant(db, "b", ACCOUNT_B)
    by_name = {a.name: a for a in db.execute(select(models.Asset)).scalars()}
    by_name["i-a"].tenant_id = ta.tenant_id
    by_name["i-b"].tenant_id, by_name["i-b"].account_id = tb.tenant_id, ACCOUNT_B
    by_name["i-x"].tenant_id = tb.tenant_id   # account_id 는 A → 충돌
    db.commit()
    return ta.tenant_id, tb.tenant_id


@pytest.fixture
def world(owned, monkeypatch):
    """로더를 가짜로 바꿔 호출 횟수·인자를 본다."""
    ta, tb = owned
    store: dict = {}
    calls: list = []

    def load(_db, tenant_ids):
        calls.append(list(tenant_ids))
        return {t: store[t] for t in tenant_ids if t in store}

    monkeypatch.setattr("db.repositories.tenant_settings.load_protected_tag_policies", load)
    return ta, tb, store, calls


def test_run_uses_each_tenants_policy_and_picks_up_changes(db, world):
    ta, tb, store, calls = world
    PP = SkipReason.SKIP_PROD_PROTECTED.value

    store[ta] = [{"key": "team", "values": ["payments"]}]
    store[tb] = []
    s = _skips(run_rule_engine(db))
    assert s["i-a"] == PP
    assert s["i-b"] is None            # B 는 [] → 태그 보호 없음
    assert s["i-x"] is None            # 충돌: B 의 정책도, A 의 것도 아닌 기본값(Team 은 비보호)
    assert s["i-n"] is None            # 소속 없음 → 기본값
    assert len(calls) == 1 and set(calls[0]) == {ta, tb}   # 배치 1회, 충돌 자산은 소속으로 세지 않음

    store[ta] = []                      # 재시작 없이 다음 호출에 반영
    assert _skips(run_rule_engine(db))["i-a"] is None


def test_conflicting_asset_does_not_consume_other_tenants_policy(db, world):
    _, tb, store, _ = world
    store[tb] = [{"key": "team", "values": ["payments"]}]
    s = _skips(run_rule_engine(db))
    assert s["i-b"] == SkipReason.SKIP_PROD_PROTECTED.value
    assert s["i-x"] is None


def _save(db, tenant_id, tags):
    """실제 저장소 행으로 정책을 저장한다(None = 저장된 정책 없음)."""
    from db.repositories import tenant_settings

    row = tenant_settings.lock_existing(db, tenant_id)
    if row is None:
        row = tenant_settings.new_record(tenant_id, updated_at=datetime.now(UTC))
        db.add(row)
    row.protected_tags = tags
    db.commit()


def test_saved_policies_drive_next_run_through_real_repository(db, owned):
    ta, tb = owned
    PP = SkipReason.SKIP_PROD_PROTECTED.value

    # 저장 행 없음 → 기본값(Team 은 비보호)
    assert _skips(run_rule_engine(db))["i-a"] is None

    _save(db, ta, [{"key": "Team", "values": [" Payments "]}])
    _save(db, tb, [])
    s = _skips(run_rule_engine(db))
    assert s["i-a"] == PP and s["i-b"] is None
    assert s["i-x"] is None and s["i-n"] is None   # 충돌 · 소속 없음은 어느 정책도 쓰지 않는다

    _save(db, ta, [])                                # 같은 프로세스, 다음 호출에서 반영
    _save(db, tb, [{"key": "team", "values": ["payments"]}])
    s = _skips(run_rule_engine(db))
    assert s["i-a"] is None and s["i-b"] == PP
    assert s["i-x"] is None                          # B 의 정책을 계정 불일치 자산이 쓰지 않는다

    _save(db, tb, None)                              # 정책 해제 → 기본값
    assert _skips(run_rule_engine(db))["i-b"] is None


def test_empty_saved_policy_keeps_other_guards(db, owned):
    ta, _ = owned
    _save(db, ta, [])
    asset = db.execute(select(models.Asset).where(models.Asset.name == "i-a")).scalar_one()
    asset.state = "stopped"
    db.commit()
    assert _skips(run_rule_engine(db))["i-a"] == SkipReason.SKIP_UNSUPPORTED_STATE.value


def test_default_protected_tags_matches_legacy_behavior_and_is_fresh():
    rules = rule_engine.default_protected_tags()
    assert rules == [
        {"key": k, "values": ["prd", "prod", "production"]}
        for k in ("env", "environment", "stage", "tier")
    ]
    rules[0]["values"].append("x")
    assert rule_engine.default_protected_tags()[0]["values"] == ["prd", "prod", "production"]
    for tags in ({"Environment": "Prod"}, {"TIER": " production "}, {"Team": "payments"}):
        assert _idle(tags, rule_engine.default_protected_tags()) == _idle(tags)
