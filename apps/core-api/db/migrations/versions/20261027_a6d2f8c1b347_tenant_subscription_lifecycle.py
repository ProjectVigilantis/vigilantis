"""tenants 구독 칸 4개 + tenant_subscription_history — 고객 구독 관리 (Post-MVP T-16 · #528)

1. tenants 에 구독의 현재 사실 4칸: cancel_at_period_end(Free 전환 예약) · billing_anchor_day(자동 갱신
   기준일) · amount_minor · currency(등록된 요금). 계약 TenantSubscription 의 불변식을 CHECK 로 거울한다.
2. tenant_subscription_history — 재개(연결 확인 성공)로 Free 가 되기 전의 구독을 남기는 최소 기록.

**기존 행은 그대로 둔다.** plan · period_start · period_end 는 손대지 않고(옛 값 basic · pilot 포함),
NULL plan 을 Free 로 채우지 않는다 — 「등록 안 됨」과 Free 는 다른 사실이다. 새 칸은 예약 없음(false) ·
기준일 없음 · 요금 미정(NULL)이라 기존 구독은 갱신 · 전환 대상이 되지 않는다. 지어낸 요금 · 기준일을 채우지 않는다.

Revision ID: a6d2f8c1b347
Revises: f514a6c3d201
Create Date: 2026-10-27 10:00:00.000000
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a6d2f8c1b347"
down_revision = "f514a6c3d201"
branch_labels = None
depends_on = None

_UUID = sa.Uuid(as_uuid=False)

# db.models._SUBSCRIPTION_SHAPE 와 같은 식이다(이름은 ck_tenants_<이름>)
_SHAPE_CHECKS = {
    "amount_with_currency": "(amount_minor IS NULL) = (currency IS NULL)",
    "amount_range": "amount_minor IS NULL OR amount_minor BETWEEN 0 AND 9007199254740991",
    "currency_format": "currency IS NULL OR currency ~ '^[A-Z]{3}$'",
    "billing_anchor_day_range": "billing_anchor_day IS NULL OR billing_anchor_day BETWEEN 1 AND 31",
    "billing_anchor_with_period": (
        "billing_anchor_day IS NULL OR (period_start IS NOT NULL AND period_end IS NOT NULL)"
    ),
    "cancel_with_period_end": "NOT cancel_at_period_end OR period_end IS NOT NULL",
}


def upgrade() -> None:
    op.add_column(
        "tenants",
        sa.Column("cancel_at_period_end", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    op.add_column("tenants", sa.Column("billing_anchor_day", sa.SmallInteger(), nullable=True))
    op.add_column("tenants", sa.Column("amount_minor", sa.BigInteger(), nullable=True))
    op.add_column("tenants", sa.Column("currency", sa.String(length=3), nullable=True))
    for name, sql in _SHAPE_CHECKS.items():
        op.create_check_constraint(op.f(f"ck_tenants_{name}"), "tenants", sql)

    op.create_table(
        "tenant_subscription_history",
        sa.Column("history_id", _UUID, nullable=False),
        sa.Column("tenant_id", _UUID, nullable=False),
        sa.Column("plan", sa.String(length=50), nullable=True),
        sa.Column("period_start", sa.Date(), nullable=True),
        sa.Column("period_end", sa.Date(), nullable=True),
        sa.Column("cancel_at_period_end", sa.Boolean(), nullable=False),
        sa.Column("billing_anchor_day", sa.SmallInteger(), nullable=True),
        sa.Column("amount_minor", sa.BigInteger(), nullable=True),
        sa.Column("currency", sa.String(length=3), nullable=True),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.tenant_id"],
            name=op.f("fk_tenant_subscription_history_tenant_id_tenants"),
        ),
        sa.PrimaryKeyConstraint("history_id", name=op.f("pk_tenant_subscription_history")),
    )
    op.create_index(
        op.f("ix_tenant_subscription_history_tenant_id"), "tenant_subscription_history", ["tenant_id"]
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_tenant_subscription_history_tenant_id"), table_name="tenant_subscription_history")
    op.drop_table("tenant_subscription_history")
    for name in reversed(_SHAPE_CHECKS):
        op.drop_constraint(op.f(f"ck_tenants_{name}"), "tenants", type_="check")
    for column in ("currency", "amount_minor", "billing_anchor_day", "cancel_at_period_end"):
        op.drop_column("tenants", column)
