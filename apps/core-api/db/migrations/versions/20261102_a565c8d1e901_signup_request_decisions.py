"""도입 신청 최종 결정 기록 (#565).

Revision ID: a565c8d1e901
Revises: f515a552e001

신청당 승인·거절 결정 한 건을 결정자 ID·결정 시점 이메일과 함께 남긴다. 기존 신청에 대한
기록은 만들지 않는다(옛 결정자는 알 수 없다). 일반 UPDATE·DELETE는 트리거가 거절한다.
DB 소유자 우회까지 막지는 않는다.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "a565c8d1e901"
down_revision = "f515a552e001"
branch_labels = None
depends_on = None

_UUID = sa.Uuid(as_uuid=False)


def upgrade() -> None:
    status = postgresql.ENUM(name="signup_request_status", create_type=False)
    op.create_table(
        "signup_request_decisions",
        sa.Column("request_id", _UUID, nullable=False),
        sa.Column("actor_user_id", _UUID, nullable=False),
        sa.Column("actor_email", sa.String(254), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("outcome", status, nullable=False),
        sa.Column("rejected_reason", sa.String(500), nullable=True),
        sa.PrimaryKeyConstraint("request_id", name=op.f("pk_signup_request_decisions")),
        sa.ForeignKeyConstraint(
            ["request_id"], ["signup_requests.request_id"],
            name=op.f("fk_signup_request_decisions_request_id_signup_requests"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["actor_user_id"], ["app_users.user_id"],
            name=op.f("fk_signup_request_decisions_actor_user_id_app_users"),
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "(outcome = 'REJECTED' AND rejected_reason IS NOT NULL "
            "AND length(btrim(rejected_reason)) > 0) OR "
            "(outcome = 'APPROVED' AND rejected_reason IS NULL)",
            name=op.f("ck_signup_request_decisions_outcome_reason_shape"),
        ),
    )
    op.create_index(
        op.f("ix_signup_request_decisions_actor_user_id"),
        "signup_request_decisions",
        ["actor_user_id"],
    )
    op.execute(
        """
        CREATE FUNCTION signup_request_decisions_append_only() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'signup_request_decisions is append-only (% rejected)', TG_OP
                USING ERRCODE = 'restrict_violation';
        END;
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER signup_request_decisions_append_only "
        "BEFORE UPDATE OR DELETE ON signup_request_decisions "
        "FOR EACH ROW EXECUTE FUNCTION signup_request_decisions_append_only()"
    )


def downgrade() -> None:
    bind = op.get_bind()
    # 비어 있음 확인과 DROP 사이에 다른 트랜잭션이 결정을 커밋하면 조용히 사라진다.
    # 쓰기를 막는 잠금을 확인 전에 잡아 트랜잭션 끝까지 유지한다.
    op.execute("LOCK TABLE signup_request_decisions IN ACCESS EXCLUSIVE MODE")
    if bind.execute(sa.text("SELECT EXISTS (SELECT 1 FROM signup_request_decisions)")).scalar_one():
        raise RuntimeError(
            "도입 신청 결정 기록이 남아 있어 손실 없는 downgrade가 불가능합니다"
        )
    op.execute("DROP TRIGGER signup_request_decisions_append_only ON signup_request_decisions")
    op.execute("DROP FUNCTION signup_request_decisions_append_only()")
    op.drop_table("signup_request_decisions")
