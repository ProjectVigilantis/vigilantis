"""후보에 AI 절감 예상을 저장한다 (#347)."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "c7a9e1d83f24"
down_revision = "95956d08c917"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 기존 후보는 미산출(null) 상태를 유지한다.
    op.add_column(
        "runbook_candidates",
        sa.Column("ai_savings_estimate", postgresql.JSONB(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("runbook_candidates", "ai_savings_estimate")
