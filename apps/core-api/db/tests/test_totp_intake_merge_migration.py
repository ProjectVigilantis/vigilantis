"""#515 TOTP 복구와 #552 고객 접수 병합 리비전 — 새 DB 와 각 형제에서의 upgrade 가 같은 단일 head 에 닿는다."""

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
from sqlalchemy import create_engine, inspect, text

BASE = "d492a544e001"
TOTP = "d515a7c3e912"
INTAKE = "e552a7c3d901"
MERGE = "f515a552e001"
TOTP_TABLES = {"totp_recovery_operations", "totp_provider_mutations"}
INTAKE_TABLES = {"signup_requests", "inquiries", "inquiry_events", "notifications", "notification_reads"}
CONFIG_PATH = Path(__file__).resolve().parents[2] / "alembic.ini"
NOW = datetime(2026, 10, 30, 9, 0, tzinfo=UTC)


@pytest.fixture()
def merge_migration_db(pg_engine, monkeypatch):
    name = f"vigilantis_merge_{uuid.uuid4().hex}"
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


def _insert_tenant(conn) -> str:
    tenant_id = str(uuid.uuid4())
    conn.execute(
        text(
            "INSERT INTO tenants (tenant_id, name, status, aws_account_id, access_role_arn, "
            "external_id, regions, collect_vpc_ids, auto_action_enabled, created_at, updated_at) "
            "VALUES (:id, 'merge customer', 'PENDING_CONNECTION', '123456789012', "
            "'arn:aws:iam::123456789012:role/vigilantis-access', :ext, "
            "'[\"ap-northeast-2\"]'::jsonb, '[]'::jsonb, false, now(), now())"
        ),
        {"id": tenant_id, "ext": "b" * 32},
    )
    return tenant_id


def _insert_platform_user(conn) -> str:
    return conn.execute(
        text(
            "INSERT INTO app_users (user_id, issuer, cognito_sub, email, role, account_type, platform_role, "
            "status, created_at, updated_at) VALUES (gen_random_uuid(), 'https://issuer.example/merge', 'merge', "
            "'merge@example.com', CAST('ADMIN' AS app_user_role), CAST('PLATFORM' AS app_user_account_type), "
            "CAST('TENANT_MANAGER' AS app_user_platform_role), CAST('ACTIVE' AS app_user_status), :n, :n) "
            "RETURNING user_id"
        ),
        {"n": NOW},
    ).scalar_one()


def _insert_rejected_signup(conn) -> str:
    return conn.execute(
        text(
            "INSERT INTO signup_requests (request_id, idempotency_key, company_name, contact_name, email, "
            "status, rejected_reason, created_at, updated_at) VALUES (gen_random_uuid(), gen_random_uuid(), "
            "'merge corp', 'merge contact', 'intake@example.com', CAST('REJECTED' AS signup_request_status), "
            "'duplicate', :n, :n) RETURNING request_id"
        ),
        {"n": NOW},
    ).scalar_one()


def _insert_blocked_recovery_state(conn, user_id) -> str:
    """차단 중 사용자 + 풀리지 않은 작업 · 신원 변경 울타리 — 병합이 지워서는 안 되는 보안 상태."""
    conn.execute(
        text("UPDATE app_users SET auth_generation = 1, mfa_recovery_state = 'RESET_PENDING' WHERE user_id = :u"),
        {"u": user_id},
    )
    operation_id = conn.execute(
        text(
            "INSERT INTO totp_recovery_operations (operation_id, target_user_id, actor_user_id, "
            "identity_verified, generation, status, revoked_session_count, created_at) "
            "VALUES (gen_random_uuid(), :u, NULL, true, 1, CAST('PROVIDER_PENDING' AS totp_recovery_status), "
            "0, :n) RETURNING operation_id"
        ),
        {"u": user_id, "n": NOW},
    ).scalar_one()
    conn.execute(
        text(
            "INSERT INTO totp_provider_mutations (mutation_id, issuer, cognito_sub, kind, started_at) "
            "VALUES (gen_random_uuid(), 'https://issuer.example/merge', 'merge', "
            "CAST('ENROLLMENT' AS totp_provider_mutation_kind), :n)"
        ),
        {"n": NOW},
    )
    return operation_id


def _assert_combined_head(engine):
    with engine.connect() as conn:
        assert conn.scalar(text("SELECT version_num FROM alembic_version")) == MERGE
        assert (TOTP_TABLES | INTAKE_TABLES) <= set(inspect(conn).get_table_names())
        assert compare_metadata(MigrationContext.configure(conn), models.Base.metadata) == []


def test_merge_revision_joins_both_unchanged_siblings_into_the_single_head():
    script = ScriptDirectory.from_config(Config(str(CONFIG_PATH)))
    assert set(script.get_revision(MERGE).down_revision) == {TOTP, INTAKE}
    assert script.get_revision(TOTP).down_revision == BASE
    assert script.get_revision(INTAKE).down_revision == BASE
    assert script.get_heads() == [MERGE]


def test_fresh_database_upgrades_to_the_combined_head(merge_migration_db):
    config, engine = merge_migration_db
    command.upgrade(config, "head")
    _assert_combined_head(engine)


def test_upgrade_from_intake_sibling_preserves_customer_and_adds_totp(merge_migration_db):
    config, engine = merge_migration_db
    command.upgrade(config, INTAKE)
    with engine.begin() as conn:
        tenant_id = _insert_tenant(conn)
        signup_id = _insert_rejected_signup(conn)
        assert TOTP_TABLES.isdisjoint(inspect(conn).get_table_names())
    command.upgrade(config, "head")
    _assert_combined_head(engine)
    with engine.connect() as conn:
        assert conn.execute(
            text("SELECT status::text, rejected_reason FROM signup_requests WHERE request_id=:r"),
            {"r": signup_id},
        ).one() == ("REJECTED", "duplicate")
        assert conn.scalar(
            text("SELECT name FROM tenants WHERE tenant_id=:id"), {"id": tenant_id}
        ) == "merge customer"


def test_upgrade_from_totp_sibling_preserves_user_and_adds_intake(merge_migration_db):
    config, engine = merge_migration_db
    command.upgrade(config, TOTP)
    with engine.begin() as conn:
        user_id = _insert_platform_user(conn)
        operation_id = _insert_blocked_recovery_state(conn, user_id)
        assert INTAKE_TABLES.isdisjoint(inspect(conn).get_table_names())
    command.upgrade(config, "head")
    _assert_combined_head(engine)
    with engine.connect() as conn:
        assert conn.execute(
            text("SELECT auth_generation, mfa_recovery_state::text FROM app_users WHERE user_id=:u"),
            {"u": user_id},
        ).one() == (1, "RESET_PENDING")
        assert conn.execute(
            text("SELECT status::text FROM totp_recovery_operations WHERE operation_id=:o"),
            {"o": operation_id},
        ).scalar_one() == "PROVIDER_PENDING"
        assert conn.scalar(
            text("SELECT count(*) FROM totp_provider_mutations WHERE resolution IS NULL")
        ) == 1
