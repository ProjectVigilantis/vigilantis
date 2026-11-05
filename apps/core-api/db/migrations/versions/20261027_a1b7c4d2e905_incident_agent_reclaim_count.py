"""incidents.agent_reclaim_count — IN_PROGRESS Claim을 상한 초과로 회수한 횟수 (#434 §4-2)

회수마다 같은 Incident에 과금되는 그래프 호출이 한 번 더 나간다. 이 수가 상한에 닿으면
PENDING으로 되돌리지 않고 FAILED로 닫는다(agent_dispatcher._reclaim_stale_claims).
기존 행은 0(회수한 적 없음)이며 지어낸 값을 채우지 않는다.

Revision ID: a1b7c4d2e905
Revises: f514a6c3d201
Create Date: 2026-10-27 10:00:00.000000
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a1b7c4d2e905"
down_revision = "f514a6c3d201"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "incidents",
        sa.Column(
            "agent_reclaim_count", sa.Integer(), nullable=False, server_default="0"
        ),
    )


def downgrade() -> None:
    op.drop_column("incidents", "agent_reclaim_count")
