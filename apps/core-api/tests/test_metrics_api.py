# ==============================================================================
# [파일 설명]
# GET /api/v1/metrics/timeseries 통합 검증(PostgreSQL) — 대시보드 시계열 2축.
#
#   축 2(SG 개방 건수)는 실제 DB 로 검증한다. 회차별 보존이 이 기능의 전제라,
#   가짜로 대신하면 "판정이 회차마다 남는가"라는 정작 중요한 것을 안 보게 된다.
#   축 1(CPU)은 CloudWatch 호출이라 services.metrics.cpu_timeseries 를 대체한다 —
#   AWS 연동 자체는 LocalStack 테스트의 몫이다.
# ==============================================================================

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from schemas.api.assets import AssetType
from schemas.api.metrics import CpuSeries, NetworkSeries
from schemas.rules import RuleEvaluationResult

from db.repositories import assets as assets_repo

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
    at = now - timedelta(hours=1)

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


def test_조회_창은_상한을_넘길_수_없다(client_pg, set_regions, no_cloudwatch):
    """점이 인스턴스당 수백 개가 되면 화면이 읽히지 않는다 — 계약 밖 값은 422."""
    set_regions(SEOUL)
    assert client_pg.get("/api/v1/metrics/timeseries?hours=337").status_code == 422
    assert client_pg.get("/api/v1/metrics/timeseries?hours=0").status_code == 422
