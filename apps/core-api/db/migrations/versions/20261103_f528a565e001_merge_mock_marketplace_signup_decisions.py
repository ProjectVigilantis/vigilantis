"""merge mock Marketplace subscriptions (#528) and TOTP/customer intake/signup decisions (#515 · #552 · #565) branches

Empty merge revision: joins e528b3c7d914 (tenants subscription projection + tenant_subscription_history +
mock Marketplace agreements/events) and a565c8d1e901 (signup_request_decisions, after the
f515a552e001 TOTP recovery/customer intake merge). Both descend from d492a544e001 and touch disjoint
objects (new enum types and tables differ; #528 only adds tenants columns/checks), so no schema or
data change is needed. Neither parent is rewritten; databases already at either one upgrade through
this revision.

Revision ID: f528a565e001
Revises: e528b3c7d914, a565c8d1e901
Create Date: 2026-11-03
"""
from __future__ import annotations

revision = "f528a565e001"
down_revision = ("e528b3c7d914", "a565c8d1e901")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
