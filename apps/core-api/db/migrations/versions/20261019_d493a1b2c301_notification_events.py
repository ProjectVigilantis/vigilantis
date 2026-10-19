"""#493 PR1: 업무 트랜잭션과 함께 저장하는 채널 중립 사건/outbox.

Revision ID: d493a1b2c301
Revises: c8e2f5a1d704
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "d493a1b2c301"
down_revision = "c8e2f5a1d704"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "notification_events",
        sa.Column("event_id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("incident_id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("execution_id", sa.Uuid(as_uuid=False), nullable=True),
        sa.Column("related_execution_id", sa.Uuid(as_uuid=False), nullable=True),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("phase", sa.String(16), nullable=False),
        sa.Column("outcome", sa.String(16), nullable=True),
        sa.Column("target_arn", sa.String(512), nullable=False),
        sa.Column("aws_account_id", sa.String(12), nullable=True),
        sa.Column("region", sa.String(32), nullable=True),
        sa.Column("risk_level", postgresql.ENUM(name="risk_level", create_type=False), nullable=True),
        sa.Column("trigger_source", postgresql.ENUM(name="trigger_source", create_type=False), nullable=True),
        sa.Column("runbook_id", postgresql.ENUM(name="runbook_id", create_type=False), nullable=True),
        sa.Column("execution_status", postgresql.ENUM(name="execution_status", create_type=False), nullable=True),
        sa.Column("reason_code", sa.String(64), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("event_id", name=op.f("pk_notification_events")),
        sa.UniqueConstraint(
            "kind", "incident_id", "execution_id", "phase",
            name="uq_notification_events_transition",
            postgresql_nulls_not_distinct=True,
        ),
        sa.CheckConstraint(
            "(kind = 'HIGH_INCIDENT_CREATED' AND phase = 'CREATED'"
            " AND execution_id IS NULL AND related_execution_id IS NULL"
            " AND risk_level IS NOT NULL AND risk_level = 'HIGH'"
            " AND trigger_source IS NULL AND execution_status IS NULL AND runbook_id IS NULL)"
            " OR (kind IN ('AUTO_ISOLATION', 'AUTO_ROLLBACK')"
            " AND phase IN ('REQUESTED', 'RESULT') AND execution_id IS NOT NULL"
            " AND trigger_source IS NOT NULL AND execution_status IS NOT NULL"
            " AND runbook_id IS NOT NULL"
            " AND ((kind = 'AUTO_ISOLATION'"
            " AND trigger_source IN ('PRE_MITIGATION_0_5S', 'TIMEOUT_ISOLATION_1M')"
            " AND related_execution_id IS NULL)"
            " OR (kind = 'AUTO_ROLLBACK' AND trigger_source = 'AUTO_ON_FAILURE')))",
            name=op.f("ck_notification_events_event_shape"),
        ),
        sa.CheckConstraint(
            "(phase IN ('CREATED', 'REQUESTED') AND outcome IS NULL)"
            " OR (phase = 'RESULT' AND outcome IS NOT NULL"
            " AND outcome IN ('SUCCESS', 'FAILED', 'REJECTED', 'UNVERIFIED'))",
            name=op.f("ck_notification_events_outcome_shape"),
        ),
        sa.CheckConstraint(
            "aws_account_id IS NULL OR aws_account_id ~ '^[0-9]{12}$'",
            name=op.f("ck_notification_events_account_shape"),
        ),
    )
    op.create_index(
        "ix_notification_events_created_at", "notification_events", ["created_at", "event_id"],
    )
    op.create_index("ix_notification_events_incident_id", "notification_events", ["incident_id"])


def downgrade() -> None:
    op.drop_table("notification_events")
