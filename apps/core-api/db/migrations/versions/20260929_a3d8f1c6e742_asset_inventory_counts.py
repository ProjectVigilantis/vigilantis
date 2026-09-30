"""asset_inventory_counts 추가 — 수집 회차별 유형별 자산 수 (시계열 축 5)

대시보드 「자산 현황 추이」는 "내 자산이 얼마나 늘고 줄었나"를 보는 자리다. 그런데 assets 행은
회차마다 덮어써 이력이 없고, 회차별로 남는 판정 이력(rule_evaluations)은 판정 대상 3종
(EC2·SG·EBS)뿐이라 NACL·시작 템플릿·ASG·대상 그룹의 증감을 복원할 수 없다. 회차를 마감할 때
관측한 유형별 건수를 남기는 표를 더한다.

과거 회차는 채우지 않는다 — 복원할 원천이 없다. 추이는 이 마이그레이션 뒤의 회차부터 선다.

Revision ID: a3d8f1c6e742
Revises: e8f4b2c9a631
Create Date: 2026-09-29 14:00:00.000000
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "a3d8f1c6e742"
down_revision = "e8f4b2c9a631"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "asset_inventory_counts",
        sa.Column("collection_run_id", sa.Uuid(as_uuid=False), nullable=False),
        # asset_type ENUM 은 baseline 이 만들었다 — 다시 만들지 않는다.
        sa.Column(
            "asset_type",
            postgresql.ENUM(name="asset_type", create_type=False),
            nullable=False,
        ),
        sa.Column("count", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "count >= 0", name=op.f("ck_asset_inventory_counts_count_non_negative")
        ),
        sa.ForeignKeyConstraint(
            ["collection_run_id"],
            ["collection_runs.collection_run_id"],
            name=op.f("fk_asset_inventory_counts_collection_run_id_collection_runs"),
        ),
        sa.PrimaryKeyConstraint(
            "collection_run_id", "asset_type", name=op.f("pk_asset_inventory_counts")
        ),
    )


def downgrade() -> None:
    op.drop_table("asset_inventory_counts")
