"""platform_events — 플랫폼 활동 로그 (#589)

append-only 통합 로그. BEFORE UPDATE OR DELETE 트리거가 예외를 던진다(INSERT만 허용) —
audit_events 와 같은 방식이고, 테이블 소유자가 트리거를 끄면 우회할 수 있다(소유자 방어가 아니다).
action 은 문자열 칸이다(행동 목록은 schemas.platform_events 가 원천 · 목록 밖 값은 기록 계약이 거절).
tenant_id · session_id · target_id 에는 FK 를 두지 않는다 — 고객사 삭제 · 세션 정리 뒤에도 기록이 남는다.
계정 종류 · 플랫폼 등급은 app_users 의 enum 타입을 그대로 쓴다.

Revision ID: c589e7a1b2d3
Revises: a1b7c4d2e905
Create Date: 2026-11-06 14:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "c589e7a1b2d3"
down_revision = "a1b7c4d2e905"
branch_labels = None
depends_on = None

_UUID = sa.Uuid(as_uuid=False)
_CATEGORIES = (
    "AUTH",
    "ACCESS",
    "USER_ADMIN",
    "TENANT_ADMIN",
    "INCIDENT",
    "ACTION",
    "SUPPORT",
    "COLLECTION",
    "NOTIFICATION",
)
_OUTCOMES = ("SUCCEEDED", "DENIED", "FAILED")
_ACTOR_KINDS = ("USER", "SYSTEM", "ANONYMOUS")
_TARGET_TYPES = (
    "USER",
    "TENANT",
    "INCIDENT",
    "EXECUTION",
    "SIGNUP_REQUEST",
    "INQUIRY",
    "COLLECTION_RUN",
    "NOTIFICATION",
)
_NEW_TYPES = (
    ("platform_event_category", _CATEGORIES),
    ("platform_event_outcome", _OUTCOMES),
    ("platform_actor_kind", _ACTOR_KINDS),
    ("platform_event_target_type", _TARGET_TYPES),
)


def _enum(name: str) -> postgresql.ENUM:
    return postgresql.ENUM(name=name, create_type=False)


def upgrade() -> None:
    bind = op.get_bind()
    for name, values in _NEW_TYPES:
        postgresql.ENUM(*values, name=name).create(bind, checkfirst=False)
    op.create_table(
        "platform_events",
        sa.Column("event_id", _UUID, nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("category", _enum("platform_event_category"), nullable=False),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("outcome", _enum("platform_event_outcome"), nullable=False),
        sa.Column("actor_kind", _enum("platform_actor_kind"), nullable=False),
        sa.Column("actor_user_id", _UUID, nullable=True),
        # 이미 있는 타입 — app_users 와 같은 타입을 쓴다
        sa.Column("actor_account_type", _enum("app_user_account_type"), nullable=True),
        sa.Column(
            "actor_platform_role", _enum("app_user_platform_role"), nullable=True
        ),
        sa.Column("session_id", _UUID, nullable=True),
        sa.Column("request_id", sa.String(length=64), nullable=True),
        sa.Column("source_ip", postgresql.INET(), nullable=True),
        sa.Column("user_agent", sa.String(length=512), nullable=True),
        sa.Column("tenant_id", _UUID, nullable=True),
        sa.Column("target_type", _enum("platform_event_target_type"), nullable=True),
        sa.Column("target_id", sa.String(length=128), nullable=True),
        sa.Column(
            "details",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(actor_kind = 'USER') = (actor_user_id IS NOT NULL)",
            name=op.f("ck_platform_events_actor_shape"),
        ),
        sa.CheckConstraint(
            "(actor_kind = 'USER') = (actor_account_type IS NOT NULL)",
            name=op.f("ck_platform_events_actor_account_shape"),
        ),
        sa.CheckConstraint(
            "(actor_platform_role IS NOT NULL) = COALESCE(actor_account_type = 'PLATFORM', false)",
            name=op.f("ck_platform_events_actor_platform_role_shape"),
        ),
        sa.CheckConstraint(
            "(target_type IS NULL) = (target_id IS NULL)",
            name=op.f("ck_platform_events_target_shape"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(details) = 'object'",
            name=op.f("ck_platform_events_details_object"),
        ),
        sa.CheckConstraint(
            "(outcome = 'SUCCEEDED') = (details->>'reason_code' IS NULL)",
            name=op.f("ck_platform_events_reason_shape"),
        ),
        sa.ForeignKeyConstraint(
            ["actor_user_id"],
            ["app_users.user_id"],
            name=op.f("fk_platform_events_actor_user_id_app_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("event_id", name=op.f("pk_platform_events")),
    )
    for name, columns in (
        ("ix_platform_events_occurred_at_event_id", ["occurred_at", "event_id"]),
        ("ix_platform_events_tenant_id_occurred_at", ["tenant_id", "occurred_at"]),
        (
            "ix_platform_events_actor_user_id_occurred_at",
            ["actor_user_id", "occurred_at"],
        ),
        ("ix_platform_events_target", ["target_type", "target_id"]),
        ("ix_platform_events_category_occurred_at", ["category", "occurred_at"]),
    ):
        op.create_index(name, "platform_events", columns, unique=False)
    op.execute(
        """
        CREATE FUNCTION platform_events_append_only() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'platform_events is append-only (% rejected)', TG_OP
                USING ERRCODE = 'restrict_violation';
        END;
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER platform_events_append_only "
        "BEFORE UPDATE OR DELETE ON platform_events "
        "FOR EACH ROW EXECUTE FUNCTION platform_events_append_only()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER platform_events_append_only ON platform_events")
    op.execute("DROP FUNCTION platform_events_append_only()")
    for name in (
        "ix_platform_events_category_occurred_at",
        "ix_platform_events_target",
        "ix_platform_events_actor_user_id_occurred_at",
        "ix_platform_events_tenant_id_occurred_at",
        "ix_platform_events_occurred_at_event_id",
    ):
        op.drop_index(name, table_name="platform_events")
    op.drop_table("platform_events")
    bind = op.get_bind()
    for name, _ in reversed(_NEW_TYPES):
        postgresql.ENUM(name=name).drop(bind, checkfirst=False)
