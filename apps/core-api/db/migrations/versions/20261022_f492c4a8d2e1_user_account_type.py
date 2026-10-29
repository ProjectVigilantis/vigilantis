"""app_users.account_type — 플랫폼 권한의 원천 (#492)

기존 사용자는 승격하지 않는다: tenant_id NULL → UNASSIGNED, 값 있음 → TENANT.
PLATFORM 은 이 마이그레이션이 만들지 않는다. 컬럼에는 서버 기본값이 없다(쓰기마다 명시).
행·Cognito 식별자(issuer, cognito_sub)는 건드리지 않는다.

downgrade 는 account_type 을 버린다 — PLATFORM/UNASSIGNED 구분은 복원되지 않는다.

Revision ID: f492c4a8d2e1
Revises: e492a3b5c701
Create Date: 2026-10-22 10:00:00.000000
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "f492c4a8d2e1"
down_revision = "e492a3b5c701"
branch_labels = None
depends_on = None

_TYPES = ("PLATFORM", "TENANT", "UNASSIGNED")


def upgrade() -> None:
    bind = op.get_bind()
    postgresql.ENUM(*_TYPES, name="app_user_account_type").create(bind, checkfirst=False)
    account_type = postgresql.ENUM(*_TYPES, name="app_user_account_type", create_type=False)
    op.add_column("app_users", sa.Column("account_type", account_type, nullable=True))
    op.execute(
        "UPDATE app_users SET account_type = "
        "(CASE WHEN tenant_id IS NULL THEN 'UNASSIGNED' ELSE 'TENANT' END)"
        "::app_user_account_type"
    )
    op.alter_column("app_users", "account_type", nullable=False)
    op.create_check_constraint(
        op.f("ck_app_users_tenant_binding_by_account_type"),
        "app_users",
        "(account_type = 'TENANT') = (tenant_id IS NOT NULL)",
    )
    op.create_check_constraint(
        op.f("ck_app_users_platform_role_is_admin"),
        "app_users",
        "account_type <> 'PLATFORM' OR role = 'ADMIN'",
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_app_users_platform_role_is_admin"), "app_users", type_="check")
    op.drop_constraint(
        op.f("ck_app_users_tenant_binding_by_account_type"), "app_users", type_="check"
    )
    op.drop_column("app_users", "account_type")
    postgresql.ENUM(name="app_user_account_type").drop(op.get_bind(), checkfirst=False)
