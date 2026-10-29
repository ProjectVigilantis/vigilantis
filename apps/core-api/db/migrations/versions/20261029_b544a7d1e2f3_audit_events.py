"""audit_events — 조치 승인 · 종료 판단 감사 기록 (T-2 · #544)

append-only 이력. BEFORE UPDATE OR DELETE 트리거가 예외를 던진다(INSERT만 허용) —
user_admin_events 와 같은 방식이고, 테이블 소유자가 트리거를 끄면 우회할 수 있다(소유자 방어가 아니다).
행위자 · 발동 원천 · 종료 판단의 짝은 CHECK 가 막는다(db/models.py AuditEvent).

Revision ID: b544a7d1e2f3
Revises: c492d6e8f4a2
Create Date: 2026-10-29 14:00:00.000000
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "b544a7d1e2f3"
down_revision = "c492d6e8f4a2"
branch_labels = None
depends_on = None

_UUID = sa.Uuid(as_uuid=False)
_ACTIONS = ("ACTION_EXECUTION_STARTED", "INCIDENT_RESOLVED")
_ACTOR_KINDS = ("TENANT_USER", "PLATFORM_USER", "SYSTEM")


def upgrade() -> None:
    bind = op.get_bind()
    postgresql.ENUM(*_ACTIONS, name="audit_event_action").create(bind, checkfirst=False)
    postgresql.ENUM(*_ACTOR_KINDS, name="audit_actor_kind").create(bind, checkfirst=False)
    action = postgresql.ENUM(*_ACTIONS, name="audit_event_action", create_type=False)
    actor_kind = postgresql.ENUM(*_ACTOR_KINDS, name="audit_actor_kind", create_type=False)
    # 이미 있는 타입 — 실행 행 · 인시던트와 같은 타입을 쓴다
    trigger_source = postgresql.ENUM(name="trigger_source", create_type=False)
    resolution = postgresql.ENUM(name="resolution_judgement", create_type=False)
    op.create_table(
        "audit_events",
        sa.Column("event_id", _UUID, nullable=False),
        sa.Column("action", action, nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("actor_kind", actor_kind, nullable=False),
        sa.Column("actor_user_id", _UUID, nullable=True),
        sa.Column("incident_id", _UUID, nullable=False),
        sa.Column("tenant_id", _UUID, nullable=True),
        sa.Column("execution_id", _UUID, nullable=True),
        sa.Column("trigger_source", trigger_source, nullable=True),
        sa.Column("resolution", resolution, nullable=True),
        sa.CheckConstraint(
            "(action = 'ACTION_EXECUTION_STARTED' AND execution_id IS NOT NULL"
            " AND trigger_source IS NOT NULL AND resolution IS NULL)"
            " OR (action = 'INCIDENT_RESOLVED' AND resolution IS NOT NULL"
            " AND execution_id IS NULL AND trigger_source IS NULL)",
            name=op.f("ck_audit_events_action_shape"),
        ),
        sa.CheckConstraint(
            "(actor_kind = 'SYSTEM') = (actor_user_id IS NULL)",
            name=op.f("ck_audit_events_actor_shape"),
        ),
        sa.CheckConstraint(
            "CASE action WHEN 'ACTION_EXECUTION_STARTED'"
            " THEN (trigger_source = 'USER_APPROVAL') = (actor_kind <> 'SYSTEM')"
            " ELSE actor_kind <> 'SYSTEM' END",
            name=op.f("ck_audit_events_actor_matches_trigger"),
        ),
        sa.ForeignKeyConstraint(
            ["actor_user_id"],
            ["app_users.user_id"],
            name=op.f("fk_audit_events_actor_user_id_app_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["incident_id"],
            ["incidents.incident_id"],
            name=op.f("fk_audit_events_incident_id_incidents"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.tenant_id"],
            name=op.f("fk_audit_events_tenant_id_tenants"),
        ),
        sa.ForeignKeyConstraint(
            ["execution_id"],
            ["action_executions.execution_id"],
            name=op.f("fk_audit_events_execution_id_action_executions"),
        ),
        sa.PrimaryKeyConstraint("event_id", name=op.f("pk_audit_events")),
        sa.UniqueConstraint("execution_id", name=op.f("uq_audit_events_execution_id")),
    )
    op.create_index(
        op.f("ix_audit_events_actor_user_id"), "audit_events", ["actor_user_id"], unique=False
    )
    op.create_index(
        op.f("ix_audit_events_incident_id"), "audit_events", ["incident_id"], unique=False
    )
    op.create_index(
        op.f("ix_audit_events_tenant_id"), "audit_events", ["tenant_id"], unique=False
    )
    op.create_index(
        "ix_audit_events_occurred_at_event_id",
        "audit_events",
        ["occurred_at", "event_id"],
        unique=False,
    )
    op.execute(
        """
        CREATE FUNCTION audit_events_append_only() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'audit_events is append-only (% rejected)', TG_OP
                USING ERRCODE = 'restrict_violation';
        END;
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER audit_events_append_only "
        "BEFORE UPDATE OR DELETE ON audit_events "
        "FOR EACH ROW EXECUTE FUNCTION audit_events_append_only()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER audit_events_append_only ON audit_events")
    op.execute("DROP FUNCTION audit_events_append_only()")
    op.drop_index("ix_audit_events_occurred_at_event_id", table_name="audit_events")
    op.drop_index(op.f("ix_audit_events_tenant_id"), table_name="audit_events")
    op.drop_index(op.f("ix_audit_events_incident_id"), table_name="audit_events")
    op.drop_index(op.f("ix_audit_events_actor_user_id"), table_name="audit_events")
    op.drop_table("audit_events")
    bind = op.get_bind()
    postgresql.ENUM(name="audit_actor_kind").drop(bind, checkfirst=False)
    postgresql.ENUM(name="audit_event_action").drop(bind, checkfirst=False)
