"""app_user_feature_permissions migration (#572 5번) — 실제 PostgreSQL.

지키는 것: 직전 리비전 다음 단일 head · 왕복 ORM 일치 · 권한 enum 4값(선언 순서) · 복합 PK 와 app_users CASCADE FK ·
PERMISSIONS_CHANGED 이력 값 · 기존 TENANT OPERATOR 전원(ACTIVE · DISABLED 모두)에게 네 가지 백필, VIEWER · ADMIN ·
PLATFORM 은 행 없음 · 다운그레이드가 표와 enum 을 버리되 이력 값은 남기고, 다시 올리면 부여 내역이 아니라 그 시점의
OPERATOR 전원에게 네 가지를 준다.
"""

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
from db.repositories import tenants as tenants_repo
from sqlalchemy import inspect, text
from sqlalchemy.orm import Session

REVISION = "c572a1b2d905"
PARENT = "a1b7c4d2e905"
ISSUER = "https://cognito-idp.ap-northeast-2.amazonaws.com/pool-feature-permissions"
NOW = datetime(2026, 11, 9, 9, 0, tzinfo=UTC)
LABELS = ["VIEW", "EXECUTE_ACTION", "RESOLVE_INCIDENT", "CREATE_INQUIRY"]
TABLE = "app_user_feature_permissions"
ENUM = "app_user_feature_permission"


@pytest.fixture()
def migration_config(pg_engine, monkeypatch):
    monkeypatch.setenv(
        "DATABASE_URL", pg_engine.url.render_as_string(hide_password=False)
    )
    get_settings.cache_clear()
    yield Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    get_settings.cache_clear()


def _insert_user(conn, sub, *, role, account_type="TENANT", tenant_id=None, status="ACTIVE"):
    """raw INSERT — 부모 리비전 모양(권한 표 없음). PLATFORM 은 CHECK 때문에 CENTRAL_ADMIN 을 싣는다."""
    platform_role = "CENTRAL_ADMIN" if account_type == "PLATFORM" else None
    conn.execute(
        text(
            "INSERT INTO app_users (user_id, issuer, cognito_sub, email, role, account_type, tenant_id, "
            "platform_role, status, created_at, updated_at) VALUES (gen_random_uuid(), :issuer, :sub, "
            ":email, CAST(:role AS app_user_role), CAST(:acct AS app_user_account_type), :tenant, "
            "CAST(:prole AS app_user_platform_role), CAST(:status AS app_user_status), :now, :now)"
        ),
        {
            "issuer": ISSUER,
            "sub": sub,
            "email": f"{sub}@example.com",
            "role": role,
            "acct": account_type,
            "tenant": tenant_id,
            "prole": platform_role,
            "status": status,
            "now": NOW,
        },
    )


def _labels(connection, type_name):
    return (
        connection.execute(text(f"SELECT unnest(enum_range(NULL::{type_name}))::text"))
        .scalars()
        .all()
    )


def _granted(conn, subs):
    """cognito_sub → 부여된 권한(PG enum 정렬 = 선언 순서) 목록. 행이 없으면 빈 목록."""
    rows = conn.execute(
        text(
            f"SELECT u.cognito_sub, p.permission::text FROM app_users u LEFT JOIN {TABLE} p "
            "ON p.user_id = u.user_id WHERE u.issuer = :i AND u.cognito_sub = ANY(:subs) "
            "ORDER BY u.cognito_sub, p.permission"
        ),
        {"i": ISSUER, "subs": list(subs)},
    )
    result: dict[str, list[str]] = {sub: [] for sub in subs}
    for sub, permission in rows:
        if permission is not None:
            result[sub].append(permission)
    return result


def _has_table(conn, name):
    return name in inspect(conn).get_table_names()


def _type_count(conn, name):
    return conn.execute(
        text("SELECT count(*) FROM pg_type WHERE typname = :n"), {"n": name}
    ).scalar_one()


def test_revision_follows_its_parent_on_a_single_chain(migration_config):
    script = ScriptDirectory.from_config(migration_config)
    assert script.get_revision(REVISION).down_revision == PARENT
    assert len(script.get_heads()) == 1


