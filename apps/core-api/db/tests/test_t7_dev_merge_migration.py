"""T-7 tenant settings(f7c1a9e4d201)와 dev head(f515a552e001) 병합 리비전.

지키는 것: 부모 2개(둘 다 그대로) · 단일 head · 새 DB / 옛 dev head DB / 옛 T-7 head DB 가 같은 head 로 올라가
기존 행(차단 중 TOTP 복구 상태 · 접수 행 · 고객 설정)을 보존 · head 테이블 수 · ORM 일치. 그 뒤 병합은 T-7 병합과 #565 결정 리비전을 잇는다.
테스트마다 일회용 DB 를 만들고 지운다.
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
from sqlalchemy import create_engine, inspect, text

BASE = "d492a544e001"
DEV_HEAD = "f515a552e001"
T7_HEAD = "f7c1a9e4d201"
MERGE = "f8c1a9e4f515"
TOTP = "d515a7c3e912"
INTAKE = "e552a7c3d901"
T7_TABLES = {"tenant_settings", "tenant_settings_cleanups", "notification_legacy_routes"}
DEV_ONLY_TABLES = {
    "totp_recovery_operations",
    "totp_provider_mutations",
    "signup_requests",
    "inquiries",
    "inquiry_events",
    "notifications",
    "notification_reads",
}
DECISIONS = "a565c8d1e901"  # dev #571 — DEV_HEAD 의 자식
LATEST = "f565c8d1a9e3"  # MERGE(T-7 병합)와 DECISIONS 를 잇는 병합 — 이제 head 의 조상(#528 병합이 뒤에 붙는다)
# head 의 테이블 수 — dev 35종 + T-7 3종 + #528 구독 보존 · 모의 계약 · 이벤트 3종 + #589 활동 로그 1종
HEAD_TABLES = 42
CONFIG_PATH = Path(__file__).resolve().parents[2] / "alembic.ini"
NOW = datetime(2026, 10, 31, 9, 0, tzinfo=UTC)


@pytest.fixture()
def scratch(pg_engine, monkeypatch):
    name = f"vigilantis_t7merge_{uuid.uuid4().hex}"
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
            "VALUES (:id, 't7 merge customer', 'PENDING_CONNECTION', '123456789012', "
            "'arn:aws:iam::123456789012:role/vigilantis-access', :ext, "
            "'[\"ap-northeast-2\"]'::jsonb, '[]'::jsonb, false, now(), now())"
        ),
        {"id": tenant_id, "ext": "c" * 32},
    )
    return tenant_id


def _insert_blocked_recovery(conn) -> str:
    """차단 중 사용자 + 풀리지 않은 TOTP 복구 작업 — 병합이 지워서는 안 되는 보안 상태."""
    user_id = conn.execute(
        text(
            "INSERT INTO app_users (user_id, issuer, cognito_sub, email, role, account_type, platform_role, "
            "status, auth_generation, mfa_recovery_state, created_at, updated_at) "
            "VALUES (gen_random_uuid(), 'https://issuer.example/t7merge', 't7merge', 't7@example.com', "
            "CAST('ADMIN' AS app_user_role), CAST('PLATFORM' AS app_user_account_type), "
            "CAST('TENANT_MANAGER' AS app_user_platform_role), CAST('ACTIVE' AS app_user_status), "
            "1, 'RESET_PENDING', :n, :n) RETURNING user_id"
        ),
        {"n": NOW},
    ).scalar_one()
    conn.execute(
        text(
            "INSERT INTO totp_recovery_operations (operation_id, target_user_id, actor_user_id, "
            "identity_verified, generation, status, revoked_session_count, created_at) "
            "VALUES (gen_random_uuid(), :u, NULL, true, 1, CAST('PROVIDER_PENDING' AS totp_recovery_status), "
            "0, :n)"
        ),
        {"u": user_id, "n": NOW},
    )
    return user_id


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


def _insert_tenant_settings(conn, tenant_id) -> None:
    conn.execute(
        text(
            "INSERT INTO tenant_settings (tenant_id, protected_tags, updated_at) "
            "VALUES (:t, CAST(:tags AS jsonb), :n)"
        ),
        {"t": tenant_id, "tags": '[{"key": "Env", "values": ["Prod"]}]', "n": NOW},
    )


def _single_head_through_latest(script) -> str:
    [head] = script.get_heads()  # head 를 병합으로 고정하지 않는다 — 뒤에 리비전이 붙어도 깨지지 않는다
    assert LATEST in {rev.revision for rev in script.walk_revisions(base="base", head=head)}
    return head


def _assert_merged_head(config, engine):
    head = _single_head_through_latest(ScriptDirectory.from_config(config))
    with engine.connect() as conn:
        assert conn.scalar(text("SELECT version_num FROM alembic_version")) == head
        names = set(inspect(conn).get_table_names()) - {"alembic_version"}
        assert len(names) == HEAD_TABLES
        assert (T7_TABLES | DEV_ONLY_TABLES | {"signup_request_decisions"}) <= names
        assert compare_metadata(MigrationContext.configure(conn), models.Base.metadata) == []


def test_merge_revision_joins_both_unchanged_heads():
    script = ScriptDirectory.from_config(Config(str(CONFIG_PATH)))
    assert set(script.get_revision(MERGE).down_revision) == {DEV_HEAD, T7_HEAD}
    assert set(script.get_revision(DEV_HEAD).down_revision) == {TOTP, INTAKE}
    assert script.get_revision(T7_HEAD).down_revision == BASE


def test_fresh_database_upgrades_to_the_merged_head(scratch):
    config, engine = scratch
    command.upgrade(config, "head")
    _assert_merged_head(config, engine)


def test_dev_head_database_upgrades_keeping_totp_and_intake_state(scratch):
    config, engine = scratch
    command.upgrade(config, DEV_HEAD)
    with engine.begin() as conn:
        assert T7_TABLES.isdisjoint(inspect(conn).get_table_names())
        user_id = _insert_blocked_recovery(conn)
        signup_id = _insert_rejected_signup(conn)
    command.upgrade(config, "head")
    _assert_merged_head(config, engine)
    with engine.connect() as conn:
        assert conn.execute(
            text("SELECT auth_generation, mfa_recovery_state::text FROM app_users WHERE user_id=:u"),
            {"u": user_id},
        ).one() == (1, "RESET_PENDING")
        assert conn.scalar(
            text("SELECT status::text FROM totp_recovery_operations WHERE target_user_id=:u"),
            {"u": user_id},
        ) == "PROVIDER_PENDING"
        assert conn.scalar(
            text("SELECT rejected_reason FROM signup_requests WHERE request_id=:r"), {"r": signup_id}
        ) == "duplicate"


def test_t7_head_database_upgrades_keeping_tenant_settings(scratch):
    config, engine = scratch
    command.upgrade(config, T7_HEAD)
    with engine.begin() as conn:
        assert DEV_ONLY_TABLES.isdisjoint(inspect(conn).get_table_names())
        tenant_id = _insert_tenant(conn)
        _insert_tenant_settings(conn, tenant_id)
    command.upgrade(config, "head")
    _assert_merged_head(config, engine)
    with engine.connect() as conn:
        assert conn.scalar(
            text("SELECT protected_tags->0->>'key' FROM tenant_settings WHERE tenant_id=:t"),
            {"t": tenant_id},
        ) == "Env"


def test_latest_merge_joins_t7_merge_and_decisions_into_the_single_head():
    script = ScriptDirectory.from_config(Config(str(CONFIG_PATH)))
    _single_head_through_latest(script)
    assert set(script.get_revision(LATEST).down_revision) == {MERGE, DECISIONS}
    assert script.get_revision(DECISIONS).down_revision == DEV_HEAD
    assert set(script.get_revision(MERGE).down_revision) == {DEV_HEAD, T7_HEAD}


def test_t7_merged_database_upgrades_keeping_settings_and_security_state(scratch):
    config, engine = scratch
    command.upgrade(config, MERGE)
    with engine.begin() as conn:
        tenant_id = _insert_tenant(conn)
        _insert_tenant_settings(conn, tenant_id)
        user_id = _insert_blocked_recovery(conn)
    command.upgrade(config, "head")
    _assert_merged_head(config, engine)
    with engine.connect() as conn:
        assert conn.scalar(
            text("SELECT protected_tags->0->>'key' FROM tenant_settings WHERE tenant_id=:t"),
            {"t": tenant_id},
        ) == "Env"
        assert conn.scalar(
            text("SELECT mfa_recovery_state FROM app_users WHERE user_id=:u"), {"u": user_id}
        ) == "RESET_PENDING"


def test_decisions_database_upgrades_keeping_decision_rows(scratch):
    config, engine = scratch
    command.upgrade(config, DECISIONS)
    with engine.begin() as conn:
        signup_id = _insert_rejected_signup(conn)
        actor_id = _insert_blocked_recovery(conn)
        conn.execute(
            text(
                "INSERT INTO signup_request_decisions (request_id, actor_user_id, actor_email, decided_at, "
                "outcome, rejected_reason) VALUES (:r, :a, 't7@example.com', :n, "
                "CAST('REJECTED' AS signup_request_status), 'duplicate')"
            ),
            {"r": signup_id, "a": actor_id, "n": NOW},
        )
    command.upgrade(config, "head")
    _assert_merged_head(config, engine)
    with engine.connect() as conn:
        assert conn.scalar(
            text("SELECT rejected_reason FROM signup_request_decisions WHERE request_id=:r"), {"r": signup_id}
        ) == "duplicate"
        assert T7_TABLES <= set(inspect(conn).get_table_names())
