"""#493 PR2: 채널별 발송 상태·현재 수신 바인딩·Slack 호출 관문.

기존 notification_events는 바꾸지 않는다. 기존 사건의 발송 행을 만들지 않는다 —
발송 행은 dispatcher가 라우트 활성화 시각과 대조해 만든다(과거 이력 소급 발송 없음).

Revision ID: d493a1b2c302
Revises: d493a1b2c301
"""

import sqlalchemy as sa
from alembic import op

revision = "d493a1b2c302"
down_revision = "d493a1b2c301"
branch_labels = None
depends_on = None

_CHANNELS = "channel IN ('SLACK', 'SNS')"


def upgrade() -> None:
    op.create_table(
        "notification_routes",
        sa.Column("aws_account_id", sa.String(12), nullable=False),
        sa.Column("channel", sa.String(8), nullable=False),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("route_epoch", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("aws_account_id", "channel", name=op.f("pk_notification_routes")),
        sa.CheckConstraint(_CHANNELS, name=op.f("ck_notification_routes_channel_known")),
        sa.CheckConstraint(
            "aws_account_id ~ '^[0-9]{12}$'", name=op.f("ck_notification_routes_account_shape"),
        ),
    )
    op.create_table(
        "notification_lanes",
        sa.Column("channel", sa.String(8), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("holder_token", sa.Uuid(as_uuid=False), nullable=True),
        sa.Column("cooldown_until", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("channel", name=op.f("pk_notification_lanes")),
        sa.CheckConstraint(_CHANNELS, name=op.f("ck_notification_lanes_channel_known")),
    )
    op.create_table(
        "notification_deliveries",
        sa.Column("delivery_id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("event_id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("channel", sa.String(8), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("tenant_id", sa.String(128), nullable=True),
        sa.Column("aws_account_id", sa.String(12), nullable=True),
        sa.Column("route_fingerprint", sa.String(64), nullable=True),
        sa.Column("route_epoch", sa.Uuid(as_uuid=False), nullable=True),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claim_token", sa.Uuid(as_uuid=False), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_category", sa.String(16), nullable=True),
        sa.Column("last_error_code", sa.String(64), nullable=True),
        sa.Column("blocked_reason", sa.String(64), nullable=True),
        sa.Column("final_reason", sa.String(16), nullable=True),
        sa.Column("receipt_kind", sa.String(16), nullable=True),
        sa.Column("receipt_ref", sa.String(256), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False,
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False,
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("delivery_id", name=op.f("pk_notification_deliveries")),
        sa.ForeignKeyConstraint(
            ["event_id"], ["notification_events.event_id"],
            name=op.f("fk_notification_deliveries_event_id_notification_events"),
        ),
        sa.UniqueConstraint(
            "event_id", "channel", name="uq_notification_deliveries_event_channel",
        ),
        sa.CheckConstraint(_CHANNELS, name=op.f("ck_notification_deliveries_channel_known")),
        sa.CheckConstraint(
            "attempts >= 0", name=op.f("ck_notification_deliveries_attempts_non_negative"),
        ),
        sa.CheckConstraint(
            "last_category IS NULL"
            " OR last_category IN ('TRANSIENT', 'PERMANENT', 'UNKNOWN')",
            name=op.f("ck_notification_deliveries_category_known"),
        ),
        sa.CheckConstraint(
            "(state IN ('PENDING', 'IN_FLIGHT', 'SUCCEEDED', 'FAILED')"
            " AND tenant_id IS NOT NULL AND aws_account_id IS NOT NULL"
            " AND route_fingerprint IS NOT NULL AND route_epoch IS NOT NULL"
            " AND blocked_reason IS NULL"
            " AND ((state = 'PENDING' AND next_attempt_at IS NOT NULL"
            " AND claim_token IS NULL AND lease_expires_at IS NULL"
            " AND finished_at IS NULL AND final_reason IS NULL AND receipt_kind IS NULL)"
            " OR (state = 'IN_FLIGHT' AND attempts >= 1 AND next_attempt_at IS NULL"
            " AND claim_token IS NOT NULL AND lease_expires_at IS NOT NULL"
            " AND finished_at IS NULL AND final_reason IS NULL AND receipt_kind IS NULL)"
            " OR (state = 'SUCCEEDED' AND attempts >= 1 AND next_attempt_at IS NULL"
            " AND claim_token IS NULL AND lease_expires_at IS NULL"
            " AND finished_at IS NOT NULL AND final_reason IS NULL"
            " AND receipt_kind IS NOT NULL"
            " AND receipt_kind IN ('SLACK_POSTED', 'SNS_ACCEPTED'))"
            " OR (state = 'FAILED' AND attempts >= 1 AND next_attempt_at IS NULL"
            " AND claim_token IS NULL AND lease_expires_at IS NULL"
            " AND finished_at IS NOT NULL AND receipt_kind IS NULL"
            " AND last_category IS NOT NULL AND final_reason IS NOT NULL"
            " AND final_reason IN ('PERMANENT', 'EXHAUSTED'))))"
            " OR (state = 'BLOCKED' AND blocked_reason IS NOT NULL"
            " AND next_attempt_at IS NULL AND claim_token IS NULL"
            " AND lease_expires_at IS NULL AND finished_at IS NOT NULL"
            " AND final_reason IS NULL AND receipt_kind IS NULL)",
            name=op.f("ck_notification_deliveries_state_shape"),
        ),
    )
    op.create_index(
        "ix_notification_deliveries_pending_due", "notification_deliveries",
        ["channel", "next_attempt_at"], postgresql_where=sa.text("state = 'PENDING'"),
    )
    op.create_index(
        "ix_notification_deliveries_in_flight_lease", "notification_deliveries",
        ["channel", "lease_expires_at"], postgresql_where=sa.text("state = 'IN_FLIGHT'"),
    )


def downgrade() -> None:
    op.drop_table("notification_deliveries")
    op.drop_table("notification_lanes")
    op.drop_table("notification_routes")
