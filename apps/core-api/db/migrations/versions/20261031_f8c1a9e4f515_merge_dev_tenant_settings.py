"""merge dev head (TOTP recovery + customer intake) and T-7 tenant settings

Empty merge revision: joins f515a552e001 (which joins d515a7c3e912 and e552a7c3d901) and
f7c1a9e4d201 (T-7 tenant settings). The two sides touch disjoint tables, so no schema or data
change is needed. No existing revision is rewritten; databases at either head upgrade through it.

Revision ID: f8c1a9e4f515
Revises: f515a552e001, f7c1a9e4d201
Create Date: 2026-11-02 09:00:00.000000
"""
from __future__ import annotations

revision = "f8c1a9e4f515"
down_revision = ("f515a552e001", "f7c1a9e4d201")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
