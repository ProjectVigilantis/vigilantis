"""account_type / auto_action_resumed_at 병합 리비전 — 실제 PostgreSQL/Alembic.

지키는 것: 부모 2개 · 단일 head · 새 DB base→head · 옛 feature 단독 / 옛 dev 단독 DB 가
각각 head 로 올라가 두 칸을 모두 갖고 시드 데이터를 보존 · 버전 = 병합 리비전 · ORM 일치.
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

MERGE = "c492d6e8f4a2"
FEATURE_REVISION = "f492c4a8d2e1"  # app_users.account_type 가지의 끝
DEV_REVISION = "f514a6c3d201"  # tenants.auto_action_resumed_at 가지
FORK = "e492a3b5c701"
ISSUER = "https://cognito-idp.ap-northeast-2.amazonaws.com/pool-merge"
TENANT_ID = "11111111-1111-4111-8111-111111111111"
FEATURE_COLUMN = ("app_users", "account_type")
DEV_COLUMN = ("tenants", "auto_action_resumed_at")


@pytest.fixture()
def merge_db(pg_engine, monkeypatch):
    """(Config, engine) — 같은 서버의 새 빈 DB. 끝나면 DROP."""
    name = f"vigilantis_merge_{uuid.uuid4().hex[:8]}"
    admin = create_engine(
        pg_engine.url.set(database="postgres"), isolation_level="AUTOCOMMIT"
    )
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    url = pg_engine.url.set(database=name)
    monkeypatch.setenv("DATABASE_URL", url.render_as_string(hide_password=False))
    get_settings.cache_clear()
    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    engine = create_engine(url)
    try:
        yield config, engine
    finally:
        engine.dispose()
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()
        get_settings.cache_clear()


def _columns(engine, table):
    return {c["name"] for c in inspect(engine).get_columns(table)}


def _versions(engine):
    with engine.connect() as conn:
        return set(conn.execute(text("SELECT version_num FROM alembic_version")).scalars())


def _seed_tenant_and_user(engine, *, with_account_type):
    """그 리비전의 모양 그대로 raw INSERT — ORM 이 아닌 당시 칸으로 넣는다."""
    user_cols = (
        "user_id, issuer, cognito_sub, email, role, tenant_id, status, created_at, updated_at"
    )
    user_vals = (
        "gen_random_uuid(), :issuer, 'merge-user', 'merge@example.com', "
        "CAST('VIEWER' AS app_user_role), :tenant, CAST('ACTIVE' AS app_user_status), now(), now()"
    )
    if with_account_type:
        user_cols += ", account_type"
        user_vals += ", CAST('TENANT' AS app_user_account_type)"
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO tenants (tenant_id, name, status, aws_account_id, access_role_arn, "
                "external_id, regions, collect_vpc_ids, auto_action_enabled, created_at, updated_at) "
                "VALUES (:tenant, 'merge-tenant', 'PENDING_CONNECTION', '123456789012', "
                "'arn:aws:iam::123456789012:role/vigilantis-access', :ext, "
                "'[\"ap-northeast-2\"]'::jsonb, '[]'::jsonb, false, now(), now())"
            ),
            {"tenant": TENANT_ID, "ext": "e" * 32},
        )
        conn.execute(
            text(f"INSERT INTO app_users ({user_cols}) VALUES ({user_vals})"),
            {"issuer": ISSUER, "tenant": TENANT_ID},
        )


def _assert_merged_head(engine, *, expected_account_type):
    assert _versions(engine) == {MERGE}
    assert FEATURE_COLUMN[1] in _columns(engine, FEATURE_COLUMN[0])
    assert DEV_COLUMN[1] in _columns(engine, DEV_COLUMN[0])
    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT t.name, t.auto_action_enabled, t.auto_action_resumed_at, "
                "u.cognito_sub, u.issuer, u.tenant_id::text AS tenant_id, "
                "u.account_type::text AS acct "
                "FROM app_users u JOIN tenants t ON t.tenant_id = u.tenant_id "
                "WHERE u.cognito_sub = 'merge-user'"
            )
        ).one()
        assert tuple(row) == (
            "merge-tenant",
            False,
            None,
            "merge-user",
            ISSUER,
            TENANT_ID,
            expected_account_type,
        )
        assert compare_metadata(MigrationContext.configure(conn), models.Base.metadata) == []


def test_merge_revision_joins_both_branches_into_a_single_head(merge_db):
    config, _ = merge_db
    script = ScriptDirectory.from_config(config)
    assert set(script.get_revision(MERGE).down_revision) == {
        FEATURE_REVISION,
        DEV_REVISION,
    }
    assert script.get_heads() == [MERGE]
    assert script.get_revision(FEATURE_REVISION).down_revision != DEV_REVISION
    assert script.get_revision(DEV_REVISION).down_revision == FORK


def test_fresh_database_upgrades_base_to_merge_head(merge_db):
    config, engine = merge_db
    command.upgrade(config, "head")
    assert _versions(engine) == {MERGE}
    assert FEATURE_COLUMN[1] in _columns(engine, FEATURE_COLUMN[0])
    assert DEV_COLUMN[1] in _columns(engine, DEV_COLUMN[0])
    with engine.connect() as conn:
        assert compare_metadata(MigrationContext.configure(conn), models.Base.metadata) == []


def test_old_feature_only_database_upgrades_to_merge_head_keeping_data(merge_db):
    config, engine = merge_db
    command.upgrade(config, FEATURE_REVISION)
    assert _versions(engine) == {FEATURE_REVISION}
    assert FEATURE_COLUMN[1] in _columns(engine, FEATURE_COLUMN[0])
    assert DEV_COLUMN[1] not in _columns(engine, DEV_COLUMN[0])
    _seed_tenant_and_user(engine, with_account_type=True)
    command.upgrade(config, "head")
    _assert_merged_head(engine, expected_account_type="TENANT")


def test_old_dev_only_database_upgrades_to_merge_head_keeping_data(merge_db):
    config, engine = merge_db
    command.upgrade(config, DEV_REVISION)
    assert _versions(engine) == {DEV_REVISION}
    assert DEV_COLUMN[1] in _columns(engine, DEV_COLUMN[0])
    assert FEATURE_COLUMN[1] not in _columns(engine, FEATURE_COLUMN[0])
    _seed_tenant_and_user(engine, with_account_type=False)
    command.upgrade(config, "head")
    # 옛 dev 의 소속 사용자는 f492 백필 규칙대로 TENANT 로 분류된다(승격 없음)
    _assert_merged_head(engine, expected_account_type="TENANT")
