# ==============================================================================
# [파일 설명]  담당: 김승철 (API 계약 · BE)
# 연결 확인 결과 GUARDDUTY_DISABLED 마이그레이션 회귀 (T-14 B안 · #588 B)
#
#   ① 자기 revision의 enum 값 순서를 확인하고, 왕복 뒤 head의 compare_metadata 가 비어 있다.
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
from config import get_settings
from db import models
from schemas.api.tenants import TenantVerificationResult
from sqlalchemy import insert, text
from sqlalchemy.exc import IntegrityError

REVISION = "d588e4b1c2a7"
PARENT = "c589e7a1b2d3"
ACCOUNT = "475763379672"
_RESULTS = "SELECT unnest(enum_range(NULL::tenant_verification_result))::text"
_REVISION_RESULTS = [
    "OK", "ASSUME_DENIED", "ACCOUNT_MISMATCH", "READ_DENIED", "GUARDDUTY_DISABLED", "UNREACHABLE",
]


@pytest.fixture()
def migration_config(pg_engine, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", pg_engine.url.render_as_string(hide_password=False))
    get_settings.cache_clear()
    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    # 후속 revision의 결과를 이 revision의 계약으로 검사하지 않는다.
    command.downgrade(config, REVISION)
    try:
        yield config
    finally:
        command.upgrade(config, "head")
        get_settings.cache_clear()


def _add_tenant(pg_engine, result: TenantVerificationResult, reason: str) -> None:
    # 값을 준 칸만 넣는다 — 이 revision 뒤에 생긴 칸(ORM 에는 있다)을 INSERT 에 싣지 않게 Core 로 쓴다
    with pg_engine.begin() as connection:
        connection.execute(insert(models.Tenant).values(
            name="학원", aws_account_id=ACCOUNT,
            access_role_arn=f"arn:aws:iam::{ACCOUNT}:role/vigilantis-access",
            external_id="e" * 32, regions=["ap-northeast-2"], last_verification_result=result,
            last_verified_at=datetime.now(UTC), last_verification_reason_code=reason,
        ))


def test_enum_follows_the_contract_and_roundtrips(pg_engine, migration_config):
    with pg_engine.connect() as connection:
        assert connection.execute(text(_RESULTS)).scalars().all() == _REVISION_RESULTS

    command.downgrade(migration_config, PARENT)
    try:
        with pg_engine.connect() as connection:
            assert "GUARDDUTY_DISABLED" not in connection.execute(text(_RESULTS)).scalars().all()
    finally:
        command.upgrade(migration_config, REVISION)
    with pg_engine.connect() as connection:
        assert connection.execute(text(_RESULTS)).scalars().all() == _REVISION_RESULTS
    # ORM과 현재 enum 계약은 후속 revision까지 적용한 head와 대조한다.
    command.upgrade(migration_config, "head")
    with pg_engine.connect() as connection:
        assert connection.execute(text(_RESULTS)).scalars().all() == [r.value for r in TenantVerificationResult]
        assert compare_metadata(MigrationContext.configure(connection), models.Base.metadata) == []


def test_downgrade_refuses_while_a_guardduty_observation_remains(pg_engine, migration_config):
    _add_tenant(pg_engine, TenantVerificationResult.GUARDDUTY_DISABLED, "DetectorDisabled")
    try:
        with pytest.raises(RuntimeError, match="GUARDDUTY_DISABLED"):
            command.downgrade(migration_config, PARENT)
        with pg_engine.connect() as connection:
            assert "GUARDDUTY_DISABLED" in connection.execute(text(_RESULTS)).scalars().all()
    finally:
        with pg_engine.begin() as connection:
            connection.execute(text("DELETE FROM tenants"))


def test_downgrade_keeps_other_observations_and_the_shape_check(pg_engine, migration_config):
    _add_tenant(pg_engine, TenantVerificationResult.READ_DENIED, "AccessDenied")
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
        command.upgrade(migration_config, REVISION)
