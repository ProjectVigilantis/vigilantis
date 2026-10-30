"""#552 독립 migration의 부모, 기존 데이터 보존, downgrade·재upgrade를 확인한다."""

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

BASE = "d492a544e001"
REVISION = "e552a7c3d901"
TABLES = {"signup_requests", "inquiries", "inquiry_events", "notifications", "notification_reads"}
CONFIG_PATH = Path(__file__).resolve().parents[2] / "alembic.ini"


def test_intake_revision_is_a_direct_child_of_review_fixed_baseline():
    script = ScriptDirectory.from_config(Config(str(CONFIG_PATH)))
    assert script.get_revision(REVISION).down_revision == BASE
    assert script.get_heads() == ["f515a552e001"]


@pytest.fixture()
def intake_migration_db(pg_engine, monkeypatch):
    name = f"vigilantis_intake_{uuid.uuid4().hex}"
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


def test_upgrade_downgrade_reupgrade_preserves_existing_customer(intake_migration_db):
    config, engine = intake_migration_db
    command.upgrade(config, BASE)
    customer_id = str(uuid.uuid4())
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO tenants (tenant_id, name, status, aws_account_id, access_role_arn, "
                "external_id, regions, collect_vpc_ids, auto_action_enabled, created_at, updated_at) "
                "VALUES (:id, 'existing customer', 'PENDING_CONNECTION', '123456789012', "
                "'arn:aws:iam::123456789012:role/vigilantis-access', :ext, "
                "'[\"ap-northeast-2\"]'::jsonb, '[]'::jsonb, false, now(), now())"
            ),
            {"id": customer_id, "ext": "a" * 32},
        )
    command.upgrade(config, REVISION)
    assert TABLES <= set(inspect(engine).get_table_names())
    with engine.connect() as conn:
        assert conn.scalar(
            text("SELECT count(*) FROM pg_trigger WHERE tgname='inquiry_events_append_only'")
        ) == 1
    command.upgrade(config, "head")
    with engine.connect() as conn:
        assert compare_metadata(MigrationContext.configure(conn), models.Base.metadata) == []

    command.downgrade(config, BASE)
    assert not TABLES.intersection(inspect(engine).get_table_names())
    with engine.connect() as conn:
        assert conn.scalar(
            text("SELECT name FROM tenants WHERE tenant_id=:id"), {"id": customer_id}
        ) == "existing customer"
    command.upgrade(config, "head")
    with engine.connect() as conn:
        assert compare_metadata(MigrationContext.configure(conn), models.Base.metadata) == []
        assert conn.scalar(
            text("SELECT name FROM tenants WHERE tenant_id=:id"), {"id": customer_id}
        ) == "existing customer"
