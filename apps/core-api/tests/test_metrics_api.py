# ==============================================================================
# [파일 설명]
# GET /api/v1/metrics/timeseries 통합 검증(PostgreSQL) — 시계열 6축.
#
#   축 2(SG 개방 건수)·축 4(자산 현황)·축 5(자산 수)는 실제 DB 로 검증한다. 회차별 보존이 이 기능의 전제라,
#   가짜로 대신하면 "판정이 회차마다 남는가"라는 정작 중요한 것을 안 보게 된다.
#   축 1(CPU)은 CloudWatch 호출이라 services.metrics.cpu_timeseries 를 대체한다 —
#   AWS 연동 자체는 LocalStack 테스트의 몫이다.
#   기존 축 검증은 전체 범위(CENTRAL_ADMIN)로 본다. 담당 고객사 범위는 맨 아래 「고객사 범위」 절이 검증한다.
# ==============================================================================

from __future__ import annotations

import threading
import uuid
from datetime import datetime, timedelta, timezone
from typing import NamedTuple

import pytest
from sqlalchemy import text

from schemas.api.assets import AssetType
from schemas.api.auth import PlatformRole, UserRole
from schemas.api.metrics import CpuSeries, NetworkSeries
from schemas.api.tenants import TenantStatus
from schemas.rules import RuleEvaluationResult

from db import models
from db.repositories import assets as assets_repo
from db.repositories import auth as auth_repo
from routers import metrics as metrics_router
from services.aws.client import TenantAccess, current_tenant

ACCOUNT = "123456789012"
SEOUL = "ap-northeast-2"
TOKYO = "ap-northeast-1"


def _arn(region: str, suffix: str) -> str:
    return f"arn:aws:ec2:{region}:{ACCOUNT}:security-group/sg-{suffix}"


@pytest.fixture
def platform_session(make_auth_session):
    """client_pg 의 기본 세션을 전체 범위(CENTRAL_ADMIN)로 바꾼다 — conftest 기본(TENANT_MANAGER · 담당 없음)은
    고객 기록을 하나도 보지 않으므로, 축의 집계 규칙을 보는 테스트가 범위에 가려지지 않게 한다."""
    return make_auth_session(platform_role=PlatformRole.CENTRAL_ADMIN)


@pytest.fixture
def set_regions(monkeypatch):
    """관제 대상 리전 고정 — /assets 와 같은 근거를 쓰므로 그쪽 함수를 대체한다."""

    def _apply(*regions: str) -> None:
        monkeypatch.setattr("routers.assets._configured_regions", lambda: list(regions))

    return _apply


@pytest.fixture
def no_cloudwatch(monkeypatch):
    """CloudWatch 를 쓰는 두 축(CPU·네트워크)을 빈 성공으로 고정 — SG 축만 보는 테스트가
    AWS 에 닿지 않게 한다. **축이 늘면 여기도 늘린다**: 빠뜨리면 그 축이 실제 호출로 새어
    로컬에서는 조용히 UNAVAILABLE 로, CI 에서는 느린 실패로 나타난다."""
    monkeypatch.setattr("routers.metrics.cpu_timeseries", lambda *a, **k: [])
    monkeypatch.setattr("routers.metrics.network_timeseries", lambda *a, **k: [])


@pytest.fixture(autouse=True)
def _fresh_cloudwatch_cache():
    """CloudWatch 축 결과 캐시는 프로세스 전역이다 — 테스트마다 비워 앞 테스트의 결과가 새지 않게 한다.
    대부분의 테스트가 같은 창(같은 시간대·같은 hours·빈 EC2 목록)으로 조회하므로 비우지 않으면
    앞 테스트가 담은 빈 곡선이 뒤 테스트의 가짜 곡선을 가린다."""
    metrics_router._cloudwatch_cache.clear()
    metrics_router._cloudwatch_inflight.clear()
    yield
    metrics_router._cloudwatch_cache.clear()
    metrics_router._cloudwatch_inflight.clear()


def _seed_run(db, *, region: str, started_at: datetime, tenant_id: str | None = None):
    run = assets_repo.start_collection_run(
        db,
        account_id=ACCOUNT,
        region=region,
        mode="localstack",
        lookback_days=3,
        period_seconds=3600,
    )
    run.started_at = started_at  # 기본값은 현재 시각 — 회차 시각을 테스트가 정한다
    run.tenant_id = tenant_id  # 소유 — NULL 은 소속 없는 MVP 기록(전체 범위만 본다)
    db.flush()
    return run


def _seed_sg(db, run, *, region: str, suffix: str, verdict: str, evaluated_at: datetime):
    """SG 1건 + 그 회차의 판정 1건. verdict=THREAT 가 '인터넷 개방'이다. 자산의 소유는 그 회차의 소유다."""
    arn = _arn(region, suffix)
    asset = assets_repo.upsert_asset(
        db,
        arn=arn,
        asset_type=AssetType.SG,
        resource_id=f"sg-{suffix}",
        account_id=ACCOUNT,
        region=region,
        spec={"attached": True, "open_to_world": []},
        collection_run_id=run.collection_run_id,
        collected_at=evaluated_at,
    )
    asset.tenant_id = run.tenant_id
    assets_repo.add_rule_evaluation(
        db,
        RuleEvaluationResult(
            asset_arn=arn,
            collection_run_id=run.collection_run_id,
            evaluation_status="COMPLETED",
            verdict=verdict,
            health_score=None,
            skip_reason_code="SKIP_ACTIVE" if verdict == "SKIP" else None,
            reason="테스트 시드",
            evaluated_at=evaluated_at,
        ),
    )


def _seed_judged(
    db,
    run,
    *,
    region: str,
    asset_type: AssetType,
    suffix: str,
    verdict: str | None,
    evaluated_at: datetime,
):
    """판정 대상 자산 1건 + 그 회차의 판정 1건 — 축 4용. ``verdict=None`` 은 판정 실패다."""
    kind = {AssetType.EC2: "instance/i", AssetType.SG: "security-group/sg", AssetType.EBS: "volume/vol"}
    arn = f"arn:aws:ec2:{region}:{ACCOUNT}:{kind[asset_type]}-{suffix}"
    assets_repo.upsert_asset(
        db,
        arn=arn,
        asset_type=asset_type,
        resource_id=arn.rsplit("/", 1)[-1],
        account_id=ACCOUNT,
        region=region,
        spec={},
        collection_run_id=run.collection_run_id,
        collected_at=evaluated_at,
    )
    assets_repo.add_rule_evaluation(
        db,
        RuleEvaluationResult(
            asset_arn=arn,
            collection_run_id=run.collection_run_id,
            evaluation_status="COMPLETED" if verdict is not None else "FAILED",
            verdict=verdict,
            health_score=None,
            skip_reason_code="SKIP_ACTIVE" if verdict == "SKIP" else None,
            reason="테스트 시드",
            evaluated_at=evaluated_at,
        ),
    )


def test_자산_현황은_회차마다_판정별_건수로_선다(client_pg, db, set_regions, no_cloudwatch):
    """축 4 — 유형을 가리지 않고 판정값마다 센다. 조치가 반영되면 다음 회차의 건수가 바뀐다."""
    set_regions(SEOUL)
    now = datetime.now(timezone.utc)

    before = now - timedelta(hours=2)
    run = _seed_run(db, region=SEOUL, started_at=before)
    for asset_type, suffix, verdict in (
        (AssetType.SG, "0001", "THREAT"),
        (AssetType.EC2, "0002", "COST_CANDIDATE"),
        (AssetType.EBS, "0003", "UNUSED"),
        (AssetType.EC2, "0004", "SKIP"),
        (AssetType.EC2, "0005", None),
    ):
        _seed_judged(db, run, region=SEOUL, asset_type=asset_type, suffix=suffix,
                     verdict=verdict, evaluated_at=before)

    # 다음 회차 — SG 를 닫았고(THREAT → SKIP) 미연결 볼륨을 지웠다(판정 행이 없다)
    after = now - timedelta(hours=1)
    run = _seed_run(db, region=SEOUL, started_at=after)
    for asset_type, suffix, verdict in (
        (AssetType.SG, "0001", "SKIP"),
        (AssetType.EC2, "0002", "COST_CANDIDATE"),
        (AssetType.EC2, "0004", "SKIP"),
        (AssetType.EC2, "0005", "SKIP"),
    ):
        _seed_judged(db, run, region=SEOUL, asset_type=asset_type, suffix=suffix,
                     verdict=verdict, evaluated_at=after)
    db.commit()

    axis = client_pg.get("/api/v1/metrics/timeseries").json()["asset_status"]

    assert axis["status"] == "READY"
    assert axis["reason_code"] is None
    counts = [{k: v for k, v in p.items() if k != "at"} for p in axis["points"]]
    assert counts == [
        {"judged": 5, "threat": 1, "cost_candidate": 1, "unused": 1, "skip": 1, "undecided": 1},
        {"judged": 4, "threat": 0, "cost_candidate": 1, "unused": 0, "skip": 3, "undecided": 0},
    ]
    assert [p["at"] for p in axis["points"]] == sorted(p["at"] for p in axis["points"])


