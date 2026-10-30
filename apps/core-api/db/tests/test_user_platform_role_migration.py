"""app_users.platform_role + 담당 고객사 배정 migration (#492 PR 5) — 실제 PostgreSQL.

지키는 것: 병합 리비전 다음 단일 head · 왕복 ORM 일치(서버 기본값 없음) · 2등급 enum · 기존 PLATFORM →
CENTRAL_ADMIN 백필(이전과 같은 전역 범위) · TENANT · UNASSIGNED 는 NULL · 배정 백필 없음 · CHECK ·
PLATFORM_ROLE_CHANGED · TENANT_ASSIGNMENTS_CHANGED 이력 값 · 다운그레이드가 배정 범위의 TENANT_MANAGER 를
UNASSIGNED 로 내리고(전역 PLATFORM 으로 넓히지 않는다) 다시 올려도 배정 · 등급을 되살리지 않는다.
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
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

REVISION = "b492e7d1a605"
PARENT = "c492d6e8f4a2"
ISSUER = "https://cognito-idp.ap-northeast-2.amazonaws.com/pool-platform-role"
NOW = datetime(2026, 10, 29, 9, 0, tzinfo=UTC)
LABELS = ["CENTRAL_ADMIN", "TENANT_MANAGER"]
ASSIGNMENTS = "app_user_tenant_assignments"


@pytest.fixture()
def migration_config(pg_engine, monkeypatch):
    monkeypatch.setenv(
        "DATABASE_URL", pg_engine.url.render_as_string(hide_password=False)
    )
    get_settings.cache_clear()
    yield Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    get_settings.cache_clear()


def _insert_user(conn, sub, *, account_type, role="ADMIN", tenant_id=None, platform_role=None):
    """raw INSERT — platform_role 이 None 이면 칸 자체를 뺀다(부모 리비전 모양 · NULL)."""
    cols = (
        "user_id, issuer, cognito_sub, email, role, account_type, tenant_id, status, "
        "created_at, updated_at"
    )
    vals = (
        "gen_random_uuid(), :issuer, :sub, :email, CAST(:role AS app_user_role), "
        "CAST(:acct AS app_user_account_type), :tenant, CAST('ACTIVE' AS app_user_status), :now, :now"
    )
    if platform_role is not None:
        cols += ", platform_role"
        vals += ", CAST(:prole AS app_user_platform_role)"
    conn.execute(
        text(f"INSERT INTO app_users ({cols}) VALUES ({vals})"),
        {
            "issuer": ISSUER,
            "sub": sub,
            "email": f"{sub}@example.com",
            "role": role,
            "acct": account_type,
            "tenant": tenant_id,
            "now": NOW,
            "prole": platform_role,
        },
    )


def _labels(connection, type_name):
    return (
        connection.execute(
            text(f"SELECT unnest(enum_range(NULL::{type_name}))::text")
        )
        .scalars()
        .all()
    )


def _assign(conn, sub, tenant_id):
    conn.execute(
        text(
            f"INSERT INTO {ASSIGNMENTS} (user_id, tenant_id) "
            "SELECT user_id, :t FROM app_users WHERE issuer = :i AND cognito_sub = :s"
        ),
        {"t": tenant_id, "i": ISSUER, "s": sub},
    )


def _assignment_count(conn):
    return conn.execute(
        text(
            f"SELECT count(*) FROM {ASSIGNMENTS} a JOIN app_users u ON u.user_id = a.user_id "
            "WHERE u.issuer = :i"
        ),
        {"i": ISSUER},
    ).scalar_one()


def _has_table(conn, name):
    return name in inspect(conn).get_table_names()


def _rows(conn, subs, *, with_platform_role=True):
    platform_role = "platform_role::text" if with_platform_role else "NULL"
    return {
        r.cognito_sub: (r.acct, r.prole)
        for r in conn.execute(
            text(
                f"SELECT cognito_sub, account_type::text AS acct, {platform_role} AS prole "
                "FROM app_users WHERE issuer = :i AND cognito_sub = ANY(:subs)"
            ),
            {"i": ISSUER, "subs": list(subs)},
        )
    }


def test_revision_follows_account_scope_merge_on_a_single_chain(migration_config):
    script = ScriptDirectory.from_config(migration_config)
    assert script.get_revision(REVISION).down_revision == PARENT
    assert len(script.get_heads()) == 1


def test_head_schema_matches_orm_with_no_default(pg_engine):
    with pg_engine.connect() as connection:
        column = next(
            c
            for c in inspect(connection).get_columns("app_users")
            if c["name"] == "platform_role"
        )
        assert column["nullable"] is True
        assert column["default"] is None
        assert _labels(connection, "app_user_platform_role") == LABELS
        assert {"PLATFORM_ROLE_CHANGED", "TENANT_ASSIGNMENTS_CHANGED"} <= set(
            _labels(connection, "user_event_action")
        )
        inspector = inspect(connection)
        assert inspector.get_pk_constraint(ASSIGNMENTS)["constrained_columns"] == [
            "user_id",
            "tenant_id",
        ]
        assert {
            (fk["referred_table"], fk["options"].get("ondelete"))
            for fk in inspector.get_foreign_keys(ASSIGNMENTS)
        } == {("app_users", "CASCADE"), ("tenants", "CASCADE")}
        assert (
            compare_metadata(
                MigrationContext.configure(connection), models.Base.metadata
            )
            == []
        )


def test_backfill_keeps_global_scope_and_downgrade_never_widens_assignment_scope(
    pg_engine, migration_config
):
    with Session(pg_engine) as session:
        tenant = tenants_repo.create_tenant(
            session,
            name="platform-role",
            aws_account_id="666677778888",
            regions=["ap-northeast-2"],
            now=NOW,
        )
        session.commit()
        tenant_id = tenant.tenant_id
    legacy = ("pr-platform", "pr-tenant", "pr-unassigned")
    graded = ("pr-central", "pr-manager")
    try:
        command.downgrade(migration_config, PARENT)
        with pg_engine.begin() as conn:
            assert "platform_role" not in {
                c["name"] for c in inspect(conn).get_columns("app_users")
            }
            # 부모 리비전의 모양 — PLATFORM 은 모두 같은 권한이었다(role=ADMIN 호환 값)
            _insert_user(conn, "pr-platform", account_type="PLATFORM")
            _insert_user(conn, "pr-tenant", account_type="TENANT", tenant_id=tenant_id)
            _insert_user(conn, "pr-unassigned", account_type="UNASSIGNED")
        command.upgrade(migration_config, "head")
        with pg_engine.begin() as conn:
            # 이전 PLATFORM 은 전역 권한이었다 — 같은 범위(CENTRAL_ADMIN)로 옮기고, 소속 · 미지정은 등급이 없다
            assert _rows(conn, legacy) == {
                "pr-platform": ("PLATFORM", "CENTRAL_ADMIN"),
                "pr-tenant": ("TENANT", None),
                "pr-unassigned": ("UNASSIGNED", None),
            }
            assert _assignment_count(conn) == 0  # 소속 · 계정 종류로 배정을 만들지 않는다
            _insert_user(conn, "pr-central", account_type="PLATFORM", platform_role="CENTRAL_ADMIN")
            _insert_user(conn, "pr-manager", account_type="PLATFORM", platform_role="TENANT_MANAGER")
            _assign(conn, "pr-manager", tenant_id)

        command.downgrade(migration_config, PARENT)
        with pg_engine.connect() as conn:
            # 배정 범위의 담당자를 이전 스키마의 전역 PLATFORM 으로 남기지 않는다 — 접근 없음으로 내린다
            assert _rows(conn, legacy + graded, with_platform_role=False) == {
                "pr-platform": ("PLATFORM", None),
                "pr-tenant": ("TENANT", None),
                "pr-unassigned": ("UNASSIGNED", None),
                "pr-central": ("PLATFORM", None),
                "pr-manager": ("UNASSIGNED", None),
            }
            assert not _has_table(conn, ASSIGNMENTS)
            assert (
                conn.execute(
                    text("SELECT count(*) FROM pg_type WHERE typname = 'app_user_platform_role'")
                ).scalar_one()
                == 0
            )
            # enum 값은 지우지 못한다(이력은 append-only) — 다시 올릴 때 IF NOT EXISTS 로 넘어간다
            assert {"PLATFORM_ROLE_CHANGED", "TENANT_ASSIGNMENTS_CHANGED"} <= set(
                _labels(conn, "user_event_action")
            )

        command.upgrade(migration_config, "head")
        with pg_engine.connect() as conn:
            # 다시 올려도 내린 담당자와 배정을 되살리지 않는다 — 남은 PLATFORM 만 다시 CENTRAL_ADMIN 이다
            assert _rows(conn, legacy + graded) == {
                "pr-platform": ("PLATFORM", "CENTRAL_ADMIN"),
                "pr-tenant": ("TENANT", None),
                "pr-unassigned": ("UNASSIGNED", None),
                "pr-central": ("PLATFORM", "CENTRAL_ADMIN"),
                "pr-manager": ("UNASSIGNED", None),
            }
            assert _assignment_count(conn) == 0
    finally:
        command.upgrade(migration_config, "head")
        with pg_engine.begin() as conn:
            conn.execute(text("DELETE FROM app_users WHERE issuer = :i"), {"i": ISSUER})
            conn.execute(text("DELETE FROM tenants WHERE tenant_id = :t"), {"t": tenant_id})


@pytest.mark.parametrize(
    "account_type,platform_role,with_tenant",
    [
        pytest.param("PLATFORM", None, False, id="platform-without-role"),
        pytest.param("TENANT", "TENANT_MANAGER", True, id="tenant-with-role"),
        pytest.param("UNASSIGNED", "CENTRAL_ADMIN", False, id="unassigned-with-role"),
    ],
)
def test_check_requires_platform_role_exactly_on_platform(db, account_type, platform_role, with_tenant):
    tenant_id = None
    if with_tenant:
        tenant_id = tenants_repo.create_tenant(
            db, name="t", aws_account_id="777788889999", regions=["ap-northeast-2"], now=NOW
        ).tenant_id
    with pytest.raises(IntegrityError, match="platform_role_by_account_type"), db.begin_nested():
        _insert_user(
            db, "bad", account_type=account_type, tenant_id=tenant_id, platform_role=platform_role
        )


def test_check_accepts_every_platform_role_on_platform(db):
    for label in LABELS:
        _insert_user(db, f"ok-{label}", account_type="PLATFORM", platform_role=label)
    assert (
        db.execute(
            text("SELECT count(*) FROM app_users WHERE issuer = :i"), {"i": ISSUER}
        ).scalar_one()
        == len(LABELS)
    )
