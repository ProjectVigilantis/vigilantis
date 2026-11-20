"""도입 신청 중복 접수 기록 migration — 실제 PostgreSQL/Alembic.

지키는 것: 부모 f598b2c3d402 · 단일 head · signup_request_duplicates(키 PK · 신청 FK RESTRICT) 생성 후 ORM 과 일치 ·
기록이 남아 있으면 downgrade 를 거절하고(지우면 그 키의 재시도가 새 신청을 만들 수 있다) 비어 있으면 표만 지운다.
테스트마다 고유 이름의 일회용 DB 를 만들고 지운다(공용 pg_engine DB 의 head 를 건드리지 않는다).
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from config import get_settings
from db import models
from sqlalchemy import create_engine, inspect, text

REVISION = "b671a1c2d301"
PARENT = "f598b2c3d402"
REQUEST_ID = "99999999-9999-4999-8999-999999999999"


@pytest.fixture()
def scratch_db(pg_engine, monkeypatch):
    """(Config, engine) — 같은 서버의 새 빈 DB. 끝나면 DROP."""
    name = f"vigilantis_signup_dup_{uuid.uuid4().hex[:8]}"
    admin = create_engine(pg_engine.url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    url = pg_engine.url.set(database=name)
    monkeypatch.setenv("DATABASE_URL", url.render_as_string(hide_password=False))
    get_settings.cache_clear()
    engine = create_engine(url)
    try:
        yield Config(str(Path(__file__).resolve().parents[2] / "alembic.ini")), engine
    finally:
        engine.dispose()
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()
        get_settings.cache_clear()


def _seed(conn):
    conn.execute(
        text(
            "INSERT INTO signup_requests (request_id, idempotency_key, company_name, contact_name, email, "
            "status, created_at, updated_at) VALUES (:r, gen_random_uuid(), '회사', '담당', 'c@example.com', "
            "CAST('NEW' AS signup_request_status), now(), now())"
        ),
        {"r": REQUEST_ID},
    )
    conn.execute(
        text(
            "INSERT INTO signup_request_duplicates (idempotency_key, request_id, company_name, contact_name, "
            "email, created_at) VALUES (gen_random_uuid(), :r, '회사', '담당', 'C@example.com', now())"
        ),
        {"r": REQUEST_ID},
    )


def test_revision_follows_the_previous_head_on_a_single_chain(scratch_db):
    config, _ = scratch_db
    script = ScriptDirectory.from_config(config)
    assert script.get_revision(REVISION).down_revision == PARENT
    assert len(script.get_heads()) == 1


def test_upgrade_creates_the_table_with_a_restrict_fk_and_matches_orm(scratch_db):
    config, engine = scratch_db
    command.upgrade(config, PARENT)
    command.upgrade(config, REVISION)
    with engine.connect() as conn:
        inspector = inspect(conn)
        assert inspector.get_pk_constraint("signup_request_duplicates")["constrained_columns"] == [
            "idempotency_key"
        ]
        assert {
            (fk["referred_table"], tuple(fk["constrained_columns"]), fk["options"].get("ondelete"))
            for fk in inspector.get_foreign_keys("signup_request_duplicates")
        } == {("signup_requests", ("request_id",), "RESTRICT")}
    # 후속 revision 은 표를 더할 수 있다. ORM 은 그 변경까지 적용한 head 와 비교한다.
    command.upgrade(config, "head")
    with engine.connect() as conn:
        assert compare_metadata(MigrationContext.configure(conn), models.Base.metadata) == []


def test_downgrade_refuses_while_records_remain(scratch_db):
    config, engine = scratch_db
    command.upgrade(config, REVISION)
    with engine.begin() as conn:
        _seed(conn)
    with pytest.raises(RuntimeError, match="중복 접수 기록"):
        command.downgrade(config, PARENT)
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM signup_request_duplicates")).scalar_one() == 1


def test_downgrade_drops_only_the_empty_table_and_upgrades_again(scratch_db):
    config, engine = scratch_db
    command.upgrade(config, REVISION)
    command.downgrade(config, PARENT)
    with engine.connect() as conn:
        assert "signup_request_duplicates" not in inspect(conn).get_table_names()
    command.upgrade(config, "head")
    with engine.connect() as conn:
        assert compare_metadata(MigrationContext.configure(conn), models.Base.metadata) == []
