"""merge signup request tenant (#609) and AWS Marketplace licenses (#610) heads

Empty merge revision: joins d572b1a7c3e4 (#609 — signup_requests.tenant_id and its foreign key)
and e600b2c4d711 (#610 — subscription_source enum value, tenants checks, marketplace_licenses).
The sides modify disjoint objects: #609 references the existing tenants primary key but does not
change tenants or its checks. No schema or data change is needed in the merge. Neither parent is
rewritten; databases already at either one apply the missing branch before joining this revision.

Revision ID: f609f610e001
Revises: d572b1a7c3e4, e600b2c4d711
Create Date: 2026-11-10
"""
from __future__ import annotations

revision = "f609f610e001"
down_revision = ("d572b1a7c3e4", "e600b2c4d711")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
