"""runbook_candidates 에 격리 추천 근거 칸 추가 (#598)

서버 규칙이 후보를 만들 때 한 번 남기는 추천 근거(schemas.recommendations.IsolationRecommendationBasis)를
nullable JSONB recommendation_basis 에 담는다. 기존 후보는 NULL 로 남고 역채움하지 않는다 — 옛 후보에
새 규칙을 소급 적용하지 않는다. 표 · 다른 컬럼 · enum 은 바꾸지 않는다.

downgrade 는 NULL 이 아닌 행이 있으면 내리지 않는다 — 칸을 지우면 기록된 판정 근거가 사라진다.

Revision ID: f598b2c3d402
Revises: e598a1b2c301
Create Date: 2026-11-13 10:00:00.000000
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "f598b2c3d402"
down_revision = "e598a1b2c301"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "runbook_candidates",
        sa.Column(
            "recommendation_basis", postgresql.JSONB(none_as_null=True), nullable=True
        ),
    )


def downgrade() -> None:
    # 쓰기를 먼저 막고 센다 — 세지 않은 사이에 커밋된 근거를 DROP COLUMN 이 지우지 못하게,
    # 이 잠금은 같은 트랜잭션의 컬럼 삭제까지 유지된다
    op.get_bind().execute(sa.text("LOCK TABLE runbook_candidates IN ACCESS EXCLUSIVE MODE"))
    used = op.get_bind().execute(
        sa.text("SELECT count(*) FROM runbook_candidates WHERE recommendation_basis IS NOT NULL")
    ).scalar_one()
    if used:
        raise RuntimeError(
            f"runbook_candidates에 recommendation_basis가 있는 행이 {used}건 있어 내릴 수 없다 — "
            "칸을 지우면 기록된 격리 추천 판정 근거가 사라진다"
        )
    op.drop_column("runbook_candidates", "recommendation_basis")
