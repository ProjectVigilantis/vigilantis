# ==============================================================================
# [파일 설명]
# GET /api/v1/metrics/timeseries 통합 검증(PostgreSQL) — 시계열 6축.
#
#   축 2(SG 개방 건수)·축 4(자산 현황)·축 5(자산 수)는 실제 DB 로 검증한다. 회차별 보존이 이 기능의 전제라,
#   가짜로 대신하면 "판정이 회차마다 남는가"라는 정작 중요한 것을 안 보게 된다.
#   축 1(CPU)은 CloudWatch 호출이라 services.metrics.cpu_timeseries 를 대체한다 —
#   AWS 연동 자체는 LocalStack 테스트의 몫이다.
# ==============================================================================

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from schemas.api.assets import AssetType
from schemas.api.metrics import CpuSeries, NetworkSeries
from schemas.rules import RuleEvaluationResult

from db import models
from db.repositories import assets as assets_repo
from routers import metrics as metrics_router

ACCOUNT = "123456789012"
SEOUL = "ap-northeast-2"
TOKYO = "ap-northeast-1"


def _arn(region: str, suffix: str) -> str:
    return f"arn:aws:ec2:{region}:{ACCOUNT}:security-group/sg-{suffix}"


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


def _seed_run(db, *, region: str, started_at: datetime):
    run = assets_repo.start_collection_run(
        db,
        account_id=ACCOUNT,
        region=region,
        mode="localstack",
        lookback_days=3,
        period_seconds=3600,
    )
    run.started_at = started_at  # 기본값은 현재 시각 — 회차 시각을 테스트가 정한다
    db.flush()
    return run


def _seed_sg(db, run, *, region: str, suffix: str, verdict: str, evaluated_at: datetime):
    """SG 1건 + 그 회차의 판정 1건. verdict=THREAT 가 '인터넷 개방'이다."""
    arn = _arn(region, suffix)
    assets_repo.upsert_asset(
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


def _seed_threat_event(db, *, region: str, event_type: str, occurred_at: datetime, key: str):
    db.add(
        models.ThreatEvent(
            source_event_id=key,
            event_type=event_type,
            target_arn=_arn(region, key),
            payload={},
            deduplication_key=key,
            occurred_at=occurred_at,
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
