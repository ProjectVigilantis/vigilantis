"""문의 전이 · 이력 추가의 동시성 (#552). 격리 DB · 실제 서비스 · 독립 commit.

inquiry_events 는 append-only 라 commit 데이터를 지울 수 없다 — 이 파일 전용 DB 를 만들고 정확히 그 DB 만 지운다.
"""

from __future__ import annotations

import threading
import uuid
from datetime import UTC, datetime

import inquiries_service
import pytest
from auth import sessions
from db import models
from db.repositories import auth as auth_repo
from exceptions import ApiError
from schemas.api.auth import AccountType, PlatformRole, UserRole
from schemas.api.errors import ErrorCode
from schemas.api.inquiries import (
    InquiryCreateRequest,
    InquiryNoteRequest,
    InquiryStatusRequest,
)
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker
from support_access import Actor

ISSUER = "https://cognito-idp.ap-northeast-2.amazonaws.com/ap-northeast-2_test"  # conftest TEST_AUTH_ISSUER


@pytest.fixture()
def factory(pg_engine):
    import os
    from pathlib import Path

    from alembic import command
    from alembic.config import Config
    from config import get_settings
    from sqlalchemy import create_engine

    core_api = Path(__file__).resolve().parents[1]
    admin_url = pg_engine.url.set(database="postgres").render_as_string(hide_password=False)
    name = f"vigilantis_test_{uuid.uuid4().hex}"
    admin = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    previous = os.environ.get("DATABASE_URL")
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    engine = None
    try:
        url = admin_url.rsplit("/", 1)[0] + "/" + name
        os.environ["DATABASE_URL"] = url
        get_settings.cache_clear()
        command.upgrade(Config(str(core_api / "alembic.ini")), "head")
        engine = create_engine(url)
        yield sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    finally:
        if engine is not None:
            engine.dispose()
        if previous is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous
        get_settings.cache_clear()
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


def seed_user(db, n, *, tenant_id=None, role=UserRole.ADMIN):
    now = datetime.now(UTC)
    platform = tenant_id is None
    user = auth_repo.create_user(
        db, issuer=ISSUER, cognito_sub=f"inq-race-{n}", email=f"inq{n}@example.com", role=role,
        account_type=AccountType.PLATFORM if platform else AccountType.TENANT,
        platform_role=PlatformRole.CENTRAL_ADMIN if platform else None,
        tenant_id=tenant_id, now=now, email_verified_at=now,
    )
    issued = sessions.issue_session(
        db, user_id=user.user_id, mfa_step=models.AuthSessionMfaStep.SOFTWARE_TOKEN_MFA,
        now=now, max_seconds=3600, idle_seconds=1800,
    )
    return user.user_id, issued.token


def actor(token, clock=lambda: datetime.now(UTC)):
    return Actor(token=token, idle_seconds=1800, clock=clock)


@pytest.fixture()
def world(factory, seed_tenant):
    """테넌트 1개 · 고객 1명 · 직원 3명 · 접수된 문의 1건."""
    with factory() as db:
        tenant_id = seed_tenant(db)
        _, customer_token = seed_user(db, 0, tenant_id=tenant_id)
        staff_tokens = [seed_user(db, n)[1] for n in (1, 2, 3)]
        db.commit()
    with factory() as db:
        created = inquiries_service.create_inquiry(
            db, actor(customer_token),
            InquiryCreateRequest(category="BUG", title="동시성", body="본문"),
        )
    return created.inquiry_id, staff_tokens


