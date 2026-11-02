"""#565 신청 결정 기록 — 실제 PostgreSQL.

지키는 것: 부모 리비전 위의 단일 head · 새 DB 와 기존 DB 의 upgrade(기존 종결 신청에 기록을 지어내지 않는다) ·
신청당 한 건 · 결정 모양 CHECK · 참조 보호 · 일반 UPDATE·DELETE 거절 · 기록이 있으면 downgrade 거절.
"""

import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from config import get_settings
from db import models
from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError, IntegrityError, OperationalError

REVISION = "a565c8d1e901"
PARENT = "f515a552e001"
CONFIG_PATH = Path(__file__).resolve().parents[2] / "alembic.ini"
NOW = datetime(2026, 11, 2, 9, 0, tzinfo=UTC)


@pytest.fixture()
def decision_migration_db(pg_engine, monkeypatch):
    name = f"vigilantis_decision_{uuid.uuid4().hex}"
    admin = create_engine(pg_engine.url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    url = pg_engine.url.set(database=name)
    monkeypatch.setenv("DATABASE_URL", url.render_as_string(hide_password=False))
    get_settings.cache_clear()
    engine = create_engine(url)
    try:
        yield Config(str(CONFIG_PATH)), engine
    finally:
        engine.dispose()
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()
        get_settings.cache_clear()


def _insert_signup(conn, status="APPROVED", reason=None) -> str:
    return conn.execute(
        text(
            "INSERT INTO signup_requests (request_id, idempotency_key, company_name, contact_name, email, "
            "status, rejected_reason, created_at, updated_at) VALUES (gen_random_uuid(), gen_random_uuid(), "
            "'decision corp', 'decision contact', 'applicant@example.com', "
            "CAST(:s AS signup_request_status), :r, :n, :n) RETURNING request_id"
        ),
        {"s": status, "r": reason, "n": NOW},
    ).scalar_one()


def _insert_operator(conn) -> str:
    return conn.execute(
        text(
            "INSERT INTO app_users (user_id, issuer, cognito_sub, email, role, account_type, platform_role, "
            "status, created_at, updated_at) VALUES (gen_random_uuid(), 'https://issuer.example/decision', "
            ":s, 'operator@example.com', CAST('ADMIN' AS app_user_role), CAST('PLATFORM' AS app_user_account_type), "
            "CAST('TENANT_MANAGER' AS app_user_platform_role), CAST('ACTIVE' AS app_user_status), :n, :n) "
            "RETURNING user_id"
        ),
        {"s": f"decision-{uuid.uuid4().hex}", "n": NOW},
    ).scalar_one()


def _insert_decision(conn, request_id, actor_id, outcome="APPROVED", reason=None):
    conn.execute(
        text(
            "INSERT INTO signup_request_decisions (request_id, actor_user_id, actor_email, decided_at, "
            "outcome, rejected_reason) VALUES (:r, :a, 'operator@example.com', :n, "
            "CAST(:o AS signup_request_status), :why)"
        ),
        {"r": request_id, "a": actor_id, "n": NOW, "o": outcome, "why": reason},
    )


def test_revision_follows_the_merge_and_leads_to_the_single_head():
    script = ScriptDirectory.from_config(Config(str(CONFIG_PATH)))
    assert script.get_revision(REVISION).down_revision == PARENT
    [head] = script.get_heads()
    assert REVISION in {rev.revision for rev in script.walk_revisions(base="base", head=head)}


def test_fresh_database_matches_orm(decision_migration_db):
    config, engine = decision_migration_db
    command.upgrade(config, "head")
    with engine.connect() as conn:
        assert "signup_request_decisions" in inspect(conn).get_table_names()
        assert compare_metadata(MigrationContext.configure(conn), models.Base.metadata) == []


def test_upgrade_keeps_terminal_signups_without_inventing_decisions(decision_migration_db):
    config, engine = decision_migration_db
    command.upgrade(config, PARENT)
    with engine.begin() as conn:
        approved = _insert_signup(conn, "APPROVED")
        rejected = _insert_signup(conn, "REJECTED", "duplicate")
    command.upgrade(config, "head")
    with engine.connect() as conn:
        assert conn.scalar(text("SELECT count(*) FROM signup_request_decisions")) == 0
        assert conn.scalar(
            text("SELECT status::text FROM signup_requests WHERE request_id=:r"), {"r": approved}
        ) == "APPROVED"
        assert conn.scalar(
            text("SELECT rejected_reason FROM signup_requests WHERE request_id=:r"), {"r": rejected}
        ) == "duplicate"


def test_downgrade_is_refused_while_a_decision_exists(decision_migration_db):
    config, engine = decision_migration_db
    command.upgrade(config, "head")
    with engine.begin() as conn:
        _insert_decision(conn, _insert_signup(conn), _insert_operator(conn))
    with pytest.raises(RuntimeError, match="downgrade"):
        command.downgrade(config, PARENT)
    with engine.connect() as conn:
        assert "signup_request_decisions" in inspect(conn).get_table_names()
        assert conn.scalar(text("SELECT count(*) FROM signup_request_decisions")) == 1


def test_downgrade_never_loses_a_decision_written_after_its_emptiness_check(decision_migration_db):
    """빈 표 확인이 끝난 바로 그 시점에 다른 연결이 결정을 쓰려 한다(대기·sleep 없이 결정적).

    쓰기가 성공해 커밋되면 downgrade 는 그 결정을 지우지 말아야 한다. 확인이 쓰기를 막는
    잠금 아래에 있으면 쓰기는 즉시 거절되어 아무것도 커밋되지 않는다.
    """
    config, engine = decision_migration_db
    command.upgrade(config, "head")
    with engine.begin() as conn:
        request_id = _insert_signup(conn)
        actor_id = _insert_operator(conn)
    outcome = {"committed": False, "refused": False}

    def write_decision_after_check(conn, cursor, statement, parameters, context, executemany):
        if (
            conn.engine.url.database != engine.url.database
            or "EXISTS" not in statement
            or "signup_request_decisions" not in statement
        ):
            return
        try:
            with engine.begin() as writer:
                writer.execute(text("LOCK TABLE signup_request_decisions IN ROW EXCLUSIVE MODE NOWAIT"))
                _insert_decision(writer, request_id, actor_id)
            outcome["committed"] = True
        except OperationalError as exc:
            assert exc.orig.sqlstate == "55P03"  # lock_not_available
            outcome["refused"] = True

    event.listen(Engine, "after_cursor_execute", write_decision_after_check)
    try:
        try:
            command.downgrade(config, PARENT)
        except RuntimeError:
            pass
    finally:
        event.remove(Engine, "after_cursor_execute", write_decision_after_check)

    assert outcome["committed"] != outcome["refused"]
    with engine.connect() as conn:
        if outcome["committed"]:
            assert conn.scalar(text("SELECT count(*) FROM signup_request_decisions")) == 1


def test_empty_table_downgrades_and_reupgrades_keeping_signups(decision_migration_db):
    config, engine = decision_migration_db
    command.upgrade(config, "head")
    with engine.begin() as conn:
        signup = _insert_signup(conn)
    command.downgrade(config, PARENT)
    with engine.connect() as conn:
        assert "signup_request_decisions" not in inspect(conn).get_table_names()
        assert conn.scalar(text("SELECT count(*) FROM pg_proc WHERE proname LIKE 'signup_request_decisions%'")) == 0
    command.upgrade(config, "head")
    with engine.connect() as conn:
        assert conn.scalar(text("SELECT count(*) FROM signup_requests WHERE request_id=:r"), {"r": signup}) == 1
        assert compare_metadata(MigrationContext.configure(conn), models.Base.metadata) == []


def test_orm_row_persists_decision_time_actor_email_and_defaults(db):
    request_id = _insert_signup(db, "REJECTED", "duplicate")
    actor_id = _insert_operator(db)
    db.add(
        models.SignupRequestDecision(
            request_id=request_id,
            actor_user_id=actor_id,
            actor_email="operator@example.com",
            outcome=models.SignupRequestStatus.REJECTED,
            rejected_reason="duplicate",
        )
    )
    db.flush()
    row = db.execute(
        text(
            "SELECT actor_user_id, actor_email, outcome::text, rejected_reason, decided_at "
            "FROM signup_request_decisions WHERE request_id=:r"
        ),
        {"r": request_id},
    ).one()
    assert row[:4] == (actor_id, "operator@example.com", "REJECTED", "duplicate")
    assert row.decided_at.tzinfo is not None


def test_one_decision_per_request(db):
    request_id = _insert_signup(db)
    actor_id = _insert_operator(db)
    _insert_decision(db, request_id, actor_id)
    with pytest.raises(IntegrityError, match="pk_signup_request_decisions"), db.begin_nested():
        _insert_decision(db, request_id, actor_id, "REJECTED", "second")


@pytest.mark.parametrize(
    "outcome,reason",
    [
        ("APPROVED", "must be empty"),
        ("REJECTED", None),
        ("REJECTED", "   "),
        ("NEW", None),
        ("IN_REVIEW", None),
    ],
)
def test_outcome_and_reason_shape(db, outcome, reason):
    request_id = _insert_signup(db)
    actor_id = _insert_operator(db)
    with pytest.raises(IntegrityError, match="outcome_reason_shape"), db.begin_nested():
        _insert_decision(db, request_id, actor_id, outcome, reason)


def test_decision_requires_existing_request_and_actor(db):
    request_id = _insert_signup(db)
    actor_id = _insert_operator(db)
    with pytest.raises(IntegrityError, match="fk_signup_request_decisions_request_id"), db.begin_nested():
        _insert_decision(db, str(uuid.uuid4()), actor_id)
    with pytest.raises(IntegrityError, match="fk_signup_request_decisions_actor_user_id"), db.begin_nested():
        _insert_decision(db, request_id, str(uuid.uuid4()))


def test_referenced_request_and_actor_cannot_be_deleted(db):
    request_id = _insert_signup(db)
    actor_id = _insert_operator(db)
    _insert_decision(db, request_id, actor_id)
    with pytest.raises(IntegrityError, match="fk_signup_request_decisions_request_id"), db.begin_nested():
        db.execute(text("DELETE FROM signup_requests WHERE request_id=:r"), {"r": request_id})
    with pytest.raises(IntegrityError, match="fk_signup_request_decisions_actor_user_id"), db.begin_nested():
        db.execute(text("DELETE FROM app_users WHERE user_id=:u"), {"u": actor_id})


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE signup_request_decisions SET actor_email='other@example.com' WHERE request_id=:r",
        "DELETE FROM signup_request_decisions WHERE request_id=:r",
    ],
)
def test_ordinary_update_and_delete_are_rejected(db, statement):
    request_id = _insert_signup(db)
    _insert_decision(db, request_id, _insert_operator(db))
    with pytest.raises(DBAPIError, match="append-only"), db.begin_nested():
        db.execute(text(statement), {"r": request_id})
    assert db.scalar(
        text("SELECT actor_email FROM signup_request_decisions WHERE request_id=:r"), {"r": request_id}
    ) == "operator@example.com"
