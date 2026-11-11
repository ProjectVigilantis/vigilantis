"""GuardDuty 탐지 읽기 — threat_event_type 에 GUARDDUTY_FINDING, 읽기 커서 테이블 (#588 C)

수집 주기가 고객 계정의 GuardDuty 탐지를 읽어 위협 이벤트로 남긴다. 위협 유형에 값 하나를 더하고, 고객사 × 리전마다
다음 회차가 읽기 시작할 갱신 시각을 guardduty_finding_cursors 에 둔다. 고객사가 지워지면 커서도 지워진다(CASCADE).

기존 행은 건드리지 않는다. downgrade 는 새 값을 가진 위협 이벤트가 있으면 거절한다 — 이전 어휘는 GuardDuty 탐지를
표현하지 못하고, 다른 유형으로 접으면 기록이 거짓이 된다. 없으면 커서 테이블을 지우고 타입을 이전 두 값으로 다시
만든다(커서는 다시 읽으면 되는 읽기 위치라 함께 버린다).

Revision ID: e588c2d3f4a5
Revises: d588e4b1c2a7
Create Date: 2026-11-10 18:00:00.000000
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "e588c2d3f4a5"
down_revision = "d588e4b1c2a7"
branch_labels = None
depends_on = None

_UUID = sa.Uuid(as_uuid=False)
_TYPE = "threat_event_type"
_VALUE = "GUARDDUTY_FINDING"
_PREVIOUS = ("OPEN_IP", "SSH_BRUTE_FORCE")
_TABLE = "guardduty_finding_cursors"


def upgrade() -> None:
    # PG 12+는 트랜잭션 안에서 ADD VALUE를 허용한다. 같은 트랜잭션에서 그 값을 쓰지만 않으면 된다.
    # Python enum 선언 순서와 맞춰 마지막에 붙인다.
    op.execute(f"ALTER TYPE {_TYPE} ADD VALUE IF NOT EXISTS '{_VALUE}'")
    op.create_table(
        _TABLE,
        sa.Column("tenant_id", _UUID, nullable=False),
        sa.Column("region", sa.String(length=32), nullable=False),
        sa.Column("updated_since", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "region", name=op.f(f"pk_{_TABLE}")),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.tenant_id"],
            name=op.f(f"fk_{_TABLE}_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
    )


def downgrade() -> None:
    bind = op.get_bind()
    used = bind.execute(
        sa.text("SELECT count(*) FROM threat_events WHERE event_type::text = :value"),
        {"value": _VALUE},
    ).scalar_one()
    if used:
        raise RuntimeError(
            f"threat_events에 {_VALUE} 행이 {used}건 있어 내릴 수 없다 — 이전 어휘는 GuardDuty 탐지를 표현하지 못한다"
        )
    op.drop_table(_TABLE)
    previous = ", ".join(f"'{value}'" for value in _PREVIOUS)
    op.execute(f"ALTER TYPE {_TYPE} RENAME TO {_TYPE}_old")
    op.execute(f"CREATE TYPE {_TYPE} AS ENUM ({previous})")
    op.execute(
        f"ALTER TABLE threat_events ALTER COLUMN event_type TYPE {_TYPE}"
        f" USING event_type::text::{_TYPE}"
    )
    op.execute(f"DROP TYPE {_TYPE}_old")
