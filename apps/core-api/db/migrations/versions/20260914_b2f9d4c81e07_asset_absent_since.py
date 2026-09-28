"""assets.absent_since 추가 — AWS 에서 사라진 자산의 표시 (Issue #332)

수집은 자산을 arn 기준 upsert 만 하고(db/repositories/assets.py upsert_asset) 이번
회차에 관측되지 않은 자산을 지우지도 표시하지도 않았다. 그래서 AWS 에서 사라진 자산이
DB 에 영구히 남고, rule_engine 이 그 자산의 **마지막 관측 회차 메트릭**으로 매 스캔
다시 판정한다(rule_engine.py 의 `run_id = collection_run_id or a.last_collection_run_id`).
판정이 COST_CANDIDATE 면 Incident 와 조치 후보까지 올라가 승인 버튼이 열리고, 실행
1단계에서야 InvalidInstanceID.NotFound 로 깨진다.

하드 삭제 대신 표시를 두는 이유는 MetricSummary·RuleEvaluation·AssetRelationship 이
assets.asset_id 를 FK 로 참조하기 때문이다 — 지우면 판정·메트릭 이력이 함께 끊긴다.

nullable 컬럼 1개만 더한다. 기존 행은 NULL(= 관측 중)로 남아 동작이 바뀌지 않는다.

Revision ID: b2f9d4c81e07
Revises: f4a1c8e29b57
Create Date: 2026-09-14 10:40:00.000000
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = 'b2f9d4c81e07'
down_revision = 'f4a1c8e29b57'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'assets',
        sa.Column('absent_since', sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    # 컬럼만 되돌린다. 소멸 표시가 사라지면 그 자산들은 다시 관측 중으로 보이고,
    # 판정 대상으로도 돌아온다 — upgrade 이전과 같은 상태다.
    op.drop_column('assets', 'absent_since')
