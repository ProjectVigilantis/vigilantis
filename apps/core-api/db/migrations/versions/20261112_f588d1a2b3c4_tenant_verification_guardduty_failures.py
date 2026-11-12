"""연결 확인 기록에 GuardDuty 를 켜지 못한 리전 목록 추가 — T-14 안내 화면 (#588 D)

tenants.last_verification_guardduty_failures(JSONB, NULL 허용)를 더한다. 연결 확인이 GUARDDUTY_DISABLED 일 때
켜지 못한 관제 리전과 리전별 사유([{region, reason_code}])를 남긴다. CHECK 는 그 칸이 GUARDDUTY_DISABLED 에만
있고 비어 있지 않은 배열임을 지킨다. 이 revision 전에 남은 GUARDDUTY_DISABLED 는 NULL 그대로 둔다 — 어느 리전
이었는지 기록이 없어 채우지 않는다(다음 연결 확인이 채운다).

CHECK 는 결과를 문자열로 견준다(::text). enum 리터럴로 쓰면, 그 값을 더한 d588e4b1c2a7 과 같은 트랜잭션에서 이
revision 이 돌 때 PostgreSQL 이 거절한다(commit 전의 새 enum 값은 쓰지 못한다). 지금은 사이의 e589b3a1c204 가
autocommit 블록으로 트랜잭션을 끊어 주지만, 그 순서에 기대지 않으려고 문자열 비교로 둔다.

downgrade 는 CHECK 와 칸을 지운다. 리전 목록은 관측값이라 가드 없이 버린다(결과 · 사유 코드는 남는다).

Revision ID: f588d1a2b3c4
Revises: e589b3a1c204
Create Date: 2026-11-12 10:00:00.000000
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "f588d1a2b3c4"
down_revision = "e589b3a1c204"
branch_labels = None
depends_on = None

_COLUMN = "last_verification_guardduty_failures"
_SHAPE = "ck_tenants_verification_guardduty_failures_shape"
_SHAPE_SQL = (
    f"{_COLUMN} IS NULL"
    " OR (last_verification_result::text = 'GUARDDUTY_DISABLED'"
    f" AND jsonb_typeof({_COLUMN}) = 'array'"
    f" AND jsonb_array_length({_COLUMN}) > 0)"
)


def upgrade() -> None:
    op.add_column("tenants", sa.Column(_COLUMN, postgresql.JSONB(none_as_null=True), nullable=True))
    op.execute(f"ALTER TABLE tenants ADD CONSTRAINT {_SHAPE} CHECK ({_SHAPE_SQL})")


def downgrade() -> None:
    op.execute(f"ALTER TABLE tenants DROP CONSTRAINT {_SHAPE}")
    op.drop_column("tenants", _COLUMN)
