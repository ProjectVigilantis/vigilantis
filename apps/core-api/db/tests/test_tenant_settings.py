"""T-7 tenant_settings · tenant_settings_cleanups — 실제 PostgreSQL · Alembic.

지키는 것: 리비전 사슬(병합 d492a544e001 바로 뒤 · 단일 head) · 왕복 · ORM 일치 · 행 모양 CHECK ·
규칙 판정 공유 인터페이스(load_protected_tag_policies) · 정리 대기열의 임대 · 준비 비밀 소비 경합.
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
from db.repositories import tenant_settings as repo
from db.repositories import tenants as tenants_repo
from sqlalchemy import create_engine, func, inspect, select, text, update
from sqlalchemy.exc import IntegrityError

REVISION = "f7c1a9e4d201"
PARENT = "d492a544e001"
TABLES = {"tenant_settings", "tenant_settings_cleanups", "notification_legacy_routes"}
NOW = datetime(2026, 10, 31, 9, 0, tzinfo=UTC)
RULES = [{"key": "Env", "values": ["Prod", " live "]}]


@pytest.fixture()
def scratch_db(pg_engine, monkeypatch):
    name = f"vigilantis_t7_{uuid.uuid4().hex[:8]}"
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


def _tenant(db) -> str:
    account = f"{uuid.uuid4().int % 10**12:012d}"
    return tenants_repo.create_tenant(
        db, name="t7", aws_account_id=account, regions=["ap-northeast-2"], now=NOW,
    ).tenant_id


def _rejected(db, **values) -> None:
    tenant_id = _tenant(db)
    with pytest.raises(IntegrityError), db.begin_nested():
        db.add(models.TenantSettingsRecord(tenant_id=tenant_id, updated_at=NOW, **values))
        db.flush()


# --- 리비전 ------------------------------------------------------------------------


def test_revision_follows_the_role_audit_merge_as_single_head():
    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    script = ScriptDirectory.from_config(config)
    assert script.get_revision(REVISION).down_revision == PARENT
    [head] = script.get_heads()
    assert REVISION in set(script.get_revision("f8c1a9e4f515").down_revision)
    ancestors = {rev.revision for rev in script.walk_revisions(base="base", head=head)}
    assert {REVISION, "f8c1a9e4f515"} <= ancestors


def test_roundtrip_matches_orm(scratch_db):
    config, engine = scratch_db
    command.upgrade(config, "head")
    command.downgrade(config, PARENT)
    with engine.connect() as conn:
        assert not TABLES & set(inspect(conn).get_table_names())
    command.upgrade(config, "head")
    with engine.connect() as conn:
        assert TABLES <= set(inspect(conn).get_table_names())
        assert compare_metadata(MigrationContext.configure(conn), models.Base.metadata) == []


# --- 행 모양 ------------------------------------------------------------------------


def test_new_settings_row_is_all_undecided(db):
    tenant_id = _tenant(db)
    db.add(repo.new_record(tenant_id, updated_at=NOW))
    db.flush()
    row = repo.get(db, tenant_id)
    assert (row.protected_tags, row.slack_source, row.email_source, row.email_attempts) == (
        None, None, None, 0,
    )


@pytest.mark.parametrize("values", [
    {"protected_tags": {"key": "env"}},
    {"slack_source": "MANAGED", "slack_secret_ref": "arn", "slack_destination_hash": "a" * 64},
    {"slack_source": "LEGACY_ENV"},
    {"slack_source": "CLEARED", "slack_secret_ref": "SLACK_A"},
    {"slack_source": "UNKNOWN"},
    {"email_source": "LEGACY_TOPIC"},
    {"email_source": "CLEARED", "email_address": "a@example.com"},
    {"email_source": "MANAGED", "email_address": "a@example.com", "email_generation": str(uuid.uuid4()),
     "email_region": "ap-northeast-2", "email_status": "READY", "email_topic_arn": "arn:t"},
    {"email_source": "MANAGED", "email_address": "a@example.com", "email_generation": str(uuid.uuid4()),
     "email_region": "ap-northeast-2", "email_status": "SETUP_FAILED"},
    {"email_source": "MANAGED", "email_address": "a@example.com", "email_generation": str(uuid.uuid4()),
     "email_region": "ap-northeast-2", "email_status": "SETUP_PENDING", "email_failure_code": "X"},
    {"email_source": "MANAGED", "email_address": "a@example.com", "email_generation": str(uuid.uuid4()),
     "email_region": "ap-northeast-2", "email_status": "SETUP_PENDING",
     "email_lease_token": str(uuid.uuid4())},
])
def test_inconsistent_rows_are_rejected(db, values):
    _rejected(db, **values)


def test_settings_row_requires_an_existing_tenant(db):
    with pytest.raises(IntegrityError), db.begin_nested():
        db.add(repo.new_record(str(uuid.uuid4()), updated_at=NOW))
        db.flush()


# --- 규칙 판정 공유 인터페이스 --------------------------------------------------------


def test_protected_tag_policies_distinguish_absent_default_and_explicit_none(db):
    absent, default, none, custom = (_tenant(db) for _ in range(4))
    for tenant_id, tags in ((default, None), (none, []), (custom, RULES)):
        record = repo.new_record(tenant_id, updated_at=NOW)
        record.protected_tags = tags
        db.add(record)
    db.flush()

    queried = [absent, default, none, custom.upper(), "not-a-uuid", uuid.UUID(custom).hex]
    policies = repo.load_protected_tag_policies(db, queried)
    assert policies == {default: None, none: [], custom: RULES}
    # 결과는 호출마다 새 값이다 — 호출자가 고쳐도 다음 조회 · 저장 값에 번지지 않는다
    policies[custom][0]["values"].append("changed")
    assert repo.load_protected_tag_policies(db, [custom]) == {custom: RULES}
    assert repo.load_protected_tag_policies(db, []) == {}
    assert repo.load_protected_tag_policies(db, ["x"]) == {}


def test_protected_tag_policies_use_one_query(db):
    tenant_ids = [_tenant(db) for _ in range(3)]
    for tenant_id in tenant_ids:
        db.add(repo.new_record(tenant_id, updated_at=NOW))
    db.flush()
    statements = []
    from sqlalchemy import event

    def count(_conn, _cursor, statement, *_args):
        statements.append(statement)

    connection = db.connection()
    event.listen(connection, "before_cursor_execute", count)
    try:
        assert set(repo.load_protected_tag_policies(db, tenant_ids)) == set(tenant_ids)
    finally:
        event.remove(connection, "before_cursor_execute", count)
    assert len(statements) == 1


# --- 정리 대기열 ----------------------------------------------------------------------


def _queue(db, tenant_id, *, purpose="RETIRED", delay=0):
    return repo.queue_cleanup(
        db, tenant_id=tenant_id, resource_kind="SECRET", purpose=purpose,
        region="ap-northeast-2", resource_name=f"vigilantis/t7/{tenant_id}/slack-{uuid.uuid4().hex}",
        delay_seconds=delay,
    )


def _make_due(db):
    db.execute(
        update(models.TenantSettingsCleanup).values(not_before=func.now())
        .execution_options(synchronize_session=False)
    )


def _cleanup_rows(db):
    db.expire_all()
    return db.scalars(select(models.TenantSettingsCleanup)).all()


def test_claim_is_a_lease_and_stale_results_are_fenced(db):
    tenant_id = _tenant(db)
    _queue(db, tenant_id)
    [claim] = repo.claim_cleanups(db, 10, lease_seconds=60)
    assert claim.attempts == 1
    assert repo.claim_cleanups(db, 10, lease_seconds=60) == []  # 임대 중에는 다시 잡히지 않는다
    _make_due(db)
    [again] = repo.claim_cleanups(db, 10, lease_seconds=60)  # 임대 만료 뒤 재claim
    assert again.attempts == 2
    assert repo.finish_cleanup(db, claim) is False  # 앞선 claim 의 늦은 결과는 반영되지 않는다
    repo.retry_cleanup(db, claim, delay_seconds=0, error_code="stale")
    assert [row.last_error_code for row in _cleanup_rows(db)] == [None]
    repo.retry_cleanup(db, again, delay_seconds=300, error_code="secretsmanager_Throttling")
    assert [row.last_error_code for row in _cleanup_rows(db)] == ["secretsmanager_Throttling"]
    assert repo.finish_cleanup(db, again) is True
    assert _cleanup_rows(db) == []


def test_staging_is_consumed_only_before_cleanup_claims_it(db):
    tenant_id = _tenant(db)
    kept = _queue(db, tenant_id, purpose="STAGING", delay=900)
    assert repo.claim_cleanups(db, 10, lease_seconds=60) == []  # 유예 중
    assert repo.consume_staging(db, kept) is True
    lost = _queue(db, tenant_id, purpose="STAGING", delay=900)
    repo.expedite_staging(db, lost)
    [claim] = repo.claim_cleanups(db, 10, lease_seconds=60)
    assert claim.cleanup_id == lost
    assert repo.consume_staging(db, lost) is False  # 정리가 먼저 잡았으면 저장은 그 비밀을 쓰지 않는다


def test_same_resource_is_queued_once(db):
    tenant_id = _tenant(db)
    name = f"vigilantis/t7/{tenant_id}/slack-{uuid.uuid4().hex}"
    values = {"tenant_id": tenant_id, "resource_kind": "SECRET", "purpose": "RETIRED",
              "region": "ap-northeast-2", "resource_name": name}
    assert repo.queue_cleanup(db, **values) is not None
    assert repo.queue_cleanup(db, **values) is None
    assert db.scalar(select(func.count()).select_from(models.TenantSettingsCleanup)) == 1
