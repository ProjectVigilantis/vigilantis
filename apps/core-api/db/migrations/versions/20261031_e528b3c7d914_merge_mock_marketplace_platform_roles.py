"""merge mock Marketplace subscriptions (#528) and platform role/audit (#492 PR 5 · #544) branches

Empty merge revision: joins d528e1f3a6b2 (mock Marketplace agreements/events + tenants
subscription source, after the already-applied c528a4d9e710) and d492a544e001 (app_users
platform_role + app_user_tenant_assignments merged with audit_events). The branches touch
disjoint objects, so no schema or data change is needed. Neither parent is rewritten;
databases already at either one (or at c528a4d9e710) upgrade through this revision.

Revision ID: e528b3c7d914
Revises: d528e1f3a6b2, d492a544e001
Create Date: 2026-10-31
"""
from __future__ import annotations

revision = "e528b3c7d914"
down_revision = ("d528e1f3a6b2", "d492a544e001")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
