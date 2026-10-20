"""로그인 기반 2종 — app_users · auth_sessions (#492 PR 1)

1. app_users — 신원은 (issuer, cognito_sub) 유니크. email은 표시·연락용이라 유니크가 아니다.
2. auth_sessions — 서버 세션. token_hash(SHA-256 hex)만 저장하고 원본 토큰은 두지 않는다.
   revoked_at·revoke_reason은 함께 NULL이거나 함께 값이 있다. 시각 CHECK 3개로 수명 창을 강제한다.

Revision ID: d3a7c5e91f08
Revises: d493a1b2c301
Create Date: 2026-10-19 10:00:00.000000
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "d3a7c5e91f08"
down_revision = "d493a1b2c301"
branch_labels = None
depends_on = None

_UUID = sa.Uuid(as_uuid=False)
_ENUMS = {
    "app_user_role": ("ADMIN", "OPERATOR", "VIEWER"),
    "app_user_status": ("ACTIVE", "DISABLED"),
    "auth_session_mfa_step": ("SOFTWARE_TOKEN_MFA", "MFA_SETUP"),
    "auth_session_revoke_reason": ("LOGOUT", "SUPERSEDED"),
}


def _enum(name: str) -> postgresql.ENUM:
    return postgresql.ENUM(*_ENUMS[name], name=name, create_type=False)


def upgrade() -> None:
    bind = op.get_bind()
    for name, values in _ENUMS.items():
        postgresql.ENUM(*values, name=name).create(bind, checkfirst=False)
    op.create_table(
        "app_users",
        sa.Column("user_id", _UUID, nullable=False),
        sa.Column("issuer", sa.String(), nullable=False),
        sa.Column("cognito_sub", sa.String(), nullable=False),
        sa.Column("email", sa.String(), nullable=False),
        sa.Column("email_verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("role", _enum("app_user_role"), nullable=False),
        sa.Column("status", _enum("app_user_status"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("user_id", name=op.f("pk_app_users")),
        sa.UniqueConstraint(
            "issuer", "cognito_sub", name=op.f("uq_app_users_issuer_cognito_sub")
        ),
    )
    op.create_table(
        "auth_sessions",
        sa.Column("session_id", _UUID, nullable=False),
        sa.Column("token_hash", sa.CHAR(length=64), nullable=False),
        sa.Column("user_id", _UUID, nullable=False),
        sa.Column("mfa_step", _enum("auth_session_mfa_step"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_activity_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("absolute_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoke_reason", _enum("auth_session_revoke_reason"), nullable=True),
        sa.CheckConstraint(
            "(revoked_at IS NULL) = (revoke_reason IS NULL)",
            name=op.f("ck_auth_sessions_revoke_shape"),
        ),
        sa.CheckConstraint(
            "absolute_expires_at > created_at",
            name=op.f("ck_auth_sessions_absolute_after_created"),
        ),
        sa.CheckConstraint(
            "last_activity_at >= created_at AND last_activity_at <= absolute_expires_at",
            name=op.f("ck_auth_sessions_activity_window"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["app_users.user_id"],
            name=op.f("fk_auth_sessions_user_id_app_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("session_id", name=op.f("pk_auth_sessions")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_auth_sessions_token_hash")),
    )
    op.create_index(
        op.f("ix_auth_sessions_user_id"), "auth_sessions", ["user_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_auth_sessions_user_id"), table_name="auth_sessions")
    op.drop_table("auth_sessions")
    op.drop_table("app_users")
    bind = op.get_bind()
    for name in reversed(list(_ENUMS)):
        postgresql.ENUM(name=name).drop(bind, checkfirst=False)