def race(factory, calls):
    """calls[i] = fn(db) 를 장벽으로 동시에 출발시켜 결과(예외 포함)를 돌려준다."""
    barrier = threading.Barrier(len(calls))
    results: list = [None] * len(calls)

    def run(index):
        with factory() as db:
            barrier.wait(10)
            try:
                results[index] = calls[index](db)
            except BaseException as exc:  # noqa: BLE001 — 결과 비교용
                results[index] = exc

    threads = [threading.Thread(target=run, args=(i,)) for i in range(len(calls))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
        assert not t.is_alive()
    return results


def events(factory, inquiry_id):
    with factory() as db:
        return db.execute(
            text(
                "SELECT action::text, before_status::text, after_status::text, occurred_at "
                "FROM inquiry_events WHERE inquiry_id = :i ORDER BY occurred_at, event_id"
            ),
            {"i": inquiry_id},
        ).all()


@pytest.mark.parametrize("target", ["IN_PROGRESS", "RESOLVED"])
def test_same_transition_race_has_one_winner_and_no_duplicate_event(factory, world, target):
    inquiry_id, tokens = world
    body = InquiryStatusRequest(status=target)
    results = race(
        factory,
        [lambda db, t=t: inquiries_service.change_status(db, actor(t), inquiry_id, body) for t in tokens],
    )

    winners = [r for r in results if not isinstance(r, BaseException)]
    losers = [r for r in results if isinstance(r, BaseException)]
    assert len(winners) == 1, results
    assert all(isinstance(e, ApiError) and e.code is ErrorCode.INQUIRY_STATE_CONFLICT for e in losers), losers
    rows = events(factory, inquiry_id)
    assert [r[:3] for r in rows] == [
        ("INQUIRY_CREATED", None, "OPEN"),
        ("STATUS_CHANGED", "OPEN", target),
    ]


def test_conflicting_forward_transitions_serialize_into_a_coherent_history(factory, world):
    inquiry_id, tokens = world
    results = race(
        factory,
        [
            lambda db: inquiries_service.change_status(db, actor(tokens[0]), inquiry_id, InquiryStatusRequest(status="RESOLVED")),
            lambda db: inquiries_service.change_status(db, actor(tokens[1]), inquiry_id, InquiryStatusRequest(status="IN_PROGRESS")),
        ],
    )
    # 순서에 따라 (RESOLVED) 단독이거나 (IN_PROGRESS → RESOLVED) 이고, 되돌리는 이력은 어느 쪽도 없다
    chain = [(r[1], r[2]) for r in events(factory, inquiry_id)[1:]]
    with factory() as db:
        final = db.execute(text("SELECT status::text FROM inquiries WHERE inquiry_id = :i"), {"i": inquiry_id}).scalar_one()
    assert final == "RESOLVED"
    assert chain in (
        [("OPEN", "RESOLVED")],
        [("OPEN", "IN_PROGRESS"), ("IN_PROGRESS", "RESOLVED")],
    )
    # RESOLVED 가 먼저면 뒤의 IN_PROGRESS 는 409 로 거절되고 이력이 남지 않는다
    assert sum(not isinstance(r, BaseException) for r in results) == len(chain)


def test_concurrent_notes_all_persist_in_a_strict_order_even_with_a_frozen_clock(factory, world):
    inquiry_id, tokens = world
    frozen = datetime.now(UTC)  # 세션 만료 안의 한 시각 — 모든 호출이 같은 시각을 읽는다
    notes = [f"메모 {n}" for n in range(6)]
    calls = [
        lambda db, n=n, t=tokens[i % 3]: inquiries_service.add_note(
            db, actor(t, clock=lambda: frozen), inquiry_id, InquiryNoteRequest(note=n)
        )
        for i, n in enumerate(notes)
    ]
    results = race(factory, calls)

    assert not [r for r in results if isinstance(r, BaseException)], results
    with factory() as db:
        rows = db.execute(
            text("SELECT note, occurred_at FROM inquiry_events WHERE inquiry_id = :i AND action = 'NOTE_ADDED' "
                 "ORDER BY occurred_at, event_id"),
            {"i": inquiry_id},
        ).all()
    assert sorted(r[0] for r in rows) == sorted(notes)
    stamps = [r[1] for r in rows]
    assert len(set(stamps)) == len(stamps)  # 같은 시각 충돌 없이 엄격히 증가
