"""T-7 tenant settings: protected tags, notification channel goals/observations, cleanup queue.

Adds three tables and changes no existing rows. tenant_settings holds one optional row per
tenant (no row = default protected-tag policy and no API-configured channel).
tenant_settings_cleanups queues platform-owned SNS topics / Secrets Manager secrets that must
be deleted; it has no FK so queued work survives tenant deletion.
notification_legacy_routes keeps startup evidence for routes-file bindings whose tenant was
deleted before the DB settings source took over; it grants no routing or send authority.

Downgrade drops the three tables. Cloud resources referenced only by those rows are not
deleted by the downgrade.

Revision ID: f7c1a9e4d201
Revises: d492a544e001
Create Date: 2026-10-31 09:00:00.000000
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "f7c1a9e4d201"
down_revision = "d492a544e001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "tenant_settings",
        sa.Column("tenant_id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("protected_tags", postgresql.JSONB(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("slack_source", sa.String(16), nullable=True),
        sa.Column("slack_secret_ref", sa.String(2048), nullable=True),
        sa.Column("slack_secret_version", sa.String(64), nullable=True),
        sa.Column("slack_destination_hash", sa.CHAR(64), nullable=True),
        sa.Column("email_source", sa.String(16), nullable=True),
        sa.Column("email_legacy_topic_arn", sa.String(512), nullable=True),
        sa.Column("email_address", sa.String(320), nullable=True),
        sa.Column("email_generation", sa.Uuid(as_uuid=False), nullable=True),
        sa.Column("email_region", sa.String(32), nullable=True),
        sa.Column("email_status", sa.String(24), nullable=True),
        sa.Column("email_topic_arn", sa.String(512), nullable=True),
        sa.Column("email_subscription_arn", sa.String(512), nullable=True),
        sa.Column("email_failure_code", sa.String(64), nullable=True),
        sa.Column("email_attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("email_next_check_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("email_lease_token", sa.Uuid(as_uuid=False), nullable=True),
        sa.Column("email_lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("email_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("tenant_id", name=op.f("pk_tenant_settings")),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.tenant_id"],
            name=op.f("fk_tenant_settings_tenant_id_tenants"),
        ),
        sa.CheckConstraint(
            "protected_tags IS NULL OR jsonb_typeof(protected_tags) = 'array'",
            name=op.f("ck_tenant_settings_protected_tags_is_array"),
        ),
        sa.CheckConstraint(
            "((slack_source IS NULL OR slack_source = 'CLEARED')"
            " AND slack_secret_ref IS NULL AND slack_secret_version IS NULL"
            " AND slack_destination_hash IS NULL)"
            " OR (slack_source = 'LEGACY_ENV' AND slack_secret_ref IS NOT NULL"
            " AND slack_secret_version IS NULL AND slack_destination_hash IS NULL)"
            " OR (slack_source = 'MANAGED' AND slack_secret_ref IS NOT NULL"
            " AND slack_secret_version IS NOT NULL AND slack_destination_hash IS NOT NULL)",
            name=op.f("ck_tenant_settings_slack_shape"),
        ),
        sa.CheckConstraint(
            "((email_source IS NULL OR email_source IN ('LEGACY_TOPIC', 'CLEARED'))"
            " AND email_address IS NULL AND email_generation IS NULL AND email_region IS NULL"
            " AND email_status IS NULL AND email_topic_arn IS NULL"
            " AND email_subscription_arn IS NULL AND email_failure_code IS NULL"
            " AND email_next_check_at IS NULL AND email_lease_token IS NULL"
            " AND ((email_source IS NOT NULL AND email_source = 'LEGACY_TOPIC')"
            " = (email_legacy_topic_arn IS NOT NULL)))"
            " OR (email_source = 'MANAGED' AND email_legacy_topic_arn IS NULL"
            " AND email_address IS NOT NULL AND email_generation IS NOT NULL"
            " AND email_region IS NOT NULL AND email_status IS NOT NULL"
            " AND email_status IN ('SETUP_PENDING', 'SETUP_FAILED',"
            " 'CONFIRMATION_PENDING', 'READY')"
            " AND (email_status = 'SETUP_FAILED') = (email_failure_code IS NOT NULL)"
            " AND (email_subscription_arn IS NULL OR email_topic_arn IS NOT NULL)"
            " AND (email_status NOT IN ('CONFIRMATION_PENDING', 'READY')"
            " OR email_subscription_arn IS NOT NULL))",
            name=op.f("ck_tenant_settings_email_shape"),
        ),
        sa.CheckConstraint(
            "(email_lease_token IS NULL) = (email_lease_expires_at IS NULL)",
            name=op.f("ck_tenant_settings_email_lease_shape"),
        ),
        sa.CheckConstraint(
            "email_attempts >= 0", name=op.f("ck_tenant_settings_email_attempts_non_negative"),
        ),
    )
    op.create_index(
        "ix_tenant_settings_email_due", "tenant_settings", ["email_next_check_at"],
        postgresql_where=sa.text("email_source = 'MANAGED'"),
    )
    op.create_table(
        "tenant_settings_cleanups",
        sa.Column("cleanup_id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("tenant_id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("resource_kind", sa.String(16), nullable=False),
        sa.Column("purpose", sa.String(16), nullable=False),
        sa.Column("region", sa.String(32), nullable=False),
        sa.Column("resource_name", sa.String(512), nullable=False),
        sa.Column("not_before", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("last_error_code", sa.String(64), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("cleanup_id", name=op.f("pk_tenant_settings_cleanups")),
        sa.UniqueConstraint(
            "resource_kind", "region", "resource_name",
            name="uq_tenant_settings_cleanups_resource",
        ),
        sa.CheckConstraint(
            "resource_kind IN ('SNS_TOPIC', 'SECRET')",
            name=op.f("ck_tenant_settings_cleanups_kind_known"),
        ),
        sa.CheckConstraint(
            "purpose IN ('STAGING', 'RETIRED')",
            name=op.f("ck_tenant_settings_cleanups_purpose_known"),
        ),
        sa.CheckConstraint(
            "attempts >= 0", name=op.f("ck_tenant_settings_cleanups_attempts_non_negative"),
        ),
    )
    op.create_index(
        "ix_tenant_settings_cleanups_not_before", "tenant_settings_cleanups", ["not_before"],
    )
    op.create_table(
        "notification_legacy_routes",
        sa.Column("aws_account_id", sa.String(12), nullable=False),
        sa.Column("channel", sa.String(8), nullable=False),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column(
            "recorded_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint(
            "aws_account_id", "channel", "tenant_id", name=op.f("pk_notification_legacy_routes"),
        ),
        sa.CheckConstraint(
            "channel IN ('SLACK', 'SNS')", name=op.f("ck_notification_legacy_routes_channel_known"),
        ),
        sa.CheckConstraint(
            "aws_account_id ~ '^[0-9]{12}$'",
            name=op.f("ck_notification_legacy_routes_account_shape"),
        ),
    )


def downgrade() -> None:
    op.drop_table("notification_legacy_routes")
    op.drop_index("ix_tenant_settings_cleanups_not_before", table_name="tenant_settings_cleanups")
    op.drop_table("tenant_settings_cleanups")
    op.drop_index("ix_tenant_settings_email_due", table_name="tenant_settings")
    op.drop_table("tenant_settings")
