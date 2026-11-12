"""문의 접수 · 전이 · 메모의 플랫폼 활동 로그(platform_events · #589 B1).

지키는 것: 실제로 쓴 경로에서만 한 행이 남는다(행동 · 결과 · 행위자 · 세션 · 문의의 테넌트 · 문의 ID ·
details) · 상태 전이 details 는 before/after 뿐이고 접수 · 메모는 비어 있다 · 문의 제목 · 본문 · 메모 ·
이메일은 행에 들어가지 않는다 · 409 · 404 · 403 · 422 는 행을 남기지 않는다 · 로그를 못 쓰면 문의 쓰기와
이력 · 알림이 함께 되돌아가고 기존 행은 그대로다.
"""

from __future__ import annotations

from datetime import UTC, datetime

import inquiries_service
import pytest
from db.repositories import inquiries as inquiries_repo
from db.repositories.platform_events import EventActor, record_platform_event
from schemas.api.auth import AccountType, PlatformRole, UserRole
from schemas.api.support import InquiryCategory, InquiryEventAction, InquiryStatus
from schemas.platform_events import PlatformActorKind, PlatformEventAction
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

BASE = "/api/v1/inquiries"
OPEN, PROGRESS, RESOLVED = InquiryStatus.OPEN, InquiryStatus.IN_PROGRESS, InquiryStatus.RESOLVED
TITLE, BODY, NOTE = "비밀-제목-7731", "비밀-본문-7731", "비밀-메모-7731"

_COLUMNS = (
    "action, outcome::text, actor_kind::text, actor_user_id::text, actor_account_type::text, "
    "actor_platform_role::text, session_id::text, tenant_id::text, target_type::text, target_id, details"
)


def _rows(db, inquiry_id: str):
    return db.execute(
        text(f"SELECT {_COLUMNS} FROM platform_events WHERE target_id = :i ORDER BY occurred_at, event_id"),
        {"i": inquiry_id},
    ).all()


def _all_event_ids(db) -> list[str]:
    return [r[0] for r in db.execute(text("SELECT event_id::text FROM platform_events ORDER BY event_id"))]


def _counts(db) -> tuple[int, int, int, int]:
    return tuple(
        db.execute(text(f"SELECT count(*) FROM {t}")).scalar_one()
        for t in ("inquiries", "inquiry_events", "notifications", "platform_events")
    )


def _stored_status(db, inquiry_id: str) -> str:
    return db.execute(
        text("SELECT status::text FROM inquiries WHERE inquiry_id = :i"), {"i": inquiry_id}
    ).scalar_one()


@pytest.fixture()
def tenant(make_tenant) -> str:
    return make_tenant()


@pytest.fixture()
def customer(login_as, tenant):
    return login_as(UserRole.OPERATOR, tenant_id=tenant)


@pytest.fixture()
def central(make_auth_session):
    return make_auth_session(UserRole.ADMIN, platform_role=PlatformRole.CENTRAL_ADMIN)


def _seed(db, make_auth_session, tenant_id: str, status: InquiryStatus = OPEN):
    """API 와 활동 로그를 거치지 않고 문의 + 생성 이력을 commit 한다 → inquiry_id."""
    author = make_auth_session(UserRole.OPERATOR, tenant_id=tenant_id)
    now = datetime.now(UTC)
    row = inquiries_repo.create_inquiry(
        db, tenant_id=tenant_id, author_user_id=author.user_id, category=InquiryCategory.OTHER,
        title="시드", body="본문", occurred_at=None, page_path=None, now=now,
    )
    inquiries_repo.add_event(
        db, inquiry_id=row.inquiry_id, actor_user_id=author.user_id,
        action=InquiryEventAction.INQUIRY_CREATED, before_status=None, after_status=OPEN, note=None, now=now,
    )
    if status is not OPEN:
        assert inquiries_repo.transition_status(db, row.inquiry_id, from_status=OPEN, to_status=status, now=now)
    db.commit()
    return row.inquiry_id


def _seed_old_platform_row(db, central) -> None:
    """기존 기록 — 실패한 요청의 rollback 이 이 행을 건드리지 않아야 한다."""
    record_platform_event(
        db,
        PlatformEventAction.INQUIRY_NOTE_ADD,
        EventActor(PlatformActorKind.USER, central.user_id, AccountType.PLATFORM, PlatformRole.CENTRAL_ADMIN),
        target_id="00000000-0000-4000-8000-000000000001",
    )
    db.commit()


# --- 접수 ----------------------------------------------------------------------------


