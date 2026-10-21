"""user_admin_events migration — 단일 head · 왕복 · ORM 일치."""

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

REVISION = "e492a3b5c701"
PARENT = "d4a7c2e9f150"
NEW_REASONS = {"ADMIN_REVOKED", "ACCOUNT_DISABLED", "PASSWORD_RESET"}


@pytest.fixture()
def migration_config(pg_engine, monkeypatch):
    monkeypatch.setenv(
        "DATABASE_URL", pg_engine.url.render_as_string(hide_password=False)
    )
    get_settings.cache_clear()
    yield Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    get_settings.cache_clear()


def _reason_labels(connection) -> set[str]:
    return set(
        connection.execute(
            text(
                "SELECT enumlabel FROM pg_enum e JOIN pg_type t ON t.oid = e.enumtypid "
                "WHERE t.typname = 'auth_session_revoke_reason'"
            )
        ).scalars()
    )


def test_revision_is_the_single_head_after_tenants(migration_config):
    script = ScriptDirectory.from_config(migration_config)
    assert script.get_revision(REVISION).down_revision == PARENT
    assert script.get_heads() == [REVISION]


def test_roundtrip_matches_orm_and_restores_enum(pg_engine, migration_config):
    command.downgrade(migration_config, PARENT)
    try:
        with pg_engine.connect() as connection:
            assert "user_admin_events" not in inspect(connection).get_table_names()
            assert _reason_labels(connection) == {"LOGOUT", "SUPERSEDED"}
            assert (
                connection.execute(
                    text("SELECT count(*) FROM pg_type WHERE typname = 'user_event_action'")
                ).scalar_one()
                == 0
            )
    finally:
        command.upgrade(migration_config, "head")
    with pg_engine.connect() as connection:
        assert "user_admin_events" in inspect(connection).get_table_names()
        assert NEW_REASONS <= _reason_labels(connection)
        assert (
            compare_metadata(
                MigrationContext.configure(connection), models.Base.metadata
            )
            == []
        )
