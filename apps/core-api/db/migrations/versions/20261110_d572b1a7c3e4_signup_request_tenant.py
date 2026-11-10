"""도입 신청이 승인으로 만든 고객사 연결 (#572 묶음 B).

Revision ID: d572b1a7c3e4
Revises: c572e4a9b210

signup_requests.tenant_id 는 NULL 허용이다. 이미 승인된 옛 신청은 어느 고객사가 만들어졌는지 알 수 없으므로
채우지 않는다. 고객사를 지우면 ON DELETE SET NULL 로 연결만 끊기고 신청과 append-only 결정 기록은 그대로다.
downgrade 는 연결 열만 버린다(고객사·신청·결정 기록은 지우지 않는다).
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "d572b1a7c3e4"
down_revision = "c572e4a9b210"
branch_labels = None
depends_on = None

_UUID = sa.Uuid(as_uuid=False)


def upgrade() -> None:
    op.add_column("signup_requests", sa.Column("tenant_id", _UUID, nullable=True))
    op.create_foreign_key(
        op.f("fk_signup_requests_tenant_id_tenants"),
        "signup_requests",
        "tenants",
        ["tenant_id"],
        ["tenant_id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("fk_signup_requests_tenant_id_tenants"), "signup_requests", type_="foreignkey"
    )
    op.drop_column("signup_requests", "tenant_id")
