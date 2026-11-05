"""incidents.agent_failure_reason — AI 분석이 FAILED로 닫힌 사유 코드 (#434)

조치 없음(빈 메뉴·IPv6 SSH 출발지)과 분석 고장을 조회 응답에서 가르려고 사유를 남긴다. 어휘가 분석
단계와 함께 늘어나므로 PG enum이 아니라 문자열이다. 기존 행은 NULL(사유 기록 이전)이고 지어낸 값을
채우지 않는다. 사유는 FAILED 호출에만 둔다(CHECK).

Revision ID: b434c7e2a915
Revises: f528f565e001
Create Date: 2026-11-05 13:00:00.000000
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "b434c7e2a915"
down_revision = "f528f565e001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("incidents", sa.Column("agent_failure_reason", sa.String(64), nullable=True))
    op.create_check_constraint(
        "failure_reason_when_failed", "incidents",
        "agent_failure_reason IS NULL OR agent_invocation_status = 'FAILED'",
    )


def downgrade() -> None:
    op.drop_constraint("failure_reason_when_failed", "incidents", type_="check")
    op.drop_column("incidents", "agent_failure_reason")
