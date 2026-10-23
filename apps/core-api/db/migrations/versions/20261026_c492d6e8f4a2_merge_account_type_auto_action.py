"""merge account_type/platform_enrollments and tenant auto_action_resumed_at branches (#492)

Empty merge revision: joins the feature branch (f492c4a8d2e1 app_users.account_type ->
b492e5d7c3a1 platform_enrollments) and the dev branch (f514a6c3d201
tenants.auto_action_resumed_at) into a single head. Both branches fork from e492a3b5c701
and touch different objects, so no schema or data change is needed.

Revision ID: c492d6e8f4a2
Revises: b492e5d7c3a1, f514a6c3d201
Create Date: 2026-10-26 10:00:00.000000
"""
from __future__ import annotations

revision = "c492d6e8f4a2"
down_revision = ("b492e5d7c3a1", "f514a6c3d201")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