def test_자산_현황도_창과_리전을_따르고_두_리전은_한_점으로_합산된다(
    client_pg, db, set_regions, no_cloudwatch
):
    set_regions(SEOUL, TOKYO)
    now = datetime.now(timezone.utc)
    # 같은 사이클의 두 리전 run — 수 초 차이로 시작해 같은 시간 칸에 떨어진다
    cycle = (now - timedelta(hours=1)).replace(minute=1, second=0, microsecond=0)
    for region, offset in ((SEOUL, 0), (TOKYO, 5)):
        at = cycle + timedelta(seconds=offset)
        run = _seed_run(db, region=region, started_at=at)
        _seed_judged(db, run, region=region, asset_type=AssetType.EC2, suffix="0001",
                     verdict="COST_CANDIDATE", evaluated_at=at)
    # 창 밖(기본 72시간)의 회차와 관제 대상이 아닌 리전의 회차는 빠진다
    old = now - timedelta(hours=100)
    run = _seed_run(db, region=SEOUL, started_at=old)
    _seed_judged(db, run, region=SEOUL, asset_type=AssetType.EC2, suffix="0009",
                 verdict="SKIP", evaluated_at=old)
    run = _seed_run(db, region="us-east-1", started_at=cycle)
    _seed_judged(db, run, region="us-east-1", asset_type=AssetType.EC2, suffix="0008",
                 verdict="SKIP", evaluated_at=cycle)
    db.commit()

    points = client_pg.get("/api/v1/metrics/timeseries").json()["asset_status"]["points"]

    assert [(p["judged"], p["cost_candidate"]) for p in points] == [(2, 2)]


def test_CloudWatch가_죽어도_자산_현황_축은_그려진다(
    client_pg, db, set_regions, monkeypatch, no_cloudwatch
):
    """대시보드가 쓰는 축이다 — 원천이 DB 라 CPU·네트워크 축의 실패에 끌려가지 않아야 한다."""
    set_regions(SEOUL)
    at = datetime.now(timezone.utc) - timedelta(hours=1)
    run = _seed_run(db, region=SEOUL, started_at=at)
    _seed_judged(db, run, region=SEOUL, asset_type=AssetType.SG, suffix="0001",
                 verdict="THREAT", evaluated_at=at)
    db.commit()

    def boom(*args, **kwargs):
        raise RuntimeError("CloudWatch 에 닿지 못했다")

    monkeypatch.setattr("routers.metrics.cpu_timeseries", boom)
    monkeypatch.setattr("routers.metrics.network_timeseries", boom)

    body = client_pg.get("/api/v1/metrics/timeseries").json()

    assert body["cpu"]["status"] == "UNAVAILABLE"
    assert body["network"]["status"] == "UNAVAILABLE"
    assert body["asset_status"]["status"] == "READY"
    assert [p["threat"] for p in body["asset_status"]["points"]] == [1]


def test_회차마다_개방_건수가_점으로_선다(client_pg, db, set_regions, no_cloudwatch):
    """이 기능의 전제 — 자산 행은 덮어써도 판정은 (자산 × 회차)로 보존된다."""
    set_regions(SEOUL)
    now = datetime.now(timezone.utc)
    for offset, threats in ((3, 1), (2, 2), (1, 0)):
        at = now - timedelta(hours=offset)
        run = _seed_run(db, region=SEOUL, started_at=at)
        for i in range(threats):
            _seed_sg(db, run, region=SEOUL, suffix=f"{i:04d}", verdict="THREAT", evaluated_at=at)
        # 개방이 아닌 SG 도 한 건 — 이것까지 세면 건수가 아니라 SG 수가 된다
        _seed_sg(db, run, region=SEOUL, suffix="9999", verdict="SKIP", evaluated_at=at)
    db.commit()

    body = client_pg.get("/api/v1/metrics/timeseries").json()

    axis = body["sg_exposure"]
    assert axis["status"] == "READY"
    assert [p["value"] for p in axis["points"]] == [1, 2, 0]
    assert axis["reason_code"] is None
    # 계약이 요구하는 오름차순 — 화면이 다시 정렬하지 않는다
    assert [p["at"] for p in axis["points"]] == sorted(p["at"] for p in axis["points"])


def test_조회_창_밖의_회차는_빠진다(client_pg, db, set_regions, no_cloudwatch):
    set_regions(SEOUL)
    now = datetime.now(timezone.utc)
    old = _seed_run(db, region=SEOUL, started_at=now - timedelta(hours=100))
    _seed_sg(db, old, region=SEOUL, suffix="0001", verdict="THREAT", evaluated_at=now - timedelta(hours=100))
    recent = _seed_run(db, region=SEOUL, started_at=now - timedelta(hours=2))
    _seed_sg(db, recent, region=SEOUL, suffix="0002", verdict="THREAT", evaluated_at=now - timedelta(hours=2))
    db.commit()

    body = client_pg.get("/api/v1/metrics/timeseries?hours=72").json()
    assert len(body["sg_exposure"]["points"]) == 1

    wide = client_pg.get("/api/v1/metrics/timeseries?hours=336").json()
    assert len(wide["sg_exposure"]["points"]) == 2


def test_두_리전의_같은_사이클은_한_점으로_합산된다(client_pg, db, set_regions, no_cloudwatch):
    """리전 격리(#231) 이후 한 사이클에 회차가 리전 수만큼 생긴다. 합치지 않으면
    곡선이 리전별 건수 사이를 오가는 톱니가 된다."""
    set_regions(SEOUL, TOKYO)
    now = datetime.now(timezone.utc)
    # 시간 칸(스캔 주기 300초) 안쪽에 못 박는다 — `now - 1h` 를 그대로 쓰면 xx:x9:58 에 도는 실행에서
    # 두 리전 run(2초 차)이 칸 경계를 넘어 두 점으로 갈린다(2026-09-28 09:49:58 실행에서 실제 발생).
    at = (now - timedelta(hours=1)).replace(minute=1, second=0, microsecond=0)

    seoul = _seed_run(db, region=SEOUL, started_at=at)
    _seed_sg(db, seoul, region=SEOUL, suffix="0001", verdict="THREAT", evaluated_at=at)
    tokyo = _seed_run(db, region=TOKYO, started_at=at + timedelta(seconds=2))
    _seed_sg(db, tokyo, region=TOKYO, suffix="0002", verdict="THREAT", evaluated_at=at)
    db.commit()

    points = client_pg.get("/api/v1/metrics/timeseries").json()["sg_exposure"]["points"]
    assert [p["value"] for p in points] == [2]


def test_관제_대상이_아닌_리전은_세지_않는다(client_pg, db, set_regions, no_cloudwatch):
    set_regions(SEOUL)
    now = datetime.now(timezone.utc)
    at = now - timedelta(hours=1)
    tokyo = _seed_run(db, region=TOKYO, started_at=at)
    _seed_sg(db, tokyo, region=TOKYO, suffix="0001", verdict="THREAT", evaluated_at=at)
    db.commit()

    assert client_pg.get("/api/v1/metrics/timeseries").json()["sg_exposure"]["points"] == []


def test_CPU축은_임계선과_창을_함께_싣는다(client_pg, set_regions, monkeypatch, no_cloudwatch):
    """임계치는 서버가 싣는다 — 화면이 상수를 따로 가지면 판정 기준과 선이 갈린다."""
    set_regions(SEOUL)
    monkeypatch.setattr(
        "routers.metrics.cpu_timeseries",
        lambda *a, **k: [
            CpuSeries(
                arn=f"arn:aws:ec2:{SEOUL}:{ACCOUNT}:instance/i-0aaa",
                resource_id="i-0aaa",
                name="seed-idle",
                points=[{"at": datetime.now(timezone.utc), "value": 2.5}],
            )
        ],
    )

    axis = client_pg.get("/api/v1/metrics/timeseries?hours=24").json()["cpu"]

    from services.rule_engine import IDLE_CPU_AVG

    assert axis["status"] == "READY"
    assert axis["idle_cpu_avg_threshold"] == IDLE_CPU_AVG
    assert axis["period_seconds"] > 0
    start = datetime.fromisoformat(axis["window_start"].replace("Z", "+00:00"))
    end = datetime.fromisoformat(axis["window_end"].replace("Z", "+00:00"))
    assert round((end - start).total_seconds() / 3600) == 24
    assert axis["series"][0]["resource_id"] == "i-0aaa"


def test_CPU_조회가_실패해도_SG축은_그려진다(client_pg, db, set_regions, monkeypatch, no_cloudwatch):
    """두 축의 원천이 다르므로 실패도 따로 받는다 — 한쪽 실패로 둘 다 비우지 않는다."""
    set_regions(SEOUL)
    now = datetime.now(timezone.utc)
    run = _seed_run(db, region=SEOUL, started_at=now - timedelta(hours=1))
    _seed_sg(db, run, region=SEOUL, suffix="0001", verdict="THREAT", evaluated_at=now - timedelta(hours=1))
    db.commit()

    def _boom(*a, **k):
        raise RuntimeError("CloudWatch 연결 실패")

    monkeypatch.setattr("routers.metrics.cpu_timeseries", _boom)

    body = client_pg.get("/api/v1/metrics/timeseries").json()

    assert body["cpu"]["status"] == "UNAVAILABLE"
    assert body["cpu"]["reason_code"] == "RuntimeError"
    assert body["cpu"]["series"] == []
    assert body["sg_exposure"]["status"] == "READY"
    assert [p["value"] for p in body["sg_exposure"]["points"]] == [1]


