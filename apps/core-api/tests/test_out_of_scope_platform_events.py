"""범위 밖 404(ACCESS_OUT_OF_SCOPE)의 플랫폼 활동 로그(#589 B) — 실제 PostgreSQL 행으로 본다.

지키는 것
  - **있는데 범위 밖**인 대상을 열면 나가는 응답은 「없는 ID」와 똑같은 404 이고, ACCESS_OUT_OF_SCOPE 한 행이 남는다.
    행위자는 요청자, 소속은 요청자의 고객사, 싣는 것은 메서드 · 경로 · 사유 코드(나간 응답 코드)뿐이다.
  - **없는 ID 는 남기지 않는다.** 표시(exceptions.OutOfScopeError)가 붙은 404 만 남는다.
  - 「없는 ID」와 「범위 밖 ID」가 응답 전에 실행하는 조회 문장이 같다 — 걸리는 시간은 단언하지 않는다(한계).
  - 행은 응답 전에 만들고 쓰기는 응답 뒤다. 쓰기가 실패해도 404 는 그대로다.
  - 대상 자리: 인시던트 상세 · 조치 실행 · 종료 판단 · 사용자 관리 대상 · TOTP 분실 복구 대상(가를 수 있는 자리),
    초대 대상 고객사 · 고객 설정 · 유형별 비용 · 문의 처리 · 문의 이력(존재 확인이 드는 자리).
    잠금 뒤 재확인(조치 · 종료)은 tests/test_incident_tenant_scope.py 가 본다.
  - 이 행은 중앙 관리자만 읽는다 — tests/test_platform_events_api.py.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import access_denial_log
import platform_event_log
import pytest
from db import models
from db.repositories import inquiries as inquiries_repo
from db.session import get_session_factory
from exceptions import ApiError, OutOfScopeError
from schemas.api.auth import AccountType, PlatformRole, UserRole
from schemas.api.errors import ErrorCode
from schemas.api.support import InquiryCategory
from schemas.incidents import AgentInvocationStatus
from schemas.platform_events import PlatformActorKind, PlatformEventCategory, PlatformEventOutcome
from schemas.runbooks import RunbookId
from sqlalchemy import event, select
from tests.group_scope import assign_tenants
from tests.test_totp_recovery_api import env  # noqa: F401 — TOTP 복구의 독립 commit 환경(픽스처)

NOW = datetime(2026, 10, 22, 5, 0, tzinfo=UTC)

# 다른 테스트가 독립 세션으로 commit 해 둔 행에 기대지 않는다 — 시작할 때 이미 있던 행은 빼고 본다
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


def _error(resp) -> tuple:
    """응답에서 요청 식별자를 뺀 전부 — 「없는 ID」와 「범위 밖 ID」가 같아야 하는 것."""
    body = resp.json()["error"]
    return resp.status_code, body["code"], body["message"]


# ----- 세계: 고객사 둘, 요청자는 mine 쪽 · 대상은 other 쪽 ------------------------------------------


@pytest.fixture()
def world(db, make_tenant, make_auth_session, make_incident, make_executable):
    mine, other = make_tenant(), make_tenant()
    incident = make_incident(db, summary_lines=["요약 1", "요약 2", "요약 3"])
    incident.agent_invocation_status = AgentInvocationStatus.SUCCEEDED
    make_executable(db, incident=incident)
    incident.tenant_id = other
    foreign_user = make_auth_session(UserRole.OPERATOR, tenant_id=other)
    staff = make_auth_session(UserRole.ADMIN, platform_role=PlatformRole.TENANT_MANAGER)
    inquiry = inquiries_repo.create_inquiry(
        db, tenant_id=other, author_user_id=foreign_user.user_id, category=InquiryCategory.OTHER,
        title="시드", body="본문", occurred_at=None, page_path=None, now=NOW,
    )
    db.commit()
    return SimpleNamespace(
        mine=mine, other=other, incident=incident.incident_id, foreign_user=foreign_user.user_id,
        staff=staff.user_id, inquiry=inquiry.inquiry_id,
    )


def _customer(login_as, world):
    """mine 의 고객사 ADMIN — 인시던트 · 사용자 관리 · 고객 설정 · 비용을 자기 고객사 범위로 본다."""
    return login_as(UserRole.ADMIN, tenant_id=world.mine)


def _manager(db, login_as, world):
    """mine 만 맡은 담당자 — 문의 처리는 관리 등급의 일이다."""
    seed = login_as(UserRole.ADMIN, platform_role=PlatformRole.TENANT_MANAGER)
    assign_tenants(db, user_id=seed.user_id, tenant_ids=(world.mine,), now=NOW)
    db.commit()
    return seed


#: 자리 이름 → (요청자, 대상을 world 에서 고르는 칸, (target_id) → (메서드, 주소, 본문), 나갈 코드, 행에 남는 경로)
SITES = {
    "incident_detail": (
        "customer", "incident",
        lambda t: ("GET", f"/api/v1/incidents/{t}", None),
        "INCIDENT_NOT_FOUND", lambda t: f"/api/v1/incidents/{t}",
    ),
    "action_execute": (
        "customer", "incident",
        lambda t: ("POST", "/api/v1/actions/execute", {
            "incident_id": t, "runbook_id": RunbookId.RUNBOOK_NACL_ADD_DENY.value,
            "idempotency_key": str(uuid.uuid4()),
        }),
        "INCIDENT_NOT_FOUND", lambda t: "/api/v1/actions/execute",  # 대상은 본문에 있다 — 경로에 남지 않는다
    ),
    "incident_resolve": (
        "customer", "incident",
        lambda t: ("POST", f"/api/v1/incidents/{t}/resolve", {"resolution": "NO_FURTHER_ACTION"}),
        "INCIDENT_NOT_FOUND", lambda t: f"/api/v1/incidents/{t}/resolve",
    ),
    "user_role": (
        "customer", "foreign_user",
        lambda t: ("PUT", f"/api/v1/users/{t}/role", {"role": "VIEWER"}),
        "USER_NOT_FOUND", lambda t: f"/api/v1/users/{t}/role",
    ),
    "user_events": (
        "customer", "foreign_user",
        lambda t: ("GET", f"/api/v1/users/{t}/events", None),
        "USER_NOT_FOUND", lambda t: f"/api/v1/users/{t}/events",
    ),
    "invite_tenant": (
        "customer", "other",
        lambda t: ("POST", "/api/v1/users", {"email": "new@example.com", "role": "VIEWER", "tenant_id": t}),
        "TENANT_NOT_FOUND", lambda t: "/api/v1/users",  # 대상은 본문에 있다
    ),
    "tenant_settings": (
        "customer", "other",
        lambda t: ("GET", f"/api/v1/tenants/{t}/settings", None),
        "TENANT_NOT_FOUND", lambda t: f"/api/v1/tenants/{t}/settings",
    ),
    "asset_costs": (
        "customer", "other",
        lambda t: ("GET", f"/api/v1/costs/by-asset-type?tenant_id={t}", None),
        "TENANT_NOT_FOUND", lambda t: "/api/v1/costs/by-asset-type",  # 대상은 질의에 있다
    ),
    "inquiry_status": (
        "manager", "inquiry",
        lambda t: ("PUT", f"/api/v1/inquiries/{t}/status", {"status": "RESOLVED"}),
        "INQUIRY_NOT_FOUND", lambda t: f"/api/v1/inquiries/{t}/status",
    ),
    "inquiry_note": (
        "manager", "inquiry",
        lambda t: ("POST", f"/api/v1/inquiries/{t}/notes", {"note": "메모"}),
        "INQUIRY_NOT_FOUND", lambda t: f"/api/v1/inquiries/{t}/notes",
    ),
    "inquiry_events": (
        "manager", "inquiry",
        lambda t: ("GET", f"/api/v1/inquiries/{t}/events", None),
        "INQUIRY_NOT_FOUND", lambda t: f"/api/v1/inquiries/{t}/events",
    ),
}


def _viewer(kind, db, login_as, world):
    return _customer(login_as, world) if kind == "customer" else _manager(db, login_as, world)


def _send(client, request):
    method, url, body = request
    return client.request(method, url, json=body)


@pytest.fixture()
def selects(pg_engine):
    """요청이 실행한 조회(SELECT) 문장을 차례로 모은다. 값은 빼고 문장만 — 「같은 일을 했는가」를 견준다."""
    seen: list[str] = []

    def record(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            seen.append(" ".join(statement.split()))

    event.listen(pg_engine, "before_cursor_execute", record)
    yield seen
    event.remove(pg_engine, "before_cursor_execute", record)


# ----- 자리마다: 범위 밖은 한 행, 없는 ID 는 0행, 응답과 조회는 같다 ---------------------------------


@pytest.mark.parametrize("site", sorted(SITES))
def test_out_of_scope_is_recorded_and_looks_exactly_like_missing(
    client_pg, db, login_as, world, selects, site
):
    viewer_kind, target_field, build, code, recorded_path = SITES[site]
    viewer = _viewer(viewer_kind, db, login_as, world)
    target = getattr(world, target_field)
    missing = str(uuid.uuid4())

    selects.clear()
    hidden = _send(client_pg, build(target))
    for_hidden = list(selects)
    assert _error(hidden)[:2] == (404, code)
    [row] = _events(db)

    selects.clear()
    absent = _send(client_pg, build(missing))
    for_absent = list(selects)

    # 나가는 응답은 같다 — 범위 밖을 404 로 묶어 둔 이유다
    assert _error(absent) == _error(hidden)
    # 없는 ID 는 행을 남기지 않는다
    assert [e.event_id for e in _events(db)] == [row.event_id]
    # 응답 전에 한 조회가 같다(쓰기는 조회가 아니고 응답 뒤다)
    assert for_absent == for_hidden

    assert (row.action, row.outcome) == ("ACCESS_OUT_OF_SCOPE", PlatformEventOutcome.DENIED)
    assert row.actor_kind is PlatformActorKind.USER
    assert (row.actor_user_id, row.session_id) == (viewer.user_id, viewer.session_id)
    assert (row.actor_account_type, row.actor_platform_role) == (viewer.account_type, viewer.platform_role)
    # 소속은 요청자의 고객사다(담당자는 없음). 대상의 고객사는 어디에도 싣지 않는다
    assert row.tenant_id == viewer.tenant_id
    assert (row.target_type, row.target_id) == (None, None)
    method = build(target)[0]
    assert row.details == {"reason_code": code, "method": method, "path": recorded_path(target)}
    if target not in recorded_path(target):
        # 대상이 본문 · 질의에 있는 자리 — 행 어디에도 대상이 남지 않는다(조사의 한계로 받아들인 것)
        assert target not in f"{row.details}{row.tenant_id}{row.target_id}"


def test_platform_and_unassigned_accounts_are_out_of_scope_for_a_customer_admin(
    client_pg, db, login_as, make_auth_session, world
):
    """고객사 관리자가 연 플랫폼 계정 · 미지정 계정 — 있지만 관리할 수 없는 대상이라 같은 일로 남는다(#589 Q3)."""
    viewer = _customer(login_as, world)
    unassigned = make_auth_session(UserRole.ADMIN, account_type=AccountType.UNASSIGNED)
    for target in (world.staff, unassigned.user_id):
        resp = client_pg.put(
            f"/api/v1/users/{target}/role", json={"role": "VIEWER"},
            headers={"X-Request-ID": "req-oos-1", "User-Agent": "pytest-agent/1.0"},
        )
        assert _error(resp)[:2] == (404, "USER_NOT_FOUND")
    rows = _events(db)
    # 요청 문맥은 응답 전에 행을 만들 때 실린다 — 응답 뒤에는 이미 풀려 있다
    assert {(r.request_id, r.user_agent) for r in rows} == {("req-oos-1", "pytest-agent/1.0")}
    assert [r.details["path"] for r in rows] == [
        f"/api/v1/users/{world.staff}/role", f"/api/v1/users/{unassigned.user_id}/role",
    ]
    # 어느 경우인지(다른 고객사 · 플랫폼 · 미지정)는 싣지 않는다 — 사유 코드는 나간 응답 코드 하나다
    assert {tuple(sorted(r.details)) for r in rows} == {("method", "path", "reason_code")}
    assert {r.actor_user_id for r in rows} == {viewer.user_id}


def test_malformed_ids_are_not_out_of_scope(client_pg, db, login_as, world):
    _customer(login_as, world)
    for url in (
        "/api/v1/incidents/not-a-uuid", "/api/v1/users/not-a-uuid/events",
        "/api/v1/tenants/not-a-uuid/settings", "/api/v1/costs/by-asset-type?tenant_id=not-a-uuid",
    ):
        assert client_pg.get(url).status_code == 404
    assert _events(db) == []


def test_in_scope_requests_and_other_refusals_record_nothing(
    client_pg, client_pg_anon, db, login_as, world, selects
):
    assert client_pg_anon.get(f"/api/v1/incidents/{world.incident}").status_code == 401  # 신원 없음
    login_as(UserRole.ADMIN)  # 중앙 관리자 — 전체 범위
    assert client_pg.get(f"/api/v1/incidents/{world.incident}").status_code == 200
    assert client_pg.get(f"/api/v1/tenants/{world.other}/settings").status_code == 200
    selects.clear()
    assert client_pg.get(f"/api/v1/incidents/{uuid.uuid4()}").status_code == 404           # 전체 범위의 「없음」
    assert client_pg.get(f"/api/v1/inquiries/{uuid.uuid4()}/events").status_code == 404
    assert client_pg.get(f"/api/v1/costs/by-asset-type?tenant_id={uuid.uuid4()}").status_code == 404
    assert _events(db) == []
    # 전체 범위의 「없음」은 더 볼 것이 없다 — 존재 확인은 범위가 걸린 조회의 404 에서만 한다
    assert not [statement for statement in selects if "EXISTS" in statement.upper()]


def test_write_failure_after_the_response_keeps_the_404(client_pg, db, login_as, world):
    _customer(login_as, world)

    def unavailable():
        raise RuntimeError("detached session unavailable")

    client_pg.app.dependency_overrides[get_session_factory] = lambda: unavailable
    resp = client_pg.get(f"/api/v1/incidents/{world.incident}")
    assert _error(resp)[:2] == (404, "INCIDENT_NOT_FOUND")
    assert _events(db) == []


# ----- TOTP 분실 복구 — 독립 commit 환경 -----------------------------------------------------------


def test_totp_recovery_of_a_hidden_target_is_out_of_scope_and_a_missing_one_is_not(env):  # noqa: F811
    own, foreign = env.tenant(), env.tenant()
    admin = env.user(UserRole.ADMIN, tenant_id=own)
    hidden = env.user(tenant_id=foreign).user_id
    platform = env.user(UserRole.ADMIN).user_id
    missing = str(uuid.uuid4())
    for target in (hidden, platform, missing):
        resp = env.post(admin, target)
        assert _error(resp)[:2] == (404, "USER_NOT_FOUND")

    with env.engine.connect() as conn:
        rows = conn.execute(
            select(models.PlatformEvent.action, models.PlatformEvent.tenant_id, models.PlatformEvent.details)
            .where(models.PlatformEvent.actor_user_id == admin.user_id)
            .order_by(models.PlatformEvent.occurred_at)
        ).all()
    # 읽기 단계와 잠금 안에서 두 번 판정해도 요청당 한 행이다. 없는 사용자는 남지 않는다
    assert [(action, tenant) for action, tenant, _ in rows] == [("ACCESS_OUT_OF_SCOPE", own)] * 2
    assert [details["path"] for *_, details in rows] == [
        f"/api/v1/users/{hidden}/totp/reset", f"/api/v1/users/{platform}/totp/reset",
    ]
    assert {details["reason_code"] for *_, details in rows} == {"USER_NOT_FOUND"}
    assert env.built == 0 and env.adapter.deleted == []  # 제공자는 부르지 않았다


# ----- 장치: 행은 응답 전에 만들고, 쓰기는 응답 뒤 -------------------------------------------------


def _request(user, *, path="/api/v1/incidents/x", route="/api/v1/incidents/{incident_id}"):
    return SimpleNamespace(
        method="GET",
        url=SimpleNamespace(path=path),
        scope={"route": SimpleNamespace(path=route)},
        state=SimpleNamespace(current_user=user),
    )


def _user(account_type=AccountType.TENANT, tenant_id=None, platform_role=None):
    return SimpleNamespace(
        user_id=str(uuid.uuid4()), account_type=account_type, tenant_id=tenant_id or str(uuid.uuid4()),
        platform_role=platform_role, session_id=str(uuid.uuid4()),
    )


def test_the_handler_builds_the_row_without_touching_the_database(monkeypatch):
    monkeypatch.setattr(
        platform_event_log, "write_detached", lambda *a, **k: pytest.fail("처리기는 쓰지 않는다")
    )
    user = _user()
    request = _request(user)
    access_denial_log.defer_out_of_scope(request, ErrorCode.INCIDENT_NOT_FOUND)

    [row] = request.state.pending_platform_events
    assert (row.action, row.actor_user_id, row.tenant_id) == ("ACCESS_OUT_OF_SCOPE", user.user_id, user.tenant_id)
    assert row.details == {"reason_code": "INCIDENT_NOT_FOUND", "method": "GET", "path": "/api/v1/incidents/x"}


def test_the_handler_queues_nothing_it_cannot_record():
    # 신원 없음 · 형식에 맞는 경로 없음 · 기록 계약 밖의 계정 — 404 는 그대로 나가고 걸리는 행이 없다
    cases = [
        SimpleNamespace(method="GET", url=SimpleNamespace(path="/x"), scope={}, state=SimpleNamespace()),
        _request(_user(), path="/a b", route=None),
        _request(_user(account_type=AccountType.UNASSIGNED)),
    ]
    for request in cases:
        access_denial_log.defer_out_of_scope(request, ErrorCode.INCIDENT_NOT_FOUND)
        assert getattr(request.state, "pending_platform_events", []) == []


def test_pending_rows_are_written_only_after_the_response_is_sent(monkeypatch):
    order: list[str] = []
    monkeypatch.setattr(
        platform_event_log, "write_detached", lambda factory, row: order.append(f"write:{row}") or True
    )

    async def app(scope, receive, send):
        scope.setdefault("state", {})["pending_platform_events"] = ["row-1", "row-2"]
        await send({"type": "http.response.start", "status": 404, "headers": []})
        await send({"type": "http.response.body", "body": b"{}"})

    async def send(message):
        order.append(message["type"])

    scope = {
        "type": "http", "state": {},
        "app": SimpleNamespace(dependency_overrides={get_session_factory: lambda: "factory"}),
    }
    asyncio.run(access_denial_log.DeferredPlatformEventMiddleware(app)(scope, None, send))

    assert order == ["http.response.start", "http.response.body", "write:row-1", "write:row-2"]
    assert "pending_platform_events" not in scope["state"]  # 한 번만 쓴다


def test_nothing_pending_opens_no_session():
    def no_factory():
        raise AssertionError("걸린 행이 없으면 세션 공장도 부르지 않는다")

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})

    async def send(message):
        return None

    scope = {
        "type": "http", "state": {},
        "app": SimpleNamespace(dependency_overrides={get_session_factory: no_factory}),
    }
    asyncio.run(access_denial_log.DeferredPlatformEventMiddleware(app)(scope, None, send))


def test_the_marker_is_an_api_error_with_the_same_response():
    """표시는 응답을 바꾸지 않는다 — 같은 코드의 ApiError 로 잡히고 같은 봉투가 나간다."""
    marked = OutOfScopeError(ErrorCode.INCIDENT_NOT_FOUND)
    assert isinstance(marked, ApiError) and marked.code is ErrorCode.INCIDENT_NOT_FOUND
