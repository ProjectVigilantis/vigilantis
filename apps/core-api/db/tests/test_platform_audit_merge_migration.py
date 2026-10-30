"""platform_role/assignment(b492e7d1a605) · audit_events(b544a7d1e2f3) 병합 리비전 — 실제 PostgreSQL/Alembic.

지키는 것: 부모 2개(둘 다 그대로) · 단일 head · 새 DB base→head · 옛 b492 단독 / 옛 b544 단독 DB 가
각각 head 로 올라가 두 갈래와 #552 테이블을 모두 갖고 시드 데이터를 보존 · 32개 테이블 · ORM 일치.
테스트마다 고유 이름의 일회용 DB 를 만들고 지운다(공용 pg_engine DB 의 head 를 건드리지 않는다).
"""

from __future__ import annotations

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
from sqlalchemy.exc import DBAPIError

MERGE = "d492a544e001"
PLATFORM_BRANCH = "b492e7d1a605"
AUDIT_BRANCH = "b544a7d1e2f3"
FORK = "c492d6e8f4a2"
ASSIGNMENTS = "app_user_tenant_assignments"
ISSUER = "https://cognito-idp.ap-northeast-2.amazonaws.com/pool-pa-merge"
TENANT_ID = "22222222-2222-4222-8222-222222222222"
INCIDENT_ID = "33333333-3333-4333-8333-333333333333"
OCCURRED_AT = datetime(2026, 10, 30, 9, 0, tzinfo=UTC)


@pytest.fixture()
def merge_db(pg_engine, monkeypatch):
    """(Config, engine) — 같은 서버의 새 빈 DB. 끝나면 DROP."""
    name = f"vigilantis_pamerge_{uuid.uuid4().hex[:8]}"
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


def _head(config) -> str:
    return ScriptDirectory.from_config(config).get_current_head()


def _versions(engine):
    with engine.connect() as conn:
        return set(conn.execute(text("SELECT version_num FROM alembic_version")).scalars())


def _tables(engine):
    return set(inspect(engine).get_table_names()) - {"alembic_version"}


def _seed_tenant(conn):
    conn.execute(
        text(
            "INSERT INTO tenants (tenant_id, name, status, aws_account_id, access_role_arn, "
            "external_id, regions, collect_vpc_ids, auto_action_enabled, created_at, updated_at) "
            "VALUES (:tenant, 'pa-merge-tenant', 'PENDING_CONNECTION', '123456789012', "
            "'arn:aws:iam::123456789012:role/vigilantis-access', :ext, "
            "'[\"ap-northeast-2\"]'::jsonb, '[]'::jsonb, false, now(), now())"
        ),
        {"tenant": TENANT_ID, "ext": "f" * 32},
    )


def _seed_platform_user(conn, *, platform_role=None):
    """그 리비전의 모양 그대로 raw INSERT — platform_role 은 b492 이후에만 있다."""
    cols = (
        "user_id, issuer, cognito_sub, email, role, account_type, status, created_at, updated_at"
    )
    vals = (
        "gen_random_uuid(), :issuer, 'pa-user', 'pa@example.com', CAST('ADMIN' AS app_user_role), "
        "CAST('PLATFORM' AS app_user_account_type), CAST('ACTIVE' AS app_user_status), now(), now()"
    )
    if platform_role is not None:
        cols += ", platform_role"
        vals += ", CAST(:prole AS app_user_platform_role)"
    conn.execute(
        text(f"INSERT INTO app_users ({cols}) VALUES ({vals})"),
        {"issuer": ISSUER, "prole": platform_role},
    )


def _seed_resolved_audit_event(conn):
    """b544 모양 그대로 — 종료된 FinOps 인시던트 + 그 종료 판단을 PLATFORM 사용자가 남긴 감사 기록 1건."""
    conn.execute(
        text(
            "INSERT INTO incidents (incident_id, tenant_id, subject_arn, category, status, "
            "initial_risk_reason_codes, summary_lines, agent_invocation_status, resolution, "
            "resolved_at, created_at, updated_at) "
            "VALUES (:incident, :tenant, 'arn:aws:ec2:ap-northeast-2:123456789012:instance/i-pa', "
            "CAST('FINOPS' AS incident_category), CAST('RESOLVED' AS incident_status), "
            "'[]'::jsonb, '[]'::jsonb, CAST('SUCCEEDED' AS agent_invocation_status), "
            "CAST('JUSTIFIED' AS resolution_judgement), :at, :at, :at)"
        ),
        {"incident": INCIDENT_ID, "tenant": TENANT_ID, "at": OCCURRED_AT},
    )
    conn.execute(
        text(
            "INSERT INTO audit_events (event_id, action, occurred_at, actor_kind, actor_user_id, "
            "incident_id, tenant_id, resolution) "
            "SELECT gen_random_uuid(), CAST('INCIDENT_RESOLVED' AS audit_event_action), :at, "
            "CAST('PLATFORM_USER' AS audit_actor_kind), user_id, :incident, :tenant, "
            "CAST('JUSTIFIED' AS resolution_judgement) "
            "FROM app_users WHERE cognito_sub = 'pa-user'"
        ),
        {"incident": INCIDENT_ID, "tenant": TENANT_ID, "at": OCCURRED_AT},
    )


