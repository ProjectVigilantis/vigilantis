"""merge mock Marketplace subscriptions (#528) and T-7 tenant settings (#564 · #576) heads

Empty merge revision: joins f528a565e001 (#528 head — tenants subscription projection columns/checks,
tenant_subscription_history, mock Marketplace agreements/events, joined with a565c8d1e901 signup decisions)
and f565c8d1a9e3 (#576 head — tenant_settings, tenant_settings_cleanups, notification_legacy_routes via
f8c1a9e4f515, joined with the same a565c8d1e901). The sides touch disjoint objects (T-7 only creates its
three tables; #528 only adds tenants columns/checks and its own enum types and tables), so no schema or
data change is needed. Neither parent is rewritten; databases already at either one upgrade through
this revision.

Revision ID: f528f565e001
Revises: f528a565e001, f565c8d1a9e3
Create Date: 2026-11-04
"""
from __future__ import annotations

revision = "f528f565e001"
down_revision = ("f528a565e001", "f565c8d1a9e3")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
