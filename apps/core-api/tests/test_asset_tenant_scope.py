# ==============================================================================
# [파일 설명]
# GET /api/v1/assets 와 자산 · 메트릭 저장소의 고객사 범위 — 실제 PostgreSQL · 실제 라우터. (#533 · #492)
#
# 범위(tenancy.TenantScope): 중앙 관리자(tenant_ids=None)는 소속 없는 MVP 기록까지 전체, 담당자는 배정된
# 고객사들만, 배정이 없으면(빈 튜플) 아무것도, 고객사 계정은 자기 고객사만 본다. 소속 없는 기록은 전체 범위에서만 보인다.
# collection_status · last_collected_at · uncollected 는 범위 안 회차에서만 산출하므로 범위 밖 고객사나 소속 없는
# 기록의 더 새 회차가 영향을 주지 못하고, 한 리전을 여러 고객사가 함께 쓰면 고객사마다 그 리전의 최신 회차를 따로 본다.
# 연결의 target_arn 은 자산 FK 가 아닌 문자열이라, 대상이 범위 안 자산으로 해석될 때만 싣는다(전체 범위는 그대로).
# 관제 대상은 범위 안 고객사마다 (고객사, 테넌트 행의 관제 리전) 쌍이고, 전체 범위만 설정 리전(legacy)의 소속 없는 기록을
# 더한다(tenancy.MonitoredRegions) — 고객사가 관제하지 않는 리전의 기록은 다른 고객사가 그 리전을 관제해도 들어오지 않는다.
# 메트릭 저장소 함수는 (소유 × 리전 × 칸)마다 마지막 회차를 고르고 범위 · 쌍은 순위 · 집계 전에 건다 — 같은 리전 · 같은 칸의
# 고객사들이 한 회차로 접히지 않고, 중앙은 소유마다의 합이다.
# ==============================================================================

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from db import models
from db.repositories import assets as assets_repo
from db.repositories import tenants as tenants_repo
from schemas.api.assets import AssetType, RelationType
from schemas.api.auth import PlatformRole, UserRole
from schemas.collections import CollectionRunStatus
from schemas.rules import RuleEvaluationResult
from tenancy import ALL_TENANTS, MonitoredRegions, TenantScope

REGION = "ap-northeast-2"  # 서울
TOKYO = "ap-northeast-1"
# 소속 없는(MVP) 기록의 계정 — 테넌트의 AWS 계정과 겹치지 않는다
MVP_ACCOUNT = "000000000001"
T0 = datetime(2026, 10, 28, 1, 0, tzinfo=UTC)
# 수집 계약의 실패 라벨 → 미관측 유형 (schemas.collections.UNOBSERVED_TYPES_BY_FAILURE)
ASG_FAILURE = '{"auto_scaling_groups": "AccessDenied"}'  # → AUTO_SCALING_GROUP
TARGET_GROUP_FAILURE = '{"alb_target_groups": "Throttling"}'  # → ALB_TARGET_GROUP
ASG_UNCOLLECTED = {"asset_type": "AUTO_SCALING_GROUP", "reason_code": "AccessDenied"}
TARGET_GROUP_UNCOLLECTED = {"asset_type": "ALB_TARGET_GROUP", "reason_code": "Throttling"}


@pytest.fixture()
def platform_session(make_auth_session):
    """client_pg 의 기본 세션을 명시 중앙 관리자(CENTRAL_ADMIN)로 둔다 — 전체 범위 검증이 conftest 기본값에 기대지 않게."""
    return make_auth_session(platform_role=PlatformRole.CENTRAL_ADMIN)


def _account() -> str:
    return f"{uuid.uuid4().int % 10**12:012d}"


def _account_of(db, tenant_id: str) -> str:
    return db.get(models.Tenant, tenant_id).aws_account_id


def _tenant_with_regions(db, regions: list[str]) -> str:
    """관제 리전을 고른 실제 테넌트(PENDING_CONNECTION — 기록 조회는 상태와 무관하다) → tenant_id."""
    account = _account()
    tenant = tenants_repo.create_tenant(
        db, name=f"t-{account}", aws_account_id=account, regions=regions, now=T0
    )
    db.commit()
    return tenant.tenant_id


def _arn(account: str, instance: str, region: str = REGION) -> str:
    return f"arn:aws:ec2:{region}:{account}:instance/{instance}"


