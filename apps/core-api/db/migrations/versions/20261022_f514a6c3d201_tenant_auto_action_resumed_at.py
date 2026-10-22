"""tenants.auto_action_resumed_at — 자동 조치를 마지막으로 다시 켠 시각 (Post-MVP T-6 · #514)

이 시각 이전에 지난 Medium 응답 기한은 다시 켜도 자동 격리하지 않는다. 기존 행은 NULL(끈 적 없음)이고
지어낸 값을 채우지 않는다 — NULL 이면 기한 비교를 하지 않아 지금 동작 그대로다.

Revision ID: f514a6c3d201
Revises: e492a3b5c701
Create Date: 2026-10-22 15:00:00.000000
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "f514a6c3d201"
down_revision = "e492a3b5c701"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tenants",
        sa.Column("auto_action_resumed_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("tenants", "auto_action_resumed_at")
