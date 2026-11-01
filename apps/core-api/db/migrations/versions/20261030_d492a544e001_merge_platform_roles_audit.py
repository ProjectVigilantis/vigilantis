"""merge platform role/assignment (#492 PR 5) and audit_events (#544) branches

Empty merge revision: joins b492e7d1a605 (app_users.platform_role +
app_user_tenant_assignments) and b544a7d1e2f3 (audit_events). Both fork from c492d6e8f4a2
and touch disjoint objects, so no schema or data change is needed. Neither parent is
rewritten; databases already at either one upgrade through this revision.

This revision's own upgrade and downgrade are no-ops. Data loss happens only when a downgrade
removes b492e7d1a605 itself (e.g. down to c492d6e8f4a2): that revision's downgrade then runs
(TENANT_MANAGER -> UNASSIGNED, assignments dropped) and re-upgrading does not restore them.

Revision ID: d492a544e001
Revises: b492e7d1a605, b544a7d1e2f3
Create Date: 2026-10-30 10:00:00.000000
"""
from __future__ import annotations

revision = "d492a544e001"
down_revision = ("b492e7d1a605", "b544a7d1e2f3")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