def _collected(
    db,
    *,
    tenant_id: str | None,
    account: str,
    instance: str,
    status: CollectionRunStatus,
    finished_at: datetime,
    error_summary: str | None = None,
    region: str = REGION,
):
    """수집 회차 하나와 그 회차가 본 EC2 자산 하나 — 둘 다 같은 tenant_id 로 둔다."""
    run = assets_repo.start_collection_run(
        db, account_id=account, region=region, mode="localstack", lookback_days=3, period_seconds=3600,
    )
    run.tenant_id = tenant_id
    run.started_at = finished_at - timedelta(minutes=1)
    asset = assets_repo.upsert_asset(
        db, arn=_arn(account, instance, region), asset_type=AssetType.EC2, resource_id=instance,
        account_id=account, region=region, spec={"instance_type": "t3.micro"},
        collection_run_id=run.collection_run_id, collected_at=finished_at, name=instance, state="running",
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


def _last(body: dict) -> datetime | None:
    value = body["last_collected_at"]
    return None if value is None else datetime.fromisoformat(value.replace("Z", "+00:00"))


def _login_manager(login_as, tenant_ids):
    """client_pg 를 담당자(TENANT_MANAGER) 세션으로 바꾼다 — tenant_ids 를 명시 배정(빈 값 = 배정 없음)."""
    return login_as(
        UserRole.ADMIN, platform_role=PlatformRole.TENANT_MANAGER, assigned_tenant_ids=tuple(tenant_ids)
    )


def _evaluate(db, asset) -> None:
    """그 자산을 본 회차의 판정 1건."""
    assets_repo.add_rule_evaluation(
        db,
        RuleEvaluationResult.model_validate(
            {
                "asset_arn": asset.arn,
                "collection_run_id": asset.last_collection_run_id,
                "evaluation_status": "COMPLETED",
                "verdict": "UNUSED",
                "health_score": 50,
                "evaluated_at": T0,
            }
        ),
    )


# --- 고객사 계정(#533) -------------------------------------------------------------------


@pytest.mark.parametrize("role", list(UserRole))
def test_tenant_account_sees_only_its_own_assets_and_collection(client_pg, login_as, collected, role):
    login_as(role, tenant_id=collected["a"])
    resp = client_pg.get("/api/v1/assets")
    assert resp.status_code == 200
    body = resp.json()
    assert _arns(resp) == {collected["mine"].arn, collected["mine2"].arn}
    # 다른 테넌트 · 소속 없는 더 새 회차(실패 · 부분 실패)가 내 상태 · 시각 · 미수집 유형에 섞이지 않는다
    assert body["collection_status"] == "READY"
    assert _last(body) == T0
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
    assert body["uncollected"] == [ASG_UNCOLLECTED]
    assert [item["arn"] for item in body["items"]] == [collected["theirs"].arn]


def test_tenant_relationships_keep_only_targets_resolving_to_its_own_assets(client_pg, login_as, collected):
    login_as(UserRole.VIEWER, tenant_id=collected["a"])
    resp = client_pg.get("/api/v1/assets")
    relationships = _item(resp, collected["mine"].arn)["relationships"]
    assert relationships == [{"relation_type": "ATTACHED_TO", "target_arn": collected["mine2"].arn}]


# --- 중앙 관리자 ----------------------------------------------------------------------------


def test_central_admin_sees_every_asset_relationship_and_each_owners_latest_run(client_pg, collected):
    # client_pg 기본 세션 = CENTRAL_ADMIN — 소속 없는 기록과 해석되지 않는 대상까지 그대로다
    resp = client_pg.get("/api/v1/assets")
    body = resp.json()
    assert {collected[k].arn for k in ("mine", "mine2", "theirs", "mvp")} <= _arns(resp)
    # 같은 리전의 소유마다 최신 회차를 따로 접는다 — A 성공 · B 실패 · 소속 없음 부분 실패 중 가장 나쁜 것이고,
    # 미수집 유형은 B(ASG)와 소속 없음(대상 그룹) 둘 다다. 마지막 수집 시각은 셋 중 가장 늦은 소속 없음의 것이다
    assert body["collection_status"] == "FAILED"
    assert body["uncollected"] == [ASG_UNCOLLECTED, TARGET_GROUP_UNCOLLECTED]
    assert _last(body) == T0 + timedelta(minutes=10)
    assert {rel["target_arn"] for rel in _item(resp, collected["mine"].arn)["relationships"]} == {
        collected["mine2"].arn, collected["theirs"].arn, collected["mvp"].arn, collected["missing_arn"],
    }


# --- 담당자(TENANT_MANAGER) --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("assigned", "visible", "status", "last", "uncollected"),
    [
        (("a",), ("mine", "mine2"), "READY", T0, []),
        (("a", "empty"), ("mine", "mine2"), "READY", T0, []),
        (("b",), ("theirs",), "FAILED", T0 + timedelta(minutes=5), [ASG_UNCOLLECTED]),
        (("a", "b"), ("mine", "mine2", "theirs"), "FAILED", T0 + timedelta(minutes=5), [ASG_UNCOLLECTED]),
        (("empty",), (), "NOT_COLLECTED", None, []),
        ((), (), "NOT_COLLECTED", None, []),
    ],
    ids=["A", "A+empty", "B", "A+B", "empty-only", "unassigned"],
)
def test_manager_sees_only_assigned_customers_records_and_collection(
    client_pg, login_as, collected, assigned, visible, status, last, uncollected
):
    _login_manager(login_as, [collected[k] for k in assigned])
    resp = client_pg.get("/api/v1/assets")
    assert resp.status_code == 200
    body = resp.json()
    # 소속 없는 MVP 기록 · 배정 밖 고객사는 어떤 배정에서도 보이지 않는다
    assert _arns(resp) == {collected[k].arn for k in visible}
    assert body["collection_status"] == status
    assert _last(body) == last
    assert body["uncollected"] == uncollected


