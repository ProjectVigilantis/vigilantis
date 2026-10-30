"""TOTP 분실 복구 migration (#515) — 실제 PostgreSQL.

지키는 것: 플랫폼 등급 · 감사 기록 병합 리비전을 부모로 두고 고객 접수(#552)와 합쳐지는 단일 head · 왕복 ORM 일치 · 기존 사용자는 세대 0 · NONE(서버 기본값) ·
작업 행 CHECK · 차단 중 사용자 · 풀리지 않은 신원 변경 울타리가 있으면 downgrade 거절(이전 스키마로 차단 · 울타리가
풀리지 않게) · TOTP_RESET 세션은 downgrade 때 ADMIN_REVOKED 로 바뀐다 · 풀리지 않은 울타리는 신원당 1개 · 울타리 CHECK.
"""

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from config import get_settings
from db import models
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

REVISION = "d515a7c3e912"
PARENT = "d492a544e001"
ISSUER = "https://cognito-idp.ap-northeast-2.amazonaws.com/pool-totp-recovery"
NOW = datetime(2026, 10, 29, 9, 0, tzinfo=UTC)


@pytest.fixture()
def migration_config(pg_engine, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", pg_engine.url.render_as_string(hide_password=False))
    get_settings.cache_clear()
    yield Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    get_settings.cache_clear()


def _insert_platform_user(conn, sub) -> str:
    """raw INSERT — #515 칸을 모르는 쓰기(부모 리비전 모양)."""
    return conn.execute(
        text(
            "INSERT INTO app_users (user_id, issuer, cognito_sub, email, role, account_type, platform_role, "
            "status, created_at, updated_at) VALUES (gen_random_uuid(), :i, :s, :e, CAST('ADMIN' AS app_user_role), "
            "CAST('PLATFORM' AS app_user_account_type), CAST('TENANT_MANAGER' AS app_user_platform_role), "
            "CAST('ACTIVE' AS app_user_status), :n, :n) RETURNING user_id"
        ),
        {"i": ISSUER, "s": sub, "e": f"{sub}@example.com", "n": NOW},
    ).scalar_one()


def _insert_fence(conn, sub, *, resolution=None):
    conn.execute(
        text(
            "INSERT INTO totp_provider_mutations (mutation_id, issuer, cognito_sub, kind, started_at, resolution, "
            "resolved_at) VALUES (gen_random_uuid(), :i, :s, 'ENROLLMENT', :n, "
            "CAST(:r AS totp_provider_mutation_resolution), :ra)"
        ),
        {"i": ISSUER, "s": sub, "n": NOW, "r": resolution, "ra": NOW if resolution else None},
    )


def _cleanup(pg_engine):
    with pg_engine.begin() as conn:
        if "totp_provider_mutations" in inspect(conn).get_table_names():
            conn.execute(text("DELETE FROM totp_provider_mutations WHERE issuer = :i"), {"i": ISSUER})
        if "totp_recovery_operations" in inspect(conn).get_table_names():
            conn.execute(
                text(
                    "DELETE FROM totp_recovery_operations WHERE target_user_id IN "
                    "(SELECT user_id FROM app_users WHERE issuer = :i)"
                ),
                {"i": ISSUER},
            )
        conn.execute(
            text("DELETE FROM auth_sessions WHERE user_id IN (SELECT user_id FROM app_users WHERE issuer = :i)"),
            {"i": ISSUER},
        )
        conn.execute(text("DELETE FROM app_users WHERE issuer = :i"), {"i": ISSUER})


def test_revision_follows_platform_audit_merge_and_joins_the_single_head(migration_config):
    script = ScriptDirectory.from_config(migration_config)
    assert script.get_revision(REVISION).down_revision == PARENT
    assert set(script.get_revision(PARENT).down_revision) == {"b492e7d1a605", "b544a7d1e2f3"}
    assert script.get_heads() == ["f515a552e001"]


def test_head_schema_matches_orm_and_existing_rows_get_generation_zero(pg_engine, migration_config):
    try:
        command.downgrade(migration_config, PARENT)
        with pg_engine.begin() as conn:
            columns = {c["name"] for c in inspect(conn).get_columns("app_users")}
            assert {"auth_generation", "mfa_recovery_state"}.isdisjoint(columns)
            tables = set(inspect(conn).get_table_names())
            assert {"app_user_tenant_assignments", "audit_events"} <= tables
            assert {"totp_recovery_operations", "totp_provider_mutations"}.isdisjoint(tables)
            legacy = _insert_platform_user(conn, "legacy")
        command.upgrade(migration_config, "head")
        with pg_engine.begin() as conn:
            assert conn.execute(
                text("SELECT auth_generation, mfa_recovery_state::text FROM app_users WHERE user_id = :u"),
                {"u": legacy},
            ).one() == (0, "NONE")
            assert (
                compare_metadata(MigrationContext.configure(conn), models.Base.metadata) == []
            )
            # 이 칸을 모르는 쓰기도 세대 0 · NONE 으로 들어간다
            later = _insert_platform_user(conn, "later")
            assert conn.execute(
                text("SELECT auth_generation, mfa_recovery_state::text FROM app_users WHERE user_id = :u"),
                {"u": later},
            ).one() == (0, "NONE")
    finally:
        command.upgrade(migration_config, "head")
        _cleanup(pg_engine)


def test_downgrade_is_refused_while_any_user_is_blocked_by_recovery(pg_engine, migration_config):
    with pg_engine.begin() as conn:
        user = _insert_platform_user(conn, "blocked")
        conn.execute(
            text(
                "UPDATE app_users SET auth_generation = 1, "
                "mfa_recovery_state = 'RESET_PENDING' WHERE user_id = :u"
            ),
            {"u": user},
        )
    try:
        with pytest.raises(RuntimeError, match="TOTP"):
            command.downgrade(migration_config, PARENT)
        with pg_engine.connect() as conn:  # 그대로 head 다
            assert "totp_recovery_operations" in inspect(conn).get_table_names()
            assert conn.execute(
                text("SELECT mfa_recovery_state::text FROM app_users WHERE user_id = :u"), {"u": user}
            ).scalar_one() == "RESET_PENDING"
    finally:
        command.upgrade(migration_config, "head")
        _cleanup(pg_engine)


def test_downgrade_is_refused_while_an_identity_fence_is_unresolved(pg_engine, migration_config):
    with pg_engine.begin() as conn:
        _insert_fence(conn, "fenced")
    try:
        with pytest.raises(RuntimeError, match="울타리"):
            command.downgrade(migration_config, PARENT)
        with pg_engine.connect() as conn:  # 그대로 head 다
            assert "totp_provider_mutations" in inspect(conn).get_table_names()
    finally:
        command.upgrade(migration_config, "head")
        _cleanup(pg_engine)


def test_one_unresolved_fence_per_identity(db):
    _insert_fence(db, "one", resolution="PROVIDER_RETURNED")
    _insert_fence(db, "one")  # 풀린 울타리는 막지 않는다
    with pytest.raises(IntegrityError, match="uq_totp_provider_mutations_unresolved_identity"), db.begin_nested():
        _insert_fence(db, "one")
    _insert_fence(db, "two")  # 다른 신원은 따로다


@pytest.mark.parametrize(
    "kind,operation,resolution,resolved,reconciler,constraint",
    [
        ("RESET", False, None, False, False, "operation_by_kind"),
        ("ENROLLMENT", True, None, False, False, "operation_by_kind"),
        ("ENROLLMENT", False, "PROVIDER_RETURNED", False, False, "resolution_shape"),
        ("ENROLLMENT", False, None, True, False, "resolution_shape"),
        ("ENROLLMENT", False, "PROVIDER_RETURNED", True, True, "reconciler_shape"),
        ("ENROLLMENT", False, None, False, True, "reconciler_shape"),
    ],
)
def test_fence_rows_keep_kind_resolution_and_reconciler_shapes(
    db, kind, operation, resolution, resolved, reconciler, constraint
):
    user = _insert_platform_user(db, "fence-check")
    with pytest.raises(IntegrityError, match=constraint), db.begin_nested():
        db.execute(
            text(
                "INSERT INTO totp_provider_mutations (mutation_id, issuer, cognito_sub, kind, operation_id, "
                "started_at, resolution, resolved_at, reconciled_by) VALUES (gen_random_uuid(), :i, 'fence-check', "
                "CAST(:k AS totp_provider_mutation_kind), :o, :n, CAST(:r AS totp_provider_mutation_resolution), "
                ":ra, :b)"
            ),
            {
                "i": ISSUER,
                "k": kind,
                "o": str(uuid.uuid4()) if operation else None,
                "n": NOW,
                "r": resolution,
                "ra": NOW if resolved else None,
                "b": user if reconciler else None,
            },
        )


def test_downgrade_maps_totp_reset_sessions_for_the_previous_schema(pg_engine, migration_config):
    with pg_engine.begin() as conn:
        user = _insert_platform_user(conn, "revoked")
        conn.execute(
            text(
                "INSERT INTO auth_sessions (session_id, token_hash, user_id, mfa_step, created_at, "
                "last_activity_at, absolute_expires_at, revoked_at, revoke_reason) VALUES (gen_random_uuid(), "
                ":h, :u, 'SOFTWARE_TOKEN_MFA', :n, :n, :e, :n, 'TOTP_RESET')"
            ),
            {"h": "a" * 64, "u": user, "n": NOW, "e": NOW + timedelta(hours=8)},
        )
    try:
        command.downgrade(migration_config, PARENT)
        with pg_engine.connect() as conn:
            assert conn.execute(
                text("SELECT revoke_reason::text FROM auth_sessions WHERE user_id = :u"), {"u": user}
            ).scalar_one() == "ADMIN_REVOKED"
    finally:
        command.upgrade(migration_config, "head")
        _cleanup(pg_engine)


@pytest.mark.parametrize(
    "identity_verified,status,finalized,constraint",
    [
        (False, "PROVIDER_PENDING", False, "identity_verified"),
        (True, "COMPLETED", False, "finalized_shape"),
        (True, "PROVIDER_PENDING", True, "finalized_shape"),
    ],
)
def test_operation_rows_require_attestation_and_a_consistent_final_shape(
    db, identity_verified, status, finalized, constraint
):
    user = _insert_platform_user(db, "op-check")
    with pytest.raises(IntegrityError, match=constraint), db.begin_nested():
        db.execute(
            text(
                "INSERT INTO totp_recovery_operations (operation_id, target_user_id, actor_user_id, "
                "identity_verified, generation, status, revoked_session_count, created_at, finalized_at) "
                "VALUES (gen_random_uuid(), :u, NULL, :v, 1, CAST(:s AS totp_recovery_status), 0, :n, :f)"
            ),
            {"u": user, "v": identity_verified, "s": status, "n": NOW, "f": NOW if finalized else None},
        )
