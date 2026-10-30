"""audit_events migration — 단일 head · 왕복 · ORM 일치 · 트리거 함수까지 지운다.

병합 리비전(d492a544e001) 뒤의 head 에서 PARENT 로 내리면 b492e7d1a605(담당 배정 · 손실 downgrade)도 함께
내려간다. 그래서 왕복은 공용 DB 가 아니라 테스트마다 만든 빈 일회용 DB 에서 하고, 되살아나는 것은 스키마뿐이다
(담당자 · 배정은 시드하지 않으므로 잃는 데이터가 없다).
"""

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

REVISION = "b544a7d1e2f3"
PARENT = "c492d6e8f4a2"
NEW_TYPES = {"audit_event_action", "audit_actor_kind"}
SHARED_TYPES = {"trigger_source", "resolution_judgement"}


@pytest.fixture()
def migration_config(pg_engine, monkeypatch):
    monkeypatch.setenv(
        "DATABASE_URL", pg_engine.url.render_as_string(hide_password=False)
    )
    get_settings.cache_clear()
    yield Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    get_settings.cache_clear()


@pytest.fixture()
def scratch_db(pg_engine, monkeypatch):
    """(Config, engine) — 같은 서버의 새 빈 DB. 끝나면 DROP."""
    name = f"vigilantis_audit_{uuid.uuid4().hex[:8]}"
    admin = create_engine(
        pg_engine.url.set(database="postgres"), isolation_level="AUTOCOMMIT"
    )
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


def _types(connection) -> set[str]:
    names = NEW_TYPES | SHARED_TYPES
    return set(
        connection.execute(
            text("SELECT typname FROM pg_type WHERE typname = ANY(:names)"),
            {"names": list(names)},
        ).scalars()
    )


def _append_only_objects(connection) -> int:
    return connection.execute(
        text(
            "SELECT (SELECT count(*) FROM pg_proc WHERE proname = 'audit_events_append_only')"
            " + (SELECT count(*) FROM pg_trigger WHERE tgname = 'audit_events_append_only')"
        )
    ).scalar_one()


def test_revision_is_the_single_head_after_the_account_scope_merge(migration_config):
    script = ScriptDirectory.from_config(migration_config)
    assert script.get_revision(REVISION).down_revision == PARENT
    # head 를 이 리비전으로 고정하지 않는다 — 뒤에 리비전이 붙으면 깨진다. 한 줄 사슬인지만 본다
    assert len(script.get_heads()) == 1


def test_roundtrip_matches_orm_and_keeps_shared_types(scratch_db):
    config, engine = scratch_db
    command.upgrade(config, "head")
    command.downgrade(config, PARENT)
    with engine.connect() as connection:
        assert "audit_events" not in inspect(connection).get_table_names()
        # 병합 뒤 head 에서 PARENT 로 내리면 담당 배정도 내려간다
        assert "app_user_tenant_assignments" not in inspect(connection).get_table_names()
        # 실행 행 · 인시던트가 쓰는 타입은 남는다
        assert _types(connection) == SHARED_TYPES
        assert _append_only_objects(connection) == 0
    command.upgrade(config, "head")
    with engine.connect() as connection:
        assert "audit_events" in inspect(connection).get_table_names()
        assert "app_user_tenant_assignments" in inspect(connection).get_table_names()
        assert _types(connection) == NEW_TYPES | SHARED_TYPES
        assert _append_only_objects(connection) == 2
        assert (
            compare_metadata(
                MigrationContext.configure(connection), models.Base.metadata
            )
            == []
        )