def test_네트워크축은_In과_Out을_한_자산_안에_싣는다(client_pg, set_regions, monkeypatch, no_cloudwatch):
    """방향을 따로 실으면 화면이 arn 으로 다시 짝지어야 한다 — 한 인스턴스의 In/Out 을
    겹쳐 그려야 '받기만 하는가'가 읽히기 때문에 계약이 짝으로 싣는다."""
    set_regions(SEOUL)
    at = datetime.now(timezone.utc)
    monkeypatch.setattr(
        "routers.metrics.network_timeseries",
        lambda *a, **k: [
            NetworkSeries(
                arn=f"arn:aws:ec2:{SEOUL}:{ACCOUNT}:instance/i-0aaa",
                resource_id="i-0aaa",
                name="seed-idle",
                in_points=[{"at": at, "value": 1024.0}],
                out_points=[{"at": at, "value": 2048.0}],
            )
        ],
    )

    axis = client_pg.get("/api/v1/metrics/timeseries?hours=24").json()["network"]

    assert axis["status"] == "READY"
    assert axis["period_seconds"] > 0
    # 초당 환산은 화면 몫이다 — 서버는 period 당 바이트를 그대로 싣는다
    assert axis["series"][0]["in_points"][0]["value"] == 1024.0
    assert axis["series"][0]["out_points"][0]["value"] == 2048.0


def test_네트워크_조회가_실패해도_CPU축은_그려진다(client_pg, set_regions, monkeypatch, no_cloudwatch):
    """CloudWatch 로 원천이 같아도 호출이 갈려 있다 — 쿼리가 2배인 네트워크 축만
    한도에 걸리는 경우가 있고, 그때 CPU 곡선까지 비우면 안 된다."""
    set_regions(SEOUL)
    monkeypatch.setattr(
        "routers.metrics.cpu_timeseries",
        lambda *a, **k: [
            CpuSeries(
                arn=f"arn:aws:ec2:{SEOUL}:{ACCOUNT}:instance/i-0aaa",
                resource_id="i-0aaa",
                name="seed-idle",
                points=[{"at": datetime.now(timezone.utc), "value": 2.5}],
            )
        ],
    )

    def _boom(*a, **k):
        raise RuntimeError("Throttling")

    monkeypatch.setattr("routers.metrics.network_timeseries", _boom)

    body = client_pg.get("/api/v1/metrics/timeseries").json()

    assert body["network"]["status"] == "UNAVAILABLE"
    assert body["network"]["reason_code"] == "RuntimeError"
    assert body["network"]["series"] == []
    assert body["network"]["period_seconds"] is None
    assert body["cpu"]["status"] == "READY"
    assert body["cpu"]["series"][0]["resource_id"] == "i-0aaa"


def test_지표_단위_실패는_AWS_상태코드를_그대로_사유로_싣는다(
    client_pg, set_regions, monkeypatch, no_cloudwatch
):
    """get_metric_data 는 쿼리 하나가 실패해도 **호출 자체는 200** 이라, 실패가
    `MetricDataResult.StatusCode` 에만 남는다(services/metrics.MetricDataError).

    그 코드를 예외 클래스명으로 환원하면 화면은 권한 문제(`Forbidden`)인지 일시 장애
    (`InternalError`)인지 구분하지 못한다 — 관제자가 할 일이 다른 두 상태다.
    """
    from schemas.assets import MetricName

    from services.metrics import MetricDataError

    set_regions(SEOUL)

    def _boom(*a, **k):
        raise MetricDataError({("i-0aaa", MetricName.NETWORK_IN): "Forbidden"})

    monkeypatch.setattr("routers.metrics.network_timeseries", _boom)

    axis = client_pg.get("/api/v1/metrics/timeseries").json()["network"]

    assert axis["status"] == "UNAVAILABLE"
    assert axis["reason_code"] == "Forbidden"
    assert axis["series"] == []


def test_조회_창은_상한을_넘길_수_없다(client_pg, set_regions, no_cloudwatch):
    """점이 인스턴스당 수백 개가 되면 화면이 읽히지 않는다 — 계약 밖 값은 422."""
    set_regions(SEOUL)
    assert client_pg.get("/api/v1/metrics/timeseries?hours=337").status_code == 422
    assert client_pg.get("/api/v1/metrics/timeseries?hours=0").status_code == 422


# ----- 축 5 자산 수 -----


def _seed_inventory(db, *, region: str, started_at: datetime, counts: dict[AssetType, int]):
    run = _seed_run(db, region=region, started_at=started_at)
    assets_repo.record_inventory_counts(db, collection_run_id=run.collection_run_id, counts=counts)
    return run


def test_자산_수는_회차마다_관측한_유형별로_선다(client_pg, db, set_regions, no_cloudwatch):
    """축 5 — 판정과 무관한 전 유형의 건수. 못 본 유형은 0이 아니라 counts 에서 빠진다."""
    set_regions(SEOUL)
    now = datetime.now(timezone.utc)
    _seed_inventory(db, region=SEOUL, started_at=now - timedelta(hours=2),
                    counts={AssetType.EC2: 4, AssetType.NACL: 2, AssetType.AUTO_SCALING_GROUP: 1})
    # 다음 회차 — EC2 하나를 지웠고, ASG 는 조회가 막혀 못 봤다(행이 없다)
    _seed_inventory(db, region=SEOUL, started_at=now - timedelta(hours=1),
                    counts={AssetType.EC2: 3, AssetType.NACL: 2})
    db.commit()

    axis = client_pg.get("/api/v1/metrics/timeseries").json()["asset_inventory"]

    assert axis["status"] == "READY"
    assert [(p["total"], p["counts"]) for p in axis["points"]] == [
        (7, {"EC2": 4, "NACL": 2, "AUTO_SCALING_GROUP": 1}),
        (5, {"EC2": 3, "NACL": 2}),
    ]


def test_자산_수는_리전을_합산하고_같은_칸의_재수집은_마지막_회차만_센다(
    client_pg, db, set_regions, no_cloudwatch
):
    set_regions(SEOUL, TOKYO)
    now = datetime.now(timezone.utc)
    cycle = (now - timedelta(hours=1)).replace(minute=1, second=0, microsecond=0)
    # 서울은 같은 칸에서 두 번 수집했다 — 합치면 자산이 두 배로 보이므로 마지막 것만 센다
    _seed_inventory(db, region=SEOUL, started_at=cycle, counts={AssetType.EC2: 9})
    _seed_inventory(db, region=SEOUL, started_at=cycle + timedelta(seconds=30),
                    counts={AssetType.EC2: 4})
    _seed_inventory(db, region=TOKYO, started_at=cycle + timedelta(seconds=5),
                    counts={AssetType.EC2: 1, AssetType.SG: 2})
    # 창 밖·관제 대상 밖 리전은 빠진다
    _seed_inventory(db, region=SEOUL, started_at=now - timedelta(hours=100),
                    counts={AssetType.EC2: 50})
    _seed_inventory(db, region="us-east-1", started_at=cycle, counts={AssetType.EC2: 50})
    db.commit()

    points = client_pg.get("/api/v1/metrics/timeseries").json()["asset_inventory"]["points"]

    assert [(p["total"], p["counts"]) for p in points] == [(7, {"EC2": 5, "SG": 2})]


def test_스냅샷이_없으면_자산_수_축은_빈_READY다(client_pg, db, set_regions, no_cloudwatch):
    """표가 생기기 전 회차는 점이 없다 — 실패가 아니다."""
    set_regions(SEOUL)
    _seed_run(db, region=SEOUL, started_at=datetime.now(timezone.utc) - timedelta(hours=1))
    db.commit()

    axis = client_pg.get("/api/v1/metrics/timeseries").json()["asset_inventory"]

    assert axis == {"status": "READY", "points": [], "reason_code": None}


def _seed_threat_event(
    db, *, region: str, event_type: str, occurred_at: datetime, key: str, tenant_id: str | None = None
):
    db.add(
        models.ThreatEvent(
            source_event_id=key,
            event_type=event_type,
            target_arn=_arn(region, key),
            payload={},
            deduplication_key=key,
            occurred_at=occurred_at,
            tenant_id=tenant_id,
        )
    )
    db.flush()


def test_위협_이벤트는_발생_칸마다_유형별로_선다(client_pg, db, set_regions, no_cloudwatch):
    """축 6 — 발생량이다. 이벤트가 없는 칸은 점이 없고(0건), 창(시작·끝)·리전 밖은 세지 않는다."""
    set_regions(SEOUL, TOKYO)
    now = datetime.now(timezone.utc)
    t1 = (now - timedelta(hours=2)).replace(minute=1, second=0, microsecond=0)
    t2 = (now - timedelta(hours=1)).replace(minute=1, second=0, microsecond=0)
    _seed_threat_event(db, region=SEOUL, event_type="SSH_BRUTE_FORCE", occurred_at=t1, key="a")
    _seed_threat_event(db, region=TOKYO, event_type="SSH_BRUTE_FORCE", occurred_at=t1 + timedelta(seconds=20), key="b")
    _seed_threat_event(db, region=SEOUL, event_type="OPEN_IP", occurred_at=t1 + timedelta(seconds=40), key="c")
    _seed_threat_event(db, region=SEOUL, event_type="OPEN_IP", occurred_at=t2, key="d")
    # 창 밖 · 관제 대상 밖 리전
    _seed_threat_event(db, region=SEOUL, event_type="OPEN_IP", occurred_at=now - timedelta(hours=100), key="e")
    _seed_threat_event(db, region="us-east-1", event_type="OPEN_IP", occurred_at=t2, key="f")
    # 발생 시각이 미래인 이벤트(시계 어긋남 · 수동 주입) — x축을 지금 이후로 늘리지 않는다
    _seed_threat_event(
        db, region=SEOUL, event_type="SSH_BRUTE_FORCE", occurred_at=now + timedelta(hours=2), key="g"
    )
    db.commit()

    axis = client_pg.get("/api/v1/metrics/timeseries").json()["threat_events"]

    assert axis["status"] == "READY"
    assert [(p["total"], p["counts"]) for p in axis["points"]] == [
        (3, {"OPEN_IP": 1, "SSH_BRUTE_FORCE": 2}),
        (1, {"OPEN_IP": 1}),
    ]


