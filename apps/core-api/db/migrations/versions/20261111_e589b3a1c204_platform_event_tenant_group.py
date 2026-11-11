"""플랫폼 이벤트 대상 종류에 TENANT_GROUP 추가 (#589 B3)

platform_event_target_type 에 TENANT_GROUP 값 하나만 더한다. 표 · 컬럼 · 다른 enum 은 건드리지 않는다.
기존 행과 값은 그대로이고 새 값은 아직 쓰이지 않은 상태로 들어온다.

downgrade 는 값을 지우지 않는다(no-op). PG 는 enum 값을 안전하게 지울 수 없다 — 타입을 새로 만들어 옮기려면
platform_events.target_type 을 다시 써야 하고, TENANT_GROUP 을 가리키는 이력 행(append-only)이 있으면 옮길 수 없다.
남겨 둔 값은 다시 upgrade 해도 IF NOT EXISTS 라 안전하다.

Revision ID: e589b3a1c204
Revises: d588e4b1c2a7
Create Date: 2026-11-11 10:00:00.000000
"""
from __future__ import annotations

from alembic import op

revision = "e589b3a1c204"
down_revision = "d588e4b1c2a7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ALTER TYPE ... ADD VALUE 는 트랜잭션 안에서 새 값을 쓸 수 없으므로 autocommit 으로 분리한다
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE platform_event_target_type ADD VALUE IF NOT EXISTS 'TENANT_GROUP'")


def downgrade() -> None:
    # PG 가 enum 값을 지우지 못하고 이력 행이 이 값을 가리킬 수 있어 값을 남긴다 (모듈 docstring 참조)
    pass
