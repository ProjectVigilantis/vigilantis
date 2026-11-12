"""GET /api/v1/platform-events (#589 C) — 실제 PostgreSQL.

지키는 것: 응답 모양(행위자 · 대상 · 요청 문맥 · details) · 최신순 · 관문(플랫폼 관리 등급만) ·
범위(중앙 관리자 = 전부, 담당자 = 담당 고객사 + 고객사 없는 본인 행 · Q4) · 범위가 담당 그룹을 따라감 ·
필터(기간 반열린 구간 · enum · 행위자 · 고객사 · 대상) · 형식이 어긋나거나 범위 밖이면 빈 목록 ·
커서 쪽 넘김(빠짐 · 겹침 없음) · 깨진 입력 422 · 미인증 401.

같은 DB 를 다른 테스트도 쓰므로 시각 구간을 이 테스트만의 먼 미래로 좁혀 본다(test_audit_events_api 와 같다).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from db import models
from db.repositories import platform_events as repo
from db.repositories import tenant_groups as groups_repo
from request_origin import RequestOrigin, request_origin_var
from schemas.api.auth import AccountType, PlatformRole, UserRole
from schemas.platform_events import (
    PlatformActorKind,
    PlatformEventAction,
    PlatformEventOutcome,
)

URL = "/api/v1/platform-events"
A = PlatformEventAction
OUT = PlatformEventOutcome
MANAGER = {"platform_role": PlatformRole.TENANT_MANAGER}


def _z(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


@pytest.fixture()
def window():
    """이 테스트만의 시각 구간 시작 — 다른 테스트의 기록과 겹치지 않는 먼 미래의 무작위 시각."""
    return datetime(2032, 1, 1, tzinfo=UTC) + timedelta(minutes=uuid.uuid4().int % 10**6)


@pytest.fixture()
def seed(db, window):
    """기록 1건을 기록 함수로 넣는다(at 은 구간 시작에서 몇 초 뒤) → PlatformEvent."""

    def _seed(action, at=0, *, actor=repo.SYSTEM, outcome=OUT.SUCCEEDED, tenant_id=None,
              target_id=None, details=None):
        return repo.record_platform_event(
            db, action, actor, outcome=outcome, tenant_id=tenant_id, target_id=target_id,
            details=details, occurred_at=window + timedelta(seconds=at),
        )

    return _seed


@pytest.fixture()
def get(client_pg, window):
    """구간 안의 목록 → 응답 JSON. 질의는 그대로 더한다."""

    def _get(**params):
        params = {"since": _z(window), "until": _z(window + timedelta(hours=1)), **params}
        response = client_pg.get(URL, params=params)
        assert response.status_code == 200, response.text
        return response.json()

    return _get


def _person(seed_, *, role=PlatformRole.CENTRAL_ADMIN) -> repo.EventActor:
    return repo.EventActor(
        PlatformActorKind.USER, user_id=seed_.user_id,
        account_type=AccountType.PLATFORM, platform_role=role,
    )


def _ids(body) -> list[str]:
    return [item["event_id"] for item in body["items"]]


def _email(db, user_id):
    return db.get(models.AppUser, user_id).email


# --- 응답 모양 · 순서 -------------------------------------------------------------


def test_central_admin_sees_every_shape_newest_first(db, make_tenant, make_auth_session, seed, get):
    tenant_id = make_tenant()
    operator = make_auth_session()
    session_id = str(uuid.uuid4())
    token = request_origin_var.set(RequestOrigin("req-589c", "198.51.100.7", "<b>agent</b>/1"))
    try:
        suspended = seed(
            A.TENANT_SUSPEND, 2,
            actor=repo.EventActor(
                PlatformActorKind.USER, user_id=operator.user_id, account_type=AccountType.PLATFORM,
                platform_role=PlatformRole.CENTRAL_ADMIN, session_id=session_id,
            ),
            tenant_id=tenant_id, target_id=tenant_id, details={"before": "ACTIVE", "after": "SUSPENDED"},
        )
    finally:
        request_origin_var.reset(token)
    denied = seed(A.AUTH_LOGIN, 1, actor=repo.ANONYMOUS, outcome=OUT.DENIED,
                  target_id=operator.user_id, details={"reason_code": "INVALID_CREDENTIALS"})
    created = seed(A.INCIDENT_CREATE, 0, tenant_id=tenant_id, target_id=str(uuid.uuid4()))

    body = get()

    assert body["next_cursor"] is None
    assert _ids(body) == [suspended.event_id, denied.event_id, created.event_id]
    by_person, by_anonymous, by_system = body["items"]
    assert by_person == {
        "event_id": suspended.event_id,
        "occurred_at": _z(suspended.occurred_at),
        "category": "TENANT_ADMIN",
        "action": "TENANT_SUSPEND",
        "outcome": "SUCCEEDED",
        "actor": {
            "kind": "USER", "user_id": operator.user_id, "email": _email(db, operator.user_id),
            "account_type": "PLATFORM", "platform_role": "CENTRAL_ADMIN",
        },
        "tenant_id": tenant_id,
        "target": {"type": "TENANT", "id": tenant_id},
        "details": {"before": "ACTIVE", "after": "SUSPENDED"},
        "session_id": session_id,
        "request_id": "req-589c",
        "source_ip": "198.51.100.7",
        # 받은 값 그대로다 — 화면이 글자로만 출력한다
        "user_agent": "<b>agent</b>/1",
    }
    assert by_anonymous["actor"] == {
        "kind": "ANONYMOUS", "user_id": None, "email": None, "account_type": None,
        "platform_role": None,
    }
    assert (by_anonymous["outcome"], by_anonymous["details"], by_anonymous["tenant_id"]) == (
        "DENIED", {"reason_code": "INVALID_CREDENTIALS"}, None,
    )
    assert by_anonymous["target"] == {"type": "USER", "id": operator.user_id}
    assert by_system["actor"]["kind"] == "SYSTEM"
    assert (by_system["session_id"], by_system["request_id"], by_system["source_ip"],
            by_system["user_agent"]) == (None, None, None, None)


def test_tenant_actor_carries_no_platform_role(make_tenant, make_auth_session, seed, get):
    tenant_id = make_tenant()
    customer = make_auth_session(UserRole.ADMIN, tenant_id=tenant_id)
    event = seed(
        A.INQUIRY_CREATE,
        actor=repo.EventActor(PlatformActorKind.USER, user_id=customer.user_id,
                              account_type=AccountType.TENANT),
        tenant_id=tenant_id, target_id=str(uuid.uuid4()),
    )

    (item,) = get()["items"]

    assert item["event_id"] == event.event_id
    assert (item["actor"]["account_type"], item["actor"]["platform_role"]) == ("TENANT", None)


# --- 관문 · 범위 ------------------------------------------------------------------


@pytest.mark.parametrize("role", list(UserRole))
def test_tenant_accounts_are_forbidden(client_pg, make_tenant, login_as, role):
    login_as(role, tenant_id=make_tenant())

    response = client_pg.get(URL)

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "AUTH_FORBIDDEN"


def test_unauthenticated_request_is_rejected(client_pg_anon):
    response = client_pg_anon.get(URL)
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "AUTH_REQUIRED"


def test_manager_sees_assigned_tenants_and_only_own_tenantless_rows(
    make_tenant, make_auth_session, login_as, seed, get
):
    """Q4: 고객사 없는 행은 본인 행만 — 자기가 한 행 + 자기 계정이 대상인 인증 행(로그인 시도는 ANONYMOUS)."""
    mine, other = make_tenant(), make_tenant()
    colleague = make_auth_session()
    manager = login_as(UserRole.ADMIN, assigned_tenant_ids=(mine,), **MANAGER)
    me = _person(manager, role=PlatformRole.TENANT_MANAGER)
    in_scope = seed(A.TENANT_SUSPEND, 0, tenant_id=mine, target_id=mine,
                    details={"before": "ACTIVE", "after": "SUSPENDED"})
    seed(A.TENANT_SUSPEND, 1, tenant_id=other, target_id=other,
         details={"before": "ACTIVE", "after": "SUSPENDED"})
    my_logout = seed(A.AUTH_LOGOUT, 2, actor=me)
    my_login = seed(A.AUTH_LOGIN, 3, actor=repo.ANONYMOUS, target_id=manager.user_id)
    my_failed_mfa = seed(A.AUTH_MFA, 4, actor=repo.ANONYMOUS, outcome=OUT.FAILED,
                         target_id=manager.user_id, details={"reason_code": "INVALID_CODE"})
    # 고객사 없는 남의 행 — 동료의 로그아웃 · 동료 계정의 로그인 시도 · 동료가 나를 대상으로 한 등급 변경
    seed(A.AUTH_LOGOUT, 5, actor=_person(colleague))
    seed(A.AUTH_LOGIN, 6, actor=repo.ANONYMOUS, target_id=colleague.user_id)
    seed(A.USER_PLATFORM_ROLE_CHANGE, 7, actor=_person(colleague), target_id=manager.user_id,
         details={"before": "CENTRAL_ADMIN", "after": "TENANT_MANAGER"})
    seed(A.COLLECTION_RUN, 8, target_id=str(uuid.uuid4()))

    body = get()

    assert _ids(body) == [
        my_failed_mfa.event_id, my_login.event_id, my_logout.event_id, in_scope.event_id,
    ]
    # 범위 밖 고객사를 짚어도 404 가 아니라 빈 목록이다
    assert get(tenant_id=other)["items"] == []
    assert get(actor_user_id=colleague.user_id)["items"] == []


def test_unassigned_manager_sees_only_own_rows(make_tenant, login_as, seed, get):
    tenant_id = make_tenant()
    manager = login_as(UserRole.ADMIN, **MANAGER)
    seed(A.TENANT_SUSPEND, 0, tenant_id=tenant_id, target_id=tenant_id,
         details={"before": "ACTIVE", "after": "SUSPENDED"})
    mine = seed(A.AUTH_LOGOUT, 1, actor=_person(manager, role=PlatformRole.TENANT_MANAGER))

    assert _ids(get()) == [mine.event_id]


def test_manager_scope_follows_group_membership(db, make_tenant, login_as, seed, get):
    """범위는 요청마다 담당 그룹으로 다시 정한다 — 그룹에 고객사가 들어오면 다음 요청부터 보인다."""
    assigned, joining = make_tenant(), make_tenant()
    login_as(UserRole.ADMIN, assigned_tenant_ids=(assigned,), **MANAGER)
    event = seed(A.TENANT_SUSPEND, tenant_id=joining, target_id=joining,
                 details={"before": "ACTIVE", "after": "SUSPENDED"})
    assert get(tenant_id=joining)["items"] == []

    group_id = db.get(models.Tenant, assigned).group_id
    assert groups_repo.set_tenant_group(db, joining, group_id, now=datetime.now(UTC))
    db.commit()

    assert _ids(get(tenant_id=joining)) == [event.event_id]


# --- 질의 ------------------------------------------------------------------------


def test_filters_narrow_by_every_field(make_tenant, make_auth_session, seed, get):
    tenant_id, other = make_tenant(), make_tenant()
    operator = make_auth_session()
    by_operator = seed(A.TENANT_SUSPEND, 0, actor=_person(operator), tenant_id=tenant_id,
                       target_id=tenant_id, details={"before": "ACTIVE", "after": "SUSPENDED"})
    by_system = seed(A.TENANT_SUSPEND, 1, tenant_id=other, target_id=other,
                     details={"before": "ACTIVE", "after": "SUSPENDED"})
    denied = seed(A.AUTH_LOGIN, 2, actor=repo.ANONYMOUS, outcome=OUT.DENIED,
                  target_id=operator.user_id, details={"reason_code": "INVALID_CREDENTIALS"})

    assert _ids(get(category="AUTH")) == [denied.event_id]
    assert _ids(get(action="TENANT_SUSPEND")) == [by_system.event_id, by_operator.event_id]
    assert _ids(get(outcome="DENIED")) == [denied.event_id]
    assert _ids(get(actor_kind="SYSTEM")) == [by_system.event_id]
    assert _ids(get(actor_user_id=operator.user_id)) == [by_operator.event_id]
    assert _ids(get(tenant_id=other)) == [by_system.event_id]
    assert _ids(get(target_type="USER")) == [denied.event_id]
    assert _ids(get(target_type="TENANT", target_id=tenant_id)) == [by_operator.event_id]
    # 함께 주면 모두 맞는 행만
    assert get(action="TENANT_SUSPEND", actor_kind="ANONYMOUS")["items"] == []


def test_user_permission_and_tenant_group_events_round_trip_with_details(
    make_tenant, make_auth_session, seed, get
):
    """#617 신규 행동 4종(기록 5건) — 저장 · action/target 필터 · 응답 검증을 지나도 details 와 응답 category 가 그대로다."""
    tenant_id = make_tenant()
    operator = make_auth_session()
    me = _person(operator)
    group_id, other_group_id = str(uuid.uuid4()), str(uuid.uuid4())
    target_user_id = str(uuid.uuid4())
    permissions = seed(
        A.USER_PERMISSIONS_CHANGE, 0, actor=me, target_id=target_user_id,
        details={"before": ["VIEW", "CREATE_INQUIRY"], "after": []},
    )
    created = seed(A.TENANT_GROUP_CREATE, 1, actor=me, target_id=group_id)
    changed = seed(
        A.TENANT_GROUP_ASSIGNMENT_CHANGE, 2, actor=me, tenant_id=tenant_id, target_id=tenant_id,
        details={"before": None, "after": group_id},
    )
    cleared = seed(
        A.TENANT_GROUP_ASSIGNMENT_CHANGE, 3, actor=me, tenant_id=tenant_id, target_id=tenant_id,
        details={"before": group_id, "after": None},
    )
    deleted = seed(
        A.TENANT_GROUP_DELETE, 4, actor=me, target_id=other_group_id,
        details={"tenant_ids": [tenant_id], "staff_user_ids": [operator.user_id, target_user_id]},
    )

    body = get(actor_user_id=operator.user_id)

    assert _ids(body) == [
        deleted.event_id, cleared.event_id, changed.event_id, created.event_id, permissions.event_id,
    ]
    by_id = {item["event_id"]: item for item in body["items"]}
    assert by_id[permissions.event_id]["details"] == {"before": ["VIEW", "CREATE_INQUIRY"], "after": []}
    assert by_id[created.event_id]["details"] == {}
    assert by_id[changed.event_id]["details"] == {"before": None, "after": group_id}
    assert by_id[cleared.event_id]["details"] == {"before": group_id, "after": None}
    assert by_id[deleted.event_id]["details"] == {
        "tenant_ids": [tenant_id], "staff_user_ids": [operator.user_id, target_user_id],
    }
    assert {key: (item["category"], item["target"]) for key, item in by_id.items()} == {
        permissions.event_id: ("USER_ADMIN", {"type": "USER", "id": target_user_id}),
        created.event_id: ("TENANT_ADMIN", {"type": "TENANT_GROUP", "id": group_id}),
        changed.event_id: ("TENANT_ADMIN", {"type": "TENANT", "id": tenant_id}),
        cleared.event_id: ("TENANT_ADMIN", {"type": "TENANT", "id": tenant_id}),
        deleted.event_id: ("TENANT_ADMIN", {"type": "TENANT_GROUP", "id": other_group_id}),
    }
    assert _ids(get(action="USER_PERMISSIONS_CHANGE")) == [permissions.event_id]
    assert _ids(get(action="TENANT_GROUP_CREATE")) == [created.event_id]
    assert _ids(get(action="TENANT_GROUP_ASSIGNMENT_CHANGE")) == [cleared.event_id, changed.event_id]
    assert _ids(get(action="TENANT_GROUP_DELETE")) == [deleted.event_id]
    assert _ids(get(target_type="TENANT_GROUP")) == [deleted.event_id, created.event_id]
    assert _ids(get(target_type="TENANT_GROUP", target_id=group_id)) == [created.event_id]


