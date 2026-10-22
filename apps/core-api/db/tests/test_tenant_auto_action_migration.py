"""tenants.auto_action_resumed_at 마이그레이션 (Post-MVP T-6 · #514) — 실제 PostgreSQL/Alembic.

기존 테넌트는 남고 새 칸은 NULL(끈 적 없음)이다 — 지어낸 시각을 채우지 않는다. 왕복 뒤 ORM 과 같다.
"""

from __future__ import annotations

import uuid
from pathlib import Path

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from config import get_settings
from db import models
from sqlalchemy import column, inspect, insert, select, table, text

REVISION = "f514a6c3d201"
PARENT = "e492a3b5c701"


def test_existing_tenants_survive_with_null_resumed_at(pg_engine, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", pg_engine.url.render_as_string(hide_password=False))
    get_settings.cache_clear()
    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    script = ScriptDirectory.from_config(config)
    assert len(script.get_heads()) == 1
    assert script.get_revision(REVISION).down_revision == PARENT

    tenant_id = str(uuid.uuid4())
    command.downgrade(config, PARENT)
    try:
        assert "auto_action_resumed_at" not in {
            c["name"] for c in inspect(pg_engine).get_columns("tenants")}
        # 옛 리비전에는 ORM 이 아니라 그때의 칸으로 넣는다
        old = table("tenants", *(column(name) for name in (
            "tenant_id", "name", "status", "aws_account_id", "access_role_arn", "external_id",
            "regions", "collect_vpc_ids", "auto_action_enabled", "created_at", "updated_at")))
        with pg_engine.begin() as conn:
            conn.execute(insert(old).values(
                tenant_id=tenant_id, name="학원", status=text("'PENDING_CONNECTION'"),
                aws_account_id="123456789012",
                access_role_arn="arn:aws:iam::123456789012:role/vigilantis-access",
                external_id="e" * 32, regions=text("'[\"ap-northeast-2\"]'::jsonb"),
                collect_vpc_ids=text("'[]'::jsonb"), auto_action_enabled=False,
                created_at=text("now()"), updated_at=text("now()"),
            ))
    finally:
        command.upgrade(config, "head")
        get_settings.cache_clear()
    try:
        with pg_engine.connect() as conn:
            row = conn.execute(
                select(models.Tenant.auto_action_enabled, models.Tenant.auto_action_resumed_at)
                .where(models.Tenant.tenant_id == tenant_id)
            ).one()
            assert tuple(row) == (False, None)
            assert compare_metadata(MigrationContext.configure(conn), models.Base.metadata) == []
    finally:
        with pg_engine.begin() as conn:
            conn.execute(text("DELETE FROM tenants WHERE tenant_id = :t"), {"t": tenant_id})
