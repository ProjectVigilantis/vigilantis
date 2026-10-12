"""action_executions.autoscaling_plan 추가 — EC2_ENABLE_AUTOSCALING 고정 실행 계획 (Issue #446)

AMI→시작 템플릿→ASG→정책은 AWS 변경이 여러 단계다. 단계 사이에서 프로세스가 끊겨도
다음 시도가 처음 정한 의도(이미지·템플릿·ASG 이름 등)를 그대로 이어받도록, 첫 AWS 변경
전에 확정한 계획을 nullable JSONB 한 칸에 commit한다. validated_command의 의미는 바꾸지
않는다.

기존 행은 건드리지 않는다 — NULL이 "계획 없음"이다.

Revision ID: b6c1e4a9d853
Revises: a3d8f1c6e742
Create Date: 2026-09-30 10:00:00.000000
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "b6c1e4a9d853"
down_revision = "a3d8f1c6e742"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "action_executions",
        sa.Column(
            "autoscaling_plan",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("action_executions", "autoscaling_plan")
