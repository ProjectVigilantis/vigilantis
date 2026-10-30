"""/api/v1/inquiries — 고객 문의 접수 · 조회 · 전이 · 메모 · 이력 (#552). 실제 PostgreSQL.

접수는 고객사(TENANT) 계정의 모든 역할이 하고, 정지 테넌트의 활성 사용자도 자기 테넌트에 접수 · 조회한다(사용자 결정).
전이 · 메모 · 이력은 플랫폼 관리 권한자(CENTRAL_ADMIN 전체 · TENANT_MANAGER 배정 고객사)뿐이며 고객사 계정은
자기 문의도 403 이다. 범위 밖 · 형식 오류 · 없는 ID 는 같은 404 INQUIRY_NOT_FOUND 다.
쓰기가 실패한 요청은 문의 · 이력 · 알림 어느 것도 남기지 않는다(한 트랜잭션).
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from auth import sessions
from db import models
from db.repositories import auth as auth_repo
from db.repositories import inquiries as inquiries_repo
from schemas.api.auth import PlatformRole, UserRole
from schemas.api.support import InquiryCategory, InquiryEventAction, InquiryStatus
from schemas.api.tenants import TenantStatus
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, SQLAlchemyError

BASE = "/api/v1/inquiries"
ITEM_KEYS = {
    "inquiry_id", "tenant_id", "tenant_name", "author_email", "author_role", "category", "title",
    "body", "occurred_at", "page_path", "status", "created_at", "updated_at",
}
EVENT_KEYS = {
    "event_id", "inquiry_id", "actor_user_id", "actor_email", "action", "before_status", "after_status", "note",
    "occurred_at",
}
OPEN, PROGRESS, RESOLVED = InquiryStatus.OPEN, InquiryStatus.IN_PROGRESS, InquiryStatus.RESOLVED


def payload(**overrides) -> dict:
    return {"category": "BUG", "title": "접속 장애", "body": "대시보드가 열리지 않습니다", **overrides}


def code(response) -> str:
    return response.json()["error"]["code"]


def scalar(db, sql: str, **params):
    return db.execute(text(sql), params).scalar_one()


def counts(db) -> tuple[int, int, int]:
    return (
        scalar(db, "SELECT count(*) FROM inquiries"),
        scalar(db, "SELECT count(*) FROM inquiry_events"),
        scalar(db, "SELECT count(*) FROM notifications"),
    )


def stored_status(db, inquiry_id: str) -> str:
    return scalar(db, "SELECT status::text FROM inquiries WHERE inquiry_id = :i", i=inquiry_id)


def event_rows(db, inquiry_id: str):
    return db.execute(
        text(
            "SELECT action::text, before_status::text, after_status::text, note, actor_user_id::text "
            "FROM inquiry_events WHERE inquiry_id = :i ORDER BY occurred_at, event_id"
        ),
        {"i": inquiry_id},
    ).all()


@pytest.fixture()
def tenant(make_tenant) -> str:
    return make_tenant()


@pytest.fixture()
def customer(login_as, tenant):
    return login_as(UserRole.VIEWER, tenant_id=tenant)


@pytest.fixture()
def central(make_auth_session):
    return make_auth_session(UserRole.ADMIN, platform_role=PlatformRole.CENTRAL_ADMIN)


def manager_of(make_auth_session, *tenant_ids: str):
    return make_auth_session(
        UserRole.ADMIN, platform_role=PlatformRole.TENANT_MANAGER, assigned_tenant_ids=tuple(tenant_ids)
    )


def seed_inquiry(db, make_auth_session, tenant_id: str, *, status: InquiryStatus = OPEN, author=None, **kwargs):
    """API 를 거치지 않고 문의 + 생성 이력을 commit 한다 → (inquiry_id, 작성자 AuthSeed)."""
    author = author or make_auth_session(UserRole.OPERATOR, tenant_id=tenant_id)
    now = datetime.now(UTC)
    row = inquiries_repo.create_inquiry(
        db,
        tenant_id=tenant_id,
        author_user_id=author.user_id,
        category=kwargs.get("category", InquiryCategory.OTHER),
        title=kwargs.get("title", "시드 문의"),
        body="본문",
        occurred_at=None,
        page_path=None,
        now=now,
    )
    inquiries_repo.add_event(
        db, inquiry_id=row.inquiry_id, actor_user_id=author.user_id,
        action=InquiryEventAction.INQUIRY_CREATED, before_status=None, after_status=OPEN, note=None, now=now,
    )
    if status is not OPEN:
        assert inquiries_repo.transition_status(
            db, row.inquiry_id, from_status=OPEN, to_status=status, now=now
        )
    db.commit()
    return row.inquiry_id, author


# --- 접수 ----------------------------------------------------------------------------


@pytest.mark.parametrize("role", [UserRole.ADMIN, UserRole.OPERATOR, UserRole.VIEWER])
def test_every_tenant_role_can_submit_and_creation_is_one_coherent_write(
    client_pg, db, login_as, make_tenant, role
):
    tenant_id = make_tenant()
    seed = login_as(role, tenant_id=tenant_id)
    when = "2026-01-02T03:04:05+09:00"

    response = client_pg.post(BASE, json=payload(category="OUTAGE", occurred_at=when, page_path="/assets"))

    assert response.status_code == 201, response.text
    body = response.json()
    assert set(body) == ITEM_KEYS
    assert body["tenant_id"] == tenant_id
    assert body["tenant_name"] == scalar(db, "SELECT name FROM tenants WHERE tenant_id = :t", t=tenant_id)
    assert body["author_email"] == scalar(db, "SELECT email FROM app_users WHERE user_id = :u", u=seed.user_id)
    assert body["author_role"] == role.value
    assert (body["category"], body["status"], body["page_path"]) == ("OUTAGE", "OPEN", "/assets")
    assert body["occurred_at"] == "2026-01-01T18:04:05Z"
    assert body["created_at"].endswith("Z") and body["created_at"] == body["updated_at"]
    assert scalar(db, "SELECT author_user_id::text FROM inquiries WHERE inquiry_id = :i", i=body["inquiry_id"]) == seed.user_id
    assert event_rows(db, body["inquiry_id"]) == [("INQUIRY_CREATED", None, "OPEN", None, seed.user_id)]
    notification = db.execute(
        text(
            "SELECT kind::text, tenant_id::text, subject, summary, inquiry_category::text "
            "FROM notifications WHERE target_id = :i"
        ),
        {"i": body["inquiry_id"]},
    ).all()
    assert notification == [("INQUIRY", tenant_id, body["tenant_name"], "접속 장애", "OUTAGE")]


@pytest.mark.parametrize("field", ["tenant_id", "author_user_id", "status", "author_email"])
def test_identity_and_server_fields_are_rejected_in_the_body(client_pg, db, customer, field):
    before = counts(db)
    response = client_pg.post(BASE, json=payload(**{field: str(uuid4())}))
    assert response.status_code == 422 and code(response) == "REQUEST_VALIDATION_FAILED"
    assert counts(db) == before


@pytest.mark.parametrize(
    "bad",
    [
        {"title": " "}, {"title": "가" * 101}, {"body": ""}, {"body": "a" * 1001}, {"category": "NOPE"},
        {"page_path": "a" * 1001}, {"occurred_at": "2026-01-01T00:00:00"}, {"body": "x\x00y"},
    ],
)
def test_invalid_submission_is_422_and_leaves_nothing(client_pg, db, customer, bad):
    before = counts(db)
    response = client_pg.post(BASE, json=payload(**bad))
    assert response.status_code == 422
    assert counts(db) == before
    assert "대시보드" not in response.text


def test_nul_never_reaches_the_response_or_storage(client_pg, db, customer):
    before = counts(db)
    response = client_pg.post(BASE, content='{"category":"BUG","title":"t","body":"SECRET\\u0000"}',
                              headers={"Content-Type": "application/json"})
    assert response.status_code == 422 and "SECRET" not in response.text
    assert counts(db) == before


def test_platform_accounts_cannot_submit(client_pg, db, central, make_auth_session, tenant):
    before = counts(db)
    for seed in (central, manager_of(make_auth_session, tenant)):
        response = client_pg.post(BASE, json=payload(), headers=seed.headers)
        assert response.status_code == 403 and code(response) == "AUTH_FORBIDDEN"
    # 잘못된 본문도 게이트가 먼저다
    assert client_pg.post(BASE, json={"bad": 1}, headers=central.headers).status_code == 403
    assert counts(db) == before


def test_suspended_tenant_user_can_submit_and_read_only_own_tenant(client_pg, db, login_as, make_tenant, make_auth_session):
    suspended = make_tenant(TenantStatus.SUSPENDED)
    other = make_tenant()
    foreign_id, _ = seed_inquiry(db, make_auth_session, other)
    seed = login_as(UserRole.VIEWER, tenant_id=suspended)

    created = client_pg.post(BASE, json=payload())
    assert created.status_code == 201, created.text
    assert created.json()["tenant_id"] == suspended

    listed = client_pg.get(BASE)
    assert listed.status_code == 200
    assert [i["inquiry_id"] for i in listed.json()["items"]] == [created.json()["inquiry_id"]]
    assert foreign_id not in listed.text
    assert event_rows(db, created.json()["inquiry_id"])[0][4] == seed.user_id


def test_unauthenticated_requests_are_401(client_pg_anon, allowed_origin):
    target = str(uuid4())
    origin = {"Origin": allowed_origin}  # 상태 변경은 Origin 검사가 먼저다
    calls = [
        client_pg_anon.post(BASE, json=payload(), headers=origin),
        client_pg_anon.get(BASE),
        client_pg_anon.put(f"{BASE}/{target}/status", json={"status": "RESOLVED"}, headers=origin),
        client_pg_anon.post(f"{BASE}/{target}/notes", json={"note": "n"}, headers=origin),
        client_pg_anon.get(f"{BASE}/{target}/events"),
    ]
    assert [c.status_code for c in calls] == [401] * 5


@pytest.mark.parametrize("who", ["customer", "manager"])
def test_disabled_and_revoked_sessions_are_denied_on_every_route(client_pg, db, make_auth_session, tenant, who):
    inquiry_id, _ = seed_inquiry(db, make_auth_session, tenant)
    seed = (
        make_auth_session(UserRole.VIEWER, tenant_id=tenant)
        if who == "customer"
        else manager_of(make_auth_session, tenant)
    )
    routes = [
        ("POST", BASE, payload()),
        ("GET", BASE, None),
        ("PUT", f"{BASE}/{inquiry_id}/status", {"status": "RESOLVED"}),
        ("POST", f"{BASE}/{inquiry_id}/notes", {"note": "n"}),
        ("GET", f"{BASE}/{inquiry_id}/events", None),
    ]

    def call_all():
        return [client_pg.request(m, p, json=b, headers=seed.headers) for m, p, b in routes]

    sessions.revoke_user_sessions(
        db, user_id=seed.user_id, now=datetime.now(UTC), reason=models.AuthSessionRevokeReason.ADMIN_REVOKED
    )
    db.commit()
    before = counts(db)
    assert [(r.status_code, code(r)) for r in call_all()] == [(401, "AUTH_REQUIRED")] * 5
    assert counts(db) == before

    fresh = (
        make_auth_session(UserRole.VIEWER, tenant_id=tenant)
        if who == "customer"
        else manager_of(make_auth_session, tenant)
    )
    seed = fresh
    assert auth_repo.set_status(
        db, user_id=fresh.user_id, status=models.AppUserStatus.DISABLED, now=datetime.now(UTC)
    )
    db.commit()
    before = counts(db)
    # 비활성 사용자의 기존 세션은 판정 단계에서 무효다(auth.service.resolve_current_user)
    assert [(r.status_code, code(r)) for r in call_all()] == [(401, "AUTH_REQUIRED")] * 5
    assert counts(db) == before
    assert stored_status(db, inquiry_id) == "OPEN"


# --- 목록 · 범위 ----------------------------------------------------------------------


def test_list_scope_by_actor_and_current_join_values(client_pg, db, make_tenant, make_auth_session):
    t1, t2, t3 = make_tenant(), make_tenant(TenantStatus.SUSPENDED), make_tenant()
    i1, a1 = seed_inquiry(db, make_auth_session, t1)
    i2, _ = seed_inquiry(db, make_auth_session, t2)
    i3, _ = seed_inquiry(db, make_auth_session, t3)
    central = make_auth_session(UserRole.ADMIN, platform_role=PlatformRole.CENTRAL_ADMIN)
    own_viewer = make_auth_session(UserRole.VIEWER, tenant_id=t1)

    def ids(seed):
        response = client_pg.get(BASE, headers=seed.headers)
        assert response.status_code == 200
        return {i["inquiry_id"] for i in response.json()["items"]}

    assert {i1, i2, i3} <= ids(central)
    assert ids(manager_of(make_auth_session, t1, t3)) == {i1, i3}
    assert ids(manager_of(make_auth_session)) == set()
    assert ids(own_viewer) == {i1}

    # 표시 값은 당시 스냅샷이 아니라 현재 조인 값이다
    db.execute(text("UPDATE app_users SET email = 'renamed@example.com' WHERE user_id = :u"), {"u": a1.user_id})
    db.execute(text("UPDATE tenants SET name = '바뀐 이름' WHERE tenant_id = :t"), {"t": t1})
    db.commit()
    item = next(i for i in client_pg.get(BASE, headers=own_viewer.headers).json()["items"] if i["inquiry_id"] == i1)
    assert (item["author_email"], item["tenant_name"], item["author_role"]) == ("renamed@example.com", "바뀐 이름", "OPERATOR")
    assert set(item) == ITEM_KEYS


def test_list_is_newest_first(client_pg, db, tenant, make_auth_session, customer):
    first = client_pg.post(BASE, json=payload(title="첫째")).json()["inquiry_id"]
    second = client_pg.post(BASE, json=payload(title="둘째")).json()["inquiry_id"]
    assert [i["inquiry_id"] for i in client_pg.get(BASE).json()["items"]] == [second, first]


# --- 관리 경로: 역할 · 범위 ------------------------------------------------------------


def management_routes(inquiry_id: str):
    return [
        ("PUT", f"{BASE}/{inquiry_id}/status", {"status": "RESOLVED"}),
        ("POST", f"{BASE}/{inquiry_id}/notes", {"note": "메모"}),
        ("GET", f"{BASE}/{inquiry_id}/events", None),
    ]


@pytest.mark.parametrize("role", [UserRole.ADMIN, UserRole.OPERATOR, UserRole.VIEWER])
def test_tenant_accounts_get_403_on_management_routes_even_for_own_inquiry(
    client_pg, db, make_tenant, make_auth_session, role
):
    tenant_id = make_tenant()
    author = make_auth_session(role, tenant_id=tenant_id)
    inquiry_id, _ = seed_inquiry(db, make_auth_session, tenant_id, author=author)
    before = counts(db)
    for method, path, body in management_routes(inquiry_id):
        response = client_pg.request(method, path, json=body, headers=author.headers)
        assert response.status_code == 403 and code(response) == "AUTH_FORBIDDEN", (method, path)
    assert counts(db) == before and stored_status(db, inquiry_id) == "OPEN"


def test_management_scope_foreign_malformed_and_missing_are_one_404(client_pg, db, make_tenant, make_auth_session):
    mine, foreign = make_tenant(), make_tenant()
    foreign_id, _ = seed_inquiry(db, make_auth_session, foreign)
    scoped = manager_of(make_auth_session, mine)
    unassigned = manager_of(make_auth_session)
    before = counts(db)
    for target in (foreign_id, "not-a-uuid", str(uuid4()), "123"):
        for actor in (scoped, unassigned):
            for method, path, body in management_routes(target):
                response = client_pg.request(method, path, json=body, headers=actor.headers)
                assert (response.status_code, code(response)) == (404, "INQUIRY_NOT_FOUND"), (method, path)
    assert counts(db) == before and stored_status(db, foreign_id) == "OPEN"


def test_manager_acts_inside_scope_and_central_admin_everywhere(client_pg, db, make_tenant, make_auth_session):
    mine, other = make_tenant(), make_tenant()
    in_scope, _ = seed_inquiry(db, make_auth_session, mine)
    out_scope, _ = seed_inquiry(db, make_auth_session, other)
    scoped = manager_of(make_auth_session, mine)
    central = make_auth_session(UserRole.ADMIN, platform_role=PlatformRole.CENTRAL_ADMIN)

    assert client_pg.put(f"{BASE}/{in_scope}/status", json={"status": "IN_PROGRESS"}, headers=scoped.headers).status_code == 200
    assert client_pg.put(f"{BASE}/{out_scope}/status", json={"status": "IN_PROGRESS"}, headers=scoped.headers).status_code == 404
    assert client_pg.put(f"{BASE}/{out_scope}/status", json={"status": "IN_PROGRESS"}, headers=central.headers).status_code == 200
    # 대문자 · 하이픈 없는 표기도 같은 식별자다
    assert client_pg.get(f"{BASE}/{in_scope.upper()}/events", headers=scoped.headers).status_code == 200
    assert client_pg.get(f"{BASE}/{in_scope.replace('-', '')}/events", headers=scoped.headers).status_code == 200


# --- 상태 전이 ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("start", "target"), [(OPEN, PROGRESS), (OPEN, RESOLVED), (PROGRESS, RESOLVED)]
)
def test_forward_transition_updates_state_and_appends_one_event(
    client_pg, db, tenant, make_auth_session, central, start, target
):
    inquiry_id, _ = seed_inquiry(db, make_auth_session, tenant, status=start)
    created_at = scalar(db, "SELECT created_at FROM inquiries WHERE inquiry_id = :i", i=inquiry_id)

    response = client_pg.put(
        f"{BASE}/{inquiry_id}/status", json={"status": target.value, "note": " 확인 중 "}, headers=central.headers
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == ITEM_KEYS and body["status"] == target.value
    assert datetime.fromisoformat(body["updated_at"]) >= created_at.replace(microsecond=0)  # 응답은 초 단위
    rows = event_rows(db, inquiry_id)
    assert rows[-1] == ("STATUS_CHANGED", start.value, target.value, "확인 중", central.user_id)
    assert len(rows) == 2


def test_status_note_is_optional(client_pg, db, tenant, make_auth_session, central):
    inquiry_id, _ = seed_inquiry(db, make_auth_session, tenant)
    assert client_pg.put(f"{BASE}/{inquiry_id}/status", json={"status": "RESOLVED"}, headers=central.headers).status_code == 200
    assert event_rows(db, inquiry_id)[-1][3] is None


@pytest.mark.parametrize(
    ("start", "target"),
    [
        (OPEN, OPEN), (PROGRESS, PROGRESS), (PROGRESS, OPEN),
        (RESOLVED, RESOLVED), (RESOLVED, OPEN), (RESOLVED, PROGRESS),
    ],
)
def test_invalid_or_noop_transition_is_409_and_writes_nothing(
    client_pg, db, tenant, make_auth_session, central, start, target
):
    inquiry_id, _ = seed_inquiry(db, make_auth_session, tenant, status=start)
    before = counts(db)
    updated_before = scalar(db, "SELECT updated_at FROM inquiries WHERE inquiry_id = :i", i=inquiry_id)

    response = client_pg.put(
        f"{BASE}/{inquiry_id}/status", json={"status": target.value, "note": "x"}, headers=central.headers
    )

    assert (response.status_code, code(response)) == (409, "INQUIRY_STATE_CONFLICT")
    assert counts(db) == before and stored_status(db, inquiry_id) == start.value
    assert scalar(db, "SELECT updated_at FROM inquiries WHERE inquiry_id = :i", i=inquiry_id) == updated_before


@pytest.mark.parametrize("body", [{"status": "DONE"}, {}, {"status": "RESOLVED", "note": " "}, {"status": "RESOLVED", "note": "a\x00"},
                                  {"status": "RESOLVED", "x": 1}])
def test_invalid_status_body_is_422(client_pg, db, tenant, make_auth_session, central, body):
    inquiry_id, _ = seed_inquiry(db, make_auth_session, tenant)
    before = counts(db)
    response = client_pg.put(f"{BASE}/{inquiry_id}/status", json=body, headers=central.headers)
    assert response.status_code == 422 and stored_status(db, inquiry_id) == "OPEN"
    assert counts(db) == before


# --- 메모 · 이력 -----------------------------------------------------------------------------


@pytest.mark.parametrize("status", [OPEN, PROGRESS, RESOLVED])
def test_note_is_append_only_commentary_in_any_status(client_pg, db, tenant, make_auth_session, central, status):
    inquiry_id, _ = seed_inquiry(db, make_auth_session, tenant, status=status)
    updated_before = scalar(db, "SELECT updated_at FROM inquiries WHERE inquiry_id = :i", i=inquiry_id)

    response = client_pg.post(f"{BASE}/{inquiry_id}/notes", json={"note": " 전화 회신 "}, headers=central.headers)

    assert response.status_code == 201, response.text
    body = response.json()
    assert set(body) == EVENT_KEYS
    assert (body["action"], body["before_status"], body["after_status"], body["note"]) == ("NOTE_ADDED", None, None, "전화 회신")
    assert (body["inquiry_id"], body["actor_user_id"]) == (inquiry_id, central.user_id)
    assert body["actor_email"] == scalar(db, "SELECT email FROM app_users WHERE user_id = :u", u=central.user_id)
    assert stored_status(db, inquiry_id) == status.value
    assert scalar(db, "SELECT updated_at FROM inquiries WHERE inquiry_id = :i", i=inquiry_id) == updated_before
    assert event_rows(db, inquiry_id)[-1][0] == "NOTE_ADDED"


@pytest.mark.parametrize("body", [{}, {"note": ""}, {"note": "   "}, {"note": "a\x00b"}, {"note": "a" * 1001}, {"note": None},
                                  {"note": "ok", "status": "RESOLVED"}])
def test_invalid_note_is_422_and_adds_no_event(client_pg, db, tenant, make_auth_session, central, body):
    inquiry_id, _ = seed_inquiry(db, make_auth_session, tenant)
    before = counts(db)
    response = client_pg.post(f"{BASE}/{inquiry_id}/notes", json=body, headers=central.headers)
    assert response.status_code == 422
    assert counts(db) == before


def test_events_are_oldest_first_across_the_whole_lifecycle(client_pg, db, make_tenant, login_as, make_auth_session):
    tenant_id = make_tenant()
    login_as(UserRole.VIEWER, tenant_id=tenant_id)
    created = client_pg.post(BASE, json=payload()).json()
    staff = make_auth_session(UserRole.ADMIN, platform_role=PlatformRole.CENTRAL_ADMIN)
    inquiry_id = created["inquiry_id"]
    assert client_pg.put(f"{BASE}/{inquiry_id}/status", json={"status": "IN_PROGRESS"}, headers=staff.headers).status_code == 200
    assert client_pg.post(f"{BASE}/{inquiry_id}/notes", json={"note": "원인 확인"}, headers=staff.headers).status_code == 201
    assert client_pg.put(f"{BASE}/{inquiry_id}/status", json={"status": "RESOLVED", "note": "복구"}, headers=staff.headers).status_code == 200
    assert client_pg.post(f"{BASE}/{inquiry_id}/notes", json={"note": "종료 후 메모"}, headers=staff.headers).status_code == 201

    response = client_pg.get(f"{BASE}/{inquiry_id}/events", headers=staff.headers)

    assert response.status_code == 200
    items = response.json()["items"]
    assert [(i["action"], i["before_status"], i["after_status"], i["note"]) for i in items] == [
        ("INQUIRY_CREATED", None, "OPEN", None),
        ("STATUS_CHANGED", "OPEN", "IN_PROGRESS", None),
        ("NOTE_ADDED", None, None, "원인 확인"),
        ("STATUS_CHANGED", "IN_PROGRESS", "RESOLVED", "복구"),
        ("NOTE_ADDED", None, None, "종료 후 메모"),
    ]
    stamps = [i["occurred_at"] for i in items]  # 응답 시각은 초 단위(UtcDateTime)라 동률일 수 있다 — 순서는 목록 순서가 정한다
    assert stamps == sorted(stamps)
    stored = [r[0] for r in db.execute(
        text("SELECT occurred_at FROM inquiry_events WHERE inquiry_id = :i ORDER BY occurred_at"), {"i": inquiry_id}
    )]
    assert len(set(stored)) == len(stored)  # 저장 시각은 마이크로초까지 엄격히 증가한다
    assert all(set(i) == EVENT_KEYS for i in items)
    emails = {c: scalar(db, "SELECT email FROM app_users WHERE user_id = :u", u=c) for c in (items[0]["actor_user_id"], staff.user_id)}
    assert [i["actor_email"] for i in items] == [emails[i["actor_user_id"]] for i in items]
    assert items[0]["actor_user_id"] != staff.user_id and items[1]["actor_email"] == emails[staff.user_id]

    # 행위자 이메일은 당시 값이 아니라 현재 조인 값이다
    db.execute(text("UPDATE app_users SET email = 'renamed-staff@example.com' WHERE user_id = :u"), {"u": staff.user_id})
    db.commit()
    renamed = client_pg.get(f"{BASE}/{inquiry_id}/events", headers=staff.headers).json()["items"]
    assert [i["actor_email"] for i in renamed if i["actor_user_id"] == staff.user_id] == ["renamed-staff@example.com"] * 4


def test_event_rows_reject_update_and_delete(db, tenant, make_auth_session):
    inquiry_id, _ = seed_inquiry(db, make_auth_session, tenant)
    for sql in (
        "UPDATE inquiry_events SET note = 'x' WHERE inquiry_id = :i",
        "DELETE FROM inquiry_events WHERE inquiry_id = :i",
    ):
        with pytest.raises(DBAPIError), db.begin_nested():
            db.execute(text(sql), {"i": inquiry_id})
    assert len(event_rows(db, inquiry_id)) == 1


# --- 원자성 ----------------------------------------------------------------------------------


def test_notification_failure_rolls_back_inquiry_and_creation_event(client_pg, db, customer, monkeypatch):
    import inquiries_service

    def boom(*args, **kwargs):
        raise SQLAlchemyError("SECRET-DB-DETAIL")

    monkeypatch.setattr(inquiries_service.operator_notifications, "add_notification", boom)
    before = counts(db)
    response = client_pg.post(BASE, json=payload())
    assert (response.status_code, code(response)) == (500, "INTERNAL_ERROR")
    assert "SECRET" not in response.text
    assert counts(db) == before


def test_event_failure_rolls_back_the_state_change(client_pg, db, tenant, make_auth_session, central, monkeypatch):
    inquiry_id, _ = seed_inquiry(db, make_auth_session, tenant)

    def boom(*args, **kwargs):
        raise SQLAlchemyError("SECRET-DB-DETAIL")

    monkeypatch.setattr(inquiries_repo, "add_event", boom)
    before = counts(db)
    response = client_pg.put(f"{BASE}/{inquiry_id}/status", json={"status": "RESOLVED"}, headers=central.headers)
    assert (response.status_code, code(response)) == (500, "INTERNAL_ERROR")
    assert "SECRET" not in response.text
    assert stored_status(db, inquiry_id) == "OPEN" and counts(db) == before