def _assert_merged_head(config, engine, *, platform_role, assignments):
    assert _versions(engine) == {_head(config)}
    tables = _tables(engine)
    assert len(tables) == 32
    assert {ASSIGNMENTS, "audit_events"} <= tables
    with engine.connect() as conn:
        assert (
            conn.execute(
                text(
                    "SELECT platform_role::text FROM app_users WHERE cognito_sub = 'pa-user'"
                )
            ).scalar_one()
            == platform_role
        )
        assert conn.execute(text(f"SELECT count(*) FROM {ASSIGNMENTS}")).scalar_one() == assignments
        assert (
            conn.execute(
                text(
                    "SELECT (SELECT count(*) FROM pg_proc WHERE proname = 'audit_events_append_only')"
                    " + (SELECT count(*) FROM pg_trigger WHERE tgname = 'audit_events_append_only')"
                )
            ).scalar_one()
            == 2
        )
        assert compare_metadata(MigrationContext.configure(conn), models.Base.metadata) == []


def test_merge_revision_joins_both_unchanged_branches_into_a_single_head(merge_db):
    config, _ = merge_db
    script = ScriptDirectory.from_config(config)
    [head] = script.get_heads()  # head 를 병합으로 고정하지 않는다 — 뒤에 리비전이 붙어도 깨지지 않는다
    assert MERGE in {rev.revision for rev in script.walk_revisions(base="base", head=head)}
    assert set(script.get_revision(MERGE).down_revision) == {PLATFORM_BRANCH, AUDIT_BRANCH}
    assert script.get_revision(PLATFORM_BRANCH).down_revision == FORK
    assert script.get_revision(AUDIT_BRANCH).down_revision == FORK


def test_fresh_database_upgrades_to_merge_head(merge_db):
    config, engine = merge_db
    command.upgrade(config, "head")
    assert _versions(engine) == {_head(config)}
    assert len(_tables(engine)) == 32
    with engine.connect() as conn:
        assert compare_metadata(MigrationContext.configure(conn), models.Base.metadata) == []


def test_platform_branch_only_database_upgrades_keeping_roles_and_assignments(merge_db):
    config, engine = merge_db
    command.upgrade(config, PLATFORM_BRANCH)
    assert _versions(engine) == {PLATFORM_BRANCH}
    assert "audit_events" not in _tables(engine)
    with engine.begin() as conn:
        _seed_tenant(conn)
        _seed_platform_user(conn, platform_role="TENANT_MANAGER")
        conn.execute(
            text(
                f"INSERT INTO {ASSIGNMENTS} (user_id, tenant_id) "
                "SELECT user_id, :t FROM app_users WHERE cognito_sub = 'pa-user'"
            ),
            {"t": TENANT_ID},
        )
    command.upgrade(config, "head")
    _assert_merged_head(config, engine, platform_role="TENANT_MANAGER", assignments=1)


def test_audit_branch_only_database_upgrades_with_platform_backfill(merge_db):
    config, engine = merge_db
    command.upgrade(config, AUDIT_BRANCH)
    assert _versions(engine) == {AUDIT_BRANCH}
    assert ASSIGNMENTS not in _tables(engine)
    with engine.begin() as conn:
        _seed_tenant(conn)
        _seed_platform_user(conn)
        _seed_resolved_audit_event(conn)
    command.upgrade(config, "head")
    # 옛 PLATFORM 은 b492 백필 규칙대로 전역 범위(CENTRAL_ADMIN)이고 배정은 만들지 않는다
    _assert_merged_head(config, engine, platform_role="CENTRAL_ADMIN", assignments=0)
    # 병합 전에 남긴 감사 기록은 칸 그대로 남고, 여전히 append-only 다
    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT e.action::text AS action, e.actor_kind::text AS kind, "
                "e.actor_user_id = u.user_id AS actor_is_user, e.incident_id::text AS incident, "
                "e.tenant_id::text AS tenant, e.execution_id, e.trigger_source, "
                "e.resolution::text AS resolution, e.occurred_at "
                "FROM audit_events e JOIN app_users u ON u.cognito_sub = 'pa-user'"
            )
        ).one()
    assert tuple(row) == (
        "INCIDENT_RESOLVED",
        "PLATFORM_USER",
        True,
        INCIDENT_ID,
        TENANT_ID,
        None,
        None,
        "JUSTIFIED",
        OCCURRED_AT,
    )
    with pytest.raises(DBAPIError, match="append-only"), engine.begin() as conn:
        conn.execute(text("DELETE FROM audit_events"))
