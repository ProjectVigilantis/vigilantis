"""사용자 관리 이력 + 세션 폐기 사유 확장 (#492 PR 3)

1. auth_session_revoke_reason 에 ADMIN_REVOKED · ACCOUNT_DISABLED · PASSWORD_RESET 추가.
2. user_admin_events — append-only 이력. BEFORE UPDATE OR DELETE 트리거가 예외를 던진다
   (INSERT만 허용). 테이블 소유자가 트리거를 끄면 우회할 수 있다 — 소유자 방어가 아니다.

Revision ID: e492a3b5c701
Revises: d4a7c2e9f150
Create Date: 2026-10-20 10:00:00.000000
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "e492a3b5c701"
down_revision = "d4a7c2e9f150"
branch_labels = None
depends_on = None

_UUID = sa.Uuid(as_uuid=False)
_NEW_REASONS = ("ADMIN_REVOKED", "ACCOUNT_DISABLED", "PASSWORD_RESET")
_OLD_REASONS = ("LOGOUT", "SUPERSEDED")
_ACTIONS = (
    "USER_INVITED",
    "INVITE_RESENT",
    "ROLE_CHANGED",
    "STATUS_CHANGED",
    "SESSIONS_REVOKED",
)
_JSONB = postgresql.JSONB(astext_type=sa.Text())


def upgrade() -> None:
    with op.get_context().autocommit_block():
        for value in _NEW_REASONS:
            op.execute(
                f"ALTER TYPE auth_session_revoke_reason ADD VALUE IF NOT EXISTS '{value}'"
            )
    bind = op.get_bind()
    postgresql.ENUM(*_ACTIONS, name="user_event_action").create(bind, checkfirst=False)
    action = postgresql.ENUM(*_ACTIONS, name="user_event_action", create_type=False)
    op.create_table(
        "user_admin_events",
        sa.Column("event_id", _UUID, nullable=False),
        sa.Column("actor_user_id", _UUID, nullable=False),
        sa.Column("target_user_id", _UUID, nullable=False),
        sa.Column("action", action, nullable=False),
        sa.Column("before_state", _JSONB, nullable=False),
        sa.Column("after_state", _JSONB, nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["actor_user_id"],
            ["app_users.user_id"],
            name=op.f("fk_user_admin_events_actor_user_id_app_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["target_user_id"],
            ["app_users.user_id"],
            name=op.f("fk_user_admin_events_target_user_id_app_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("event_id", name=op.f("pk_user_admin_events")),
    )
    op.create_index(
        op.f("ix_user_admin_events_actor_user_id"),
        "user_admin_events",
        ["actor_user_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_user_admin_events_target_user_id"),
        "user_admin_events",
        ["target_user_id"],
        unique=False,
    )
    op.execute(
        """
        CREATE FUNCTION user_admin_events_append_only() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'user_admin_events is append-only (% rejected)', TG_OP
                USING ERRCODE = 'restrict_violation';
        END;
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER user_admin_events_append_only "
        "BEFORE UPDATE OR DELETE ON user_admin_events "
        "FOR EACH ROW EXECUTE FUNCTION user_admin_events_append_only()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER user_admin_events_append_only ON user_admin_events")
    op.execute("DROP FUNCTION user_admin_events_append_only()")
    op.drop_index(
        op.f("ix_user_admin_events_target_user_id"), table_name="user_admin_events"
    )
    op.drop_index(
        op.f("ix_user_admin_events_actor_user_id"), table_name="user_admin_events"
    )
    op.drop_table("user_admin_events")
    bind = op.get_bind()
    postgresql.ENUM(name="user_event_action").drop(bind, checkfirst=False)
    # PG는 enum 값 삭제를 지원하지 않는다 — 타입을 새로 만들고 컬럼을 옮긴다.
    # 새 사유로 폐기된 세션은 이전 enum이 표현할 수 없어 LOGOUT 으로 접는다.
    op.execute("ALTER TYPE auth_session_revoke_reason RENAME TO auth_session_revoke_reason_old")
    postgresql.ENUM(*_OLD_REASONS, name="auth_session_revoke_reason").create(
        bind, checkfirst=False
    )
    op.execute(
        "ALTER TABLE auth_sessions ALTER COLUMN revoke_reason "
        "TYPE auth_session_revoke_reason USING ("
        "CASE WHEN revoke_reason IS NULL THEN NULL "
        "WHEN revoke_reason::text IN ('LOGOUT', 'SUPERSEDED') "
        "THEN revoke_reason::text ELSE 'LOGOUT' END"
        ")::auth_session_revoke_reason"
    )
    op.execute("DROP TYPE auth_session_revoke_reason_old")
