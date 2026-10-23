"""platform_enrollments (#492) — 실제 PostgreSQL.

지키는 것: 단일 head · 왕복(이 테이블 · enum 만 지움, 기존 행 보존) · ORM 일치 · 행 모양 CHECK 5개 ·
token_hash · redeemed_user_id 유일 · app_users FK RESTRICT · 조건부 전이(예약 · 회수) · 같은 링크 동시 예약 1건.
"""

import threading
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from config import get_settings
from db import models
from db.repositories import auth as auth_repo
from db.repositories import enrollment as enrollment_repo
from schemas.api.auth import AccountType, UserRole
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

REVISION = "b492e5d7c3a1"
PARENT = "f492c4a8d2e1"
ISSUER = "https://cognito-idp.ap-northeast-2.amazonaws.com/pool-enroll"
NOW = datetime(2026, 10, 23, 9, 0, tzinfo=UTC)
LATER = NOW + timedelta(hours=72)
S = models.PlatformEnrollmentStatus


@pytest.fixture()
def migration_config(pg_engine, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", pg_engine.url.render_as_string(hide_password=False))
    get_settings.cache_clear()
    yield Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    get_settings.cache_clear()


def _hash() -> str:
    return uuid.uuid4().hex * 2


def _user(db, sub=None) -> str:
    row = auth_repo.create_user(
        db, issuer=ISSUER, cognito_sub=sub or f"sub-{uuid.uuid4().hex}", email="p@example.com",
        role=UserRole.ADMIN, account_type=AccountType.PLATFORM, tenant_id=None, now=NOW,
    )
    return row.user_id


def _insert(db, **cols) -> None:
    """raw INSERT — ORM 을 거치지 않고 DB 제약만 본다."""
    values = {
        "enrollment_id": str(uuid.uuid4()), "token_hash": _hash(), "status": "ISSUED",
        "created_at": NOW, "expires_at": LATER, **cols,
    }
    names = ", ".join(values)
    params = ", ".join(
        "CAST(:status AS platform_enrollment_status)" if k == "status" else f":{k}" for k in values
    )
    db.execute(text(f"INSERT INTO platform_enrollments ({names}) VALUES ({params})"), values)


# ----- 마이그레이션 --------------------------------------------------------------


def test_revision_follows_account_type_on_a_single_chain(migration_config):
    script = ScriptDirectory.from_config(migration_config)
    assert script.get_revision(REVISION).down_revision == PARENT
    assert len(script.get_heads()) == 1


def test_roundtrip_drops_only_enrollment_state_and_matches_orm(pg_engine, migration_config):
    with Session(pg_engine) as session:
        user_id = _user(session, sub="roundtrip-user")
        session.commit()
    before = set(inspect(pg_engine).get_table_names())
    try:
        command.downgrade(migration_config, PARENT)
        with pg_engine.connect() as connection:
            assert set(inspect(connection).get_table_names()) == before - {"platform_enrollments"}
            assert connection.execute(
                text("SELECT count(*) FROM pg_type WHERE typname = 'platform_enrollment_status'")
            ).scalar_one() == 0
            # 이전 리비전의 상태(계정 종류 칸 · 사용자 행)는 그대로다
            assert "account_type" in {c["name"] for c in inspect(connection).get_columns("app_users")}
            assert connection.execute(
                text("SELECT account_type::text FROM app_users WHERE user_id = :u"), {"u": user_id}
            ).scalar_one() == "PLATFORM"
    finally:
        command.upgrade(migration_config, "head")
        with pg_engine.begin() as connection:
            connection.execute(text("DELETE FROM app_users WHERE user_id = :u"), {"u": user_id})
    with pg_engine.connect() as connection:
        assert "platform_enrollments" in inspect(connection).get_table_names()
        assert connection.execute(
            text("SELECT unnest(enum_range(NULL::platform_enrollment_status))::text")
        ).scalars().all() == ["ISSUED", "RESERVED", "REDEEMED", "REVOKED"]
        assert compare_metadata(MigrationContext.configure(connection), models.Base.metadata) == []


# ----- 행 모양 CHECK · 유일 · FK -------------------------------------------------


@pytest.mark.parametrize(
    "shape",
    [
        pytest.param({"expires_at": NOW}, id="expires-not-after-created"),
        pytest.param({"reserved_at": NOW, "email": "a@example.com"}, id="issued-with-reservation"),
        pytest.param({"status": "RESERVED"}, id="reserved-without-reservation"),
        pytest.param({"status": "RESERVED", "reserved_at": NOW}, id="reservation-without-email"),
        pytest.param({"status": "RESERVED", "email": "a@example.com"}, id="email-without-reserved-at"),
        pytest.param(
            {"status": "REDEEMED", "reserved_at": NOW, "email": "a@example.com", "redeemed_at": NOW},
            id="redeemed-without-user",
        ),
        pytest.param({"status": "REVOKED"}, id="revoked-without-time"),
        pytest.param({"revoked_at": NOW}, id="issued-with-revoked-time"),
    ],
)
def test_check_constraints_reject_bad_shapes(db, shape):
    with pytest.raises(IntegrityError), db.begin_nested():
        _insert(db, **shape)


@pytest.mark.parametrize(
    "with_time", [pytest.param(True, id="issued-with-user"), pytest.param(False, id="user-without-time")]
)
def test_redemption_requires_redeemed_status_and_time(db, with_time):
    user_id = _user(db)
    shape = {"reserved_at": NOW, "email": "a@example.com", "redeemed_user_id": user_id}
    if with_time:
        shape |= {"status": "RESERVED", "redeemed_at": NOW}
    else:
        shape |= {"status": "REDEEMED"}
    with pytest.raises(IntegrityError), db.begin_nested():
        _insert(db, **shape)


def test_check_constraints_accept_every_lifecycle_shape(db):
    user_id = _user(db)
    reservation = {"reserved_at": NOW, "email": "a@example.com"}
    _insert(db)
    _insert(db, status="RESERVED", **reservation)
    _insert(db, status="REDEEMED", redeemed_user_id=user_id, redeemed_at=NOW, **reservation)
    _insert(db, status="REVOKED", revoked_at=NOW)
    _insert(db, status="REVOKED", revoked_at=NOW, **reservation)
    assert db.execute(text("SELECT count(*) FROM platform_enrollments")).scalar_one() == 5


def test_token_hash_and_redeemed_user_are_unique_and_user_is_restricted(db):
    user_id = _user(db)
    token_hash = _hash()
    redeemed = {"status": "REDEEMED", "reserved_at": NOW, "email": "a@example.com",
                "redeemed_user_id": user_id, "redeemed_at": NOW}
    _insert(db, token_hash=token_hash)
    with pytest.raises(IntegrityError, match="token_hash"), db.begin_nested():
        _insert(db, token_hash=token_hash)
    _insert(db, **redeemed)
    with pytest.raises(IntegrityError, match="redeemed_user_id"), db.begin_nested():
        _insert(db, **redeemed)  # 한 계정은 링크 하나로만 만들어진다
    with pytest.raises(IntegrityError), db.begin_nested():
        db.execute(text("DELETE FROM app_users WHERE user_id = :u"), {"u": user_id})
    with pytest.raises(IntegrityError), db.begin_nested():
        _insert(db, **{**redeemed, "redeemed_user_id": str(uuid.uuid4())})


# ----- 저장소 전이 ----------------------------------------------------------------


def test_reserve_takes_only_issued_unexpired_links_once(db):
    live = enrollment_repo.add(db, enrollment_id=str(uuid.uuid4()), token_hash=_hash(), now=NOW, expires_at=LATER)
    assert enrollment_repo.is_redeemable(db, token_hash=live.token_hash, now=NOW)

    assert enrollment_repo.reserve(db, token_hash=live.token_hash, email="a@example.com", now=NOW) == live.enrollment_id
    row = enrollment_repo.get(db, live.enrollment_id)
    assert (row.status, row.reserved_at, row.email) == (S.RESERVED, NOW, "a@example.com")
    assert not enrollment_repo.is_redeemable(db, token_hash=live.token_hash, now=NOW)
    assert enrollment_repo.reserve(db, token_hash=live.token_hash, email="b@example.com", now=NOW) is None
    assert enrollment_repo.get(db, live.enrollment_id).email == "a@example.com"

    assert enrollment_repo.reserve(db, token_hash=_hash(), email="a@example.com", now=NOW) is None


def test_expired_link_is_neither_redeemable_nor_reservable(db):
    row = enrollment_repo.add(db, enrollment_id=str(uuid.uuid4()), token_hash=_hash(), now=NOW, expires_at=LATER)
    assert not enrollment_repo.is_redeemable(db, token_hash=row.token_hash, now=LATER)
    assert enrollment_repo.reserve(db, token_hash=row.token_hash, email="a@example.com", now=LATER) is None
    assert enrollment_repo.get(db, row.enrollment_id).status is S.ISSUED


def test_revoke_takes_issued_and_reserved_but_never_redeemed(db):
    issued = enrollment_repo.add(db, enrollment_id=str(uuid.uuid4()), token_hash=_hash(), now=NOW, expires_at=LATER)
    reserved = enrollment_repo.add(db, enrollment_id=str(uuid.uuid4()), token_hash=_hash(), now=NOW, expires_at=LATER)
    redeemed = enrollment_repo.add(db, enrollment_id=str(uuid.uuid4()), token_hash=_hash(), now=NOW, expires_at=LATER)
    enrollment_repo.reserve(db, token_hash=reserved.token_hash, email="r@example.com", now=NOW)
    enrollment_repo.reserve(db, token_hash=redeemed.token_hash, email="d@example.com", now=NOW)
    row = enrollment_repo.lock(db, redeemed.enrollment_id)
    row.status, row.redeemed_user_id, row.redeemed_at = S.REDEEMED, _user(db), NOW
    db.flush()

    assert enrollment_repo.revoke(db, enrollment_id=issued.enrollment_id, now=NOW)
    assert enrollment_repo.revoke(db, enrollment_id=reserved.enrollment_id, now=NOW)
    assert not enrollment_repo.revoke(db, enrollment_id=issued.enrollment_id, now=NOW)
    assert not enrollment_repo.revoke(db, enrollment_id=redeemed.enrollment_id, now=NOW)
    states = {r.enrollment_id: r.status for r in enrollment_repo.list_all(db)}
    assert states == {
        issued.enrollment_id: S.REVOKED,
        reserved.enrollment_id: S.REVOKED,
        redeemed.enrollment_id: S.REDEEMED,
    }
    # 예약 흔적(이메일)은 회수 뒤에도 남는다 — 운영자가 Cognito 상태를 맞출 근거다
    assert enrollment_repo.get(db, reserved.enrollment_id).email == "r@example.com"


def test_concurrent_reserve_waits_on_the_row_and_only_one_wins(pg_engine):
    """독립 연결 2개: A 가 잡고 commit 전에 B 가 같은 행에서 기다리는지 확인한 뒤 A commit → B 는 0행."""
    token_hash = _hash()
    with Session(pg_engine) as setup:
        row = enrollment_repo.add(setup, enrollment_id=str(uuid.uuid4()), token_hash=token_hash, now=NOW, expires_at=LATER)
        setup.commit()
        enrollment_id = row.enrollment_id
    results: dict[str, str | None] = {}
    try:
        with Session(pg_engine) as first:
            assert enrollment_repo.reserve(first, token_hash=token_hash, email="a@example.com", now=NOW) == enrollment_id

            def second() -> None:
                with Session(pg_engine) as other:
                    results["b"] = enrollment_repo.reserve(other, token_hash=token_hash, email="b@example.com", now=NOW)
                    other.commit()

            worker = threading.Thread(target=second)
            worker.start()
            deadline = time.monotonic() + 10
            with pg_engine.connect() as probe:
                while time.monotonic() < deadline:
                    waiting = probe.execute(
                        text("SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                             "AND wait_event_type = 'Lock' AND query LIKE 'UPDATE platform_enrollments%'")
                    ).scalar_one()
                    probe.rollback()
                    if waiting:
                        break
                    time.sleep(0.05)
                else:
                    pytest.fail("두 번째 예약이 행 잠금을 기다리지 않았다")
            first.commit()
            worker.join(timeout=10)
        assert results == {"b": None}
        with Session(pg_engine) as check:
            row = enrollment_repo.get(check, enrollment_id)
            assert (row.status, row.email) == (S.RESERVED, "a@example.com")
    finally:
        with pg_engine.begin() as connection:
            connection.execute(text("DELETE FROM platform_enrollments WHERE enrollment_id = :e"), {"e": enrollment_id})
