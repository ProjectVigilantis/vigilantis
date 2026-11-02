"""merge T-7 merge head and signup request decisions (#565)

Empty merge revision: joins f8c1a9e4f515 (T-7 tenant settings + dev head f515a552e001) and
a565c8d1e901 (signup_request_decisions, a child of f515a552e001). The sides touch disjoint
tables, so no schema or data change is needed. No existing revision is rewritten; databases at
either head upgrade through it.

Revision ID: f565c8d1a9e3
Revises: f8c1a9e4f515, a565c8d1e901
Create Date: 2026-11-02 12:00:00.000000
"""
from __future__ import annotations

revision = "f565c8d1a9e3"
down_revision = ("f8c1a9e4f515", "a565c8d1e901")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
