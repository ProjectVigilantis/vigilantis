"""고객사 그룹 migration (#572 2번) — 실제 PostgreSQL/Alembic.

지키는 것: 단일 head · 그룹 표 · tenants.group_id(SET NULL) · 그룹 배정(CASCADE) · GROUP_ASSIGNMENTS_CHANGED 값 ·
ORM 일치 · 고객사 단위 직접 배정이 남은 DB 는 upgrade 를 거절하고 그 배정을 지우지 않는다(0건이면 변환 없이 도입) ·
downgrade 가 그룹을 버리고 빈 직접 배정 표를 되살린다(배정을 풀어 옮기지 않는다).
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

REVISION = "c572e4a9b210"
PARENT = "c597d1e2f3a4"
GROUPS = "tenant_groups"
GROUP_ASSIGNMENTS = "app_user_group_assignments"
TENANT_ASSIGNMENTS = "app_user_tenant_assignments"
TENANT_ID = "44444444-4444-4444-8444-444444444444"
GROUP_ID = "55555555-5555-4555-8555-555555555555"


@pytest.fixture()
def scratch_db(pg_engine, monkeypatch):
    """(Config, engine) — 같은 서버의 새 빈 DB. 끝나면 DROP."""
    name = f"vigilantis_groups_{uuid.uuid4().hex[:8]}"
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


def _versions(engine):
    with engine.connect() as conn:
        return set(conn.execute(text("SELECT version_num FROM alembic_version")).scalars())


def _tables(engine):
    return set(inspect(engine).get_table_names())


def _seed_tenant(conn):
    conn.execute(
        text(
            "INSERT INTO tenants (tenant_id, name, status, aws_account_id, access_role_arn, "
            "external_id, regions, collect_vpc_ids, auto_action_enabled, created_at, updated_at) "
            "VALUES (:tenant, 'groups-tenant', 'PENDING_CONNECTION', '123456789012', "
            "'arn:aws:iam::123456789012:role/vigilantis-access', :ext, "
            "'[\"ap-northeast-2\"]'::jsonb, '[]'::jsonb, true, now(), now())"
        ),
        {"tenant": TENANT_ID, "ext": "e" * 32},
    )


def _seed_manager(conn):
    conn.execute(
        text(
            "INSERT INTO app_users (user_id, issuer, cognito_sub, email, role, account_type, platform_role, "
            "status, created_at, updated_at) VALUES (gen_random_uuid(), 'https://issuer.example/groups', "
            "'groups-manager', 'gm@example.com', CAST('ADMIN' AS app_user_role), "
            "CAST('PLATFORM' AS app_user_account_type), CAST('TENANT_MANAGER' AS app_user_platform_role), "
            "CAST('ACTIVE' AS app_user_status), now(), now())"
        )
    )


def _manager_id(conn) -> str:
    return conn.execute(
        text("SELECT user_id::text FROM app_users WHERE cognito_sub = 'groups-manager'")
    ).scalar_one()


def _count(conn, table) -> int:
    return conn.execute(text(f"SELECT count(*) FROM {table}")).scalar_one()


def _event_labels(conn) -> set[str]:
    return set(conn.execute(text("SELECT unnest(enum_range(NULL::user_event_action))::text")).scalars())


def test_revision_follows_the_previous_head_on_a_single_chain(scratch_db):
    config, _ = scratch_db
    script = ScriptDirectory.from_config(config)
    assert script.get_revision(REVISION).down_revision == PARENT
    # head 를 이 리비전으로 고정하지 않는다 — 뒤에 리비전이 붙어도 깨지지 않게 한 줄 사슬인지만 본다
    assert len(script.get_heads()) == 1


def test_upgrade_creates_groups_drops_direct_assignments_and_matches_orm(scratch_db):
    config, engine = scratch_db
    command.upgrade(config, "head")
    with engine.connect() as conn:
        inspector = inspect(conn)
        tables = set(inspector.get_table_names())
        assert {GROUPS, GROUP_ASSIGNMENTS} <= tables
        assert TENANT_ASSIGNMENTS not in tables
        column = next(c for c in inspector.get_columns("tenants") if c["name"] == "group_id")
        assert column["nullable"] is True
        assert {
            (fk["referred_table"], fk["options"].get("ondelete"))
            for fk in inspector.get_foreign_keys("tenants")
            if fk["constrained_columns"] == ["group_id"]
        } == {(GROUPS, "SET NULL")}
        assert inspector.get_pk_constraint(GROUP_ASSIGNMENTS)["constrained_columns"] == ["user_id", "group_id"]
        assert {
            (fk["referred_table"], fk["options"].get("ondelete"))
            for fk in inspector.get_foreign_keys(GROUP_ASSIGNMENTS)
        } == {("app_users", "CASCADE"), (GROUPS, "CASCADE")}
        assert {"GROUP_ASSIGNMENTS_CHANGED", "TENANT_ASSIGNMENTS_CHANGED"} <= _event_labels(conn)
        assert compare_metadata(MigrationContext.configure(conn), models.Base.metadata) == []


def test_upgrade_refuses_while_a_direct_assignment_remains_and_keeps_it(scratch_db):
    config, engine = scratch_db
    command.upgrade(config, PARENT)
    with engine.begin() as conn:
        _seed_tenant(conn)
        _seed_manager(conn)
        conn.execute(
            text(f"INSERT INTO {TENANT_ASSIGNMENTS} (user_id, tenant_id) VALUES (:u, :t)"),
            {"u": _manager_id(conn), "t": TENANT_ID},
        )

    with pytest.raises(RuntimeError, match="직접 배정 1건"):
        command.upgrade(config, "head")

    # 지우지도 무시하지도 않는다 — 리비전 · 배정 · 표가 그대로다
    assert _versions(engine) == {PARENT}
    assert GROUPS not in _tables(engine)
    with engine.connect() as conn:
        assert _count(conn, TENANT_ASSIGNMENTS) == 1

    # 운영자가 배정을 비우면 변환 없이 도입된다
    with engine.begin() as conn:
        conn.execute(text(f"DELETE FROM {TENANT_ASSIGNMENTS}"))
    command.upgrade(config, "head")
    assert _versions(engine) == {ScriptDirectory.from_config(config).get_current_head()}
    with engine.connect() as conn:
        assert (_count(conn, GROUPS), _count(conn, GROUP_ASSIGNMENTS)) == (0, 0)
        assert conn.execute(
            text("SELECT group_id FROM tenants WHERE tenant_id = :t"), {"t": TENANT_ID}
        ).scalar_one() is None


def test_downgrade_drops_groups_and_restores_an_empty_direct_assignment_table(scratch_db):
    config, engine = scratch_db
    command.upgrade(config, "head")
    with engine.begin() as conn:
        _seed_tenant(conn)
        _seed_manager(conn)
        conn.execute(
            text(f"INSERT INTO {GROUPS} (group_id, name, created_at, updated_at) VALUES (:g, '광주 지사', now(), now())"),
            {"g": GROUP_ID},
        )
        conn.execute(text("UPDATE tenants SET group_id = :g WHERE tenant_id = :t"), {"g": GROUP_ID, "t": TENANT_ID})
        conn.execute(
            text(f"INSERT INTO {GROUP_ASSIGNMENTS} (user_id, group_id) VALUES (:u, :g)"),
            {"u": _manager_id(conn), "g": GROUP_ID},
        )

    command.downgrade(config, PARENT)
    with engine.connect() as conn:
        tables = set(inspect(conn).get_table_names())
        assert {GROUPS, GROUP_ASSIGNMENTS}.isdisjoint(tables)
        assert "group_id" not in {c["name"] for c in inspect(conn).get_columns("tenants")}
        # 그룹 배정을 고객사 배정으로 풀어 옮기지 않는다 — 담당자는 배정 없음으로 돌아간다(범위를 넓히지 않는다)
        assert _count(conn, TENANT_ASSIGNMENTS) == 0
        assert conn.execute(
            text("SELECT platform_role::text FROM app_users WHERE cognito_sub = 'groups-manager'")
        ).scalar_one() == "TENANT_MANAGER"
        assert _count(conn, "tenants") == 1
        # enum 값은 지우지 못한다(이력은 append-only) — 다시 올릴 때 IF NOT EXISTS 로 넘어간다
        assert "GROUP_ASSIGNMENTS_CHANGED" in _event_labels(conn)

    command.upgrade(config, "head")
    with engine.connect() as conn:
        assert (_count(conn, GROUPS), _count(conn, GROUP_ASSIGNMENTS)) == (0, 0)
        assert compare_metadata(MigrationContext.configure(conn), models.Base.metadata) == []
