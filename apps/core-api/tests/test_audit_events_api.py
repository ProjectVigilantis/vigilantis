"""GET /api/v1/audit-events (T-2 B · #544) — 실제 PostgreSQL.

지키는 것: 두 종류의 응답 모양 · 최신순 · 범위(고객사 = 자기 테넌트, 플랫폼 = 전부) · 행위자 이메일
(PLATFORM_USER 는 플랫폼 계정에게만 · 고객사 계정에는 늘 null · SYSTEM 은 늘 null) · incident_id ·
since/until 반열린 구간 · 커서 쪽 넘김(빠짐 · 겹침 없음) · 깨진 입력 422 · 미인증 401 · 고객사 모든 역할 조회.

같은 DB 를 다른 테스트도 쓰므로, 플랫폼 계정이 보는 「전부」는 이 테스트만의 시각 구간으로 좁혀 본다.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from db import models
from schemas.api.audit import AuditActorKind, AuditEventAction
from schemas.api.auth import UserRole
from schemas.api.incidents import ResolutionJudgement
from schemas.api.tenants import TenantStatus
from schemas.runbooks import RunbookId, TriggerSource

URL = "/api/v1/audit-events"
STARTED = AuditEventAction.ACTION_EXECUTION_STARTED
RESOLVED = AuditEventAction.INCIDENT_RESOLVED
PLATFORM, TENANT, SYSTEM = (
    AuditActorKind.PLATFORM_USER, AuditActorKind.TENANT_USER, AuditActorKind.SYSTEM,
)


def _z(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


@pytest.fixture()
def window():
    """이 테스트만의 시각 구간 시작 — 다른 테스트의 기록과 겹치지 않는 먼 미래의 무작위 시각."""
    return datetime(2031, 1, 1, tzinfo=UTC) + timedelta(minutes=uuid.uuid4().int % 10**6)


@pytest.fixture()
def seed(db, make_incident, make_execution):
    """기록 1건을 직접 넣는다(쓰기 경로는 test_audit_events 가 본다) → AuditEvent."""

    def _seed(incident, at, *, kind=SYSTEM, user_id=None, trigger=TriggerSource.PRE_MITIGATION_0_5S,
              resolution=None):
        values = {
            "occurred_at": at, "actor_kind": kind, "actor_user_id": user_id,
            "incident_id": incident.incident_id, "tenant_id": incident.tenant_id,
        }
        if resolution is None:
            execution = make_execution(
                db, incident, runbook_id=RunbookId.RUNBOOK_EC2_ISOLATE, trigger_source=trigger,
            )
            values |= {"action": STARTED, "execution_id": execution.execution_id,
                       "trigger_source": trigger}
        else:
            values |= {"action": RESOLVED, "resolution": resolution}
        row = models.AuditEvent(**values)
        db.add(row)
        db.flush()
        return row

    return _seed


def _incident(db, make_incident, tenant_id=None):
    incident = make_incident(db)
    incident.tenant_id = tenant_id
    db.flush()
    return incident


def _email(db, user_id):
    return db.get(models.AppUser, user_id).email


def _get(client, **params):
    response = client.get(URL, params=params)
    assert response.status_code == 200, response.text
    return response.json()


# --- 응답 모양 · 순서 -------------------------------------------------------------


def test_platform_viewer_sees_both_kinds_newest_first(
    client_pg, db, make_incident, make_tenant, make_auth_session, seed, window
):
    tenant_id = make_tenant()
    operator = make_auth_session()  # 플랫폼 계정은 ADMIN 뿐이다
    customer = make_auth_session(UserRole.OPERATOR, tenant_id=tenant_id)
    incident = _incident(db, make_incident, tenant_id)
    auto = seed(incident, window)
    approved = seed(incident, window + timedelta(seconds=1), kind=TENANT,
                    user_id=customer.user_id, trigger=TriggerSource.USER_APPROVAL)
    closed = seed(incident, window + timedelta(seconds=2), kind=PLATFORM,
                  user_id=operator.user_id, resolution=ResolutionJudgement.JUSTIFIED)

    body = _get(client_pg, since=_z(window), until=_z(window + timedelta(hours=1)))

    assert body["next_cursor"] is None
    assert [item["event_id"] for item in body["items"]] == [
        closed.event_id, approved.event_id, auto.event_id,
    ]
    resolved, started, system = body["items"]
    assert resolved == {
        "action": "INCIDENT_RESOLVED", "event_id": closed.event_id,
        "occurred_at": _z(closed.occurred_at),
        "actor": {"kind": "PLATFORM_USER", "email": _email(db, operator.user_id)},
        "incident_id": incident.incident_id, "tenant_id": tenant_id, "resolution": "JUSTIFIED",
    }
    assert started["actor"] == {"kind": "TENANT_USER", "email": _email(db, customer.user_id)}
    assert (started["trigger_source"], started["execution_id"]) == (
        "USER_APPROVAL", approved.execution_id,
    )
    assert (started["runbook_id"], started["target_arn"]) == (
        "RUNBOOK_EC2_ISOLATE", incident.subject_arn,
    )
    assert system["actor"] == {"kind": "SYSTEM", "email": None}
    assert system["trigger_source"] == "PRE_MITIGATION_0_5S"


# --- 범위 · 이메일 ---------------------------------------------------------------


@pytest.mark.parametrize("role", list(UserRole))
def test_tenant_viewer_sees_only_its_tenant_and_never_an_operator_email(
    client_pg, db, make_incident, make_tenant, make_auth_session, login_as, seed, window, role
):
    """고객사 계정에는 PLATFORM_USER 이메일이 늘 null 이다(#546 리뷰 포인트 2) — 모든 역할에서."""
    mine, other = make_tenant(), make_tenant()
    operator = make_auth_session()  # 플랫폼 계정은 ADMIN 뿐이다
    colleague = make_auth_session(UserRole.OPERATOR, tenant_id=mine)
    stranger = make_auth_session(UserRole.OPERATOR, tenant_id=other)
    own = _incident(db, make_incident, mine)
    foreign = _incident(db, make_incident, other)
    unassigned = _incident(db, make_incident)
    by_operator = seed(own, window, kind=PLATFORM, user_id=operator.user_id,
                       trigger=TriggerSource.USER_APPROVAL)
    closed_by_operator = seed(own, window + timedelta(seconds=1), kind=PLATFORM,
                              user_id=operator.user_id, resolution=ResolutionJudgement.JUSTIFIED)
    by_colleague = seed(own, window + timedelta(seconds=2), kind=TENANT,
                        user_id=colleague.user_id, trigger=TriggerSource.USER_APPROVAL)
    seed(foreign, window + timedelta(seconds=3), kind=TENANT, user_id=stranger.user_id,
         trigger=TriggerSource.USER_APPROVAL)
    seed(unassigned, window + timedelta(seconds=4))
    login_as(role, tenant_id=mine)

    body = _get(client_pg)

    assert [item["event_id"] for item in body["items"]] == [
        by_colleague.event_id, closed_by_operator.event_id, by_operator.event_id,
    ]
    actors = [item["actor"] for item in body["items"]]
    assert actors == [
        {"kind": "TENANT_USER", "email": _email(db, colleague.user_id)},
        {"kind": "PLATFORM_USER", "email": None},
        {"kind": "PLATFORM_USER", "email": None},
    ]
    assert _email(db, operator.user_id) not in str(body)
    # 범위 밖 인시던트를 짚어도 404 가 아니라 빈 목록이다
    for incident in (foreign, unassigned):
        assert _get(client_pg, incident_id=incident.incident_id)["items"] == []


def test_suspended_tenant_can_still_read(client_pg, db, make_incident, make_tenant, login_as, seed,
                                         window):
    tenant_id = make_tenant(TenantStatus.SUSPENDED)
    event = seed(_incident(db, make_incident, tenant_id), window)
    login_as(UserRole.VIEWER, tenant_id=tenant_id)

    assert [item["event_id"] for item in _get(client_pg)["items"]] == [event.event_id]


def test_unauthenticated_request_is_rejected(client_pg_anon):
    response = client_pg_anon.get(URL)
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "AUTH_REQUIRED"


# --- 질의 ------------------------------------------------------------------------


def test_incident_filter_accepts_any_uuid_spelling(client_pg, db, make_incident, seed, window):
    target = _incident(db, make_incident)
    event = seed(target, window)
    seed(_incident(db, make_incident), window)

    for spelling in (target.incident_id, target.incident_id.upper().replace("-", "")):
        body = _get(client_pg, incident_id=spelling)
        assert [item["event_id"] for item in body["items"]] == [event.event_id]
    assert _get(client_pg, incident_id="not-a-uuid") == {"items": [], "next_cursor": None}
    assert _get(client_pg, incident_id=str(uuid.uuid4())) == {"items": [], "next_cursor": None}


def test_since_until_is_half_open(client_pg, db, make_incident, seed, window):
    incident = _incident(db, make_incident)
    at = [seed(incident, window + timedelta(seconds=n)) for n in range(3)]

    body = _get(
        client_pg, incident_id=incident.incident_id,
        since=_z(window + timedelta(seconds=1)), until=_z(window + timedelta(seconds=2)),
    )

    assert [item["event_id"] for item in body["items"]] == [at[1].event_id]
    # 다른 시간대 표기도 같은 순간이다
    kst = (window + timedelta(seconds=1)).astimezone(
        datetime.now().astimezone().tzinfo
    ).isoformat()
    assert len(_get(client_pg, incident_id=incident.incident_id, since=kst)["items"]) == 2


def test_pages_cover_every_event_once_with_ties_broken_by_event_id(
    client_pg, db, make_incident, seed, window
):
    incident = _incident(db, make_incident)
    rows = [seed(incident, window + timedelta(seconds=n // 2)) for n in range(5)]  # 같은 시각 2건씩
    expected = [
        row.event_id
        for row in sorted(rows, key=lambda row: (row.occurred_at, row.event_id), reverse=True)
    ]

    seen, cursor, pages = [], None, 0
    while True:
        params = {"incident_id": incident.incident_id, "limit": 2}
        if cursor is not None:
            params["cursor"] = cursor
        body = _get(client_pg, **params)
        seen += [item["event_id"] for item in body["items"]]
        pages += 1
        cursor = body["next_cursor"]
        if cursor is None:
            break

    assert seen == expected
    assert pages == 3


def test_exact_page_has_no_next_cursor(client_pg, db, make_incident, seed, window):
    incident = _incident(db, make_incident)
    for n in range(2):
        seed(incident, window + timedelta(seconds=n))

    body = _get(client_pg, incident_id=incident.incident_id, limit=2)

    assert (len(body["items"]), body["next_cursor"]) == (2, None)


@pytest.mark.parametrize(
    "params",
    [
        {"cursor": "!!!"},
        {"cursor": "bm90LWpzb24"},  # base64("not-json")
        {"cursor": "WzFd"},  # base64("[1]")
        {"cursor": "WyIyMDMxLTAxLTAxVDAwOjAwOjAwIiwgIng"},  # 시간대 없는 시각 · 깨진 식별자
        {"cursor": ""},
        {"limit": 0},
        {"limit": 101},
        {"since": "2031-01-01T00:00:00"},  # 시간대 없음
        {"until": "yesterday"},
    ],
    ids=["garbage", "not-json", "wrong-shape", "naive-cursor", "empty-cursor", "limit-0",
         "limit-101", "naive-since", "bad-until"],
)
def test_broken_query_is_422(client_pg, params):
    response = client_pg.get(URL, params=params)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "REQUEST_VALIDATION_FAILED"