def test_create_writes_one_row_with_the_authorized_actor_and_own_tenant(client_pg, db, tenant, customer):
    before = _all_event_ids(db)

    response = client_pg.post(BASE, json={"category": "BUG", "title": TITLE, "body": BODY})

    assert response.status_code == 201, response.text
    inquiry_id = response.json()["inquiry_id"]
    assert _rows(db, inquiry_id) == [
        (
            "INQUIRY_CREATE", "SUCCEEDED", "USER", customer.user_id, "TENANT", None,
            customer.session_id, tenant, "INQUIRY", inquiry_id, {},
        )
    ]
    assert len(_all_event_ids(db)) == len(before) + 1


# --- 상태 전이 ---------------------------------------------------------------------------


@pytest.mark.parametrize("start, target", [(OPEN, PROGRESS), (OPEN, RESOLVED), (PROGRESS, RESOLVED)])
def test_status_change_writes_the_transition_for_the_inquiry_tenant_not_the_actor(
    client_pg, db, tenant, make_auth_session, central, start, target
):
    inquiry_id = _seed(db, make_auth_session, tenant, start)

    response = client_pg.put(
        f"{BASE}/{inquiry_id.upper()}/status", json={"status": target.value, "note": NOTE}, headers=central.headers
    )

    assert response.status_code == 200, response.text
    assert _rows(db, inquiry_id) == [
        (
            "INQUIRY_STATUS_CHANGE", "SUCCEEDED", "USER", central.user_id, "PLATFORM", "CENTRAL_ADMIN",
            central.session_id, tenant, "INQUIRY", inquiry_id, {"before": start.value, "after": target.value},
        )
    ]


@pytest.mark.parametrize(
    ("start", "target"),
    [(OPEN, OPEN), (PROGRESS, OPEN), (RESOLVED, RESOLVED), (RESOLVED, PROGRESS)],
)
def test_conflicting_or_noop_transition_writes_no_row(client_pg, db, tenant, make_auth_session, central, start, target):
    inquiry_id = _seed(db, make_auth_session, tenant, start)
    before = _counts(db)

    response = client_pg.put(f"{BASE}/{inquiry_id}/status", json={"status": target.value}, headers=central.headers)

    assert response.status_code == 409
    assert _counts(db) == before and _rows(db, inquiry_id) == []
    assert _stored_status(db, inquiry_id) == start.value


def test_same_status_transition_adds_no_second_row(client_pg, db, tenant, make_auth_session, central):
    inquiry_id = _seed(db, make_auth_session, tenant)
    url = f"{BASE}/{inquiry_id}/status"
    assert client_pg.put(url, json={"status": "IN_PROGRESS"}, headers=central.headers).status_code == 200
    before = _counts(db)

    assert client_pg.put(url, json={"status": "IN_PROGRESS"}, headers=central.headers).status_code == 409

    assert _counts(db) == before and len(_rows(db, inquiry_id)) == 1


# --- 메모 ----------------------------------------------------------------------------------


@pytest.mark.parametrize("status", [OPEN, PROGRESS, RESOLVED])
def test_note_writes_one_row_with_empty_details(client_pg, db, tenant, make_auth_session, central, status):
    inquiry_id = _seed(db, make_auth_session, tenant, status)

    response = client_pg.post(f"{BASE}/{inquiry_id}/notes", json={"note": NOTE}, headers=central.headers)

    assert response.status_code == 201, response.text
    assert _rows(db, inquiry_id) == [
        (
            "INQUIRY_NOTE_ADD", "SUCCEEDED", "USER", central.user_id, "PLATFORM", "CENTRAL_ADMIN",
            central.session_id, tenant, "INQUIRY", inquiry_id, {},
        )
    ]


# --- 문의 · 메모 · 이메일은 싣지 않는다 ----------------------------------------------------------


def test_no_inquiry_text_note_or_email_reaches_the_platform_rows(client_pg, db, tenant, customer, central):
    inquiry_id = client_pg.post(BASE, json={"category": "BUG", "title": TITLE, "body": BODY}).json()["inquiry_id"]
    assert client_pg.put(
        f"{BASE}/{inquiry_id}/status", json={"status": "IN_PROGRESS", "note": NOTE}, headers=central.headers
    ).status_code == 200
    assert client_pg.post(f"{BASE}/{inquiry_id}/notes", json={"note": NOTE}, headers=central.headers).status_code == 201

    dumped = "\n".join(
        r[0] for r in db.execute(
            text("SELECT row_to_json(p)::text FROM platform_events p WHERE target_id = :i"), {"i": inquiry_id}
        )
    )
    emails = [
        db.execute(text("SELECT email FROM app_users WHERE user_id = :u"), {"u": u}).scalar_one()
        for u in (customer.user_id, central.user_id)
    ]
    assert dumped.count("INQUIRY_") == 3
    for secret in (TITLE, BODY, NOTE, *emails):
        assert secret not in dumped


