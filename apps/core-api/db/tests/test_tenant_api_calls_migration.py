"""tenant_api_calls_hourly 마이그레이션 (#572 6번) — 실제 PostgreSQL/Alembic.

내려가면 표가 없고 올라오면 ORM 과 같다. 같은 (고객사, 정시)의 UPSERT 는 한 행에 더해지고, 고객사를 지우면
통계도 함께 지워진다(CASCADE). 음수 호출 수와 정시가 아닌 칸은 DB 가 거절한다.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import delete, inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from config import get_settings
from db import models
from db.repositories import platform as platform_repo
from db.repositories import tenants as tenants_repo

REVISION = "d572a6c1e906"
PARENT = "c572a1b2d905"  # dev #601 기능 권한 표 — 병합 뒤 그 뒤로 잇는다
TABLE = "tenant_api_calls_hourly"


def test_round_trip_matches_orm(pg_engine, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", pg_engine.url.render_as_string(hide_password=False))
    get_settings.cache_clear()
    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    script = ScriptDirectory.from_config(config)
    assert len(script.get_heads()) == 1
    assert script.get_revision(REVISION).down_revision == PARENT

    command.downgrade(config, PARENT)
    try:
        assert TABLE not in inspect(pg_engine).get_table_names()
    finally:
        command.upgrade(config, "head")
        get_settings.cache_clear()
    with pg_engine.connect() as conn:
        assert TABLE in inspect(conn).get_table_names()
        assert compare_metadata(MigrationContext.configure(conn), models.Base.metadata) == []


def test_upsert_adds_to_one_bucket_and_cascades_with_tenant(pg_engine):
    noon = datetime(2031, 6, 15, 12, tzinfo=UTC)
    earlier = noon - timedelta(hours=1)
    with Session(pg_engine) as session:
        tenant = tenants_repo.create_tenant(
            session, name="통계 고객사", aws_account_id="210987654321", regions=["ap-northeast-2"],
            now=datetime.now(UTC),
        )
        tenant_id = tenant.tenant_id
        session.commit()
    try:
        with Session(pg_engine) as session:
            platform_repo.add_call(session, tenant_id=tenant_id, hour=noon)
            platform_repo.add_call(session, tenant_id=tenant_id, hour=noon)
            platform_repo.add_call(session, tenant_id=tenant_id, hour=earlier)
            session.commit()
            rows = session.execute(
                select(models.TenantApiCallHourly.hour, models.TenantApiCallHourly.calls)
                .where(models.TenantApiCallHourly.tenant_id == tenant_id)
                .order_by(models.TenantApiCallHourly.hour)
            ).all()
            assert [(row.hour.astimezone(UTC), row.calls) for row in rows] == [(earlier, 1), (noon, 2)]

        for bad in (
            dict(hour=noon - timedelta(hours=2), calls=-1),  # 음수
            dict(hour=noon + timedelta(minutes=30), calls=1),  # 정시 아님
        ):
            with Session(pg_engine) as session:
                session.add(models.TenantApiCallHourly(tenant_id=tenant_id, **bad))
                with pytest.raises(IntegrityError):
                    session.flush()
                session.rollback()
    finally:
        with Session(pg_engine) as session:
            session.execute(delete(models.Tenant).where(models.Tenant.tenant_id == tenant_id))
            session.commit()
    with Session(pg_engine) as session:
        assert session.scalar(
            select(models.TenantApiCallHourly.calls).where(models.TenantApiCallHourly.tenant_id == tenant_id)
        ) is None