def test_manager_relationships_need_both_ends_inside_the_assigned_customers(client_pg, login_as, collected):
    _login_manager(login_as, [collected["a"], collected["b"]])
    resp = client_pg.get("/api/v1/assets")
    relationships = _item(resp, collected["mine"].arn)["relationships"]
    # 대상이 다른 배정 고객사(B)의 자산이면 싣고, 소속 없는 자산 · 해석되지 않는 ARN 은 싣지 않는다
    assert {(rel["relation_type"], rel["target_arn"]) for rel in relationships} == {
        ("ATTACHED_TO", collected["mine2"].arn),
        ("SECURED_BY", collected["theirs"].arn),
    }


def test_each_customer_keeps_its_own_latest_run_when_customers_share_a_region(
    client_pg, db, login_as, make_tenant, monkeypatch
):
    """Y 의 실패가 같은 리전 X 의 더 새 성공에 가려지지 않는다 — 리전당 1건만 고르면 READY 가 된다."""
    monkeypatch.setattr("routers.assets._configured_regions", lambda: [REGION])
    x, y = make_tenant(), make_tenant()
    failed = _collected(
        db, tenant_id=y, account=_account_of(db, y), instance="i-0y", status=CollectionRunStatus.FAILED,
        finished_at=T0, error_summary=ASG_FAILURE,
    )
    newer = _collected(
        db, tenant_id=x, account=_account_of(db, x), instance="i-0x", status=CollectionRunStatus.SUCCESS,
        finished_at=T0 + timedelta(minutes=5),
    )
    db.commit()
    expected = sorted([failed.last_collection_run_id, newer.last_collection_run_id])

    for scope in (TenantScope((x, y)), ALL_TENANTS):
        runs = assets_repo.latest_collection_run_per_region(db, [REGION], scope=scope)
        assert sorted(run.collection_run_id for run in runs) == expected

    _login_manager(login_as, [x, y])
    body = client_pg.get("/api/v1/assets").json()
    assert body["collection_status"] == "FAILED"
    assert body["uncollected"] == [ASG_UNCOLLECTED]
    assert _last(body) == T0 + timedelta(minutes=5)


@pytest.fixture()
def paired(db, make_tenant, monkeypatch):
    """A 는 서울, B 는 도쿄를 관제하고 설정(legacy) 리전은 서울뿐이다.

    쌍 밖의 기록 — A 의 도쿄(A 가 관제하지 않는다), 소속 없는 도쿄(설정 리전이 아니다) — 은 가장 새 회차이고 실패한다.
    리전 합집합으로 거르면 B 의 도쿄 때문에 둘이 들어와 목록 · 상태 · 시각 · 미수집 유형이 모두 바뀐다.
    """
    monkeypatch.setattr("routers.assets._configured_regions", lambda: [REGION])
    a = make_tenant()  # 관제 리전 [REGION]
    b = _tenant_with_regions(db, [TOKYO])
    acct_a, acct_b = _account_of(db, a), _account_of(db, b)
    success = CollectionRunStatus.SUCCESS
    seeded = {
        "a": a,
        "b": b,
        "a_seoul": _collected(db, tenant_id=a, account=acct_a, instance="i-0as", status=success, finished_at=T0),
        "b_tokyo": _collected(db, tenant_id=b, account=acct_b, instance="i-0bt", status=success,
                              finished_at=T0 - timedelta(minutes=5), region=TOKYO),
        "legacy_seoul": _collected(db, tenant_id=None, account=MVP_ACCOUNT, instance="i-0ns", status=success,
                                   finished_at=T0 - timedelta(minutes=10)),
        "a_tokyo": _collected(db, tenant_id=a, account=acct_a, instance="i-0at", status=CollectionRunStatus.FAILED,
                              finished_at=T0 + timedelta(minutes=5), error_summary=ASG_FAILURE, region=TOKYO),
        "legacy_tokyo": _collected(db, tenant_id=None, account=MVP_ACCOUNT, instance="i-0nt",
                                   status=CollectionRunStatus.FAILED, finished_at=T0 + timedelta(minutes=10),
                                   error_summary=TARGET_GROUP_FAILURE, region=TOKYO),
    }
    db.commit()
    return seeded


