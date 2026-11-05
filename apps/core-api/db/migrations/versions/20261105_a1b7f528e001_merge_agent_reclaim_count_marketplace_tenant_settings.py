"""merge incidents.agent_reclaim_count (#434 §4-2) and the #528·#564 dev head

Empty merge revision: joins a1b7c4d2e905 (#434 — incidents.agent_reclaim_count column, branched from
f514a6c3d201) and f528f565e001 (dev head — mock Marketplace subscriptions and T-7 tenant settings,
joined through a565c8d1e901). The sides touch disjoint objects (a1b7c4d2e905 only adds one incidents
column with a server default; the dev side never touches incidents), so no schema or data change is
needed. Neither parent is rewritten; databases already at either one upgrade through this revision.

Revision ID: a1b7f528e001
Revises: a1b7c4d2e905, f528f565e001
Create Date: 2026-11-05
"""
from __future__ import annotations

revision = "a1b7f528e001"
down_revision = ("a1b7c4d2e905", "f528f565e001")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
