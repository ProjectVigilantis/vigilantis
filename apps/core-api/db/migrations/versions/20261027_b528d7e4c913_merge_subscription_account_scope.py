"""merge tenant subscription lifecycle and account scope branches (#528)

Empty merge revision: joins a6d2f8c1b347 (tenants subscription columns +
tenant_subscription_history, on f514a6c3d201) and c492d6e8f4a2 (the #492 merge of
app_users.account_type with f514a6c3d201).

a6d2f8c1b347 keeps its original parent, so a database that already applied it records exactly
the revisions it ran; upgrading it to this merge runs the #492 branch (f492c4a8d2e1 ->
c492d6e8f4a2) first. Re-parenting a6d2f8c1b347 onto c492d6e8f4a2 instead would
make such a database look as if the #492 revisions had been applied and skip them. The two
sides touch disjoint objects (tenants subscription columns / tenant_subscription_history vs
app_users.account_type), so no schema or data change is needed.

Revision ID: b528d7e4c913
Revises: a6d2f8c1b347, c492d6e8f4a2
Create Date: 2026-10-27
"""
from __future__ import annotations

revision = "b528d7e4c913"
down_revision = ("a6d2f8c1b347", "c492d6e8f4a2")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