def test_id_filters_accept_any_uuid_spelling_and_ignore_garbage(make_tenant, seed, get):
    tenant_id = make_tenant()
    event = seed(A.TENANT_SUSPEND, tenant_id=tenant_id, target_id=tenant_id,
                 details={"before": "ACTIVE", "after": "SUSPENDED"})

    assert _ids(get(tenant_id=tenant_id.upper().replace("-", ""))) == [event.event_id]
    empty = {"items": [], "next_cursor": None}
    assert get(tenant_id="not-a-uuid") == empty
    assert get(actor_user_id="not-a-uuid") == empty
    assert get(tenant_id=str(uuid.uuid4())) == empty


def test_since_until_is_half_open(client_pg, seed, window):
    at = [seed(A.COLLECTION_RUN, n, target_id=str(uuid.uuid4())) for n in range(3)]

    response = client_pg.get(URL, params={
        "since": _z(window + timedelta(seconds=1)), "until": _z(window + timedelta(seconds=2)),
    })

    assert response.status_code == 200, response.text
    assert _ids(response.json()) == [at[1].event_id]


def test_pages_cover_every_event_once_with_ties_broken_by_event_id(seed, get):
    rows = [seed(A.COLLECTION_RUN, n // 2, target_id=str(uuid.uuid4())) for n in range(5)]
    expected = [
        row.event_id
        for row in sorted(rows, key=lambda row: (row.occurred_at, row.event_id), reverse=True)
    ]

    seen, cursor, pages = [], None, 0
    while True:
        params = {"limit": 2} if cursor is None else {"limit": 2, "cursor": cursor}
        body = get(**params)
        seen += _ids(body)
        pages += 1
        cursor = body["next_cursor"]
        # 커서가 먹지 않으면 같은 쪽이 되풀이된다 — 무한히 돌지 않고 실패하게 한다
        if cursor is None or pages > len(rows):
            break

    assert seen == expected
    assert pages == 3


def test_exact_page_has_no_next_cursor(seed, get):
    for n in range(2):
        seed(A.COLLECTION_RUN, n, target_id=str(uuid.uuid4()))

    body = get(limit=2)

    assert (len(body["items"]), body["next_cursor"]) == (2, None)


@pytest.mark.parametrize(
    "params",
    [
        {"cursor": "!!!"},
        {"cursor": "WzFd"},  # base64("[1]")
        {"cursor": ""},
        {"limit": 0},
        {"limit": 101},
        {"since": "2032-01-01T00:00:00"},  # 시간대 없음
        {"until": "yesterday"},
        {"category": "NOPE"},
        {"action": "NOPE"},
        {"outcome": "NOPE"},
        {"actor_kind": "NOPE"},
        {"target_type": "NOPE"},
        {"target_id": "x"},  # 대상 종류 없이
        {"target_type": "USER", "target_id": ""},
        {"target_type": "USER", "target_id": "x" * 129},
    ],
    ids=["garbage-cursor", "wrong-shape-cursor", "empty-cursor", "limit-0", "limit-101",
         "naive-since", "bad-until", "bad-category", "bad-action", "bad-outcome",
         "bad-actor-kind", "bad-target-type", "target-id-alone", "empty-target-id",
         "long-target-id"],
)
def test_broken_query_is_422(client_pg, params):
    response = client_pg.get(URL, params=params)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "REQUEST_VALIDATION_FAILED"
