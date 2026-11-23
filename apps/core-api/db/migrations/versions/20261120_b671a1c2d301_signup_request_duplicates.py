"""도입 신청 중복 접수 기록.

Revision ID: b671a1c2d301
Revises: a623c4d5e601

같은 이메일의 처리 중 · 승인 신청에 묶여 새 신청을 만들지 않은 접수의 재시도 키와 검증 결과 값을 남긴다.
그 키의 재시도도 신청 행의 키와 같은 규칙(같은 값 202 · 다른 값 409)을 따르게 하기 위해서다.
기존 행은 만들지 않는다(이 revision 전에는 걸러진 키를 남기지 않았다).
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "b671a1c2d301"
down_revision = "a623c4d5e601"
branch_labels = None
depends_on = None

_UUID = sa.Uuid(as_uuid=False)


def upgrade() -> None:
    op.create_table(
        "signup_request_duplicates",
        sa.Column("idempotency_key", _UUID, nullable=False),
        sa.Column("request_id", _UUID, nullable=False),
        sa.Column("company_name", sa.String(100), nullable=False),
        sa.Column("contact_name", sa.String(100), nullable=False),
        sa.Column("email", sa.String(254), nullable=False),
        sa.Column("phone", sa.String(100), nullable=True),
        sa.Column("note", sa.String(1000), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("idempotency_key", name=op.f("pk_signup_request_duplicates")),
        sa.ForeignKeyConstraint(
            ["request_id"], ["signup_requests.request_id"],
            name=op.f("fk_signup_request_duplicates_request_id_signup_requests"),
            ondelete="RESTRICT",
        ),
    )
    op.create_index(
        op.f("ix_signup_request_duplicates_request_id"),
        "signup_request_duplicates",
        ["request_id"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    # 행을 지우면 그 키의 재시도가 새 신청을 만들 수 있다. 확인과 DROP 사이의 쓰기를 막는 잠금을 먼저 잡는다.
    op.execute("LOCK TABLE signup_request_duplicates IN ACCESS EXCLUSIVE MODE")
    if bind.execute(sa.text("SELECT EXISTS (SELECT 1 FROM signup_request_duplicates)")).scalar_one():
        raise RuntimeError(
            "도입 신청 중복 접수 기록이 남아 있어 손실 없는 downgrade가 불가능합니다"
        )
    op.drop_index(op.f("ix_signup_request_duplicates_request_id"), table_name="signup_request_duplicates")
    op.drop_table("signup_request_duplicates")