def test_head_schema_matches_orm(pg_engine):
    with pg_engine.connect() as connection:
        assert _labels(connection, ENUM) == LABELS
        assert "PERMISSIONS_CHANGED" in _labels(connection, "user_event_action")
        inspector = inspect(connection)
        assert inspector.get_pk_constraint(TABLE)["constrained_columns"] == ["user_id", "permission"]
        assert {
            (fk["referred_table"], fk["constrained_columns"][0], fk["options"].get("ondelete"))
            for fk in inspector.get_foreign_keys(TABLE)
        } == {("app_users", "user_id", "CASCADE")}
        assert {c["name"]: c["nullable"] for c in inspector.get_columns(TABLE)} == {
            "user_id": False,
            "permission": False,
        }
        assert (
            compare_metadata(MigrationContext.configure(connection), models.Base.metadata) == []
        )


def test_backfill_grants_every_operator_all_four_and_nobody_else(pg_engine, migration_config):
    with Session(pg_engine) as session:
        tenant = tenants_repo.create_tenant(
            session,
            name="feature-permissions",
            aws_account_id="555566667777",
            regions=["ap-northeast-2"],
            now=NOW,
        )
        session.commit()
        tenant_id = tenant.tenant_id
    operators = ("fp-operator", "fp-operator-disabled")
    others = ("fp-viewer", "fp-admin", "fp-platform")
    try:
        command.downgrade(migration_config, PARENT)
        with pg_engine.begin() as conn:
            assert not _has_table(conn, TABLE)
            assert _type_count(conn, ENUM) == 0
            _insert_user(conn, "fp-operator", role="OPERATOR", tenant_id=tenant_id)
            _insert_user(conn, "fp-operator-disabled", role="OPERATOR", tenant_id=tenant_id, status="DISABLED")
            _insert_user(conn, "fp-viewer", role="VIEWER", tenant_id=tenant_id)
            _insert_user(conn, "fp-admin", role="ADMIN", tenant_id=tenant_id)
            _insert_user(conn, "fp-platform", role="ADMIN", account_type="PLATFORM")

        command.upgrade(migration_config, "head")
        with pg_engine.connect() as conn:
            # 이전 OPERATOR 는 네 가지를 모두 할 수 있었다 — 활성 · 비활성 모두 같은 범위로 옮긴다
            assert _granted(conn, operators) == {sub: LABELS for sub in operators}
            assert _granted(conn, others) == {sub: [] for sub in others}

        # 부여분을 줄인 뒤 내리고 올리면 되살리지 않고 그 시점의 OPERATOR 전원에게 다시 네 가지를 준다
        with pg_engine.begin() as conn:
            conn.execute(
                text(
                    f"DELETE FROM {TABLE} WHERE permission <> 'VIEW' AND user_id = "
                    "(SELECT user_id FROM app_users WHERE issuer = :i AND cognito_sub = 'fp-operator')"
                ),
                {"i": ISSUER},
            )
            assert _granted(conn, ("fp-operator",)) == {"fp-operator": ["VIEW"]}

        command.downgrade(migration_config, PARENT)
        with pg_engine.connect() as conn:
            assert not _has_table(conn, TABLE)
            assert _type_count(conn, ENUM) == 0
            # enum 값은 지우지 못한다(이력은 append-only) — 다시 올릴 때 IF NOT EXISTS 로 넘어간다
            assert "PERMISSIONS_CHANGED" in _labels(conn, "user_event_action")

        command.upgrade(migration_config, "head")
        with pg_engine.connect() as conn:
            assert _granted(conn, operators) == {sub: LABELS for sub in operators}
            assert _granted(conn, others) == {sub: [] for sub in others}
    finally:
        command.upgrade(migration_config, "head")
        with pg_engine.begin() as conn:
            conn.execute(text("DELETE FROM app_users WHERE issuer = :i"), {"i": ISSUER})
            conn.execute(text("DELETE FROM tenants WHERE tenant_id = :t"), {"t": tenant_id})
