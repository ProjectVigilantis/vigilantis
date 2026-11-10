"""#609 신청-고객사 연결과 #610 실제 Marketplace 권리의 빈 병합 — 실제 PostgreSQL/Alembic.

새 DB와 각 부모까지만 적용한 DB가 단일 head에 도달하고 같은 스키마를 갖는지 확인한다.
기존 부모를 바꾸지 않으며, 테이블·컬럼·제약·인덱스·enum과 ORM을 대조한다.
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

FORK = "c572e4a9b210"
SIGNUP = "d572b1a7c3e4"
MARKETPLACE = "e600b2c4d711"
MERGE = "f609f610e001"
CONFIG_PATH = Path(__file__).resolve().parents[2] / "alembic.ini"


@pytest.fixture()
def merge_db(pg_engine, monkeypatch):
    name = f"vigilantis_signup_marketplace_{uuid.uuid4().hex[:8]}"
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


def _schema(engine):
    inspector = inspect(engine)
    tables = {}
    for table in inspector.get_table_names():
        columns = inspector.get_columns(table)
        for column in columns:
            column["type"] = str(column["type"])
        tables[table] = {
            "columns": columns,
            "primary_key": inspector.get_pk_constraint(table),
            "foreign_keys": inspector.get_foreign_keys(table),
            "checks": inspector.get_check_constraints(table),
            "unique_constraints": inspector.get_unique_constraints(table),
            "indexes": inspector.get_indexes(table),
        }
    return tables, inspector.get_enums()


def test_merge_joins_unchanged_parents_into_one_head():
    script = ScriptDirectory.from_config(Config(str(CONFIG_PATH)))
    [head] = script.get_heads()
    assert MERGE in {revision.revision for revision in script.walk_revisions(head=head)}
    assert set(script.get_revision(MERGE).down_revision) == {SIGNUP, MARKETPLACE}
    assert script.get_revision(SIGNUP).down_revision == FORK
    assert script.get_revision(MARKETPLACE).down_revision == FORK


@pytest.mark.parametrize("start", ["base", SIGNUP, MARKETPLACE])
def test_each_start_upgrades_to_the_same_schema(merge_db, pg_engine, start):
    config, engine = merge_db
    command.upgrade(config, start)
    inspector = inspect(engine)
    if start != "base":
        with engine.connect() as conn:
            assert list(conn.execute(text("SELECT version_num FROM alembic_version")).scalars()) == [start]
        assert ("tenant_id" in {column["name"] for column in inspector.get_columns("signup_requests")}) == (
            start == SIGNUP
        )
        assert inspector.has_table("marketplace_licenses") == (start == MARKETPLACE)

    command.upgrade(config, "head")

    [head] = ScriptDirectory.from_config(config).get_heads()
    with engine.connect() as conn:
        assert list(conn.execute(text("SELECT version_num FROM alembic_version")).scalars()) == [head]
        assert len(set(inspect(conn).get_table_names()) - {"alembic_version"}) == 45
        assert compare_metadata(MigrationContext.configure(conn), models.Base.metadata) == []
    # pg_engine은 새 DB를 base→head로 올린 기준이다. ORM 비교가 다루지 않는 CHECK·enum도 대조한다.
    assert _schema(engine) == _schema(pg_engine)
