# ==============================================================================
# [파일 설명]  담당: 김승철 (API 계약 · BE)
# 연결 확인 기록의 GuardDuty 실패 리전 목록 마이그레이션 회귀 (T-14 · #588 D)
#
#   ① 왕복: downgrade 가 칸과 CHECK 를 지우고, 다시 올린 head 의 compare_metadata 가 비어 있다.
#   ② 이 revision 전에 남은 GUARDDUTY_DISABLED 는 목록 없이(NULL) 그대로 남는다 — 지어내 채우지 않는다.
#   ③ CHECK: 목록은 GUARDDUTY_DISABLED 에만, 비어 있지 않은 배열로만 들어간다.
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
from sqlalchemy import inspect, text
from sqlalchemy.exc import DBAPIError

REVISION = "f588d1a2b3c4"
PARENT = "d588e4b1c2a7"
ACCOUNT = "475763379672"
COLUMN = "last_verification_guardduty_failures"
SHAPE = "ck_tenants_verification_guardduty_failures_shape"
# 이 revision 전에도 있던 칸만 쓴다 — 부모 revision 에서도 같은 문장으로 넣는다
_INSERT = text(
    "INSERT INTO tenants (tenant_id, name, status, aws_account_id, access_role_arn, external_id, regions,"
    " collect_vpc_ids, auto_action_enabled, cancel_at_period_end, last_verification_result, last_verified_at,"
    " last_verification_reason_code, created_at, updated_at)"
    " VALUES (gen_random_uuid(), 'academy', 'PENDING_CONNECTION', :account,"
    " :role_arn, repeat('e', 32),"
    " CAST(:regions AS jsonb), CAST('[]' AS jsonb), true, false,"
    " CAST(:result AS tenant_verification_result), :now, :reason, :now, :now)"
)
_ONE_REGION = '[{"region": "ap-northeast-2", "reason_code": "DetectorDisabled"}]'


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
        with pg_engine.begin() as connection:
            connection.execute(text("DELETE FROM tenants"))
        command.upgrade(config, "head")
        get_settings.cache_clear()


def _add(pg_engine, result: str, reason: str) -> None:
    with pg_engine.begin() as connection:
        connection.execute(_INSERT, {
            "account": ACCOUNT, "role_arn": f"arn:aws:iam::{ACCOUNT}:role/vigilantis-access",
            "regions": '["ap-northeast-2"]', "result": result, "reason": reason,
            "now": datetime.now(UTC),
        })


def _columns(pg_engine) -> set[str]:
    return {column["name"] for column in inspect(pg_engine).get_columns("tenants")}


def _checks(pg_engine) -> set[str]:
    return {check["name"] for check in inspect(pg_engine).get_check_constraints("tenants")}


def test_roundtrip_drops_and_restores_the_column_and_its_check(pg_engine, migration_config):
    assert COLUMN in _columns(pg_engine) and SHAPE in _checks(pg_engine)

    command.downgrade(migration_config, PARENT)
    try:
        assert COLUMN not in _columns(pg_engine) and SHAPE not in _checks(pg_engine)
    finally:
        command.upgrade(migration_config, REVISION)

    assert COLUMN in _columns(pg_engine) and SHAPE in _checks(pg_engine)
    command.upgrade(migration_config, "head")
    with pg_engine.connect() as connection:
        assert compare_metadata(MigrationContext.configure(connection), models.Base.metadata) == []


def test_an_earlier_guardduty_observation_keeps_its_result_with_no_region_list(pg_engine, migration_config):
    command.downgrade(migration_config, PARENT)
    try:
        _add(pg_engine, "GUARDDUTY_DISABLED", "DetectorDisabled")
    finally:
        command.upgrade(migration_config, REVISION)

    with pg_engine.connect() as connection:
        row = connection.execute(text(
            f"SELECT last_verification_result::text, last_verification_reason_code, {COLUMN} FROM tenants"
        )).one()
    assert tuple(row) == ("GUARDDUTY_DISABLED", "DetectorDisabled", None)


@pytest.mark.parametrize("result,reason,value,accepted", [
    ("GUARDDUTY_DISABLED", "DetectorDisabled", _ONE_REGION, True),
    ("GUARDDUTY_DISABLED", "DetectorDisabled", "[]", False),  # 빈 목록
    ("GUARDDUTY_DISABLED", "DetectorDisabled", '{"region": "ap-northeast-2"}', False),  # 배열이 아님
    ("READ_DENIED", "AccessDenied", _ONE_REGION, False),  # 다른 결과
], ids=["list", "empty", "object", "other-result"])
def test_check_allows_the_list_only_on_guardduty_disabled(
    pg_engine, migration_config, result, reason, value, accepted
):
    _add(pg_engine, result, reason)
    update = text(f"UPDATE tenants SET {COLUMN} = CAST(:value AS jsonb)")

    if accepted:
        with pg_engine.begin() as connection:
            connection.execute(update, {"value": value})
        return
    # 배열이 아닌 값은 CHECK 위반이나 jsonb_array_length 오류로 거절된다 — 어느 쪽이든 들어가지 않는다
    with pytest.raises(DBAPIError), pg_engine.begin() as connection:
        connection.execute(update, {"value": value})
    with pg_engine.connect() as connection:
        assert connection.execute(text(f"SELECT {COLUMN} FROM tenants")).scalar_one() is None