def _pairs(paired) -> tuple[tuple[str, str], ...]:
    return ((paired["a"], REGION), (paired["b"], TOKYO))


@pytest.mark.parametrize(
    ("viewer", "visible"),
    [
        ("central", ("a_seoul", "b_tokyo", "legacy_seoul")),
        ("manager A+B", ("a_seoul", "b_tokyo")),
        ("tenant A", ("a_seoul",)),
    ],
)
def test_records_are_read_only_in_their_owners_monitored_regions(client_pg, login_as, paired, viewer, visible):
    """중앙도 A 의 도쿄를 보지 않는다(B 가 도쿄를 관제해도) — 소속 없는 기록은 설정 리전(서울)에서만 본다.

    쌍 밖 회차는 더 새 실패지만 상태 · 마지막 수집 시각 · 미수집 유형에 섞이지 않는다 — 셋 다 쌍 안 회차의 값이다.
    """
    if viewer == "manager A+B":
        _login_manager(login_as, [paired["a"], paired["b"]])
    elif viewer == "tenant A":
        login_as(UserRole.VIEWER, tenant_id=paired["a"])
    resp = client_pg.get("/api/v1/assets")
    assert resp.status_code == 200
    body = resp.json()
    assert _arns(resp) == {paired[k].arn for k in visible}
    assert body["collection_status"] == "READY"
    assert _last(body) == T0
    assert body["uncollected"] == []


def test_latest_runs_pick_each_monitored_pair_and_legacy_region_only(db, paired):
    run_of = {k: paired[k].last_collection_run_id for k in ("a_seoul", "b_tokyo", "legacy_seoul")}
    a, b = paired["a"], paired["b"]

    def picked(regions: MonitoredRegions, scope: TenantScope) -> list:
        return [run.collection_run_id for run in assets_repo.latest_collection_run_per_region(db, regions, scope=scope)]

    central = MonitoredRegions(pairs=_pairs(paired), legacy=(REGION,))
    # (리전, 소유 — 소속 없음 먼저) 순: 도쿄의 B, 서울의 소속 없음, 서울의 A. 쌍 밖의 더 새 회차는 후보가 아니다
    assert picked(central, ALL_TENANTS) == [run_of["b_tokyo"], run_of["legacy_seoul"], run_of["a_seoul"]]
    assert picked(MonitoredRegions(pairs=_pairs(paired)), TenantScope((a, b))) == [
        run_of["b_tokyo"], run_of["a_seoul"],
    ]
    # 범위는 쌍과 별개로 다시 건다 — 쌍에 B 가 있어도 A 범위는 A 의 회차뿐이고, 소속 없는 legacy 도 보지 않는다
    assert picked(central, TenantScope((a,))) == [run_of["a_seoul"]]
    assert picked(MonitoredRegions(pairs=(), legacy=(REGION,)), TenantScope((a,))) == []
    assert picked(MonitoredRegions(pairs=()), ALL_TENANTS) == []
    assert assets_repo.last_finished_collection_at(db, central, scope=ALL_TENANTS) == T0
    assert assets_repo.last_finished_collection_at(db, MonitoredRegions(pairs=()), scope=ALL_TENANTS) is None


# --- 저장소 경계 ---------------------------------------------------------------------------


@pytest.mark.parametrize("regions", [None, [REGION]], ids=["distinct-on", "lateral"])
def test_latest_run_per_region_is_narrowed_before_selection_on_both_paths(db, collected, regions):
    a, b = collected["a"], collected["b"]
    run_of = {k: collected[k].last_collection_run_id for k in ("mine", "theirs", "mvp")}

    def picked(scope: TenantScope) -> list:
        return assets_repo.latest_collection_run_per_region(db, regions, scope=scope)

    # A 의 최신은 mine 의 회차(mine2 보다 늦다) — B · 소속 없음의 더 새 회차가 그 자리를 차지하지 않는다
    assert [run.collection_run_id for run in picked(TenantScope((a,)))] == [run_of["mine"]]
    assert picked(TenantScope((collected["empty"],))) == []
    assert picked(TenantScope(())) == []
    assert sorted(run.collection_run_id for run in picked(TenantScope((a, b)))) == sorted(
        [run_of["mine"], run_of["theirs"]]
    )
    # 전체 범위는 소유마다 1건(소속 없음 포함) — 순서는 (리전, 소유 — 소속 없음 먼저)
    every = picked(ALL_TENANTS)
    assert [run.tenant_id for run in every] == [None, *sorted([a, b])]
    assert {run.collection_run_id for run in every} == set(run_of.values())
    assert {run.tenant_id: run.status for run in every} == {
        a: CollectionRunStatus.SUCCESS, b: CollectionRunStatus.FAILED, None: CollectionRunStatus.PARTIAL,
    }


