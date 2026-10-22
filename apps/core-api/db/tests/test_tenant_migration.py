# ==============================================================================
# [파일 설명]  담당: 김승철 (API 계약 · BE)
# tenants 테이블과 뿌리 5개의 tenant_id 마이그레이션 회귀 (Post-MVP T-4 · #485)
#
# 지키는 것 셋.
#   ① 마이그레이션 = ORM — 왕복(downgrade → upgrade) 뒤 compare_metadata 가 비어 있다.
#   ② 기존 행은 남고 tenant_id 는 NULL(MVP 단일 계정 기록)이다 — 지어낸 기본 테넌트로 채우지 않는다.
#   ③ 계약(schemas.api.tenants)의 불변식이 DB CHECK 로도 막힌다 — API 를 거치지 않는 쓰기도 같다.
# ==============================================================================

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import delete, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from config import get_settings
from db import models
from schemas.api.auth import UserRole
from schemas.api.incidents import IncidentCategory
from schemas.api.tenants import TenantStatus, TenantVerificationResult

REVISION = "d4a7c2e9f150"
PARENT = "d493a1b2c302"
ROOTS = ("collection_runs", "assets", "threat_events", "incidents", "action_executions")
# 뿌리 5개(데이터의 소속) + 사용자의 소속(app_users)
TENANT_COLUMN_TABLES = (*ROOTS, "app_users")
ACCOUNT = "475763379672"
ROLE = f"arn:aws:iam::{ACCOUNT}:role/vigilantis-access"


