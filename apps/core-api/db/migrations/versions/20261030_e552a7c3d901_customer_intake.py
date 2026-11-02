"""도입 신청·고객 문의·처리 기록·운영자 수신함 (#552).

Revision ID: e552a7c3d901
Revises: d492a544e001

#515 d515a7c3e912와 같은 부모의 독립 가지다. 두 가지의 통합 시 나중에
통합되는 PR 담당자가 실제 head를 확인해 merge revision을 만든다.
문의 기록은 일반 UPDATE·DELETE를 거절한다. DB 소유자 우회까지 막지는 않는다.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "e552a7c3d901"
down_revision = "d492a544e001"
branch_labels = None
depends_on = None

_UUID = sa.Uuid(as_uuid=False)
_ENUMS = {
    "signup_request_status": ("NEW", "IN_REVIEW", "APPROVED", "REJECTED"),
    "inquiry_category": ("OUTAGE", "BUG", "OTHER"),
    "inquiry_status": ("OPEN", "IN_PROGRESS", "RESOLVED"),
    "inquiry_event_action": ("INQUIRY_CREATED", "STATUS_CHANGED", "NOTE_ADDED"),
    "operator_notification_kind": ("SIGNUP_REQUEST", "INQUIRY"),
}


def _enum(name):
    return postgresql.ENUM(*_ENUMS[name], name=name, create_type=False)


def upgrade() -> None:
    for name, values in _ENUMS.items():
        postgresql.ENUM(*values, name=name).create(op.get_bind(), checkfirst=False)

    op.create_table(
        "signup_requests",
        sa.Column("request_id", _UUID, nullable=False),
        sa.Column("idempotency_key", _UUID, nullable=False),
        sa.Column("company_name", sa.String(100), nullable=False),
        sa.Column("contact_name", sa.String(100), nullable=False),
        sa.Column("email", sa.String(254), nullable=False),
        sa.Column("phone", sa.String(100), nullable=True),
        sa.Column("note", sa.String(1000), nullable=True),
        sa.Column("status", _enum("signup_request_status"), nullable=False),
        sa.Column("rejected_reason", sa.String(500), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("request_id", name=op.f("pk_signup_requests")),
        sa.UniqueConstraint("idempotency_key", name=op.f("uq_signup_requests_idempotency_key")),
        sa.CheckConstraint(
            "(status = 'REJECTED' AND rejected_reason IS NOT NULL "
            "AND length(btrim(rejected_reason)) > 0) OR "
            "(status <> 'REJECTED' AND rejected_reason IS NULL)",
            name=op.f("ck_signup_requests_rejection_reason_by_status"),
        ),
    )
    op.create_index(
        "ix_signup_requests_created_at_request_id", "signup_requests", ["created_at", "request_id"]
    )
    op.create_table(
        "inquiries",
        sa.Column("inquiry_id", _UUID, nullable=False),
        sa.Column("tenant_id", _UUID, nullable=False),
        sa.Column("author_user_id", _UUID, nullable=False),
        sa.Column("category", _enum("inquiry_category"), nullable=False),
        sa.Column("title", sa.String(100), nullable=False),
        sa.Column("body", sa.String(1000), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("page_path", sa.String(1000), nullable=True),
        sa.Column("status", _enum("inquiry_status"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("inquiry_id", name=op.f("pk_inquiries")),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.tenant_id"],
            name=op.f("fk_inquiries_tenant_id_tenants"), ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["author_user_id"], ["app_users.user_id"],
            name=op.f("fk_inquiries_author_user_id_app_users"), ondelete="RESTRICT",
        ),
    )
    op.create_index("ix_inquiries_tenant_id", "inquiries", ["tenant_id"])
    op.create_index("ix_inquiries_author_user_id", "inquiries", ["author_user_id"])
    op.create_index("ix_inquiries_created_at_inquiry_id", "inquiries", ["created_at", "inquiry_id"])
    op.create_table(
        "inquiry_events",
        sa.Column("event_id", _UUID, nullable=False),
        sa.Column("inquiry_id", _UUID, nullable=False),
        sa.Column("actor_user_id", _UUID, nullable=False),
        sa.Column("action", _enum("inquiry_event_action"), nullable=False),
        sa.Column("before_status", _enum("inquiry_status"), nullable=True),
        sa.Column("after_status", _enum("inquiry_status"), nullable=True),
        sa.Column("note", sa.String(1000), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("event_id", name=op.f("pk_inquiry_events")),
        sa.ForeignKeyConstraint(
            ["inquiry_id"], ["inquiries.inquiry_id"],
            name=op.f("fk_inquiry_events_inquiry_id_inquiries"), ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["actor_user_id"], ["app_users.user_id"],
            name=op.f("fk_inquiry_events_actor_user_id_app_users"), ondelete="RESTRICT",
        ),
    )
    op.create_index("ix_inquiry_events_inquiry_id", "inquiry_events", ["inquiry_id"])
    op.create_index("ix_inquiry_events_actor_user_id", "inquiry_events", ["actor_user_id"])
    op.execute(
        """
        CREATE FUNCTION inquiry_events_append_only() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'inquiry_events is append-only (% rejected)', TG_OP
                USING ERRCODE = 'restrict_violation';
        END;
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER inquiry_events_append_only BEFORE UPDATE OR DELETE ON inquiry_events "
        "FOR EACH ROW EXECUTE FUNCTION inquiry_events_append_only()"
    )
    op.create_table(
        "notifications",
        sa.Column("notification_id", _UUID, nullable=False),
        sa.Column("kind", _enum("operator_notification_kind"), nullable=False),
        sa.Column("target_id", _UUID, nullable=False),
        sa.Column("tenant_id", _UUID, nullable=True),
        sa.Column("subject", sa.String(100), nullable=False),
        sa.Column("summary", sa.String(100), nullable=False),
        sa.Column("inquiry_category", _enum("inquiry_category"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("notification_id", name=op.f("pk_notifications")),
        sa.UniqueConstraint("kind", "target_id", name=op.f("uq_notifications_kind_target_id")),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.tenant_id"],
            name=op.f("fk_notifications_tenant_id_tenants"), ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "(kind = 'SIGNUP_REQUEST' AND tenant_id IS NULL AND inquiry_category IS NULL)"
            " OR (kind = 'INQUIRY' AND tenant_id IS NOT NULL AND inquiry_category IS NOT NULL)",
            name=op.f("ck_notifications_notification_target_shape"),
        ),
    )
    op.create_index("ix_notifications_tenant_id", "notifications", ["tenant_id"])
    op.create_index(
        "ix_notifications_created_at_notification_id",
        "notifications", ["created_at", "notification_id"],
    )
    op.create_table(
        "notification_reads",
        sa.Column("notification_id", _UUID, nullable=False),
        sa.Column("user_id", _UUID, nullable=False),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("notification_id", "user_id", name=op.f("pk_notification_reads")),
        sa.ForeignKeyConstraint(
            ["notification_id"], ["notifications.notification_id"],
            name=op.f("fk_notification_reads_notification_id_notifications"), ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["app_users.user_id"],
            name=op.f("fk_notification_reads_user_id_app_users"), ondelete="CASCADE",
        ),
    )


def downgrade() -> None:
    op.drop_table("notification_reads")
    op.drop_table("notifications")
    op.execute("DROP TRIGGER inquiry_events_append_only ON inquiry_events")
    op.execute("DROP FUNCTION inquiry_events_append_only()")
    op.drop_table("inquiry_events")
    op.drop_table("inquiries")
    op.drop_table("signup_requests")
    for name in reversed(_ENUMS):
        postgresql.ENUM(name=name).drop(op.get_bind(), checkfirst=False)
