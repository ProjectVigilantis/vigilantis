"""merge account_type and tenant auto_action_resumed_at branches (#492)

Empty merge revision: joins the app_users.account_type branch (f492c4a8d2e1) and the
tenants.auto_action_resumed_at branch (f514a6c3d201) into a single head. Both branches
fork from e492a3b5c701 and touch different columns, so no schema or data change is needed.

Revision ID: c492d6e8f4a2
Revises: f492c4a8d2e1, f514a6c3d201
Create Date: 2026-10-26 10:00:00.000000
"""
from __future__ import annotations

revision = "c492d6e8f4a2"
down_revision = ("f492c4a8d2e1", "f514a6c3d201")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
