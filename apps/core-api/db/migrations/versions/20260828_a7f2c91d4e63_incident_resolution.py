"""incidents.resolution·resolved_at 추가 — 관제자 종료 판단 보존 (Issue #199)

종료 처리는 상태를 RESOLVED로 옮기는 것에 더해 관제자가 고른 판단(정당했다·
과잉이었다)을 남긴다. 화면 조작으로만 두면 다시 조회하는 순간 사라져, 두 관제자가
같은 인시던트를 다르게 보게 된다.

종료 시각을 updated_at으로 대신하지 않는다 — 그 값은 자식(실행·후보) 상태가 바뀔
때도 함께 올라가므로(repositories/incidents.py touch_incident) 종료 시점을 가리키지
못한다.

기존 행은 전부 NULL이다. 두 컬럼은 status='RESOLVED'에서만 채워지되, RESOLVED인데
판단이 없는 것은 허용한다 — 관제자 판단 없이 종료되는 경로가 뒤에 생길 수 있다.

Revision ID: a7f2c91d4e63
Revises: c3d5a81f47e2
Create Date: 2026-08-28 10:40:00.000000
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = 'a7f2c91d4e63'
down_revision = 'c3d5a81f47e2'
branch_labels = None
depends_on = None

# 값 원천은 packages/schemas/api/incidents.py ResolutionJudgement — JUSTIFIED 1종.
# `과잉이었다`는 종료 값이 아니라 해제 실행으로 넘어가는 트리거다(#196 §D)
_RESOLUTION_JUDGEMENT = ('JUSTIFIED',)


def upgrade() -> None:
    bind = op.get_bind()
    postgresql.ENUM(
        *_RESOLUTION_JUDGEMENT, name='resolution_judgement'
    ).create(bind, checkfirst=True)

    op.add_column(
        'incidents',
        sa.Column(
            'resolution',
            postgresql.ENUM(
                *_RESOLUTION_JUDGEMENT, name='resolution_judgement', create_type=False
            ),
            nullable=True,
        ),
    )
    op.add_column(
        'incidents',
        sa.Column('resolved_at', sa.DateTime(timezone=True), nullable=True),
    )
    op.create_check_constraint(
        'resolution_with_resolved_status',
        'incidents',
        "((resolution IS NULL) = (resolved_at IS NULL))"
        " AND (resolution IS NULL OR status = 'RESOLVED')",
    )


def downgrade() -> None:
    op.drop_constraint('resolution_with_resolved_status', 'incidents', type_='check')
    op.drop_column('incidents', 'resolved_at')
    op.drop_column('incidents', 'resolution')
    postgresql.ENUM(name='resolution_judgement').drop(op.get_bind(), checkfirst=True)