def test_last_finished_collection_counts_only_runs_in_scope(db, collected):
    a, b = collected["a"], collected["b"]
    assert assets_repo.last_finished_collection_at(db, [REGION], scope=TenantScope((a,))) == T0
    assert assets_repo.last_finished_collection_at(
        db, [REGION], scope=TenantScope((a, b))
    ) == T0 + timedelta(minutes=5)
    assert assets_repo.last_finished_collection_at(db, [REGION], scope=ALL_TENANTS) == T0 + timedelta(minutes=10)
    assert assets_repo.last_finished_collection_at(db, [REGION], scope=TenantScope(())) is None


def test_latest_rule_evaluations_are_read_only_for_assets_in_scope(db, collected):
    for key in ("mine", "theirs", "mvp"):
        _evaluate(db, collected[key])
    db.flush()
    a, b = collected["a"], collected["b"]
    ids = {key: collected[key].asset_id for key in ("mine", "theirs", "mvp")}

    def judged(scope: TenantScope) -> set[str]:
        return set(assets_repo.latest_rule_evaluation_by_asset(db, scope=scope))

    assert judged(TenantScope((a,))) == {ids["mine"]}
    assert judged(TenantScope((a, b))) == {ids["mine"], ids["theirs"]}
    assert judged(TenantScope(())) == set()
    assert judged(ALL_TENANTS) == set(ids.values())


# --- 메트릭 저장소(축 2 · 4 · 5 · 6) ---------------------------------------------------------

METRIC_AT = datetime(2026, 10, 29, 3, 0, tzinfo=UTC)  # 칸(5분) 경계 — 아래 회차가 모두 이 칸에 든다
BUCKET_SECONDS = 300
SINCE = METRIC_AT - timedelta(hours=1)
UNTIL = METRIC_AT + timedelta(hours=1)
# 소유마다 그 칸 마지막 회차의 SG 개방(THREAT 판정) · 위협 이벤트 · EC2 재고 수
METRIC_COUNTS = {"A": 1, "B": 2, None: 4}
# A 의 같은 칸 앞선 회차 — (소유 × 리전)마다 마지막 회차만 세면 보이지 않는다
SUPERSEDED_COUNT = 10


def _sg(db, run, *, tenant_id: str | None, account: str, tag: str) -> None:
    """소유 tenant_id 의 SG 1건(회차의 리전)과 그 회차의 THREAT(인터넷 개방) 판정 1건."""
    arn = f"arn:aws:ec2:{run.region}:{account}:security-group/sg-{tag}"
    asset = assets_repo.upsert_asset(
        db, arn=arn, asset_type=AssetType.SG, resource_id=f"sg-{tag}", account_id=account, region=run.region,
        spec={"attached": True, "open_to_world": []}, collection_run_id=run.collection_run_id,
        collected_at=run.started_at,
    )
    asset.tenant_id = tenant_id
    assets_repo.add_rule_evaluation(
        db,
        RuleEvaluationResult(
            asset_arn=arn, collection_run_id=run.collection_run_id, evaluation_status="COMPLETED",
            verdict="THREAT", health_score=None, reason="테스트 시드", evaluated_at=run.started_at,
        ),
    )


def _judged_run(
    db, *, tenant_id: str | None, account: str, started_at: datetime, count: int, tag: str, region: str = REGION
):
    """소유 tenant_id 의 성공 회차 1개 — 그 소유의 SG count 개(전부 THREAT) · EC2 재고 count."""
    run = assets_repo.start_collection_run(
        db, account_id=account, region=region, mode="localstack", lookback_days=3, period_seconds=3600,
    )
    run.tenant_id = tenant_id
    run.started_at = started_at
    for i in range(count):
        _sg(db, run, tenant_id=tenant_id, account=account, tag=f"{tag}{i:03d}")
    assets_repo.record_inventory_counts(db, collection_run_id=run.collection_run_id, counts={AssetType.EC2: count})
    assets_repo.finish_collection_run(
        db, run.collection_run_id, CollectionRunStatus.SUCCESS, finished_at=started_at + timedelta(minutes=1)
    )
    return run


