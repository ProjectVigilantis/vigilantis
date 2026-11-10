"""연결 확인 결과에 GUARDDUTY_DISABLED 추가 — T-14 B안 GuardDuty 켜기 (#588 B)

tenant_verification_result 에 값 하나를 더한다. 연결 확인이 관제 리전 중 하나라도 GuardDuty 를 켜지 못하면
이 값으로 남는다(OK 가 아니므로 사유 코드가 함께 있다 — ck_tenants_verification_shape 는 그대로 맞는다).

downgrade 는 이 값을 가진 행이 있으면 거절한다(되돌리면 그 관측을 다른 값으로 바꿔 적게 된다). 없으면 타입을
이전 다섯 값으로 다시 만든다. 그 CHECK 가 enum 리터럴('OK')을 담고 있어 칸의 타입을 바꾸는 동안 내렸다 다시 건다.

Revision ID: d588e4b1c2a7
Revises: f609f610e001
Create Date: 2026-11-06 18:00:00.000000
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "d588e4b1c2a7"
down_revision = "f609f610e001"
branch_labels = None
depends_on = None

_TYPE = "tenant_verification_result"
_VALUE = "GUARDDUTY_DISABLED"
_PREVIOUS = ("OK", "ASSUME_DENIED", "ACCOUNT_MISMATCH", "READ_DENIED", "UNREACHABLE")
_SHAPE = "ck_tenants_verification_shape"
_SHAPE_SQL = (
    "((last_verification_result IS NULL) = (last_verified_at IS NULL))"
    " AND ((last_verification_result IS NULL OR last_verification_result = 'OK')"
    " = (last_verification_reason_code IS NULL))"
)


def upgrade() -> None:
    # PG16: 값을 추가한 트랜잭션에서는 새 값을 쓰지 않는다. 다시 적용해도 안전하게 IF NOT EXISTS.
    op.execute(f"ALTER TYPE {_TYPE} ADD VALUE IF NOT EXISTS '{_VALUE}' BEFORE 'UNREACHABLE'")


def downgrade() -> None:
    bind = op.get_bind()
    if bind.execute(sa.text(
        f"SELECT EXISTS (SELECT 1 FROM tenants WHERE last_verification_result::text = '{_VALUE}')"
    )).scalar_one():
        raise RuntimeError(f"{_VALUE} 관측이 남아 있어 손실 없는 downgrade 가 불가능합니다")
    op.execute(f"ALTER TABLE tenants DROP CONSTRAINT {_SHAPE}")
    op.execute(f"ALTER TYPE {_TYPE} RENAME TO {_TYPE}_old")
    values = ", ".join(f"'{v}'" for v in _PREVIOUS)
    op.execute(f"CREATE TYPE {_TYPE} AS ENUM ({values})")
    op.execute(
        f"ALTER TABLE tenants ALTER COLUMN last_verification_result TYPE {_TYPE}"
        f" USING last_verification_result::text::{_TYPE}"
    )
    op.execute(f"DROP TYPE {_TYPE}_old")
    op.execute(f"ALTER TABLE tenants ADD CONSTRAINT {_SHAPE} CHECK ({_SHAPE_SQL})")
