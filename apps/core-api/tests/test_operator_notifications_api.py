"""/api/v1/notifications — 운영자 알림 수신함 · 읽음 (#552). 실제 PostgreSQL + 실세션.

지키는 것.
  ① 플랫폼 관리 역할만(고객사 계정 403 · 본문 검증보다 먼저, 미인증 401).
  ② 가시 범위: 가입 신청은 CENTRAL_ADMIN 만, 문의는 배정 고객사별. 배정 없는 담당자는 빈 목록 · 0.
  ③ items 는 최신 50건, unread_count 는 권한 범위 전체. 읽음은 사용자별이다.
  ④ 배정을 잃으면 숨겨져도 읽음은 남고, 다시 배정되면 읽음 상태가 돌아온다.
  ⑤ 개별 읽음은 하나라도 없거나 범위 밖이면 404 이고 아무것도 쓰지 않는다. null 은 자기 범위의 안 읽은 전부.
  ⑥ 읽음은 멱등이고 처음 read_at 이 유지된다. 회수된 자격은 쓰지 못한다.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from auth import sessions
from db import models
from db.repositories import auth as auth_repo
from db.repositories import operator_notifications as repo
from schemas.api.auth import PlatformRole, UserRole
from schemas.api.support import InquiryCategory, NotificationKind
from sqlalchemy import func, select

BASE = "/api/v1/notifications"
READ = BASE + "/read"
T0 = datetime(2026, 10, 30, 1, 0, tzinfo=UTC)


def _signup(db, minutes=0) -> str:
    n = repo.add_notification(
        db,
        kind=NotificationKind.SIGNUP_REQUEST,
        target_id=str(uuid.uuid4()),
        tenant_id=None,
        subject="학원",
        summary="가입 신청",
        inquiry_category=None,
        created_at=T0 + timedelta(minutes=minutes),
    )
    db.commit()
    return n.notification_id


def _inquiry(db, tenant_id, minutes=0) -> str:
    n = repo.add_notification(
        db,
        kind=NotificationKind.INQUIRY,
        target_id=str(uuid.uuid4()),
        tenant_id=tenant_id,
        subject="문의",
        summary="장애 문의",
        inquiry_category=InquiryCategory.OUTAGE,
        created_at=T0 + timedelta(minutes=minutes),
    )
    db.commit()
    return n.notification_id


def _get(client, seed):
    resp = client.get(BASE, headers=seed.headers)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _ids(body):
    return [i["notification_id"] for i in body["items"]]


def _read(client, seed, ids):
    return client.post(READ, json={"notification_ids": ids}, headers=seed.headers)


def _reads(db, user_id=None):
    q = select(func.count()).select_from(models.OperatorNotificationRead)
    if user_id:
        q = q.where(models.OperatorNotificationRead.user_id == user_id)
    return db.scalar(q)


def _manager(make_auth_session, *tenants):
    return make_auth_session(
        UserRole.ADMIN, platform_role=PlatformRole.TENANT_MANAGER, assigned_tenant_ids=tuple(tenants)
    )


def _assign(db, seed, *tenants):
    auth_repo.replace_assigned_tenants(db, user_id=seed.user_id, tenant_ids=tuple(tenants), now=T0)
    db.commit()


def test_tenant_users_are_forbidden_before_body_validation(client_pg, db, make_auth_session, make_tenant):
    tenant_user = make_auth_session(UserRole.ADMIN, tenant_id=make_tenant())
    assert client_pg.get(BASE, headers=tenant_user.headers).status_code == 403
    bad = client_pg.post(READ, json={"notification_ids": ["nope"]}, headers=tenant_user.headers)
    assert bad.status_code == 403


def test_anonymous_is_rejected(client_pg_anon):
    assert client_pg_anon.get(BASE).status_code == 401


def test_central_admin_sees_signups_and_every_inquiry(client_pg, db, platform_session, make_tenant):
    a, b = make_tenant(), make_tenant()
    ids = [_signup(db, 1), _inquiry(db, a, 2), _inquiry(db, b, 3)]
    body = _get(client_pg, platform_session)
    assert _ids(body) == ids[::-1] and body["unread_count"] == 3
    assert all(i["read_at"] is None for i in body["items"])


def test_manager_sees_only_assigned_inquiries_and_no_signups(client_pg, db, make_auth_session, make_tenant):
    a, b = make_tenant(), make_tenant()
    _signup(db, 1)
    mine = _inquiry(db, a, 2)
    _inquiry(db, b, 3)
    body = _get(client_pg, _manager(make_auth_session, a))
    assert _ids(body) == [mine] and body["unread_count"] == 1


def test_manager_without_assignment_sees_nothing(client_pg, db, make_auth_session, make_tenant):
    _signup(db)
    _inquiry(db, make_tenant())
    assert _get(client_pg, _manager(make_auth_session)) == {"items": [], "unread_count": 0}


def test_unread_count_covers_everything_while_items_are_the_latest_fifty(client_pg, db, platform_session, make_tenant):
    tenant = make_tenant()
    ids = [_inquiry(db, tenant, m) for m in range(55)]
    body = _get(client_pg, platform_session)
    assert body["unread_count"] == 55
    assert _ids(body) == ids[::-1][:50]
    # 화면 밖(가장 오래된) 알림도 읽을 수 있고, 읽으면 총 개수만 줄어든다
    assert _read(client_pg, platform_session, [ids[0]]).status_code == 204
    after = _get(client_pg, platform_session)
    assert after["unread_count"] == 54 and _ids(after) == _ids(body)


def test_equal_created_at_orders_by_id_descending(client_pg, db, platform_session, make_tenant):
    tenant = make_tenant()
    ids = [_inquiry(db, tenant, 0) for _ in range(3)]
    assert _ids(_get(client_pg, platform_session)) == sorted(ids, reverse=True)


def test_reads_are_per_user(client_pg, db, platform_session, make_auth_session, make_tenant):
    tenant = make_tenant()
    nid = _inquiry(db, tenant)
    other = _manager(make_auth_session, tenant)
    assert _read(client_pg, platform_session, [nid]).status_code == 204
    mine = _get(client_pg, platform_session)
    theirs = _get(client_pg, other)
    assert mine["unread_count"] == 0 and mine["items"][0]["read_at"] is not None
    assert theirs["unread_count"] == 1 and theirs["items"][0]["read_at"] is None
    assert _reads(db, other.user_id) == 0


def test_read_never_changes_the_notification(client_pg, db, platform_session, make_tenant):
    nid = _inquiry(db, make_tenant())
    before = db.get(models.OperatorNotification, nid)
    snapshot = (before.kind, before.target_id, before.subject, before.summary, before.created_at)
    assert client_pg.post(READ, json={"notification_ids": None}, headers=platform_session.headers).status_code == 204
    db.expire_all()
    after = db.get(models.OperatorNotification, nid)
    assert snapshot == (after.kind, after.target_id, after.subject, after.summary, after.created_at)


def test_losing_assignment_hides_but_keeps_reads_and_reassignment_recovers(
    client_pg, db, make_auth_session, make_tenant
):
    a, b = make_tenant(), make_tenant()
    na, nb = _inquiry(db, a, 1), _inquiry(db, b, 2)
    manager = _manager(make_auth_session, a, b)
    assert _read(client_pg, manager, [na]).status_code == 204
    read_at = {i["notification_id"]: i["read_at"] for i in _get(client_pg, manager)["items"]}[na]

    _assign(db, manager, b)
    hidden = _get(client_pg, manager)
    assert _ids(hidden) == [nb] and hidden["unread_count"] == 1
    assert _read(client_pg, manager, [na]).status_code == 404
    assert _reads(db, manager.user_id) == 1

    _assign(db, manager, a, b)
    back = {i["notification_id"]: i["read_at"] for i in _get(client_pg, manager)["items"]}
    assert back == {na: read_at, nb: None}


def test_new_assignment_adds_notifications_to_the_next_request(client_pg, db, make_auth_session, make_tenant):
    a, b = make_tenant(), make_tenant()
    _inquiry(db, a, 1)
    nb = _inquiry(db, b, 2)
    manager = _manager(make_auth_session, a)
    assert _get(client_pg, manager)["unread_count"] == 1
    _assign(db, manager, a, b)
    body = _get(client_pg, manager)
    assert nb in _ids(body) and body["unread_count"] == 2


def test_explicit_mixed_list_is_atomic(client_pg, db, make_auth_session, make_tenant):
    a, b = make_tenant(), make_tenant()
    ok, outside, signup = _inquiry(db, a, 1), _inquiry(db, b, 2), _signup(db, 3)
    manager = _manager(make_auth_session, a)
    for bad in (outside, signup, str(uuid.uuid4())):
        resp = _read(client_pg, manager, [ok, bad])
        assert resp.status_code == 404 and resp.json()["error"]["code"] == "NOTIFICATION_NOT_FOUND"
        assert _reads(db) == 0
    assert _get(client_pg, manager)["unread_count"] == 1


def test_null_marks_only_the_callers_authorized_unread(client_pg, db, platform_session, make_auth_session, make_tenant):
    a, b = make_tenant(), make_tenant()
    mine, theirs = _inquiry(db, a, 1), _inquiry(db, b, 2)
    manager = _manager(make_auth_session, a)
    assert client_pg.post(READ, json={"notification_ids": None}, headers=manager.headers).status_code == 204
    assert _get(client_pg, manager)["unread_count"] == 0
    assert _reads(db, manager.user_id) == 1
    assert _get(client_pg, platform_session)["unread_count"] == 2
    assert {r.notification_id for r in db.scalars(select(models.OperatorNotificationRead))} == {mine}
    assert theirs not in {r.notification_id for r in db.scalars(select(models.OperatorNotificationRead))}


def test_null_marks_everything_including_hidden_beyond_the_page_and_new_arrivals(
    client_pg, db, platform_session, make_tenant
):
    tenant = make_tenant()
    for m in range(55):
        _inquiry(db, tenant, m)
    assert client_pg.post(READ, headers=platform_session.headers, json={}).status_code == 204
    assert _get(client_pg, platform_session)["unread_count"] == 0
    assert _reads(db) == 55
    fresh = _inquiry(db, tenant, 100)
    assert _get(client_pg, platform_session)["unread_count"] == 1
    assert _ids(_get(client_pg, platform_session))[0] == fresh


def test_replay_and_duplicates_keep_the_original_read_at(client_pg, db, platform_session, make_tenant):
    nid = _inquiry(db, make_tenant())
    assert _read(client_pg, platform_session, [nid, nid]).status_code == 204
    first = _get(client_pg, platform_session)["items"][0]["read_at"]
    assert _read(client_pg, platform_session, [nid]).status_code == 204
    assert client_pg.post(READ, json={"notification_ids": None}, headers=platform_session.headers).status_code == 204
    assert _get(client_pg, platform_session)["items"][0]["read_at"] == first
    assert _reads(db) == 1


def test_empty_list_is_a_no_op(client_pg, db, platform_session, make_tenant):
    _inquiry(db, make_tenant())
    assert _read(client_pg, platform_session, []).status_code == 204
    assert _reads(db) == 0


@pytest.mark.parametrize(
    "body",
    [{"notification_ids": ["nope"]}, {"notification_ids": "x"}, {"notification_ids": [str(uuid.uuid4())] * 51}, {"x": 1}],
)
def test_invalid_bodies_are_422_and_write_nothing(client_pg, db, platform_session, make_tenant, body):
    _inquiry(db, make_tenant())
    resp = client_pg.post(READ, json=body, headers=platform_session.headers)
    assert resp.status_code == 422
    assert _reads(db) == 0


def test_revoked_session_cannot_read_or_write(client_pg, db, platform_session, make_tenant):
    nid = _inquiry(db, make_tenant())
    sessions.revoke_session(db, platform_session.token, now=datetime.now(UTC))
    db.commit()
    assert client_pg.get(BASE, headers=platform_session.headers).status_code == 401
    assert _read(client_pg, platform_session, [nid]).status_code == 401
    assert _reads(db) == 0


def _change_between_router_dependency_and_write_authorization(monkeypatch, change):
    """라우터 의존성(플랫폼 관리 게이트)이 통과한 뒤, 쓰기 인가가 사용자 잠금을 잡는 시점에 바꾼다."""
    original = auth_repo.lock_users_table

    def wrapped(session):
        change(session)
        session.flush()
        original(session)

    monkeypatch.setattr(auth_repo, "lock_users_table", wrapped)


def test_assignment_removed_after_the_dependency_snapshot_writes_nothing(
    client_pg, db, make_auth_session, make_tenant, monkeypatch
):
    tenant = make_tenant()
    nid = _inquiry(db, tenant)
    manager = _manager(make_auth_session, tenant)
    _change_between_router_dependency_and_write_authorization(
        monkeypatch,
        lambda s: auth_repo.replace_assigned_tenants(
            s, user_id=manager.user_id, tenant_ids=(), now=T0
        ),
    )
    resp = _read(client_pg, manager, [nid])
    assert resp.status_code == 404 and _reads(db) == 0


def test_platform_role_demoted_after_the_dependency_snapshot_writes_nothing(
    client_pg, db, make_auth_session, make_tenant, monkeypatch
):
    nid = _inquiry(db, make_tenant())
    signup = _signup(db, 1)
    admin = make_auth_session(UserRole.ADMIN, platform_role=PlatformRole.CENTRAL_ADMIN)
    _change_between_router_dependency_and_write_authorization(
        monkeypatch,
        lambda s: auth_repo.set_platform_role(
            s, user_id=admin.user_id, platform_role=PlatformRole.TENANT_MANAGER, now=T0
        ),
    )
    # 명시 목록과 null 모두: 강등된 사용자는 배정이 없어 아무것도 볼 수 없다
    resp = _read(client_pg, admin, [nid, signup])
    assert resp.status_code == 404 and resp.json()["error"]["code"] == "NOTIFICATION_NOT_FOUND"
    assert _reads(db) == 0
    assert client_pg.post(READ, json={}, headers=admin.headers).status_code == 204
    assert _reads(db) == 0


def test_session_revoked_after_the_dependency_snapshot_writes_nothing(
    client_pg, db, platform_session, make_tenant, monkeypatch
):
    nid = _inquiry(db, make_tenant())
    _change_between_router_dependency_and_write_authorization(
        monkeypatch,
        lambda s: sessions.revoke_session(
            s, platform_session.token, now=datetime.now(UTC)
        ),
    )
    for ids in ([nid], None):
        resp = _read(client_pg, platform_session, ids)
        assert resp.status_code == 401
    assert _reads(db) == 0


def test_poll_then_new_arrival_then_null_read_marks_the_arrival_read(
    client_pg, db, platform_session, make_tenant
):
    tenant = make_tenant()
    first = _inquiry(db, tenant, 1)
    polled = _get(client_pg, platform_session)  # 화면이 본 시점
    assert _ids(polled) == [first] and polled["unread_count"] == 1
    arrival = _inquiry(db, tenant, 2)  # 폴링 이후 도착 — 화면은 아직 모른다
    assert client_pg.post(READ, json={"notification_ids": None}, headers=platform_session.headers).status_code == 204
    after = {i["notification_id"]: i["read_at"] for i in _get(client_pg, platform_session)["items"]}
    assert after[arrival] is not None and after[first] is not None
    assert _get(client_pg, platform_session)["unread_count"] == 0
    assert _reads(db) == 2