def _threat_event(
    db, *, tenant_id: str | None, account: str, occurred_at: datetime, key: str, region: str = REGION
) -> None:
    db.add(
        models.ThreatEvent(
            source_event_id=key, event_type="OPEN_IP",
            target_arn=f"arn:aws:ec2:{region}:{account}:security-group/sg-{key}", payload={},
            deduplication_key=key, occurred_at=occurred_at, tenant_id=tenant_id,
        )
    )
    db.flush()


@pytest.fixture()
def metric_world(db, make_tenant):
    """A · B 고객사와 소속 없는 기록이 같은 리전 · 같은 칸에 회차를 남긴다 → {라벨: tenant_id}.

    늦게 시작한 순서는 A(앞선 회차) < A < B < 소속 없음 — 리전만으로 순위를 매기면 소속 없음 하나만 남고,
    순위 뒤에 범위를 걸면 A · B 범위에서 아무것도 남지 않는다.
    """
    owners = {"A": make_tenant(), "B": make_tenant(), None: None}
    accounts = {label: _account_of(db, t) if t else MVP_ACCOUNT for label, t in owners.items()}
    _judged_run(db, tenant_id=owners["A"], account=accounts["A"], started_at=METRIC_AT + timedelta(seconds=5),
                count=SUPERSEDED_COUNT, tag="a-old")
    for offset, (label, tenant_id) in enumerate(owners.items(), start=2):
        started = METRIC_AT + timedelta(seconds=10 * offset)
        tag = (label or "n").lower()
        _judged_run(db, tenant_id=tenant_id, account=accounts[label], started_at=started,
                    count=METRIC_COUNTS[label], tag=tag)
        for i in range(METRIC_COUNTS[label]):
            _threat_event(db, tenant_id=tenant_id, account=accounts[label], occurred_at=started, key=f"{tag}-ev{i}")
    db.commit()
    return owners


def _window(scope: TenantScope) -> dict:
    return {"regions": [REGION], "since": SINCE, "bucket_seconds": BUCKET_SECONDS, "scope": scope}


@pytest.mark.parametrize(
    ("assigned", "visible"),
    [
        (None, ("A", "B", None)),
        (("A", "B"), ("A", "B")),
        (("A",), ("A",)),
        (("B",), ("B",)),
        ((), ()),
    ],
    ids=["everyone", "A+B", "A", "B", "nobody"],
)
def test_metric_histories_count_each_owners_last_run_once_within_scope(db, metric_world, assigned, visible):
    scope = ALL_TENANTS if assigned is None else TenantScope(tuple(metric_world[label] for label in assigned))
    total = sum(METRIC_COUNTS[label] for label in visible)
    expected = [(METRIC_AT, total)] if visible else []

    sg = assets_repo.sg_exposure_history(db, **_window(scope))
    status = assets_repo.asset_status_history(db, **_window(scope))
    inventory = assets_repo.asset_inventory_history(db, **_window(scope))
    threats = assets_repo.threat_event_history(
        db, regions=[REGION], since=SINCE, until=UNTIL, bucket_seconds=BUCKET_SECONDS, scope=scope
    )

    assert [(b.observed_at, b.open_count) for b in sg] == expected
    assert [(b.observed_at, b.threat) for b in status] == expected
    assert [(b.observed_at, b.count) for b in inventory if b.asset_type is AssetType.EC2] == expected
    assert [(b.observed_at, b.count) for b in threats] == expected


def test_judgement_of_an_asset_outside_the_scope_is_not_counted_for_the_runs_owner(db, make_tenant):
    """회차의 소유만으로 판정이 그 고객사 것이 되지 않는다 — 판정된 자산도 범위 안이어야 센다."""
    a = make_tenant()
    run = _judged_run(db, tenant_id=a, account=_account_of(db, a), started_at=METRIC_AT, count=1, tag="own")
    _sg(db, run, tenant_id=None, account=MVP_ACCOUNT, tag="stray")
    db.commit()

    mine = TenantScope((a,))
    assert [b.open_count for b in assets_repo.sg_exposure_history(db, **_window(mine))] == [1]
    assert [b.threat for b in assets_repo.asset_status_history(db, **_window(mine))] == [1]
    assert [b.open_count for b in assets_repo.sg_exposure_history(db, **_window(ALL_TENANTS))] == [2]
    assert [b.threat for b in assets_repo.asset_status_history(db, **_window(ALL_TENANTS))] == [2]