# --- 쓰지 않은 요청 ------------------------------------------------------------------------------


def test_rejected_requests_write_no_inquiry_row(client_pg, db, make_tenant, make_auth_session, tenant, customer):
    foreign = _seed(db, make_auth_session, make_tenant())
    mine = _seed(db, make_auth_session, tenant)
    scoped = make_auth_session(
        UserRole.ADMIN, platform_role=PlatformRole.TENANT_MANAGER, assigned_tenant_ids=(tenant,)
    )
    before = _counts(db)

    assert client_pg.put(f"{BASE}/{foreign}/status", json={"status": "RESOLVED"}, headers=scoped.headers).status_code == 404
    assert client_pg.post(f"{BASE}/{foreign}/notes", json={"note": NOTE}, headers=scoped.headers).status_code == 404
    assert client_pg.put(f"{BASE}/{mine}/status", json={"status": "DONE"}, headers=scoped.headers).status_code == 422
    assert client_pg.post(f"{BASE}/{mine}/notes", json={"note": " "}, headers=scoped.headers).status_code == 422
    assert client_pg.post(f"{BASE}/{mine}/notes", json={"note": NOTE}, headers=customer.headers).status_code == 403
    assert client_pg.post(BASE, json={"category": "BUG", "title": " ", "body": BODY}).status_code == 422

    # 문의 · 문의 기록 · 알림은 그대로다. 활동 로그에는 403 한 건의 권한 거절(ACCESS_FORBIDDEN · #589 B)만 는다
    inquiries, inquiry_events, notifications, platform_rows = _counts(db)
    assert (inquiries, inquiry_events, notifications) == before[:3]
    assert platform_rows == before[3] + 1
    denied = db.execute(
        text(
            "SELECT action, details->>'path' FROM platform_events"
            " WHERE category = 'ACCESS' AND actor_user_id::text = :u"
        ),
        {"u": customer.user_id},
    ).all()
    assert denied == [("ACCESS_FORBIDDEN", f"{BASE}/{mine}/notes")]


# --- 원자성 --------------------------------------------------------------------------------------


def _fail_after_write(monkeypatch) -> list:
    """실제 기록 함수가 행을 쓰고 flush 한 뒤 실패시킨다 — 이미 쓴 행이 요청과 함께 사라지는지 본다."""
    real = inquiries_service.record_platform_event
    written: list = []

    def record_then_fail(db, *args, **kwargs):
        written.append(real(db, *args, **kwargs))
        raise SQLAlchemyError("SECRET-DB-DETAIL")

    monkeypatch.setattr(inquiries_service, "record_platform_event", record_then_fail)
    return written


def test_platform_event_failure_rolls_back_the_inquiry_event_and_notification(
    client_pg, db, customer, central, monkeypatch
):
    _seed_old_platform_row(db, central)
    before_ids, before = _all_event_ids(db), _counts(db)
    written = _fail_after_write(monkeypatch)

    response = client_pg.post(BASE, json={"category": "BUG", "title": TITLE, "body": BODY})

    assert len(written) == 1  # 행은 실제로 flush 됐다가 되돌아간다
    assert response.status_code == 500 and "SECRET" not in response.text
    assert _counts(db) == before and _all_event_ids(db) == before_ids


def test_platform_event_failure_rolls_back_the_status_change_and_its_history(
    client_pg, db, tenant, make_auth_session, central, monkeypatch
):
    inquiry_id = _seed(db, make_auth_session, tenant)
    _seed_old_platform_row(db, central)
    before_ids, before = _all_event_ids(db), _counts(db)
    written = _fail_after_write(monkeypatch)

    response = client_pg.put(
        f"{BASE}/{inquiry_id}/status", json={"status": "RESOLVED", "note": NOTE}, headers=central.headers
    )

    assert len(written) == 1  # 행은 실제로 flush 됐다가 되돌아간다
    assert response.status_code == 500 and "SECRET" not in response.text
    assert _stored_status(db, inquiry_id) == "OPEN"
    assert _counts(db) == before and _all_event_ids(db) == before_ids


def test_platform_event_failure_rolls_back_the_note_history(
    client_pg, db, tenant, make_auth_session, central, monkeypatch
):
    inquiry_id = _seed(db, make_auth_session, tenant)
    _seed_old_platform_row(db, central)
    before_ids, before = _all_event_ids(db), _counts(db)
    written = _fail_after_write(monkeypatch)

    response = client_pg.post(f"{BASE}/{inquiry_id}/notes", json={"note": NOTE}, headers=central.headers)

    assert len(written) == 1  # 행은 실제로 flush 됐다가 되돌아간다
    assert response.status_code == 500 and "SECRET" not in response.text
    assert _counts(db) == before and _all_event_ids(db) == before_ids
