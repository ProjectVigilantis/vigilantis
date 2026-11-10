"""실제 AWS Marketplace 권리의 투영 원천 (ADR-0012 2차 개정)

1. subscription_source 에 AWS_MARKETPLACE 를 더한다. 상품을 등록하기 전에는 이 값이 생기지 않는다.
2. tenants 의 권리 끝 시각 CHECK 를 계약 투영 둘(모의 · 실제)로 넓히고, 실제 투영의 모양 CHECK 를 더한다.
   entitlement_end_with_mock 은 이름째 entitlement_end_with_contract 로 바꾼다.
3. marketplace_licenses: GetEntitlements 로 다시 읽은 라이선스의 사본. 테넌트와는 고객 AWS 계정으로 잇는다.

**기존 행은 그대로 둔다.** AWS_MARKETPLACE 행이 없으므로 CHECK 를 넓혀도 기존 행의 판정이 같다.
downgrade 는 라이선스 표를 버리고 CHECK 를 되돌린다. AWS_MARKETPLACE 행이 있으면 되돌리기 전에 실패한다
(PostgreSQL 은 ENUM 값을 지울 수 없어 값 자체는 남는다).

Revision ID: e600b2c4d711
Revises: c572e4a9b210
Create Date: 2026-11-10 10:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "e600b2c4d711"
down_revision = "c572e4a9b210"
branch_labels = None
depends_on = None

_OLD_ENTITLEMENT = (
    "entitlement_end_with_mock",
    "(subscription_source IS NOT DISTINCT FROM 'MOCK_MARKETPLACE') = (entitlement_ends_at IS NOT NULL)",
)
# db.models._SUBSCRIPTION_SOURCE_SHAPE 와 같은 식이다(이름은 ck_tenants_<이름>)
_NEW_TENANT_CHECKS = {
    "entitlement_end_with_contract": (
        "(subscription_source IS NOT DISTINCT FROM 'MOCK_MARKETPLACE'"
        " OR subscription_source IS NOT DISTINCT FROM 'AWS_MARKETPLACE') = (entitlement_ends_at IS NOT NULL)"
    ),
    "marketplace_projection_shape": (
        "subscription_source IS DISTINCT FROM 'AWS_MARKETPLACE' OR (plan IN ('Business', 'Enterprise')"
        " AND period_start IS NOT NULL AND period_end IS NOT NULL AND amount_minor IS NULL)"
    ),
}
# db.models._MARKETPLACE_LICENSE_SHAPE 와 같은 식이다(이름은 ck_marketplace_licenses_<이름>)
_LICENSE_CHECKS = {
    "account_format": "customer_aws_account_id ~ '^[0-9]{12}$'",
    "plan_is_paid": "plan IS NULL OR plan IN ('Business', 'Enterprise')",
    "active_shape": "NOT active OR (plan IS NOT NULL AND expires_at IS NOT NULL)",
}


def upgrade() -> None:
    # 새 ENUM 값은 커밋된 뒤에야 식에 쓸 수 있다 — 아래 CHECK 가 'AWS_MARKETPLACE' 를 쓰므로 먼저 커밋한다
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE subscription_source ADD VALUE IF NOT EXISTS 'AWS_MARKETPLACE'")

    op.drop_constraint(op.f(f"ck_tenants_{_OLD_ENTITLEMENT[0]}"), "tenants", type_="check")
    for name, sql in _NEW_TENANT_CHECKS.items():
        op.create_check_constraint(op.f(f"ck_tenants_{name}"), "tenants", sql)

    op.create_table(
        "marketplace_licenses",
        sa.Column("license_arn", sa.String(length=2048), nullable=False),
        sa.Column("customer_aws_account_id", sa.String(length=12), nullable=False),
        sa.Column("product_code", sa.String(length=255), nullable=False),
        sa.Column("agreement_id", sa.String(length=255), nullable=True),
        sa.Column("dimension", sa.String(length=255), nullable=True),
        sa.Column("plan", sa.String(length=50), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("refreshed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        *(
            sa.CheckConstraint(sql, name=op.f(f"ck_marketplace_licenses_{name}"))
            for name, sql in _LICENSE_CHECKS.items()
        ),
        sa.PrimaryKeyConstraint("license_arn", name=op.f("pk_marketplace_licenses")),
    )
    op.create_index(
        op.f("ix_marketplace_licenses_customer_aws_account_id"),
        "marketplace_licenses",
        ["customer_aws_account_id"],
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_marketplace_licenses_customer_aws_account_id"), table_name="marketplace_licenses"
    )
    op.drop_table("marketplace_licenses")
    for name in reversed(list(_NEW_TENANT_CHECKS)):
        op.drop_constraint(op.f(f"ck_tenants_{name}"), "tenants", type_="check")
    op.create_check_constraint(op.f(f"ck_tenants_{_OLD_ENTITLEMENT[0]}"), "tenants", _OLD_ENTITLEMENT[1])