def test_위협_이벤트가_없으면_빈_READY다(client_pg, db, set_regions, no_cloudwatch):
    set_regions(SEOUL)

    axis = client_pg.get("/api/v1/metrics/timeseries").json()["threat_events"]

    assert axis == {"status": "READY", "points": [], "reason_code": None}


def test_같은_칸에서_다시_수집하면_판정_축은_마지막_회차만_센다(client_pg, db, set_regions, no_cloudwatch):
    """즉시 스캔과 타이머 스캔이 한 칸에 겹쳐도 건수가 회차 수만큼 부풀지 않는다(2026-09-30)."""
    set_regions(SEOUL)
    at = (datetime.now(timezone.utc) - timedelta(hours=1)).replace(minute=1, second=0, microsecond=0)
    first = _seed_run(db, region=SEOUL, started_at=at)
    _seed_sg(db, first, region=SEOUL, suffix="0001", verdict="THREAT", evaluated_at=at)
    second = _seed_run(db, region=SEOUL, started_at=at + timedelta(seconds=20))
    _seed_sg(db, second, region=SEOUL, suffix="0001", verdict="THREAT", evaluated_at=at)
    _seed_sg(db, second, region=SEOUL, suffix="0002", verdict="THREAT", evaluated_at=at)
    # 판정 없이 끝난(실패) 회차가 칸 끝에 와도 앞선 판정이 사라지지 않는다
    _seed_run(db, region=SEOUL, started_at=at + timedelta(seconds=40))
    db.commit()

    body = client_pg.get("/api/v1/metrics/timeseries").json()

    assert [p["value"] for p in body["sg_exposure"]["points"]] == [2]
    assert [(p["judged"], p["threat"]) for p in body["asset_status"]["points"]] == [(2, 2)]


# ── 축 격리: DB 오류 (#425) ───────────────────────────────────────────────────
# 여섯 축이 요청의 세션 하나를 차례로 쓴다. PostgreSQL 은 SQL 오류가 난 트랜잭션을 되돌릴
# 때까지 다음 문장을 전부 거절하므로, 한 축의 실패가 뒤 축을 InternalError 로 끌고 간다.
# Python 예외 주입으로는 이 경로가 열리지 않아 **실제 SQL 오류**를 같은 세션에 낸다.


def _fail_with_sql_error(db) -> None:
    """같은 세션에서 실제 SQL 오류를 낸다 — 트랜잭션이 중단 상태로 남는다."""
    db.execute(text("SELECT 1/0"))


def _seed_every_db_axis(db, *, at: datetime) -> None:
    """DB 축 4개(SG·자산 현황·자산 수·위협 이벤트)가 각자 점 하나씩 갖게 시드한다."""
    run = _seed_run(db, region=SEOUL, started_at=at)
    _seed_sg(db, run, region=SEOUL, suffix="0001", verdict="THREAT", evaluated_at=at)
    _seed_inventory(db, region=SEOUL, started_at=at + timedelta(seconds=10),
                    counts={AssetType.EC2: 2})
    _seed_threat_event(db, region=SEOUL, event_type="OPEN_IP", occurred_at=at, key="a")
    db.commit()


def test_DB_축의_SQL_오류는_그_축만_내리고_뒤_축은_그려진다(
    client_pg, db, set_regions, monkeypatch, no_cloudwatch
):
    """축 2(SG)에서 SQL 오류가 나도 뒤의 축 4·5·6은 시드한 값 그대로 READY 다."""
    set_regions(SEOUL)
    _seed_every_db_axis(
        db, at=(datetime.now(timezone.utc) - timedelta(hours=1)).replace(minute=1, second=0, microsecond=0)
    )
    monkeypatch.setattr(
        "db.repositories.assets.sg_exposure_history",
        lambda session, **kwargs: _fail_with_sql_error(session),
    )

    body = client_pg.get("/api/v1/metrics/timeseries").json()

    assert body["sg_exposure"]["status"] == "UNAVAILABLE"
    assert body["sg_exposure"]["reason_code"] == "DataError"
    assert body["asset_status"]["status"] == "READY"
    assert [(p["judged"], p["threat"]) for p in body["asset_status"]["points"]] == [(1, 1)]
    assert body["asset_inventory"]["status"] == "READY"
    assert [p["counts"] for p in body["asset_inventory"]["points"]] == [{"EC2": 2}]
    assert body["threat_events"]["status"] == "READY"
    assert [p["counts"] for p in body["threat_events"]["points"]] == [{"OPEN_IP": 1}]


def test_CPU_축의_자산_조회_SQL_오류는_네트워크와_DB_축을_끌고_가지_않는다(
    client_pg, db, set_regions, monkeypatch, no_cloudwatch
):
    """축 1(CPU)도 곡선을 그릴 EC2 목록을 DB 에서 읽는다 — 맨 앞 축의 SQL 오류가 뒤 다섯 축에 번지지 않는다."""
    set_regions(SEOUL)
    _seed_every_db_axis(
        db, at=(datetime.now(timezone.utc) - timedelta(hours=1)).replace(minute=1, second=0, microsecond=0)
    )
    original = assets_repo.list_assets
    calls = []

    def _first_call_fails(session, **kwargs):
        calls.append(kwargs)
        if len(calls) == 1:  # CPU 축의 호출만 — 네트워크 축은 같은 함수를 다시 부른다
            _fail_with_sql_error(session)
        return original(session, **kwargs)

    monkeypatch.setattr("db.repositories.assets.list_assets", _first_call_fails)

    body = client_pg.get("/api/v1/metrics/timeseries").json()

    assert body["cpu"]["status"] == "UNAVAILABLE"
    assert body["cpu"]["reason_code"] == "DataError"
    assert body["network"]["status"] == "READY"
    assert [p["value"] for p in body["sg_exposure"]["points"]] == [1]
    for axis in ("sg_exposure", "asset_status", "asset_inventory", "threat_events"):
        assert body[axis]["status"] == "READY", axis


def test_세션을_되돌리지_못해도_응답은_축마다_상태를_싣는다(
    client_pg, db, set_regions, monkeypatch, no_cloudwatch
):
    """되돌리기마저 실패하면(연결 단절 등) 뒤 축은 각자 UNAVAILABLE 로 내려간다 — 응답 전체가 500 이 되지 않는다."""
    set_regions(SEOUL)
    _seed_every_db_axis(
        db, at=(datetime.now(timezone.utc) - timedelta(hours=1)).replace(minute=1, second=0, microsecond=0)
    )
    broken = {"after_sg": False}

    def _sg_fails(session, **kwargs):
        broken["after_sg"] = True
        _fail_with_sql_error(session)

    monkeypatch.setattr("db.repositories.assets.sg_exposure_history", _sg_fails)
    real_rollback = db.rollback

    def _rollback_fails_after_sg():
        # 앞 축(CPU·네트워크)이 EC2 목록을 읽고 끝내는 되돌리기는 그대로 둔다 — SG 오류 뒤부터 끊긴다
        if broken["after_sg"]:
            raise RuntimeError("연결이 끊겼다")
        real_rollback()

    monkeypatch.setattr(db, "rollback", _rollback_fails_after_sg)

    response = client_pg.get("/api/v1/metrics/timeseries")

    assert response.status_code == 200
    body = response.json()
    assert body["cpu"]["status"] == "READY"
    assert body["network"]["status"] == "READY"
    assert body["sg_exposure"]["reason_code"] == "DataError"
    for axis in ("asset_status", "asset_inventory", "threat_events"):
        assert body[axis]["status"] == "UNAVAILABLE", axis
        assert body[axis]["reason_code"] == "InternalError", axis


# ── CloudWatch 축: 조회 창 정렬과 결과 재사용 (#425) ─────────────────────────────
# 실제 cpu_timeseries·network_timeseries·_fetch_metrics 를 지나게 두고 클라이언트만 가짜로 바꾼다 —
# "CloudWatch 를 몇 번 불렀나"는 get_metric_data 호출 수로 세야 의미가 있다.

HOUR = timedelta(hours=1)
BOUNDARY = datetime(2026, 10, 7, 10, 0, tzinfo=timezone.utc)