def test_newer_run_judging_only_assets_outside_the_scope_does_not_hide_earlier_in_scope_history(db, make_tenant):
    """같은 소유 · 리전 · 칸의 더 새 회차가 범위 밖 · 소속 없는 자산만 판정했다 — 범위 안 판정이 있는 앞선 회차를 센다.

    '판정이 있는 회차'를 범위와 무관하게 고르면 더 새 회차가 1순위가 되고, 그 판정이 범위에서 모두 빠져 점이 사라진다.
    """
    a, other = make_tenant(), make_tenant()
    account_a = _account_of(db, a)
    _judged_run(db, tenant_id=a, account=account_a, started_at=METRIC_AT + timedelta(seconds=5), count=1, tag="a-in")
    newer = _judged_run(
        db, tenant_id=a, account=account_a, started_at=METRIC_AT + timedelta(seconds=20), count=0, tag="a-none"
    )
    _sg(db, newer, tenant_id=other, account=_account_of(db, other), tag="b-out")
    _sg(db, newer, tenant_id=None, account=MVP_ACCOUNT, tag="n-out0")
    _sg(db, newer, tenant_id=None, account=MVP_ACCOUNT, tag="n-out1")
    db.commit()

    mine = TenantScope((a,))
    assert [(b.observed_at, b.open_count) for b in assets_repo.sg_exposure_history(db, **_window(mine))] == [
        (METRIC_AT, 1)
    ]
    assert [(b.observed_at, b.threat) for b in assets_repo.asset_status_history(db, **_window(mine))] == [
        (METRIC_AT, 1)
    ]
    # 전체 범위는 종전대로 그 칸 A 소유의 마지막 회차(더 새 회차)의 판정 3건만 센다 — 앞선 회차와 더하지 않는다
    central = _window(ALL_TENANTS)
    assert [(b.observed_at, b.open_count) for b in assets_repo.sg_exposure_history(db, **central)] == [
        (METRIC_AT, 3)
    ]
    assert [(b.observed_at, b.threat) for b in assets_repo.asset_status_history(db, **central)] == [(METRIC_AT, 3)]


# 쌍 관제 세계: (소유, 리전) → 그 칸 회차의 SG 개방 · 위협 이벤트 · EC2 재고 수. 2의 거듭제곱이라 합이 어느 칸이 섞였는지 말한다.
# A 는 서울, B 는 도쿄를 관제하고 legacy 는 서울이다 — A 의 도쿄 · 소속 없는 도쿄는 쌍 밖이다.
PAIRED_COUNTS = {("A", REGION): 1, ("B", TOKYO): 2, (None, REGION): 4, ("A", TOKYO): 8, (None, TOKYO): 16}


def _all_histories(db, regions, scope: TenantScope) -> dict[str, list[tuple]]:
    window = {"regions": regions, "since": SINCE, "bucket_seconds": BUCKET_SECONDS, "scope": scope}
    return {
        "sg": [(b.observed_at, b.open_count) for b in assets_repo.sg_exposure_history(db, **window)],
        "status": [(b.observed_at, b.threat) for b in assets_repo.asset_status_history(db, **window)],
        "inventory": [
            (b.observed_at, b.count)
            for b in assets_repo.asset_inventory_history(db, **window)
            if b.asset_type is AssetType.EC2
        ],
        "threats": [(b.observed_at, b.count) for b in assets_repo.threat_event_history(db, until=UNTIL, **window)],
    }


@pytest.fixture()
def paired_metric_world(db, make_tenant):
    """PAIRED_COUNTS 대로 같은 칸에 회차 · 판정 · 재고 · 이벤트를 남긴다 → {라벨: tenant_id}. 쌍 밖 회차가 가장 늦다."""
    owners = {"A": make_tenant(), "B": _tenant_with_regions(db, [TOKYO]), None: None}
    for offset, ((label, region), count) in enumerate(PAIRED_COUNTS.items(), start=1):
        tenant_id = owners[label]
        account = _account_of(db, tenant_id) if tenant_id else MVP_ACCOUNT
        started = METRIC_AT + timedelta(seconds=10 * offset)
        tag = f"{(label or 'n').lower()}-{region}-"
        _judged_run(db, tenant_id=tenant_id, account=account, started_at=started, count=count, tag=tag, region=region)
        for i in range(count):
            _threat_event(db, tenant_id=tenant_id, account=account, occurred_at=started, key=f"{tag}ev{i}",
                          region=region)
    db.commit()
    return owners


