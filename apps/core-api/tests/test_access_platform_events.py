"""권한 거절(403 AUTH_FORBIDDEN)의 플랫폼 활동 로그(#589 B) — 실제 PostgreSQL 행으로 본다.

지키는 것
  - 세션이 있는 요청의 403 AUTH_FORBIDDEN 은 ACCESS_FORBIDDEN 한 행이다 — 관문의 거절도, 서비스가 잠금 안에서
    다시 판정한 거절도 같다. 행위자는 요청자, 고객사 계정이면 그 고객사가 소속이다.
  - 싣는 것은 메서드와 경로뿐이다(질의 문자열 없음). 경로가 기록 형식에 맞지 않으면 라우트 틀을 싣는다.
  - 신원 없는 거절(401 · 출처 403)과 다른 오류(404 · 409 · 422)는 남기지 않는다.
  - 요청 트랜잭션과 따로 남는다 — 요청이 쥐었던 잠금을 기다리지 않고, 따로 쓰기가 실패해도 403 은 그대로다.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from config import TenantSettings, get_tenant_settings
from db import models
from db.repositories import auth as auth_repo
from db.repositories import tenants as tenants_repo
from fastapi.testclient import TestClient
from schemas.api.auth import AccountType, PlatformRole, UserRole
from schemas.api.tenants import TenantStatus
from schemas.platform_events import (
    PlatformActorKind,
    PlatformEventCategory,
    PlatformEventOutcome,
)
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from tests.group_scope import assign_tenants

TENANTS = "/api/v1/tenants"
NOW = datetime(2026, 10, 22, 5, 0, tzinfo=UTC)
MISSING = "00000000-0000-0000-0000-000000000000"


# 다른 테스트가 독립 세션으로 commit 해 둔 행(정리가 덜 된 것 포함)에 기대지 않는다 — 이 테스트가 시작할 때
# 이미 있던 행은 빼고 본다
_seen_before: set[str] = set()


@pytest.fixture(autouse=True)
def _baseline(request):
    _seen_before.clear()
    if "db" in request.fixturenames:
        session = request.getfixturevalue("db")
        _seen_before.update(session.scalars(select(models.PlatformEvent.event_id)).all())
    yield


def _events(db):
    db.expire_all()
    return db.scalars(
        select(models.PlatformEvent)
        .where(
            models.PlatformEvent.category == PlatformEventCategory.ACCESS,
            models.PlatformEvent.event_id.notin_(_seen_before),
        )
        .order_by(models.PlatformEvent.occurred_at)
    ).all()


def _code(resp) -> str:
    return resp.json()["error"]["code"]


def _is_forbidden_row(event, seed, method, path, *, tenant=None) -> None:
    assert (event.action, event.outcome) == ("ACCESS_FORBIDDEN", PlatformEventOutcome.DENIED)
    assert event.actor_kind is PlatformActorKind.USER
    assert (event.actor_user_id, event.session_id) == (seed.user_id, seed.session_id)
    assert (event.actor_account_type, event.actor_platform_role) == (seed.account_type, seed.platform_role)
    assert (event.tenant_id, event.target_type, event.target_id) == (tenant, None, None)
    assert event.details == {"reason_code": "AUTH_FORBIDDEN", "method": method, "path": path}


def test_gate_refusal_of_a_tenant_account_is_recorded_in_its_tenant(client_pg, db, login_as, make_tenant):
    tenant_id = make_tenant()
    seed = login_as(UserRole.ADMIN, tenant_id=tenant_id)  # 고객사 ADMIN — 테넌트 라우터는 플랫폼 전용이다
    resp = client_pg.get(TENANTS + "?unassigned_only=true", headers={"X-Request-ID": "req-403-1"})
    assert (resp.status_code, _code(resp)) == (403, "AUTH_FORBIDDEN")

    [event] = _events(db)
    _is_forbidden_row(event, seed, "GET", TENANTS, tenant=tenant_id)  # 질의 문자열은 없다
    assert event.request_id == "req-403-1"
    assert seed.account_type is AccountType.TENANT


def test_service_refusal_outside_the_managers_scope_keeps_the_real_path(client_pg, db, login_as, make_tenant):
    mine, other = make_tenant(), make_tenant()
    seed = login_as(UserRole.ADMIN, platform_role=PlatformRole.TENANT_MANAGER)
    assign_tenants(db, user_id=seed.user_id, tenant_ids=(mine,), now=NOW)
    db.commit()

    resp = client_pg.get(f"{TENANTS}/{other}?probe=1")
    assert (resp.status_code, _code(resp)) == (403, "AUTH_FORBIDDEN")
    [event] = _events(db)
    # 어느 고객사를 열려 했는지가 경로에 남는다(질의 문자열은 빠진다). 플랫폼 계정이라 소속은 없다
    _is_forbidden_row(event, seed, "GET", f"{TENANTS}/{other}")

    assert client_pg.get(f"{TENANTS}/{mine}").status_code == 200  # 범위 안은 행이 없다
    assert len(_events(db)) == 1


def test_a_path_outside_the_record_format_falls_back_to_the_route_template(
    client_pg, db, login_as, make_tenant
):
    seed = login_as(UserRole.ADMIN, tenant_id=make_tenant())
    resp = client_pg.get(f"{TENANTS}/not%20a%20uuid")  # 공백이 든 경로 — 기록 형식은 공백을 받지 않는다
    assert resp.status_code == 403
    [event] = _events(db)
    assert event.details == {
        "reason_code": "AUTH_FORBIDDEN", "method": "GET", "path": TENANTS + "/{tenant_id}",
    }
    assert event.actor_user_id == seed.user_id


def test_each_refused_request_is_its_own_row(client_pg, db, login_as, make_tenant):
    seed = login_as(UserRole.ADMIN, tenant_id=make_tenant())
    for _ in range(3):
        assert client_pg.post(TENANTS, json={}).status_code == 403  # 관문이 본문 검증보다 먼저다
    events = _events(db)
    assert len(events) == 3
    for event in events:
        _is_forbidden_row(event, seed, "POST", TENANTS, tenant=seed.tenant_id)


def test_refusals_without_an_identity_and_other_errors_record_nothing(
    client_pg, client_pg_anon, db, platform_session, make_tenant
):
    assert client_pg_anon.get(TENANTS).status_code == 401                                   # 세션 없음
    bad_origin = client_pg.post(TENANTS, json={}, headers={"Origin": "https://evil.example"})
    assert (bad_origin.status_code, _code(bad_origin)) == (403, "AUTH_ORIGIN_REJECTED")     # 신원 판정 전의 403
    assert client_pg.get(f"{TENANTS}/{MISSING}").status_code == 404                         # 없는 고객사
    assert client_pg.post(TENANTS, json={}).status_code == 422                              # 계약 밖 본문
    suspended = make_tenant(TenantStatus.SUSPENDED)
    assert client_pg.post(f"{TENANTS}/{suspended}/suspend", json={"reason": "x"}).status_code == 409
    assert _events(db) == []


def test_detached_write_failure_keeps_the_403(client_pg, db, login_as, make_tenant):
    import db.session as db_session

    login_as(UserRole.ADMIN, tenant_id=make_tenant())

    def unavailable():
        raise RuntimeError("detached session unavailable")

    client_pg.app.dependency_overrides[db_session.get_session_factory] = lambda: unavailable
    resp = client_pg.get(TENANTS)
    assert (resp.status_code, _code(resp)) == (403, "AUTH_FORBIDDEN")
    assert _events(db) == []


def test_refusal_inside_the_lock_commits_apart_without_waiting(pg_engine, committed_auth, monkeypatch):
    """서비스가 사용자 표 잠금 안에서 다시 판정해 거절한 403 — 그 잠금을 쥔 채 따로 쓰면 FK 확인이 기다린다(5초 뒤 실패).

    의존성이 신원을 본 뒤 중앙 관리자가 담당자로 강등된 상황이다. 요청은 아무것도 쓰지 못하고, 거절 행만 남는다."""
    import api_usage
    import db.session as db_session
    from config import get_settings
    from main import create_app

    engine = create_engine(pg_engine.url, connect_args={"options": "-c lock_timeout=5000"})
    sessions = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    tenant_id = committed_auth.make_tenant()
    seed = committed_auth.make()  # CENTRAL_ADMIN
    original = auth_repo.lock_users_table

    def demote_then_lock(session):
        session.get(models.AppUser, seed.user_id).platform_role = PlatformRole.TENANT_MANAGER
        session.flush()
        original(session)

    monkeypatch.setattr(auth_repo, "lock_users_table", demote_then_lock)
    get_settings.cache_clear()
    app = create_app()

    def _get_db():
        session = sessions()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[db_session.get_db] = _get_db
    app.dependency_overrides[db_session.get_session_factory] = lambda: sessions
    app.dependency_overrides[api_usage.get_usage_session_factory] = lambda: sessions
    app.dependency_overrides[get_tenant_settings] = lambda: TenantSettings(
        TENANT_ONBOARDING_TEMPLATE_URL="https://templates.example.com/t.yaml",
        PLATFORM_PRINCIPAL_ARN="arn:aws:iam::999988887777:role/vigilantis-platform",
    )
    try:
        with TestClient(app, headers=seed.headers) as client:
            resp = client.patch(f"{TENANTS}/{tenant_id}", json={"name": "새 이름"})

        assert (resp.status_code, _code(resp)) == (403, "AUTH_FORBIDDEN")
        with sessions() as check:
            [event] = check.scalars(
                select(models.PlatformEvent).where(models.PlatformEvent.actor_user_id == seed.user_id)
            ).all()
            # 행위자의 등급은 의존성이 본 그때의 값이다(강등은 요청과 함께 되돌려졌다)
            _is_forbidden_row(event, seed, "PATCH", f"{TENANTS}/{tenant_id}")
            assert tenants_repo.get_tenant(check, tenant_id).tenant.name != "새 이름"
    finally:
        engine.dispose()  # 사람 행 · 고객사는 committed_auth 정리가 지운다


def test_only_requests_with_a_valid_method_are_recorded():
    """기록 형식이 받지 않는 메서드 · 경로면 행을 만들지 않는다 — 처리기가 403 대신 500 을 내면 안 된다."""
    from types import SimpleNamespace

    import access_denial_log

    def details(method, path, route=None):
        request = SimpleNamespace(
            method=method,
            url=SimpleNamespace(path=path),
            scope={"route": SimpleNamespace(path=route)} if route else {},
        )
        return access_denial_log._details(request)

    assert details("GET", "/api/v1/tenants") == {
        "reason_code": "AUTH_FORBIDDEN", "method": "GET", "path": "/api/v1/tenants",
    }
    assert details("GET", "/a b", "/a/{x}")["path"] == "/a/{x}"
    assert details("GET", "/" + "x" * 600, "/long/{id}")["path"] == "/long/{id}"
    assert details("GET", "/a b") is None            # 틀도 없다
    assert details("get", "/api/v1/tenants") is None  # 형식 밖 메서드


def test_a_403_without_an_identity_is_not_recorded_and_opens_no_session():
    """신원 없는 403 이 처리기까지 와도 남기지 않는다 — 세션을 열지도, 예외를 올리지도 않는다."""
    from types import SimpleNamespace

    import access_denial_log
    import db.session as db_session

    def no_session():
        raise AssertionError("신원이 없으면 세션을 열지 않는다")

    request = SimpleNamespace(
        method="GET",
        url=SimpleNamespace(path="/api/v1/tenants"),
        scope={},
        state=SimpleNamespace(),  # current_user 없음
        app=SimpleNamespace(dependency_overrides={db_session.get_session_factory: lambda: no_session}),
    )
    assert access_denial_log.record_forbidden(request) is None