@pytest.fixture()
def migration_config(pg_engine, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", pg_engine.url.render_as_string(hide_password=False))
    get_settings.cache_clear()
    yield Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    get_settings.cache_clear()


def _tenant(**over) -> models.Tenant:
    values = dict(
        name="학원", aws_account_id=ACCOUNT, access_role_arn=ROLE, external_id="e" * 32,
        regions=["ap-northeast-2"],
    )
    values.update(over)
    return models.Tenant(**values)


# ----- ① ② 마이그레이션 ---------------------------------------------------------

def test_revision_follows_notification_deliveries_on_a_single_chain(migration_config):
    script = ScriptDirectory.from_config(migration_config)
    assert script.get_revision(REVISION).down_revision == PARENT
    assert len(script.get_heads()) == 1


def test_roundtrip_matches_orm(pg_engine, migration_config):
    command.downgrade(migration_config, PARENT)
    try:
        with pg_engine.connect() as connection:
            inspector = inspect(connection)
            assert "tenants" not in inspector.get_table_names()
            for table in TENANT_COLUMN_TABLES:
                assert "tenant_id" not in {c["name"] for c in inspector.get_columns(table)}
    finally:
        command.upgrade(migration_config, "head")
    with pg_engine.connect() as connection:
        inspector = inspect(connection)
        for table in TENANT_COLUMN_TABLES:
            column = next(c for c in inspector.get_columns(table) if c["name"] == "tenant_id")
            assert column["nullable"] is True
        assert compare_metadata(MigrationContext.configure(connection), models.Base.metadata) == []
        assert connection.execute(text(
            "SELECT unnest(enum_range(NULL::tenant_status))::text")).scalars().all() == [
            s.value for s in TenantStatus]
        assert connection.execute(text(
            "SELECT unnest(enum_range(NULL::tenant_verification_result))::text")).scalars().all() == [
            r.value for r in TenantVerificationResult]


def test_existing_rows_survive_and_stay_unassigned(pg_engine, migration_config):
    # head 에서 넣은 인시던트가 downgrade(칸 제거) → upgrade(칸 추가) 뒤에도 남고 tenant_id 는 NULL 이다
    with Session(pg_engine) as session:
        incident = models.Incident(subject_arn="arn:aws:ec2:ap-northeast-2:1:instance/i-x",
                                   category=IncidentCategory.FINOPS)
        session.add(incident)
        session.commit()
        incident_id = incident.incident_id
    try:
        command.downgrade(migration_config, PARENT)
        command.upgrade(migration_config, "head")
        with Session(pg_engine) as session:
            stored = session.get(models.Incident, incident_id)
            assert stored is not None and stored.tenant_id is None
            # 지어낸 기본 테넌트가 없다
            assert session.execute(text("SELECT count(*) FROM tenants")).scalar_one() == 0
    finally:
        command.upgrade(migration_config, "head")
        with Session(pg_engine) as session:
            session.execute(delete(models.Incident).where(models.Incident.incident_id == incident_id))
            session.commit()


# ----- ③ 계약 불변식의 DB 거울 ---------------------------------------------------

def test_new_tenant_is_pending_with_defaults(db):
    tenant = _tenant()
    db.add(tenant)
    db.flush()
    assert tenant.status == TenantStatus.PENDING_CONNECTION
    assert tenant.collect_vpc_ids == [] and tenant.auto_action_enabled is True


def test_root_rows_can_point_at_a_tenant(db):
    tenant = _tenant()
    db.add(tenant)
    db.flush()
    incident = models.Incident(subject_arn="arn:aws:ec2:ap-northeast-2:1:instance/i-y",
                               category=IncidentCategory.FINOPS, tenant_id=tenant.tenant_id)
    db.add(incident)
    db.flush()
    assert incident.tenant_id == tenant.tenant_id


def test_unknown_tenant_is_rejected_by_the_foreign_key(db):
    db.add(models.Incident(subject_arn="arn:aws:ec2:ap-northeast-2:1:instance/i-z",
                           category=IncidentCategory.FINOPS,
                           tenant_id="00000000-0000-0000-0000-000000000000"))
    with pytest.raises(IntegrityError):
        db.flush()


VERIFIED = dict(last_verification_result=TenantVerificationResult.OK,
                last_verified_at=models._utcnow())


@pytest.mark.parametrize("over", [
    pytest.param(dict(aws_account_id="47576337967", access_role_arn="arn:aws:iam::47576337967:role/x"),
                 id="account-11-digits"),
    pytest.param(dict(access_role_arn="arn:aws:iam::111122223333:role/vigilantis-access"),
                 id="role-in-another-account"),
    pytest.param(dict(external_id="short"), id="short-external-id"),
    pytest.param(dict(regions=[]), id="no-region"),
    pytest.param(dict(collect_vpc_ids={"vpc": 1}), id="vpc-ids-not-array"),
    pytest.param(dict(status=TenantStatus.ACTIVE), id="active-never-verified"),
    pytest.param(dict(status=TenantStatus.SUSPENDED, suspended_at=models._utcnow()), id="suspended-without-reason"),
    pytest.param(dict(suspended_at=models._utcnow(), suspended_reason="x"), id="suspension-fields-while-pending"),
    pytest.param(dict(last_verification_result=TenantVerificationResult.OK), id="result-without-time"),
    pytest.param(dict(**VERIFIED, last_verification_reason_code="x"), id="ok-with-reason"),
    pytest.param(dict(last_verification_result=TenantVerificationResult.READ_DENIED,
                      last_verified_at=models._utcnow()), id="failure-without-reason"),
    pytest.param(dict(period_start=date(2026, 12, 31), period_end=date(2026, 10, 1)), id="period-reversed"),
])
def test_contract_invariants_are_checked_by_the_database(db, over):
    db.add(_tenant(**over))
    with pytest.raises(IntegrityError):
        db.flush()


def test_valid_active_and_suspended_shapes_pass(db):
    active = _tenant(status=TenantStatus.ACTIVE, **VERIFIED)
    # ACTIVE 뒤의 확인 실패는 표현할 수 있다 — 상태를 자동으로 내리지 않는다
    failed_later = _tenant(
        aws_account_id="111122223333", access_role_arn="arn:aws:iam::111122223333:role/vigilantis-access",
        external_id="f" * 32, status=TenantStatus.ACTIVE,
        last_verification_result=TenantVerificationResult.ASSUME_DENIED,
        last_verified_at=models._utcnow(), last_verification_reason_code="AccessDenied")
    suspended = _tenant(
        aws_account_id="222233334444", access_role_arn="arn:aws:iam::222233334444:role/vigilantis-access",
        external_id="g" * 32, status=TenantStatus.SUSPENDED, **VERIFIED,
        suspended_at=models._utcnow(), suspended_reason="계약 만료")
    db.add_all([active, failed_later, suspended])
    db.flush()


@pytest.mark.parametrize("column,value", [("aws_account_id", ACCOUNT), ("external_id", "e" * 32)])
def test_account_and_external_id_are_unique(db, column, value):
    db.add(_tenant())
    db.flush()
    other = dict(aws_account_id="111122223333",
                 access_role_arn="arn:aws:iam::111122223333:role/vigilantis-access", external_id="h" * 32)
    other[column] = value
    if column == "aws_account_id":
        other["access_role_arn"] = ROLE
    db.add(_tenant(**other))
    with pytest.raises(IntegrityError):
        db.flush()


# ----- 사용자의 소속 (PR #506 리뷰 · PR #504 「맡기는 일 3」) -----

def _user(**over) -> models.AppUser:
    values = dict(issuer="https://cognito-idp.ap-northeast-2.amazonaws.com/pool",
                  cognito_sub="sub-1", email="a@example.com", role=UserRole.VIEWER)
    values.update(over)
    return models.AppUser(**values)


def test_user_belongs_to_at_most_one_tenant(db):
    tenant = _tenant()
    db.add(tenant)
    db.flush()
    member = _user(tenant_id=tenant.tenant_id)
    unassigned = _user(cognito_sub="sub-2")  # 소속 없음 — 권한 해석은 T-1 몫이다
    db.add_all([member, unassigned])
    db.flush()
    assert member.tenant_id == tenant.tenant_id and unassigned.tenant_id is None


def test_user_pointing_at_an_unknown_tenant_is_rejected(db):
    db.add(_user(tenant_id="00000000-0000-0000-0000-000000000000"))
    with pytest.raises(IntegrityError):
        db.flush()


def test_tenant_with_users_cannot_be_deleted(db):
    # 연결 전 테넌트는 지울 수 있지만(DELETABLE_STATUSES), 사용자가 있으면 FK 가 막는다 —
    # 그래서 고객사 사용자는 연결(ACTIVE) 뒤에 초대한다(PR #504 「맡기는 일 3」 추천)
    tenant = _tenant()
    db.add(tenant)
    db.flush()
    db.add(_user(tenant_id=tenant.tenant_id))
    db.flush()
    db.delete(tenant)
    with pytest.raises(IntegrityError):
        db.flush()


def test_existing_users_stay_unassigned_after_upgrade(pg_engine, migration_config):
    with Session(pg_engine) as session:
        user = _user(cognito_sub="sub-migration")
        session.add(user)
        session.commit()
        user_id = user.user_id
    try:
        command.downgrade(migration_config, PARENT)
        command.upgrade(migration_config, "head")
        with Session(pg_engine) as session:
            stored = session.get(models.AppUser, user_id)
            assert stored is not None and stored.tenant_id is None
    finally:
        command.upgrade(migration_config, "head")
        with Session(pg_engine) as session:
            session.execute(delete(models.AppUser).where(models.AppUser.user_id == user_id))
            session.commit()