@pytest.mark.parametrize(
    ("pairs", "legacy", "scope", "visible"),
    [
        ((("A", REGION), ("B", TOKYO)), (REGION,), None, (("A", REGION), ("B", TOKYO), (None, REGION))),
        ((("A", REGION), ("B", TOKYO)), (), ("A", "B"), (("A", REGION), ("B", TOKYO))),
        ((("A", REGION),), (), ("A",), (("A", REGION),)),
        # 쌍은 권한이 아니다 — 범위가 A 뿐이면 쌍의 B 를 세지 않는다
        ((("A", REGION), ("B", TOKYO)), (), ("A",), (("A", REGION),)),
        ((), (REGION,), None, ((None, REGION),)),
        ((), (), None, ()),
    ],
    ids=["central", "manager A+B", "manager A", "scope-over-pairs", "legacy only", "empty"],
)
def test_metric_histories_count_only_monitored_owner_region_pairs(
    db, paired_metric_world, pairs, legacy, scope, visible
):
    owners = paired_metric_world
    regions = MonitoredRegions(pairs=tuple((owners[label], region) for label, region in pairs), legacy=legacy)
    tenant_scope = ALL_TENANTS if scope is None else TenantScope(tuple(owners[label] for label in scope))
    total = sum(PAIRED_COUNTS[pair] for pair in visible)
    expected = [(METRIC_AT, total)] if visible else []

    for name, points in _all_histories(db, regions, tenant_scope).items():
        assert points == expected, name


def test_judgement_of_an_asset_outside_the_monitored_pairs_neither_counts_nor_hides_earlier_history(db, make_tenant):
    """A 의 서울 회차가 B(도쿄만 관제)의 서울 자산을 판정했다 — 쌍 밖 자산이라 세지 않고, 그것만 판정한 더 새 회차가
    같은 칸 A 의 앞선 판정을 가리지도 않는다. 회차만 쌍으로 거르면 2(B 의 서울 SG 둘)가 선다."""
    a, b = make_tenant(), _tenant_with_regions(db, [TOKYO])
    account_a = _account_of(db, a)
    _judged_run(db, tenant_id=a, account=account_a, started_at=METRIC_AT + timedelta(seconds=5), count=1, tag="a-in")
    newer = _judged_run(
        db, tenant_id=a, account=account_a, started_at=METRIC_AT + timedelta(seconds=20), count=0, tag="a-none"
    )
    for i in range(2):
        _sg(db, newer, tenant_id=b, account=_account_of(db, b), tag=f"b-seoul{i}")
    db.commit()

    regions = MonitoredRegions(pairs=((a, REGION), (b, TOKYO)))
    histories = _all_histories(db, regions, TenantScope((a, b)))
    assert histories["sg"] == [(METRIC_AT, 1)]
    assert histories["status"] == [(METRIC_AT, 1)]


def test_empty_monitored_regions_select_nothing_that_a_region_list_selects(db, collected, metric_world):
    nothing = MonitoredRegions(pairs=())
    reads = {
        "assets": lambda r: assets_repo.list_assets(db, regions=r),
        "latest runs": lambda r: assets_repo.latest_collection_run_per_region(db, r),
        "last finished": lambda r: assets_repo.last_finished_collection_at(db, r),
        **{
            name: (lambda r, name=name: _all_histories(db, r, ALL_TENANTS)[name])
            for name in ("sg", "status", "inventory", "threats")
        },
    }
    for name, read in reads.items():
        assert read([REGION]), name  # 같은 기록을 리전 목록은 읽는다 — 빈 결과가 우연이 아니다
        assert not read(nothing), name


def test_empty_scope_selects_nothing_that_the_full_scope_selects(db, collected, metric_world):
    _evaluate(db, collected["mine"])
    db.flush()
    nobody = TenantScope(())
    regions = [REGION]
    reads = {
        "assets": lambda s: assets_repo.list_assets(db, regions=regions, scope=s),
        "latest runs (lateral)": lambda s: assets_repo.latest_collection_run_per_region(db, regions, scope=s),
        "latest runs (distinct on)": lambda s: assets_repo.latest_collection_run_per_region(db, None, scope=s),
        "last finished": lambda s: assets_repo.last_finished_collection_at(db, regions, scope=s),
        "relationships": lambda s: assets_repo.list_all_relationships(db, scope=s),
        "evaluations": lambda s: assets_repo.latest_rule_evaluation_by_asset(db, scope=s),
        "sg exposure": lambda s: assets_repo.sg_exposure_history(db, **_window(s)),
        "asset status": lambda s: assets_repo.asset_status_history(db, **_window(s)),
        "inventory": lambda s: assets_repo.asset_inventory_history(db, **_window(s)),
        "threat events": lambda s: assets_repo.threat_event_history(
            db, regions=regions, since=SINCE, until=UNTIL, bucket_seconds=BUCKET_SECONDS, scope=s
        ),
    }
    for name, read in reads.items():
        assert read(ALL_TENANTS), name  # 같은 기록을 전체 범위는 읽는다 — 빈 결과가 우연이 아니다
        assert not read(nobody), name
