"""tenant_api_calls_daily — 고객사별 API 호출 수의 UTC 일 단위 버킷 (#572 6번)

고객사(TENANT) 계정이 세션을 통과해 `/api/*` 를 부른 횟수를 고객사 × 날짜로 쌓는다(api_usage 미들웨어).
기존 호출은 센 적이 없으므로 행을 만들지 않는다 — 지어낸 수를 채우지 않는다.
고객사가 지워지면 통계도 함께 지워진다(CASCADE).

Revision ID: d572a6c1e906
Revises: a1b7c4d2e905
Create Date: 2026-11-09 10:00:00.000000
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "d572a6c1e906"
down_revision = "a1b7c4d2e905"
branch_labels = None
depends_on = None

_UUID = sa.Uuid(as_uuid=False)


def upgrade() -> None:
    op.create_table(
        "tenant_api_calls_daily",
        sa.Column("tenant_id", _UUID, nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("calls", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "day", name=op.f("pk_tenant_api_calls_daily")),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.tenant_id"],
            name=op.f("fk_tenant_api_calls_daily_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        sa.CheckConstraint("calls >= 0", name=op.f("ck_tenant_api_calls_daily_calls_nonnegative")),
    )
    op.create_index("ix_tenant_api_calls_daily_day", "tenant_api_calls_daily", ["day"])


def downgrade() -> None:
    # 통계는 30일 보관 집계라 되돌릴 때 함께 버린다 — 손실 없는 downgrade 를 주장하지 않는다
    op.drop_index("ix_tenant_api_calls_daily_day", table_name="tenant_api_calls_daily")
    op.drop_table("tenant_api_calls_daily")
