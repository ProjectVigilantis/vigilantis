"""runbook_candidates.parameters 추가 — Runbook별 typed 파라미터 저장 (Issue #154)

후보가 싣는 것은 AI가 정하는 값만이고, 기존 display_parameters는 그 값에서 서버가
만드는 화면 표시본이 된다. 두 컬럼을 함께 두는 이유는 관제 화면이 승인 시점에 본
문구를 그대로 보존하기 위해서다.

기존 행에는 빈 객체를 채운다 — NOT NULL을 위한 backfill일 뿐 typed 값으로의 이관이
아니다. 그 상태의 행은 현행 후보 계약(RunbookCandidateData)에 어긋나므로 실행 접수가
거절한다(workflows._candidate_meets_contract) — 잘못된 값이 실행으로 새지 않는다.

Revision ID: b71c3f0a92d4
Revises: e4947bbcd48a
Create Date: 2026-08-26 12:20:00.000000
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = 'b71c3f0a92d4'
down_revision = 'e4947bbcd48a'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'runbook_candidates',
        sa.Column(
            'parameters',
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    # 기본값은 이전용이다. 이후 삽입은 ORM이 값을 명시하므로 서버 기본값을 남기지
    # 않는다 — 남겨 두면 파라미터를 빠뜨린 삽입이 조용히 빈 객체로 저장된다.
    op.alter_column('runbook_candidates', 'parameters', server_default=None)


def downgrade() -> None:
    op.drop_column('runbook_candidates', 'parameters')
