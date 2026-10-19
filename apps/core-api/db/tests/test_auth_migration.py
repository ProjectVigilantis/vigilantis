"""auth_users_sessions migration — 단일 체인 · 왕복 · ORM 일치."""

from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from config import get_settings
from db import models
from sqlalchemy import inspect, text

REVISION = "d3a7c5e91f08"
PARENT = "c8e2f5a1d704"
TABLES = {"app_users", "auth_sessions"}
ENUM_TYPES = {
    "app_user_role",
    "app_user_status",
    "auth_session_mfa_step",
    "auth_session_revoke_reason",
}


@pytest.fixture()
def migration_config(pg_engine, monkeypatch):
    monkeypatch.setenv(
        "DATABASE_URL", pg_engine.url.render_as_string(hide_password=False)
    )
    get_settings.cache_clear()
    yield Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    get_settings.cache_clear()


def _enum_types(connection) -> set[str]:
    return set(
        connection.execute(
            text("SELECT typname FROM pg_type WHERE typtype = 'e'")
        ).scalars()
    )


def test_revision_follows_service_transitions(migration_config):
    script = ScriptDirectory.from_config(migration_config)
    assert script.get_revision(REVISION).down_revision == PARENT


def test_roundtrip_matches_orm(pg_engine, migration_config):
    command.downgrade(migration_config, PARENT)
    try:
        with pg_engine.connect() as connection:
            assert not TABLES & set(inspect(connection).get_table_names())
            assert not ENUM_TYPES & _enum_types(connection)
    finally:
        command.upgrade(migration_config, "head")
    with pg_engine.connect() as connection:
        assert TABLES <= set(inspect(connection).get_table_names())
        assert ENUM_TYPES <= _enum_types(connection)
        assert (
            compare_metadata(
                MigrationContext.configure(connection), models.Base.metadata
            )
            == []
        )
