# ==============================================================================
# [파일 설명]  담당: 김승철 (API 계약 · BE)
# 연결 확인 결과 GUARDDUTY_DISABLED 마이그레이션 회귀 (T-14 B안 · #588 B)
#
#   ① enum 값 순서 = 계약(TenantVerificationResult)이고, 왕복 뒤 compare_metadata 가 비어 있다.
#   ② 그 값의 관측이 남아 있으면 downgrade 를 거절한다(다른 값으로 바꿔 적지 않는다).
#   ③ downgrade 가 내렸다 다시 건 verification_shape CHECK 가 그대로 막는다.
# ==============================================================================

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from config import get_settings
from db import models
from schemas.api.tenants import TenantVerificationResult

REVISION = "d588e4b1c2a7"
PARENT = "f609f610e001"
ACCOUNT = "475763379672"
_RESULTS = "SELECT unnest(enum_range(NULL::tenant_verification_result))::text"


@pytest.fixture()
def migration_config(pg_engine, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", pg_engine.url.render_as_string(hide_password=False))
    get_settings.cache_clear()
    yield Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    get_settings.cache_clear()


def _tenant(result: TenantVerificationResult | None = None, reason: str | None = None) -> models.Tenant:
    return models.Tenant(
        name="학원", aws_account_id=ACCOUNT, access_role_arn=f"arn:aws:iam::{ACCOUNT}:role/vigilantis-access",
        external_id="e" * 32, regions=["ap-northeast-2"], last_verification_result=result,
        last_verified_at=None if result is None else datetime.now(UTC), last_verification_reason_code=reason,
    )


def test_enum_follows_the_contract_and_roundtrips(pg_engine, migration_config):
    with pg_engine.connect() as connection:
        assert connection.execute(text(_RESULTS)).scalars().all() == [r.value for r in TenantVerificationResult]

    command.downgrade(migration_config, PARENT)
    try:
        with pg_engine.connect() as connection:
            assert "GUARDDUTY_DISABLED" not in connection.execute(text(_RESULTS)).scalars().all()
    finally:
        command.upgrade(migration_config, "head")
    with pg_engine.connect() as connection:
        assert connection.execute(text(_RESULTS)).scalars().all() == [r.value for r in TenantVerificationResult]
        assert compare_metadata(MigrationContext.configure(connection), models.Base.metadata) == []


def test_downgrade_refuses_while_a_guardduty_observation_remains(pg_engine, migration_config):
    with Session(pg_engine) as session:
        session.add(_tenant(TenantVerificationResult.GUARDDUTY_DISABLED, "DetectorDisabled"))
        session.commit()
    try:
        with pytest.raises(RuntimeError, match="GUARDDUTY_DISABLED"):
            command.downgrade(migration_config, PARENT)
        with pg_engine.connect() as connection:
            assert "GUARDDUTY_DISABLED" in connection.execute(text(_RESULTS)).scalars().all()
    finally:
        with pg_engine.begin() as connection:
            connection.execute(text("DELETE FROM tenants"))


def test_downgrade_keeps_other_observations_and_the_shape_check(pg_engine, migration_config):
    with Session(pg_engine) as session:
        session.add(_tenant(TenantVerificationResult.READ_DENIED, "AccessDenied"))
        session.commit()
    command.downgrade(migration_config, PARENT)
    try:
        with pg_engine.connect() as connection:
            assert connection.execute(text(
                "SELECT last_verification_result::text FROM tenants")).scalar_one() == "READ_DENIED"
        with pytest.raises(IntegrityError, match="verification_shape"), pg_engine.begin() as connection:
            # 실패인데 사유가 없다 — 다시 건 CHECK 가 막는다
            connection.execute(text("UPDATE tenants SET last_verification_reason_code = NULL"))
    finally:
        with pg_engine.begin() as connection:
            connection.execute(text("DELETE FROM tenants"))
        command.upgrade(migration_config, "head")
