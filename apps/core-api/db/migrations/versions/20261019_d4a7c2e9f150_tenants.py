"""tenants + 뿌리 테이블 5개의 tenant_id — 고객 계정 연결의 DB 1단계 (Post-MVP T-4)

1. tenants — 고객 계정 1개. 공개 계약 schemas.api.tenants(#485)의 불변식을 CHECK 로 거울한다:
   계정 ID 12자리 · 역할이 그 계정에 있음 · ExternalId 16자 이상 · 리전 1개 이상 · 확인 결과와
   시각의 짝(사유는 실패일 때만) · ACTIVE 는 확인 기록이 있음 · 정지 기록은 SUSPENDED 일 때만 ·
   구독 기간 순서. 계정 ID 와 ExternalId 는 각각 유일하다.
2. collection_runs · assets · threat_events · incidents · action_executions 에 nullable tenant_id
   (FK · 인덱스). 나머지 테이블은 FK 로 따라온다.

**기존 행은 NULL 로 둔다 — NULL = MVP 단일 계정 기록이다.** 마이그레이션이 계정 ID · 역할 ·
ExternalId 를 지어내 기본 테넌트를 만들지 않는다(연결 확인 없는 ACTIVE 가 된다). 채우기와
NOT NULL 전환은 그 계정의 테넌트가 실제로 연결된 뒤의 단계다.

Revision ID: d4a7c2e9f150
Revises: d493a1b2c302
Create Date: 2026-10-19 15:00:00.000000
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "d4a7c2e9f150"
down_revision = "d493a1b2c302"
branch_labels = None
depends_on = None

_UUID = sa.Uuid(as_uuid=False)
_JSONB = postgresql.JSONB(astext_type=sa.Text())
_STATUS = ("PENDING_CONNECTION", "ACTIVE", "SUSPENDED")
_RESULT = ("OK", "ASSUME_DENIED", "ACCOUNT_MISMATCH", "READ_DENIED", "UNREACHABLE")
ROOT_TABLES = ("collection_runs", "assets", "threat_events", "incidents", "action_executions")


def upgrade() -> None:
    bind = op.get_bind()
    postgresql.ENUM(*_STATUS, name="tenant_status").create(bind, checkfirst=True)
    postgresql.ENUM(*_RESULT, name="tenant_verification_result").create(bind, checkfirst=True)

    op.create_table(
        "tenants",
        sa.Column("tenant_id", _UUID, nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("status", postgresql.ENUM(name="tenant_status", create_type=False), nullable=False),
        sa.Column("aws_account_id", sa.String(length=12), nullable=False),
        sa.Column("access_role_arn", sa.String(length=2048), nullable=False),
        sa.Column("external_id", sa.String(length=128), nullable=False),
        sa.Column("regions", _JSONB, nullable=False),
        sa.Column("collect_vpc_ids", _JSONB, nullable=False),
        sa.Column("auto_action_enabled", sa.Boolean(), nullable=False),
        sa.Column(
            "last_verification_result",
            postgresql.ENUM(name="tenant_verification_result", create_type=False),
            nullable=True,
        ),
        sa.Column("last_verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_verification_reason_code", sa.String(length=200), nullable=True),
        sa.Column("plan", sa.String(length=50), nullable=True),
        sa.Column("period_start", sa.Date(), nullable=True),
        sa.Column("period_end", sa.Date(), nullable=True),
        sa.Column("suspended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("suspended_reason", sa.String(length=500), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("aws_account_id ~ '^[0-9]{12}$'", name=op.f("ck_tenants_account_id_format")),
        sa.CheckConstraint(
            "split_part(access_role_arn, ':', 5) = aws_account_id",
            name=op.f("ck_tenants_role_in_tenant_account"),
        ),
        sa.CheckConstraint("length(external_id) >= 16", name=op.f("ck_tenants_external_id_min_length")),
        sa.CheckConstraint(
            "jsonb_typeof(regions) = 'array' AND jsonb_array_length(regions) > 0",
            name=op.f("ck_tenants_regions_non_empty_array"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(collect_vpc_ids) = 'array'", name=op.f("ck_tenants_collect_vpc_ids_is_array")
        ),
        sa.CheckConstraint(
            "((last_verification_result IS NULL) = (last_verified_at IS NULL))"
            " AND ((last_verification_result IS NULL OR last_verification_result = 'OK')"
            " = (last_verification_reason_code IS NULL))",
            name=op.f("ck_tenants_verification_shape"),
        ),
        sa.CheckConstraint(
            "status <> 'ACTIVE' OR last_verified_at IS NOT NULL", name=op.f("ck_tenants_active_was_verified")
        ),
        sa.CheckConstraint(
            "((status = 'SUSPENDED') = (suspended_at IS NOT NULL))"
            " AND ((status = 'SUSPENDED') = (suspended_reason IS NOT NULL))",
            name=op.f("ck_tenants_suspension_with_status"),
        ),
        sa.CheckConstraint(
            "period_start IS NULL OR period_end IS NULL OR period_end >= period_start",
            name=op.f("ck_tenants_period_ordered"),
        ),
        sa.PrimaryKeyConstraint("tenant_id", name=op.f("pk_tenants")),
        sa.UniqueConstraint("aws_account_id", name=op.f("uq_tenants_aws_account_id")),
        sa.UniqueConstraint("external_id", name=op.f("uq_tenants_external_id")),
    )

    for table in ROOT_TABLES:
        op.add_column(table, sa.Column("tenant_id", _UUID, nullable=True))
        op.create_foreign_key(
            op.f(f"fk_{table}_tenant_id_tenants"), table, "tenants", ["tenant_id"], ["tenant_id"]
        )
        op.create_index(op.f(f"ix_{table}_tenant_id"), table, ["tenant_id"])


def downgrade() -> None:
    for table in reversed(ROOT_TABLES):
        op.drop_index(op.f(f"ix_{table}_tenant_id"), table_name=table)
        op.drop_constraint(op.f(f"fk_{table}_tenant_id_tenants"), table, type_="foreignkey")
        op.drop_column(table, "tenant_id")
    op.drop_table("tenants")
    bind = op.get_bind()
    postgresql.ENUM(name="tenant_verification_result").drop(bind, checkfirst=True)
    postgresql.ENUM(name="tenant_status").drop(bind, checkfirst=True)