def _at(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class _CountingCloudWatch:
    """get_metric_data 만 흉내 낸다 — 받은 창을 period 로 잘라 칸마다 점 하나를 돌려준다.

    값은 칸 시작 시각에서 만든다. 창 경계가 움직이면 같은 시각대라도 칸과 값이 달라지므로,
    두 조회의 점이 같다는 단언이 "같은 창으로 불렀다"를 함께 증명한다. ``statuses`` 는 호출마다
    하나씩 꺼내 쓰는 StatusCode 다(비면 ``Complete``).
    """

    def __init__(self, statuses: list[str] | None = None):
        self.calls: list[dict] = []
        self._statuses = list(statuses or [])

    def get_metric_data(self, **kwargs):
        self.calls.append(kwargs)
        start, end = kwargs["StartTime"], kwargs["EndTime"]
        period = timedelta(seconds=kwargs["MetricDataQueries"][0]["MetricStat"]["Period"])
        stamps = []
        t = start
        while t < end:
            stamps.append(t)
            t += period
        status = self._statuses.pop(0) if self._statuses else "Complete"
        return {
            "MetricDataResults": [
                {
                    "Id": q["Id"],
                    "StatusCode": status,
                    "Timestamps": stamps if status == "Complete" else [],
                    "Values": [round(s.timestamp() / 3600 % 100, 2) for s in stamps]
                    if status == "Complete" else [],
                }
                for q in kwargs["MetricDataQueries"]
            ]
        }


@pytest.fixture
def cloudwatch(monkeypatch):
    """services.metrics 가 부르는 클라이언트를 가짜로 바꾸고, 조회 시각을 테스트가 정하게 한다."""

    def _install(client: _CountingCloudWatch, clock: dict) -> None:
        monkeypatch.setattr("services.metrics.aws_client", lambda service, region: client)
        monkeypatch.setattr("routers.metrics._now", lambda: clock["now"])

    return _install


def _seed_ec2(db, *, suffix: str) -> None:
    run = _seed_run(db, region=SEOUL, started_at=BOUNDARY - 2 * HOUR)
    assets_repo.upsert_asset(
        db,
        arn=f"arn:aws:ec2:{SEOUL}:{ACCOUNT}:instance/i-{suffix}",
        asset_type=AssetType.EC2,
        resource_id=f"i-{suffix}",
        account_id=ACCOUNT,
        region=SEOUL,
        spec={},
        collection_run_id=run.collection_run_id,
        collected_at=BOUNDARY - 2 * HOUR,
    )
    db.commit()


def _cloudwatch_axes(body: dict) -> dict:
    """CPU·네트워크 축에서 창과 점만 — 재사용 여부를 비교할 단위."""
    return {name: {k: body[name][k] for k in ("window_start", "window_end", "series")}
            for name in ("cpu", "network")}


@pytest.mark.parametrize(
    ("now", "hours", "period", "expected"),
    [
        # 정착 지연(10분)이 지난 뒤 — 끝은 직전 정각, 진행 중인 칸은 빠진다
        (BOUNDARY + timedelta(minutes=12), 3, 3600, (BOUNDARY - 3 * HOUR, BOUNDARY)),
        # 정착 지연 안 — 아직 한 칸 앞의 창이다
        (BOUNDARY + timedelta(minutes=9, seconds=59), 3, 3600, (BOUNDARY - 4 * HOUR, BOUNDARY - HOUR)),
        (BOUNDARY + timedelta(minutes=10), 3, 3600, (BOUNDARY - 3 * HOUR, BOUNDARY)),
        # 입자가 5분이면 경계도 5분 단위다
        (BOUNDARY + timedelta(minutes=17), 1, 300, (BOUNDARY - timedelta(minutes=55), BOUNDARY + timedelta(minutes=5))),
        # 창 길이가 입자 배수가 아니면 올려 잡는다(5시간 → 1일)
        (BOUNDARY, 5, 86400,
         (datetime(2026, 10, 6, tzinfo=timezone.utc), datetime(2026, 10, 7, tzinfo=timezone.utc))),
    ],
)
def test_CloudWatch_조회_창은_입자_경계에_맞춘다(now, hours, period, expected):
    assert metrics_router._cloudwatch_window(now, hours=hours, period_seconds=period) == expected


def test_같은_입자_안에서는_CloudWatch를_다시_부르지_않고_점도_같다(client_pg, db, set_regions, cloudwatch):
    """완료 조건 2 — 성공한 조회는 입자당 한 번이고, 그 사이의 조회는 시각·값이 같은 점을 받는다."""
    set_regions(SEOUL)
    _seed_ec2(db, suffix="0001")
    cw, clock = _CountingCloudWatch(), {}
    cloudwatch(cw, clock)

    bodies = []
    # 10:12 · 10:48 · 다음 정각을 넘겼지만 정착 지연 안인 11:05 — 셋 다 같은 창이다
    for minutes in (12, 48, 65):
        clock["now"] = BOUNDARY + timedelta(minutes=minutes)
        bodies.append(client_pg.get("/api/v1/metrics/timeseries?hours=3").json())

    assert len(cw.calls) == 2  # CPU 1회 + 네트워크 1회
    assert _cloudwatch_axes(bodies[0]) == _cloudwatch_axes(bodies[1]) == _cloudwatch_axes(bodies[2])
    cpu = bodies[0]["cpu"]
    assert cpu["status"] == "READY"
    assert (_at(cpu["window_start"]), _at(cpu["window_end"])) == (BOUNDARY - 3 * HOUR, BOUNDARY)
    # 마지막 점은 끝난 칸이다 — 진행 중인 10시 칸은 그리지 않는다
    assert [_at(p["at"]) for p in cpu["series"][0]["points"]] == [
        BOUNDARY - 3 * HOUR, BOUNDARY - 2 * HOUR, BOUNDARY - HOUR
    ]
    assert {(c["StartTime"], c["EndTime"]) for c in cw.calls} == {(BOUNDARY - 3 * HOUR, BOUNDARY)}


def test_입자_경계를_지나면_한_칸_밀리고_그때만_다시_부른다(client_pg, db, set_regions, cloudwatch):
    set_regions(SEOUL)
    _seed_ec2(db, suffix="0001")
    cw, clock = _CountingCloudWatch(), {}
    cloudwatch(cw, clock)

    clock["now"] = BOUNDARY + timedelta(minutes=48)
    before = client_pg.get("/api/v1/metrics/timeseries?hours=3").json()
    clock["now"] = BOUNDARY + timedelta(minutes=72)
    after = client_pg.get("/api/v1/metrics/timeseries?hours=3").json()

    assert len(cw.calls) == 4
    for name, points_key in (("cpu", "points"), ("network", "in_points")):
        before_at = [_at(p["at"]) for p in before[name]["series"][0][points_key]]
        after_at = [_at(p["at"]) for p in after[name]["series"][0][points_key]]
        assert after_at == [at + HOUR for at in before_at], name
        assert _at(after[name]["window_end"]) == BOUNDARY + HOUR, name
    # 지난 창의 결과는 버린다 — 단일 프로세스에 입자마다 쌓이지 않는다
    assert {k.window_end for k in metrics_router._cloudwatch_cache} == {BOUNDARY + HOUR}


def test_실패한_조회는_재사용하지_않고_다음_조회에서_다시_부른다(client_pg, db, set_regions, cloudwatch):
    """스로틀·권한 오류 한 번으로 그 축이 한 입자 내내 비지 않는다 — 실패는 담지 않는다."""
    set_regions(SEOUL)
    _seed_ec2(db, suffix="0001")
    cw, clock = _CountingCloudWatch(statuses=["Forbidden"]), {"now": BOUNDARY + timedelta(minutes=12)}
    cloudwatch(cw, clock)

    first = client_pg.get("/api/v1/metrics/timeseries?hours=3").json()
    second = client_pg.get("/api/v1/metrics/timeseries?hours=3").json()

    assert (first["cpu"]["status"], first["cpu"]["reason_code"]) == ("UNAVAILABLE", "Forbidden")
    assert first["network"]["status"] == "READY"
    assert second["cpu"]["status"] == "READY"
    assert len(second["cpu"]["series"][0]["points"]) == 3
    # CPU 는 두 번(실패 → 재시도), 성공한 네트워크는 첫 조회의 결과를 다시 쓴다
    assert len(cw.calls) == 3


def test_곡선_대상이_바뀌면_같은_창에서도_다시_부른다(client_pg, db, set_regions, cloudwatch):
    """입자 중간에 새로 수집된 인스턴스가 다음 경계까지 곡선 목록에서 빠지지 않는다."""
    set_regions(SEOUL)
    _seed_ec2(db, suffix="0001")
    cw, clock = _CountingCloudWatch(), {"now": BOUNDARY + timedelta(minutes=12)}
    cloudwatch(cw, clock)

    client_pg.get("/api/v1/metrics/timeseries?hours=3")
    _seed_ec2(db, suffix="0002")
    body = client_pg.get("/api/v1/metrics/timeseries?hours=3").json()

    assert len(cw.calls) == 4
    assert [len(s["points"]) for s in body["cpu"]["series"]] == [3, 3]


def test_CloudWatch를_부르는_동안_DB_트랜잭션을_쥐지_않는다(client_pg, db, set_regions, cloudwatch):
    """조회를 기다리는 동안 풀 연결을 쥐면 CloudWatch 가 느릴 때 다른 API 까지 연결을 못 얻는다."""
    set_regions(SEOUL)
    _seed_ec2(db, suffix="0001")
    seen: list[bool] = []

    class _Observing(_CountingCloudWatch):
        def get_metric_data(self, **kwargs):
            seen.append(db.in_transaction())
            return super().get_metric_data(**kwargs)

    cloudwatch(_Observing(), {"now": BOUNDARY + timedelta(minutes=12)})
    client_pg.get("/api/v1/metrics/timeseries?hours=3")

    assert seen == [False, False]  # CPU · 네트워크


# ── 동시 조회: 진행 중인 조회 하나를 나눈다 (#425) ─────────────────────────────
# 동기 엔드포인트라 요청이 스레드풀에서 겹친다. 스레드로 그 겹침을 직접 만든다 — 조회 함수가
# 붙잡혀 있는 동안 다른 요청이 무엇을 하는지(기다리는가 · 따로 부르는가 · 막히는가)를 본다.

WAIT = 5  # 초 — 실패 시 테스트가 영원히 멈추지 않게 하는 상한일 뿐, 정상 경로는 즉시 끝난다


def _key(axis: str = "cpu") -> metrics_router._CloudWatchKey:
    return metrics_router._CloudWatchKey(axis, BOUNDARY - 3 * HOUR, BOUNDARY, 3600, ())


class _HeldFetch:
    """부르면 ``release`` 될 때까지 붙잡히는 조회 함수. 호출 수를 센다."""

    def __init__(self, *, result: list | None = None, error: Exception | None = None):
        self.calls = 0
        self.entered = threading.Event()
        self.release = threading.Event()
        self._result = result if result is not None else ["series"]
        self._error = error

    def __call__(self):
        self.calls += 1
        self.entered.set()
        self.release.wait(WAIT)
        if self._error is not None:
            raise self._error
        return self._result


def _call_in_thread(key, fetch) -> tuple[threading.Thread, dict]:
    outcome: dict = {}

    def _run():
        try:
            outcome["value"] = metrics_router._cached_cloudwatch(key, fetch)
        except Exception as exc:  # 스레드 밖에서 단언하려고 담아 둔다
            outcome["error"] = exc

    thread = threading.Thread(target=_run)
    thread.start()
    return thread, outcome


def test_같은_키로_동시에_들어온_조회는_진행_중인_한_번을_나눠_받는다():
    fetch = _HeldFetch()
    first, first_out = _call_in_thread(_key(), fetch)
    try:
        assert fetch.entered.wait(WAIT)
        second, second_out = _call_in_thread(_key(), fetch)
        second.join(0.2)
        assert second.is_alive()  # 따로 부르지 않고 기다린다
        assert fetch.calls == 1
    finally:
        fetch.release.set()
    first.join(WAIT)
    second.join(WAIT)

    assert fetch.calls == 1
    assert first_out["value"] is second_out["value"]


def test_진행_중인_실패는_기다리던_요청도_받고_다음_조회는_다시_부른다():
    """기다리던 요청은 같은 조회의 실패를 받는다. 실패는 담지 않으므로 끝난 뒤의 조회는 새로 부른다."""
    fetch = _HeldFetch(error=RuntimeError("스로틀"))
    first, first_out = _call_in_thread(_key(), fetch)
    try:
        assert fetch.entered.wait(WAIT)
        second, second_out = _call_in_thread(_key(), fetch)
        second.join(0.2)
    finally:
        fetch.release.set()
    first.join(WAIT)
    second.join(WAIT)

    assert fetch.calls == 1
    assert isinstance(first_out["error"], RuntimeError)
    assert second_out["error"] is first_out["error"]

    retry = _HeldFetch(result=["recovered"])
    retry.release.set()
    assert metrics_router._cached_cloudwatch(_key(), retry) == ["recovered"]
    assert retry.calls == 1


def test_한_키의_조회가_붙잡혀_있어도_다른_키는_기다리지_않는다():
    """느리게 실패하는 CPU 조회 뒤로 네트워크 축이 줄을 서지 않는다."""
    slow = _HeldFetch()
    held, _ = _call_in_thread(_key("cpu"), slow)
    try:
        assert slow.entered.wait(WAIT)
        fast = _HeldFetch(result=["network"])
        fast.release.set()
        other, other_out = _call_in_thread(_key("network"), fast)
        other.join(WAIT)
        assert not other.is_alive()
        assert other_out["value"] == ["network"]
    finally:
        slow.release.set()
    held.join(WAIT)


# ── 고객사 범위 (#492 PR 5 · #533) ─────────────────────────────────────────────
# 담당자(TENANT_MANAGER)는 배정 고객사만, 중앙(CENTRAL_ADMIN)은 소속 없는 MVP 기록까지 전부, 담당이 없으면
# 아무것도 보지 않는다. 세 소유(A · B · 소속 없음)가 같은 리전 · 같은 칸에 회차를 하나씩 남기고 건수를 1 · 2 · 4 로
# 갈라 둔다 — 합 하나로 어느 소유가 섞였는지 드러난다. 같은 칸이라 범위를 집계 뒤에 걸거나 칸의 「마지막 회차」를
# 고객사 구분 없이 고르면 숫자가 어긋난다. 세 소유의 EC2 는 같은 리전 · 같은 인스턴스 ID 다(계정만 다르다).

URL = "/api/v1/metrics/timeseries"
_OWNED_COUNTS = {"A": 1, "B": 2, None: 4}
_SHARED_INSTANCE = "i-0001"
_DB_AXES = ("sg_exposure", "asset_status", "asset_inventory", "threat_events")


class _Owned(NamedTuple):
    tenant_id: str | None
    ec2_arn: str


def _seed_owned_ec2(db, run, *, resource_id: str = _SHARED_INSTANCE) -> str:
    """그 회차 소유의 EC2 1대 → ARN. ARN 의 계정은 소유 고객사의 계정이다(소속 없으면 MVP 계정)."""
    account = ACCOUNT if run.tenant_id is None else db.get(models.Tenant, run.tenant_id).aws_account_id
    arn = f"arn:aws:ec2:{run.region}:{account}:instance/{resource_id}"
    asset = assets_repo.upsert_asset(
        db,
        arn=arn,
        asset_type=AssetType.EC2,
        resource_id=resource_id,
        account_id=account,
        region=run.region,
        spec={},
        collection_run_id=run.collection_run_id,
        collected_at=run.started_at,
    )
    asset.tenant_id = run.tenant_id
    db.flush()
    return arn


def _seed_cell(db, *, tenant_id: str | None, region: str, count: int, at: datetime, tag: str) -> str:
    """한 소유 · 한 리전의 회차 1개 — 개방 SG · 위협 이벤트 · EC2 자산 수가 모두 count 건, EC2 1대 → 그 ARN."""
    run = _seed_run(db, region=region, started_at=at, tenant_id=tenant_id)
    for i in range(count):
        _seed_sg(db, run, region=region, suffix=f"{tag}{i:03d}", verdict="THREAT", evaluated_at=at)
        _seed_threat_event(db, region=region, event_type="OPEN_IP", occurred_at=at,
                           key=f"{tag}{i:03d}", tenant_id=tenant_id)
    assets_repo.record_inventory_counts(
        db, collection_run_id=run.collection_run_id, counts={AssetType.EC2: count}
    )
    return _seed_owned_ec2(db, run)


def _seed_owned_world(db, make_tenant) -> dict[str | None, _Owned]:
    """A · B 고객사(ACTIVE · 관제 리전 서울)와 소속 없는 기록을 같은 칸에 시드한다 → {라벨: 소유}."""
    tenants = {"A": make_tenant(), "B": make_tenant(), None: None}
    at = (datetime.now(timezone.utc) - timedelta(hours=1)).replace(minute=1, second=0, microsecond=0)
    world: dict[str | None, _Owned] = {}
    for offset, (label, tenant_id) in enumerate(tenants.items()):
        arn = _seed_cell(db, tenant_id=tenant_id, region=SEOUL, count=_OWNED_COUNTS[label],
                         at=at + timedelta(seconds=5 * offset), tag=(label or "n").lower())
        world[label] = _Owned(tenant_id, arn)
    db.commit()
    return world


def _assign(db, user_id: str, tenant_ids) -> None:
    auth_repo.replace_assigned_tenants(
        db, user_id=user_id, tenant_ids=tuple(tenant_ids), now=datetime.now(timezone.utc)
    )
    db.commit()


def _login_manager(login_as, db, tenant_ids):
    """client_pg 를 담당자(TENANT_MANAGER) 세션으로 바꾸고 tenant_ids 를 배정한다."""
    seed = login_as(UserRole.ADMIN, platform_role=PlatformRole.TENANT_MANAGER)
    _assign(db, seed.user_id, tenant_ids)
    return seed


class _OwnerCloudWatch:
    """services.metrics.aws_client 대역. 클라이언트를 만든 순간의 테넌트 문맥(= 그 호출이 나간 자격)과
    물은 인스턴스 ID 를 남긴다. 값은 소유마다 달라(`values`, None = 프로세스 자격) 곡선이 다른 소유의
    ARN 에 실리면 드러난다. `failing` 의 소유는 지표 단위 Forbidden 으로 답한다."""

    def __init__(self, values: dict[str | None, float], failing: frozenset):
        self.values = values
        self.failing = failing
        self.calls: list[tuple[TenantAccess | None, str, tuple[str, ...]]] = []

    def __call__(self, service: str, region: str):
        access = current_tenant()
        owner = access.tenant_id if access is not None else None
        factory = self

        class _Client:
            def get_metric_data(self, **kwargs):
                queries = kwargs["MetricDataQueries"]
                ids = tuple(sorted({q["MetricStat"]["Metric"]["Dimensions"][0]["Value"] for q in queries}))
                factory.calls.append((access, region, ids))
                ok = owner not in factory.failing
                return {
                    "MetricDataResults": [
                        {
                            "Id": q["Id"],
                            "StatusCode": "Complete" if ok else "Forbidden",
                            "Timestamps": [kwargs["StartTime"]] if ok else [],
                            "Values": [factory.values.get(owner, 1.0)] if ok else [],
                        }
                        for q in queries
                    ]
                }

        return _Client()


@pytest.fixture
def owner_cloudwatch(monkeypatch):
    def _install(values=None, failing=()) -> _OwnerCloudWatch:
        fake = _OwnerCloudWatch(dict(values or {}), frozenset(failing))
        monkeypatch.setattr("services.metrics.aws_client", fake)
        return fake

    return _install


def _callers(cw: _OwnerCloudWatch) -> list[str | None]:
    """호출마다 나간 자격의 고객사(None = 프로세스 자격)."""
    return [access.tenant_id if access is not None else None for access, _, _ in cw.calls]


def _db_totals(body: dict) -> dict[str, list[int]]:
    """DB 네 축의 점별 합 — 시드가 한 칸이라 점이 있으면 하나다."""
    return {
        "sg_exposure": [p["value"] for p in body["sg_exposure"]["points"]],
        "asset_status": [p["threat"] for p in body["asset_status"]["points"]],
        "asset_inventory": [p["total"] for p in body["asset_inventory"]["points"]],
        "threat_events": [p["total"] for p in body["threat_events"]["points"]],
    }


def _series_values(body: dict, axis: str, points: str) -> dict[str, list[float]]:
    return {s["arn"]: [p["value"] for p in s[points]] for s in body[axis]["series"]}


@pytest.mark.parametrize(
    ("assigned", "visible"),
    [
        (None, ("A", "B", None)),
        (("A",), ("A",)),
        (("B",), ("B",)),
        (("A", "B"), ("A", "B")),
        ((), ()),
    ],
    ids=["central", "manager-A", "manager-B", "manager-AB", "manager-unassigned"],
)
def test_여섯_축은_범위_안_소유만_집계하고_그_소유의_자격으로만_부른다(
    client_pg, db, set_regions, make_tenant, login_as, owner_cloudwatch, assigned, visible
):
    set_regions(SEOUL)
    world = _seed_owned_world(db, make_tenant)
    if assigned is not None:  # None 은 client_pg 기본(중앙) 그대로
        _login_manager(login_as, db, [world[label].tenant_id for label in assigned])
    cw = owner_cloudwatch()

    body = client_pg.get(URL).json()

    for axis in ("cpu", "network", *_DB_AXES):
        assert (body[axis]["status"], body[axis]["reason_code"]) == ("READY", None), axis
    total = sum(_OWNED_COUNTS[label] for label in visible)
    assert _db_totals(body) == {axis: [total] if visible else [] for axis in _DB_AXES}
    arns = {world[label].ec2_arn for label in visible}
    assert set(_series_values(body, "cpu", "points")) == arns
    assert set(_series_values(body, "network", "in_points")) == arns
    # 소유마다 CPU · 네트워크 한 번씩, 자기 인스턴스만 그 자격으로. 소속 없는 기록(None)은 중앙만 프로세스
    # 자격으로 부르고, 담당이 없으면 AWS 를 부르지 않는다.
    owners = [world[label].tenant_id for label in visible]
    assert sorted(_callers(cw), key=str) == sorted(owners * 2, key=str)
    assert {ids for _, _, ids in cw.calls} <= {(_SHARED_INSTANCE,)}


def test_담당이_바뀌면_다음_요청부터_바뀐_범위로_본다(
    client_pg, db, set_regions, make_tenant, login_as, owner_cloudwatch
):
    set_regions(SEOUL)
    world = _seed_owned_world(db, make_tenant)
    a, b = world["A"], world["B"]
    manager = _login_manager(login_as, db, [a.tenant_id])
    cw = owner_cloudwatch()

    first = client_pg.get(URL).json()
    _assign(db, manager.user_id, [b.tenant_id])
    second = client_pg.get(URL).json()
    _assign(db, manager.user_id, [])
    third = client_pg.get(URL).json()

    assert _db_totals(first) == {axis: [1] for axis in _DB_AXES}
    assert set(_series_values(first, "cpu", "points")) == {a.ec2_arn}
    assert _db_totals(second) == {axis: [2] for axis in _DB_AXES}
    assert set(_series_values(second, "cpu", "points")) == {b.ec2_arn}
    assert _db_totals(third) == {axis: [] for axis in _DB_AXES}
    assert third["cpu"]["series"] == third["network"]["series"] == []
    # 같은 창이어도 A 의 결과를 B 에 다시 쓰지 않는다 — B 는 B 자격으로 새로 불렀다. 회수 뒤에는 부르지 않는다
    assert _callers(cw) == [a.tenant_id, a.tenant_id, b.tenant_id, b.tenant_id]


def test_같은_리전_같은_인스턴스_ID라도_소유마다_따로_부르고_결과를_섞어_쓰지_않는다(
    client_pg, db, set_regions, make_tenant, login_as, owner_cloudwatch
):
    """중앙은 소유마다 곡선을 그 ARN 에 싣는다. 담당자의 같은 창 조회는 중앙이 담은 결과(대상이 다르다)를 쓰지
    않고 자기 고객사 자격으로 부르며, 그 결과는 같은 담당 범위의 다음 조회가 다시 쓴다."""
    set_regions(SEOUL)
    world = _seed_owned_world(db, make_tenant)
    values = {world["A"].tenant_id: 11.0, world["B"].tenant_id: 22.0, None: 44.0}
    cw = owner_cloudwatch(values)

    central = client_pg.get(URL).json()

    expected = {owned.ec2_arn: [values[owned.tenant_id]] for owned in world.values()}
    assert _series_values(central, "cpu", "points") == expected
    assert _series_values(central, "network", "in_points") == expected
    assert _series_values(central, "network", "out_points") == expected
    assert len(cw.calls) == 6

    _login_manager(login_as, db, [world["A"].tenant_id])
    mine = client_pg.get(URL).json()
    again = client_pg.get(URL).json()

    assert _series_values(mine, "cpu", "points") == {world["A"].ec2_arn: [11.0]}
    assert _callers(cw)[6:] == [world["A"].tenant_id] * 2
    assert again["cpu"] == mine["cpu"] and again["network"] == mine["network"]


@pytest.mark.parametrize("status", [TenantStatus.SUSPENDED, TenantStatus.PENDING_CONNECTION])
def test_쓸_수_없는_고객사_자격은_빈_성공이_아니라_UNAVAILABLE이고_AWS를_부르지_않는다(
    client_pg, db, set_regions, make_tenant, login_as, owner_cloudwatch, status
):
    set_regions(SEOUL)
    tenant_id = make_tenant(status)
    at = (datetime.now(timezone.utc) - timedelta(hours=1)).replace(minute=1, second=0, microsecond=0)
    run = _seed_run(db, region=SEOUL, started_at=at, tenant_id=tenant_id)
    _seed_sg(db, run, region=SEOUL, suffix="0001", verdict="THREAT", evaluated_at=at)
    _seed_owned_ec2(db, run)
    db.commit()
    _login_manager(login_as, db, [tenant_id])
    cw = owner_cloudwatch()

    body = client_pg.get(URL).json()

    for axis in ("cpu", "network"):
        assert (body[axis]["status"], body[axis]["reason_code"]) == (
            "UNAVAILABLE", f"TENANT_{status.value}"
        ), axis
        assert body[axis]["series"] == [], axis
    assert cw.calls == []
    # 기록 조회는 자격과 무관하다 — DB 축은 그대로 그려진다
    assert [p["value"] for p in body["sg_exposure"]["points"]] == [1]


@pytest.mark.parametrize("status", [TenantStatus.SUSPENDED, TenantStatus.PENDING_CONNECTION])
@pytest.mark.parametrize("manager", [False, True], ids=["central", "manager"])
def test_ACTIVE가_아닌_고객사만_빠지고_나머지_고객사의_곡선은_그려진다(
    client_pg, db, set_regions, make_tenant, login_as, owner_cloudwatch, monkeypatch, status, manager
):
    set_regions(SEOUL)
    active, inactive = make_tenant(), make_tenant(status)
    at = (datetime.now(timezone.utc) - timedelta(hours=1)).replace(minute=1, second=0, microsecond=0)
    active_arn = _seed_cell(db, tenant_id=active, region=SEOUL, count=1, at=at, tag="a")
    _seed_cell(db, tenant_id=inactive, region=SEOUL, count=2, at=at, tag="b")
    db.commit()
    if manager:
        _login_manager(login_as, db, [active, inactive])
    cw = owner_cloudwatch()
    in_transaction: list[bool] = []

    def _observing(service: str, region: str):
        in_transaction.append(db.in_transaction())  # CloudWatch 클라이언트를 만드는 호출 경계
        return cw(service, region)

    monkeypatch.setattr("services.metrics.aws_client", _observing)

    body = client_pg.get(URL).json()

    for axis in ("cpu", "network"):
        assert (body[axis]["status"], body[axis]["reason_code"]) == ("READY", None), axis
    assert set(_series_values(body, "cpu", "points")) == {active_arn}
    assert set(_series_values(body, "network", "in_points")) == {active_arn}
    assert _callers(cw) == [active, active]
    assert in_transaction == [False, False]  # CPU · 네트워크 — 호출 동안 DB 트랜잭션을 쥐지 않는다
    # DB 축은 자격과 무관하게 두 고객사를 모두 센다
    assert _db_totals(body)["sg_exposure"] == [3]


def test_한_고객사의_조회_실패는_전체_범위의_축을_내리고_다른_담당_범위로_번지지_않는다(
    client_pg, db, set_regions, make_tenant, login_as, owner_cloudwatch
):
    set_regions(SEOUL)
    world = _seed_owned_world(db, make_tenant)
    cw = owner_cloudwatch(failing={world["B"].tenant_id})

    central = client_pg.get(URL).json()

    # A · 소속 없음 곡선만 성공으로 남기지 않는다 — 한 고객사가 빠졌다고 적을 자리가 없다
    for axis in ("cpu", "network"):
        assert (central[axis]["status"], central[axis]["reason_code"]) == ("UNAVAILABLE", "Forbidden")
        assert central[axis]["series"] == []

    _login_manager(login_as, db, [world["A"].tenant_id])
    mark = len(cw.calls)
    mine = client_pg.get(URL).json()

    assert mine["cpu"]["status"] == mine["network"]["status"] == "READY"
    assert set(_series_values(mine, "cpu", "points")) == {world["A"].ec2_arn}
    assert _callers(cw)[mark:] == [world["A"].tenant_id] * 2


def test_역할_ARN이나_ExternalId가_바뀌면_같은_창에서도_새_자격으로_다시_부르고_밖에_싣지_않는다(
    client_pg, db, set_regions, make_tenant, login_as, owner_cloudwatch
):
    set_regions(SEOUL)
    tenant_id = make_tenant()
    at = (datetime.now(timezone.utc) - timedelta(hours=1)).replace(minute=1, second=0, microsecond=0)
    _seed_owned_ec2(db, _seed_run(db, region=SEOUL, started_at=at, tenant_id=tenant_id))
    db.commit()
    _login_manager(login_as, db, [tenant_id])
    cw = owner_cloudwatch()

    client_pg.get(URL)
    tenant = db.get(models.Tenant, tenant_id)
    rotated_role = f"arn:aws:iam::{tenant.aws_account_id}:role/vigilantis-rotated"
    rotated_external_id = uuid.uuid4().hex
    tenant.access_role_arn = rotated_role
    tenant.external_id = rotated_external_id
    db.commit()
    response = client_pg.get(URL)

    used = [access for access, _, _ in cw.calls]
    assert len(used) == 4  # CPU · 네트워크 × 두 요청 — 바뀐 자격은 앞 결과를 다시 쓰지 않는다
    assert used[0] == used[1] != used[2] == used[3]
    assert (used[2].access_role_arn, used[2].external_id) == (rotated_role, rotated_external_id)
    assert response.json()["cpu"]["status"] == "READY"
    assert rotated_external_id not in response.text
    assert rotated_external_id not in repr(list(metrics_router._cloudwatch_cache))


def test_고객사_관제_리전은_설정_리전_밖이어도_담당자와_중앙이_함께_본다(
    client_pg, db, set_regions, make_tenant, login_as, owner_cloudwatch
):
    """설정 리전(AWS_REGIONS)은 소속 없는 MVP 기록의 관제 범위다 — 고객사 기록은 그 고객사의 관제 리전을 따른다."""
    set_regions(SEOUL)
    tenant_id = make_tenant()
    db.get(models.Tenant, tenant_id).regions = [TOKYO]
    at = (datetime.now(timezone.utc) - timedelta(hours=1)).replace(minute=1, second=0, microsecond=0)
    run = _seed_run(db, region=TOKYO, started_at=at, tenant_id=tenant_id)
    _seed_sg(db, run, region=TOKYO, suffix="0001", verdict="THREAT", evaluated_at=at)
    db.commit()
    owner_cloudwatch()

    central = client_pg.get(URL).json()
    _login_manager(login_as, db, [tenant_id])
    mine = client_pg.get(URL).json()

    assert [p["value"] for p in central["sg_exposure"]["points"]] == [1]
    assert [p["value"] for p in mine["sg_exposure"]["points"]] == [1]


# ── 소유별 관제 리전 (리뷰 반례) ───────────────────────────────────────────────
# A 는 서울만 관제하지만(도쿄를 끊었다) 도쿄에 옛 자산 · 회차 · 이벤트가 남아 있고, B 는 도쿄만 관제하며 서울에
# 관제 밖 기록이 있다. 설정 리전(소속 없는 기록의 관제 범위)은 서울이고 도쿄에 소속 없는 기록이 있다. 리전을
# 합집합(서울 · 도쿄)으로 거르면 관제 밖 셋이 모두 들어오고 CloudWatch 는 A 자격으로 도쿄를 부른다.
# 관제 안 기록은 1 · 2 · 4 건, 관제 밖 기록은 각 8 건이다 — 관제 밖이 하나라도 섞이면 합이 7 을 넘고, 합 하나로
# 어느 관제 안 칸이 빠졌는지도 드러난다. 모두 같은 시간 칸이다.

_INSIDE = {("A", SEOUL): 1, ("B", TOKYO): 2, (None, SEOUL): 4}
_OUTSIDE = {("A", TOKYO): 8, ("B", SEOUL): 8, (None, TOKYO): 8}
_Cell = tuple[str | None, str]  # (라벨, 리전)


def _seed_region_world(db, make_tenant) -> tuple[dict[str | None, str | None], dict[_Cell, str]]:
    """반례의 기록을 시드한다 → ({라벨: tenant_id}, {(라벨, 리전): EC2 ARN})."""
    tenants = {"A": make_tenant(), "B": make_tenant(), None: None}  # 기본 관제 리전은 서울
    db.get(models.Tenant, tenants["B"]).regions = [TOKYO]
    at = (datetime.now(timezone.utc) - timedelta(hours=1)).replace(minute=1, second=0, microsecond=0)
    arns: dict[_Cell, str] = {}
    for offset, ((label, region), count) in enumerate({**_INSIDE, **_OUTSIDE}.items()):
        arns[(label, region)] = _seed_cell(
            db, tenant_id=tenants[label], region=region, count=count,
            at=at + timedelta(seconds=5 * offset), tag=f"{(label or 'n').lower()}{region[-1]}",
        )
    db.commit()
    return tenants, arns


def _called_cells(calls, tenants: dict[str | None, str | None]) -> list[_Cell]:
    """CloudWatch 호출마다 (나간 자격의 라벨, 리전). 소속 없는 기록의 프로세스 자격은 None 이다."""
    label_of = {tenant_id: label for label, tenant_id in tenants.items()}
    cells = [(label_of[access.tenant_id if access is not None else None], region)
             for access, region, _ in calls]
    return sorted(cells, key=str)


@pytest.mark.parametrize(
    ("assigned", "visible"),
    [
        (None, (("A", SEOUL), ("B", TOKYO), (None, SEOUL))),
        (("A", "B"), (("A", SEOUL), ("B", TOKYO))),
        (("A",), (("A", SEOUL),)),
        (("B",), (("B", TOKYO),)),
    ],
    ids=["central", "manager-AB", "manager-A", "manager-B"],
)
def test_소유마다_그_관제_리전의_기록만_여섯_축에_싣고_CloudWatch도_그_리전만_그_자격으로_부른다(
    client_pg, db, set_regions, make_tenant, login_as, owner_cloudwatch, assigned, visible
):
    set_regions(SEOUL)
    tenants, arns = _seed_region_world(db, make_tenant)
    if assigned is not None:  # None 은 client_pg 기본(중앙) 그대로
        _login_manager(login_as, db, [tenants[label] for label in assigned])
    cw = owner_cloudwatch()

    body = client_pg.get(URL).json()

    for axis in ("cpu", "network", *_DB_AXES):
        assert (body[axis]["status"], body[axis]["reason_code"]) == ("READY", None), axis
    total = sum(_INSIDE[cell] for cell in visible)
    assert _db_totals(body) == {axis: [total] for axis in _DB_AXES}
    expected_arns = {arns[cell] for cell in visible}
    assert set(_series_values(body, "cpu", "points")) == expected_arns
    assert set(_series_values(body, "network", "in_points")) == expected_arns
    # 관제 밖 칸의 인스턴스는 대상에 오르지 않는다 — A 자격으로 도쿄를, 프로세스 자격으로 도쿄를 부르지 않는다
    assert _called_cells(cw.calls, tenants) == sorted([*visible] * 2, key=str)


def test_관제를_끊은_리전의_기록은_남아_있고_다시_관제하면_다음_요청부터_센다(
    client_pg, db, set_regions, make_tenant, login_as, owner_cloudwatch
):
    """제외는 리전 설정에 따른 조회 조건일 뿐이다 — 기록을 지우거나 숨겨 두지 않으므로, A 가 도쿄를 다시 관제하면
    남아 있던 도쿄 기록이 다음 요청에 돌아오고 그때부터 A 자격으로 도쿄를 부른다. B 의 서울 기록은 A 가 서울을
    관제해도 여전히 빠진다."""
    set_regions(SEOUL)
    tenants, arns = _seed_region_world(db, make_tenant)
    _login_manager(login_as, db, [tenants["A"], tenants["B"]])
    cw = owner_cloudwatch()

    before = client_pg.get(URL).json()
    mark = len(cw.calls)
    db.get(models.Tenant, tenants["A"]).regions = [SEOUL, TOKYO]
    db.commit()
    after = client_pg.get(URL).json()

    assert _db_totals(before) == {axis: [1 + 2] for axis in _DB_AXES}
    assert _db_totals(after) == {axis: [1 + 2 + 8] for axis in _DB_AXES}
    assert set(_series_values(before, "cpu", "points")) == {arns[("A", SEOUL)], arns[("B", TOKYO)]}
    assert set(_series_values(after, "cpu", "points")) == {
        arns[("A", SEOUL)], arns[("A", TOKYO)], arns[("B", TOKYO)]
    }
    assert _called_cells(cw.calls[:mark], tenants) == sorted([("A", SEOUL), ("B", TOKYO)] * 2, key=str)
    assert _called_cells(cw.calls[mark:], tenants) == sorted(
        [("A", SEOUL), ("A", TOKYO), ("B", TOKYO)] * 2, key=str
    )
