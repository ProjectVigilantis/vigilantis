"""Merge subscription and audit histories without changing applied revision parents.

Revision ID: c528a4d9e710
Revises: b528d7e4c913, b544a7d1e2f3
Create Date: 2026-10-30
"""
from __future__ import annotations

revision = "c528a4d9e710"
down_revision = ("b528d7e4c913", "b544a7d1e2f3")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
