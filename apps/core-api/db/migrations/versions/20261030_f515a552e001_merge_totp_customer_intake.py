"""merge TOTP recovery (#515) and customer intake (#552) branches

Empty merge revision: joins d515a7c3e912 (TOTP recovery) and e552a7c3d901 (customer intake).
Both fork from d492a544e001 and touch disjoint objects, so no schema or data change is needed.
Neither parent is rewritten; databases already at either one upgrade through this revision.

Revision ID: f515a552e001
Revises: d515a7c3e912, e552a7c3d901
Create Date: 2026-10-30 12:00:00.000000
"""
from __future__ import annotations

revision = "f515a552e001"
down_revision = ("d515a7c3e912", "e552a7c3d901")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
