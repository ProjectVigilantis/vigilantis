"""app_users.account_type migration — 실제 PostgreSQL.

지키는 것: 단일 head · 왕복 ORM 일치 · 기존 행 분류(승격 없음) · 식별자 보존 ·
CHECK 3종 · 플래그 누락 INSERT 거절 · 기본값 없음 · 다운그레이드 행 보존.
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
from schemas.api.tenants import TenantVerificationResult
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

REVISION = "f492c4a8d2e1"
PARENT = "e492a3b5c701"
ISSUER = "https://cognito-idp.ap-northeast-2.amazonaws.com/pool-acct"
NOW = datetime(2026, 10, 22, 9, 0, tzinfo=UTC)
ENUM_LABELS = ["PLATFORM", "TENANT", "UNASSIGNED"]


@pytest.fixture()
def migration_config(pg_engine, monkeypatch):
    monkeypatch.setenv(
        "DATABASE_URL", pg_engine.url.render_as_string(hide_password=False)
    )
    get_settings.cache_clear()
    yield Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    get_settings.cache_clear()


def _insert_user(conn, sub, role="VIEWER", tenant_id=None, account_type=None):
    """raw INSERT — account_type 가 None 이면 칸 자체를 빼서(옛 리비전 모양) 넣는다."""
    cols = "user_id, issuer, cognito_sub, email, role, tenant_id, status, created_at, updated_at"
    vals = (
        "gen_random_uuid(), :issuer, :sub, :email, CAST(:role AS app_user_role), "
        ":tenant, CAST('ACTIVE' AS app_user_status), :now, :now"
    )
    if account_type is not None:
        cols += ", account_type"
        vals += ", CAST(:acct AS app_user_account_type)"
    conn.execute(
        text(f"INSERT INTO app_users ({cols}) VALUES ({vals})"),
        {
            "issuer": ISSUER,
            "sub": sub,
            "email": f"{sub}@example.com",
            "role": role,
            "tenant": tenant_id,
            "now": NOW,
            "acct": account_type,
        },
    )


def test_revision_follows_user_admin_events_on_a_single_chain(migration_config):
    script = ScriptDirectory.from_config(migration_config)
    assert script.get_revision(REVISION).down_revision == PARENT
    assert len(script.get_heads()) == 1


def test_head_schema_matches_orm_with_no_default(pg_engine):
    # pg_engine = 새 DB 에 base → head 로 올린 스키마
    with pg_engine.connect() as connection:
        column = next(
            c
            for c in inspect(connection).get_columns("app_users")
            if c["name"] == "account_type"
        )
        assert column["nullable"] is False
        assert column["default"] is None
        assert (
            connection.execute(
                text("SELECT unnest(enum_range(NULL::app_user_account_type))::text")
            )
            .scalars()
            .all()
            == ENUM_LABELS
        )
        assert (
            compare_metadata(
                MigrationContext.configure(connection), models.Base.metadata
            )
            == []
        )


def test_legacy_rows_are_classified_never_promoted_and_survive_roundtrip(
    pg_engine, migration_config
):
    with Session(pg_engine) as session:
        tenant = tenants_repo.create_tenant(
            session,
            name="legacy",
            aws_account_id="333344445555",
            regions=["ap-northeast-2"],
            now=NOW,
        )
        assert tenants_repo.record_verification(
            session,
            tenant.tenant_id,
            result=TenantVerificationResult.OK,
            reason_code=None,
            now=NOW,
        )
        session.commit()
        tenant_id = tenant.tenant_id
    subs = ("legacy-null", "legacy-admin", "legacy-member")
    try:
        command.downgrade(migration_config, PARENT)
        with pg_engine.begin() as conn:
            assert "account_type" not in {
                c["name"] for c in inspect(conn).get_columns("app_users")
            }
            _insert_user(conn, subs[0])  # 소속 없음, VIEWER
            _insert_user(conn, subs[1], role="ADMIN")  # 소속 없는 옛 ADMIN — 승격되지 않는다
            _insert_user(conn, subs[2], tenant_id=tenant_id)
        command.upgrade(migration_config, "head")
        with pg_engine.connect() as conn:
            rows = {
                r.cognito_sub: r
                for r in conn.execute(
                    text(
                        "SELECT issuer, cognito_sub, role::text AS role, tenant_id::text AS tenant_id, "
                        "account_type::text AS acct FROM app_users "
                        "WHERE cognito_sub = ANY(:subs)"
                    ),
                    {"subs": list(subs)},
                )
            }
        assert {s: (r.acct, r.role, r.tenant_id) for s, r in rows.items()} == {
            "legacy-null": ("UNASSIGNED", "VIEWER", None),
            "legacy-admin": ("UNASSIGNED", "ADMIN", None),
            "legacy-member": ("TENANT", "VIEWER", tenant_id),
        }
        assert {r.issuer for r in rows.values()} == {ISSUER}
        # 다운그레이드도 행·식별자를 남긴다
        command.downgrade(migration_config, PARENT)
        with pg_engine.connect() as conn:
            kept = conn.execute(
                text(
                    "SELECT cognito_sub, tenant_id::text AS tenant_id FROM app_users "
                    "WHERE cognito_sub = ANY(:subs) ORDER BY cognito_sub"
                ),
                {"subs": list(subs)},
            ).all()
            assert [(r.cognito_sub, r.tenant_id) for r in kept] == [
                ("legacy-admin", None),
                ("legacy-member", tenant_id),
                ("legacy-null", None),
            ]
            assert (
                conn.execute(
                    text(
                        "SELECT count(*) FROM pg_type WHERE typname = 'app_user_account_type'"
                    )
                ).scalar_one()
                == 0
            )
    finally:
        command.upgrade(migration_config, "head")
        with pg_engine.begin() as conn:
            conn.execute(
                text("DELETE FROM app_users WHERE cognito_sub = ANY(:subs)"),
                {"subs": list(subs)},
            )
            conn.execute(
                text("DELETE FROM tenants WHERE tenant_id = :t"), {"t": tenant_id}
            )


def test_insert_without_account_type_is_rejected(db):
    with pytest.raises(IntegrityError, match="account_type"), db.begin_nested():
        _insert_user(db, "no-flag")


@pytest.mark.parametrize(
    "account_type,role,has_tenant",
    [
        pytest.param("PLATFORM", "ADMIN", True, id="platform-with-tenant"),
        pytest.param("UNASSIGNED", "VIEWER", True, id="unassigned-with-tenant"),
        pytest.param("TENANT", "VIEWER", False, id="tenant-without-tenant"),
        pytest.param("PLATFORM", "VIEWER", False, id="platform-not-admin"),
        pytest.param("PLATFORM", "OPERATOR", False, id="platform-operator"),
    ],
)
def test_check_constraints_reject_bad_shapes(db, account_type, role, has_tenant):
    tenant_id = None
    if has_tenant:
        tenant = tenants_repo.create_tenant(
            db, name="t", aws_account_id="444455556666", regions=["ap-northeast-2"], now=NOW
        )
        tenant_id = tenant.tenant_id
    with pytest.raises(IntegrityError), db.begin_nested():
        _insert_user(
            db, "bad", role=role, tenant_id=tenant_id, account_type=account_type
        )


def test_check_constraints_accept_the_three_valid_shapes(db):
    tenant = tenants_repo.create_tenant(
        db, name="t", aws_account_id="444455556666", regions=["ap-northeast-2"], now=NOW
    )
    tenants_repo.record_verification(
        db, tenant.tenant_id, result=TenantVerificationResult.OK, reason_code=None, now=NOW
    )
    _insert_user(db, "p", role="ADMIN", account_type="PLATFORM")
    _insert_user(db, "u", account_type="UNASSIGNED")
    _insert_user(db, "t", tenant_id=tenant.tenant_id, account_type="TENANT")
    assert (
        db.execute(
            text("SELECT count(*) FROM app_users WHERE issuer = :i"), {"i": ISSUER}
        ).scalar_one()
        == 3
    )
