"""GET /api/v1/platform/overview 와 고객사별 API 호출 집계 (#572 6번) — 실제 PostgreSQL.

지키는 것: 중앙 관리계정만 200(관리자 직원계정 · 고객사 계정 403, 미인증 401) · 상태 집계(상태별 고객사 ·
최근 24h 수집 회차 · 상태별 인시던트)가 이미 저장된 행을 세는 것 · hours 1..720 과 시간 칸(UTC 정시, 오래된 것부터) ·
고객사(TENANT) 계정의 `/api/*` 호출(업무 API · 403 응답 · /auth/me · /auth/activity)이 고객사 × UTC 1시간 칸에 쌓이고
플랫폼 계정 · 미인증 호출은 쌓이지 않는 것 · 판정 입력(asset_count · staff_count)과 threshold_per_hour=null ·
조회 기간 밖 칸 제외와 정렬 · 날짜가 바뀐 첫 기록의 30일 보관 정리 · 기록 실패가 응답을 바꾸지 않는 것 · 계약 검증.

같은 DB 를 다른 테스트도 쓰므로 상태 집계는 호출 전후의 차이로 본다. 시각은 이 테스트가 고정한 지금(now)이다 —
세션 유효 기간 안이어야 하므로 먼 미래로 옮기지 않는다.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import api_usage
import pytest
from auth.http import get_clock
from db import models
from db.repositories import assets as assets_repo
from db.repositories import platform as platform_repo
from platform_overview import floor_to_hour
from schemas.api.assets import AssetType
from schemas.api.auth import PlatformRole, UserRole
from schemas.api.incidents import IncidentStatus
from schemas.api.platform import (
    API_USAGE_DEFAULT_WINDOW_HOURS,
    API_USAGE_MAX_WINDOW_HOURS,
    API_USAGE_RETENTION_DAYS,
    PlatformOverviewResponse,
)
from schemas.api.tenants import TenantStatus
from schemas.collections import CollectionRunStatus
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError

URL = "/api/v1/platform/overview"
REGION = "ap-northeast-2"
HOUR = timedelta(hours=1)
DAY = timedelta(days=1)


def _z(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


@pytest.fixture()
def now(client_pg, monkeypatch) -> datetime:
    """조회 시각과 기록 시각을 같은 지금으로 고정한다 — 테스트 중 정시 · 자정을 지나도 칸이 갈리지 않는다."""
    fixed = datetime.now(UTC).replace(microsecond=0)
    client_pg.app.dependency_overrides[get_clock] = lambda: (lambda: fixed)
    monkeypatch.setattr(api_usage, "_utcnow", lambda: fixed)
    return fixed


@pytest.fixture()
def this_hour(now) -> datetime:
    return floor_to_hour(now)


def _get(client, **params):
    response = client.get(URL, params=params)
    assert response.status_code == 200, response.text
    body = response.json()
    PlatformOverviewResponse.model_validate(body)
    return body


def _usage(body, tenant_id):
    return next(item for item in body["api_usage"]["items"] if item["tenant_id"] == tenant_id)


def _buckets(db):
    rows = db.execute(
        select(models.TenantApiCallHourly.tenant_id, models.TenantApiCallHourly.hour, models.TenantApiCallHourly.calls)
    ).all()
    return {(tenant_id, hour.astimezone(UTC)): calls for tenant_id, hour, calls in rows}


def _run(db, tenant_id, status, *, at, started=None):
    run = assets_repo.start_collection_run(
        db, account_id="123456789012", region=REGION, mode="aws", lookback_days=14, period_seconds=300,
        tenant_id=tenant_id,
    )
    if started is not None:
        run.started_at = started
        db.flush()
    if status is not CollectionRunStatus.IN_PROGRESS:
        assert assets_repo.finish_collection_run(db, run.collection_run_id, status, finished_at=at)
    return run.collection_run_id


def _asset(db, tenant_id, run_id, suffix, *, at, absent=False):
    asset = assets_repo.upsert_asset(
        db, arn=f"arn:aws:ec2:{REGION}:123456789012:instance/i-{tenant_id[:8]}{suffix}", asset_type=AssetType.EC2,
        resource_id=f"i-{tenant_id[:8]}{suffix}", account_id="123456789012", region=REGION, spec={},
        collection_run_id=run_id, collected_at=at, tenant_id=tenant_id,
    )
    if absent:
        asset.absent_since = at
        db.flush()


# --- 플랫폼 상태 ---------------------------------------------------------------------


def test_중앙_관리계정이_저장된_기록을_센_플랫폼_상태를_받는다(
    client_pg, db, make_tenant, make_auth_session, make_incident, now, this_hour
):
    before = _get(client_pg)

    active = make_tenant(TenantStatus.ACTIVE)
    make_tenant(TenantStatus.PENDING_CONNECTION)
    make_tenant(TenantStatus.SUSPENDED)
    recent, old = now - HOUR, now - timedelta(hours=25)
    run = _run(db, active, CollectionRunStatus.SUCCESS, at=recent)
    _run(db, active, CollectionRunStatus.SUCCESS, at=now - timedelta(hours=23, minutes=59))
    _run(db, active, CollectionRunStatus.PARTIAL, at=recent)
    _run(db, active, CollectionRunStatus.FAILED, at=old)  # 24시간 밖
    _run(db, active, CollectionRunStatus.IN_PROGRESS, at=None, started=recent)
    _run(db, active, CollectionRunStatus.IN_PROGRESS, at=None, started=now - 2 * DAY)  # 24시간 밖
    make_incident(db)
    make_incident(db)
    make_incident(db, status=IncidentStatus.ANALYZING)
    make_incident(db, status=IncidentStatus.RESOLVED)
    # 판정 입력 — 수집 자산 2개(사라진 자산 1개는 빼고), 고객사 계정 3개(상태 무관)
    for suffix in ("a", "b"):
        _asset(db, active, run, suffix, at=recent)
    _asset(db, active, run, "gone", at=recent, absent=True)
    make_auth_session(UserRole.ADMIN, tenant_id=active)
    make_auth_session(UserRole.VIEWER, tenant_id=active)
    make_auth_session(UserRole.OPERATOR, tenant_id=active, status=models.AppUserStatus.DISABLED)

    after = _get(client_pg)

    assert after["generated_at"] == _z(now)
    assert after["tenants"]["total"] - before["tenants"]["total"] == 3
    for status in ("PENDING_CONNECTION", "ACTIVE", "SUSPENDED"):
        assert after["tenants"]["by_status"][status] - before["tenants"]["by_status"][status] == 1
    delta = {k: after["collection"][k] - before["collection"][k] for k in ("success", "partial", "failed", "in_progress")}
    assert delta == {"success": 2, "partial": 1, "failed": 0, "in_progress": 1}
    assert after["collection"]["window_hours"] == 24
    assert after["incidents"]["total"] - before["incidents"]["total"] == 4
    incident_delta = {
        status: after["incidents"]["by_status"][status] - before["incidents"]["by_status"][status]
        for status in after["incidents"]["by_status"]
    }
    assert incident_delta == {
        "ANALYZING": 1, "AWAITING_APPROVAL": 2, "ACTION_IN_PROGRESS": 0, "AWAITING_CLOSURE": 0, "RESOLVED": 1, "FAILED": 0,
    }
    usage = after["api_usage"]
    assert (usage["window_hours"], usage["retention_days"]) == (API_USAGE_DEFAULT_WINDOW_HOURS, API_USAGE_RETENTION_DAYS)
    assert usage["hours"] == [_z(this_hour - (23 - i) * HOUR) for i in range(24)]
    # 호출 0인 고객사도 실리고, 과다 호출 기준은 계산식이 없어 null 이다
    assert _usage(after, active) == {
        "tenant_id": active, "tenant_name": db.get(models.Tenant, active).name, "tenant_status": "ACTIVE",
        "asset_count": 2, "staff_count": 3, "threshold_per_hour": None, "total_calls": 0, "hourly": [0] * 24,
    }


@pytest.mark.parametrize("hours", [1, API_USAGE_MAX_WINDOW_HOURS])
def test_hours가_조회_기간을_정한다(client_pg, now, this_hour, hours):
    body = _get(client_pg, hours=hours)
    assert body["api_usage"]["window_hours"] == hours
    assert body["api_usage"]["hours"][0] == _z(this_hour - (hours - 1) * HOUR)
    assert body["api_usage"]["hours"][-1] == _z(this_hour)


@pytest.mark.parametrize("hours", [0, API_USAGE_MAX_WINDOW_HOURS + 1, "x"])
def test_hours_범위_밖은_422다(client_pg, hours):
    response = client_pg.get(URL, params={"hours": hours})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "REQUEST_VALIDATION_FAILED"


# --- 접근 -----------------------------------------------------------------------------


def test_관리자_직원계정은_403이다(client_pg, login_as, make_tenant):
    login_as(UserRole.ADMIN, platform_role=PlatformRole.TENANT_MANAGER, assigned_tenant_ids=(make_tenant(),))
    response = client_pg.get(URL)
    assert (response.status_code, response.json()["error"]["code"]) == (403, "AUTH_FORBIDDEN")


@pytest.mark.parametrize("role", [UserRole.ADMIN, UserRole.OPERATOR, UserRole.VIEWER])
def test_고객사_계정은_역할과_무관하게_403이다(client_pg, login_as, make_tenant, role):
    login_as(role, tenant_id=make_tenant())
    response = client_pg.get(URL)
    assert (response.status_code, response.json()["error"]["code"]) == (403, "AUTH_FORBIDDEN")


def test_미인증은_401이다(client_pg):
    response = client_pg.get(URL, headers={"Cookie": ""})
    assert (response.status_code, response.json()["error"]["code"]) == (401, "AUTH_REQUIRED")


# --- 호출 집계 -------------------------------------------------------------------------


def test_고객사_계정의_호출만_고객사별_현재_시간_칸에_쌓인다(client_pg, db, login_as, make_tenant, now, this_hour):
    first, second = make_tenant(), make_tenant()

    login_as(UserRole.VIEWER, tenant_id=first)
    assert client_pg.get("/api/v1/incidents").status_code == 200  # 업무 API
    assert client_pg.get("/api/v1/tenants").status_code == 403  # 거절된 호출도 호출이다
    assert client_pg.get("/api/v1/auth/me").status_code == 200
    assert client_pg.post("/api/v1/auth/activity").status_code == 204
    assert client_pg.get("/health").status_code == 200  # /api/* 밖
    login_as(UserRole.ADMIN, tenant_id=second)
    assert client_pg.get("/api/v1/assets").status_code == 200
    # 플랫폼 계정과 미인증 호출은 세지 않는다
    login_as(UserRole.ADMIN, platform_role=PlatformRole.TENANT_MANAGER, assigned_tenant_ids=(first,))
    assert client_pg.get("/api/v1/tenants").status_code == 200
    assert client_pg.get("/api/v1/incidents", headers={"Cookie": ""}).status_code == 401
    login_as(UserRole.ADMIN, platform_role=PlatformRole.CENTRAL_ADMIN)
    assert client_pg.get("/api/v1/auth/me").status_code == 200

    body = _get(client_pg, hours=3)
    assert _buckets(db) == {(first, this_hour): 4, (second, this_hour): 1}
    assert (_usage(body, first)["total_calls"], _usage(body, first)["hourly"]) == (4, [0, 0, 4])
    assert _usage(body, second)["hourly"] == [0, 0, 1]
    ids = [item["tenant_id"] for item in body["api_usage"]["items"]]
    assert ids.index(first) < ids.index(second)
    # 중앙 관리계정의 이 조회도 세지 않았다
    assert _buckets(db) == {(first, this_hour): 4, (second, this_hour): 1}


def test_조회_기간_밖_칸은_빠지고_호출_수_내림차순이다(client_pg, db, make_tenant, now, this_hour):
    busy, quiet, idle = make_tenant(), make_tenant(), make_tenant()
    for hour, calls in ((this_hour - 3 * HOUR, 5), (this_hour - 2 * HOUR, 2), (this_hour, 1)):
        for _ in range(calls):
            platform_repo.add_call(db, tenant_id=quiet, hour=hour)
    for _ in range(10):
        platform_repo.add_call(db, tenant_id=busy, hour=this_hour - HOUR)

    three = _get(client_pg, hours=3)
    assert three["api_usage"]["hours"] == [_z(this_hour - 2 * HOUR), _z(this_hour - HOUR), _z(this_hour)]
    assert (_usage(three, quiet)["total_calls"], _usage(three, quiet)["hourly"]) == (3, [2, 0, 1])
    assert (_usage(three, busy)["hourly"], _usage(three, idle)["hourly"]) == ([0, 10, 0], [0, 0, 0])
    ids = [item["tenant_id"] for item in three["api_usage"]["items"]]
    assert ids.index(busy) < ids.index(quiet) < ids.index(idle)

    four = _get(client_pg, hours=4)
    assert _usage(four, quiet)["hourly"] == [5, 2, 0, 1]


def test_날짜가_바뀐_첫_기록이_보관_기간_밖_칸을_지운다(client_pg, db, login_as, make_tenant, now, this_hour, monkeypatch):
    tenant = make_tenant()
    today_start = datetime(now.year, now.month, now.day, tzinfo=UTC)
    expired = today_start - API_USAGE_RETENTION_DAYS * DAY + 23 * HOUR  # 보관 첫날 직전 칸
    oldest_kept = today_start - (API_USAGE_RETENTION_DAYS - 1) * DAY  # 보관 첫날 0시 칸
    platform_repo.add_call(db, tenant_id=tenant, hour=expired)
    platform_repo.add_call(db, tenant_id=tenant, hour=oldest_kept)
    login_as(UserRole.ADMIN, tenant_id=tenant)

    # 앱의 첫 기록 — 오늘 기준으로 정리한다
    assert client_pg.get("/api/v1/incidents").status_code == 200
    assert _buckets(db) == {(tenant, oldest_kept): 1, (tenant, this_hour): 1}

    # 같은 날의 다음 기록은 정리하지 않는다
    platform_repo.add_call(db, tenant_id=tenant, hour=expired)
    assert client_pg.get("/api/v1/incidents").status_code == 200
    assert _buckets(db) == {(tenant, expired): 1, (tenant, oldest_kept): 1, (tenant, this_hour): 2}

    # 날짜가 바뀌면 새 기준으로 다시 정리한다 — 어제의 첫 보관일 칸도 빠진다
    monkeypatch.setattr(api_usage, "_utcnow", lambda: now + DAY)
    assert client_pg.get("/api/v1/incidents").status_code == 200
    assert _buckets(db) == {(tenant, this_hour): 2, (tenant, this_hour + DAY): 1}


def test_같은_날_겹친_기록_중_하나만_정리를_맡고_실패하면_차례를_돌려놓는다(monkeypatch):
    """DB 없이 미들웨어의 정리 차례만 본다 — 스레드풀에서 겹친 두 기록이 둘 다 정리하지 않는다."""
    import threading
    from contextlib import nullcontext

    middleware = api_usage.TenantApiUsageMiddleware(app=None)
    fixed = datetime(2031, 6, 15, 12, tzinfo=UTC)
    monkeypatch.setattr(api_usage, "_utcnow", lambda: fixed)
    barrier = threading.Barrier(2)
    prunes: list[bool] = []
    outcome = {"ok": True}

    def fake_record(_db, *, tenant_id, at, prune):
        barrier.wait(timeout=5)  # 둘 다 차례 판정을 마친 뒤에야 진행한다
        prunes.append(prune)
        return outcome["ok"]

    monkeypatch.setattr(api_usage, "record_call", fake_record)
    threads = [threading.Thread(target=middleware._record, args=(nullcontext, "t-1")) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
    assert sorted(prunes) == [False, True]

    # 정리를 맡은 기록이 실패하면 차례가 돌아와 다음 기록이 다시 맡는다
    barrier = threading.Barrier(1)
    prunes.clear()
    outcome["ok"] = False
    monkeypatch.setattr(api_usage, "_utcnow", lambda: fixed + DAY)
    middleware._record(nullcontext, "t-1")
    middleware._record(nullcontext, "t-1")
    assert prunes == [True, True]
    outcome["ok"] = True
    middleware._record(nullcontext, "t-1")
    middleware._record(nullcontext, "t-1")
    assert prunes[2:] == [True, False]

    # 자정 직전에 들어와 늦게 기록되는 전날 요청은 추적 날짜를 뒤로 돌리지 못한다 — 다음날이 또 정리하지 않는다
    prunes.clear()
    monkeypatch.setattr(api_usage, "_utcnow", lambda: fixed)
    middleware._record(nullcontext, "t-1")
    monkeypatch.setattr(api_usage, "_utcnow", lambda: fixed + DAY)
    middleware._record(nullcontext, "t-1")
    assert prunes == [False, False]


def test_기록_실패는_응답을_바꾸지_않고_오류_종류만_로그에_남긴다(
    client_pg, db, login_as, make_tenant, now, monkeypatch, caplog
):
    tenant = make_tenant()
    login_as(UserRole.ADMIN, tenant_id=tenant)

    def boom(*_args, **_kwargs):
        raise SQLAlchemyError("boom")

    monkeypatch.setattr(platform_repo, "add_call", boom)
    with caplog.at_level(logging.ERROR, logger="vigilantis.api_usage"):
        response = client_pg.get("/api/v1/incidents")
    assert response.status_code == 200
    assert _buckets(db) == {}
    messages = [record.getMessage() for record in caplog.records if record.name == "vigilantis.api_usage"]
    assert messages == ["api_usage_record_failed error_type=SQLAlchemyError"]
    # 기록 세션은 되돌려졌고 다음 요청은 그대로 돈다
    assert db.scalar(select(func.count()).select_from(models.Tenant).where(models.Tenant.tenant_id == tenant)) == 1
    assert client_pg.get("/api/v1/incidents").status_code == 200
