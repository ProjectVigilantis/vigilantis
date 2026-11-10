"""도입 신청 → 고객사 연결 migration (#572 묶음 B) — 실제 PostgreSQL/Alembic.

지키는 것: 부모 c572e4a9b210 · 단일 head · signup_requests.tenant_id NULL 허용 + tenants FK(SET NULL) · 새 표 없음 ·
옛 승인 건은 NULL 로 남는다(채우지 않는다) · 고객사를 지워도 신청과 append-only 결정 기록이 그대로다 ·
downgrade 는 열만 버리고 신청·결정 행을 보존하며 다시 올려도 ORM 과 일치한다.
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

REVISION = "d572b1a7c3e4"
PARENT = "c572e4a9b210"
TENANT_ID = "66666666-6666-4666-8666-666666666666"
LEGACY_ID = "77777777-7777-4777-8777-777777777777"
LINKED_ID = "88888888-8888-4888-8888-888888888888"


@pytest.fixture()
def scratch_db(pg_engine, monkeypatch):
    """(Config, engine) — 같은 서버의 새 빈 DB. 끝나면 DROP."""
    name = f"vigilantis_signup_tenant_{uuid.uuid4().hex[:8]}"
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


def _seed_tenant(conn):
    conn.execute(
        text(
            "INSERT INTO tenants (tenant_id, name, status, aws_account_id, access_role_arn, "
            "external_id, regions, collect_vpc_ids, auto_action_enabled, created_at, updated_at) "
            "VALUES (:tenant, 'signup-tenant', 'PENDING_CONNECTION', '123456789012', "
            "'arn:aws:iam::123456789012:role/vigilantis-access', :ext, "
            "'[\"ap-northeast-2\"]'::jsonb, '[]'::jsonb, true, now(), now())"
        ),
        {"tenant": TENANT_ID, "ext": "f" * 32},
    )


def _seed_actor(conn) -> str:
    return conn.execute(
        text(
            "INSERT INTO app_users (user_id, issuer, cognito_sub, email, role, account_type, platform_role, "
            "status, created_at, updated_at) VALUES (gen_random_uuid(), 'https://issuer.example/signup', "
            "'signup-actor', 'actor@example.com', CAST('ADMIN' AS app_user_role), "
            "CAST('PLATFORM' AS app_user_account_type), CAST('CENTRAL_ADMIN' AS app_user_platform_role), "
            "CAST('ACTIVE' AS app_user_status), now(), now()) RETURNING user_id::text"
        )
    ).scalar_one()


def _seed_approved_request(conn, request_id: str, actor_id: str, tenant_column: bool):
    cols = ", tenant_id" if tenant_column else ""
    vals = ", :tenant" if tenant_column else ""
    conn.execute(
        text(
            "INSERT INTO signup_requests (request_id, idempotency_key, company_name, contact_name, email, "
            f"status, created_at, updated_at{cols}) VALUES (:r, gen_random_uuid(), '회사', '담당', "
            f"'c@example.com', CAST('APPROVED' AS signup_request_status), now(), now(){vals})"
        ),
        {"r": request_id, "tenant": TENANT_ID},
    )
    conn.execute(
        text(
            "INSERT INTO signup_request_decisions (request_id, actor_user_id, actor_email, decided_at, outcome) "
            "VALUES (:r, :a, 'actor@example.com', now(), CAST('APPROVED' AS signup_request_status))"
        ),
        {"r": request_id, "a": actor_id},
    )


def test_revision_follows_the_previous_head_on_a_single_chain(scratch_db):
    config, _ = scratch_db
    script = ScriptDirectory.from_config(config)
    assert script.get_revision(REVISION).down_revision == PARENT
    assert len(script.get_heads()) == 1


def test_upgrade_adds_nullable_set_null_fk_without_a_new_table_and_matches_orm(scratch_db):
    config, engine = scratch_db
    command.upgrade(config, PARENT)
    before = set(inspect(engine).get_table_names())
    command.upgrade(config, "head")
    with engine.connect() as conn:
        inspector = inspect(conn)
        assert set(inspector.get_table_names()) == before
        column = next(c for c in inspector.get_columns("signup_requests") if c["name"] == "tenant_id")
        assert column["nullable"] is True
        assert {
            (fk["referred_table"], fk["options"].get("ondelete"))
            for fk in inspector.get_foreign_keys("signup_requests")
            if fk["constrained_columns"] == ["tenant_id"]
        } == {("tenants", "SET NULL")}
        assert compare_metadata(MigrationContext.configure(conn), models.Base.metadata) == []


def test_legacy_approved_request_stays_null_after_upgrade(scratch_db):
    config, engine = scratch_db
    command.upgrade(config, PARENT)
    with engine.begin() as conn:
        _seed_approved_request(conn, LEGACY_ID, _seed_actor(conn), tenant_column=False)

    command.upgrade(config, "head")
    with engine.connect() as conn:
        assert conn.execute(
            text("SELECT tenant_id FROM signup_requests WHERE request_id = :r"), {"r": LEGACY_ID}
        ).scalar_one() is None


def test_deleting_a_linked_tenant_keeps_request_and_decision(scratch_db):
    config, engine = scratch_db
    command.upgrade(config, "head")
    with engine.begin() as conn:
        _seed_tenant(conn)
        _seed_approved_request(conn, LINKED_ID, _seed_actor(conn), tenant_column=True)
        assert conn.execute(
            text("SELECT tenant_id::text FROM signup_requests WHERE request_id = :r"), {"r": LINKED_ID}
        ).scalar_one() == TENANT_ID

    with engine.begin() as conn:
        conn.execute(text("DELETE FROM tenants WHERE tenant_id = :t"), {"t": TENANT_ID})

    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT status::text, tenant_id FROM signup_requests WHERE request_id = :r"), {"r": LINKED_ID}
        ).one()
        assert (row[0], row[1]) == ("APPROVED", None)
        decision = conn.execute(
            text("SELECT outcome::text, actor_email FROM signup_request_decisions WHERE request_id = :r"),
            {"r": LINKED_ID},
        ).one()
        assert tuple(decision) == ("APPROVED", "actor@example.com")


def test_downgrade_drops_only_the_column_and_keeps_rows(scratch_db):
    config, engine = scratch_db
    command.upgrade(config, "head")
    with engine.begin() as conn:
        _seed_tenant(conn)
        _seed_approved_request(conn, LINKED_ID, _seed_actor(conn), tenant_column=True)

    command.downgrade(config, PARENT)
    with engine.connect() as conn:
        inspector = inspect(conn)
        assert "tenant_id" not in {c["name"] for c in inspector.get_columns("signup_requests")}
        assert conn.execute(text("SELECT count(*) FROM signup_requests")).scalar_one() == 1
        assert conn.execute(text("SELECT count(*) FROM signup_request_decisions")).scalar_one() == 1
        assert conn.execute(text("SELECT count(*) FROM tenants")).scalar_one() == 1

    command.upgrade(config, "head")
    with engine.connect() as conn:
        assert compare_metadata(MigrationContext.configure(conn), models.Base.metadata) == []
