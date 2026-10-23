"""platform_enrollments — 플랫폼 계정 등록 링크(운영 CLI 발급 1회용) (#492)

토큰 원문은 두지 않고 SHA-256 hex(token_hash)만 유니크로 둔다. 상태 ISSUED → RESERVED → REDEEMED,
ISSUED · RESERVED → REVOKED 의 행 모양을 CHECK 5개가 강제한다. 링크 1개당 앱 사용자 1명(redeemed_user_id 유니크,
app_users FK RESTRICT). 기존 테이블 · 행은 건드리지 않는다.

downgrade 는 이 테이블과 enum 만 지운다 — 발급 · 예약 기록은 사라지고, 링크로 만든 app_users 행은 남는다.

Revision ID: b492e5d7c3a1
Revises: f492c4a8d2e1
Create Date: 2026-10-23 10:00:00.000000
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "b492e5d7c3a1"
down_revision = "f492c4a8d2e1"
branch_labels = None
depends_on = None

_UUID = sa.Uuid(as_uuid=False)
_STATUSES = ("ISSUED", "RESERVED", "REDEEMED", "REVOKED")


def upgrade() -> None:
    bind = op.get_bind()
    postgresql.ENUM(*_STATUSES, name="platform_enrollment_status").create(bind, checkfirst=False)
    status = postgresql.ENUM(*_STATUSES, name="platform_enrollment_status", create_type=False)
    op.create_table(
        "platform_enrollments",
        sa.Column("enrollment_id", _UUID, nullable=False),
        sa.Column("token_hash", sa.CHAR(length=64), nullable=False),
        sa.Column("status", status, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reserved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("email", sa.String(), nullable=True),
        sa.Column("redeemed_user_id", _UUID, nullable=True),
        sa.Column("redeemed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "expires_at > created_at",
            name=op.f("ck_platform_enrollments_expires_after_created"),
        ),
        sa.CheckConstraint(
            "(reserved_at IS NULL) = (email IS NULL)",
            name=op.f("ck_platform_enrollments_reservation_shape"),
        ),
        sa.CheckConstraint(
            "status = 'REVOKED' OR ((status = 'ISSUED') = (reserved_at IS NULL))",
            name=op.f("ck_platform_enrollments_reserved_unless_issued"),
        ),
        sa.CheckConstraint(
            "((status = 'REDEEMED') = (redeemed_user_id IS NOT NULL))"
            " AND ((redeemed_user_id IS NULL) = (redeemed_at IS NULL))",
            name=op.f("ck_platform_enrollments_redemption_shape"),
        ),
        sa.CheckConstraint(
            "(status = 'REVOKED') = (revoked_at IS NOT NULL)",
            name=op.f("ck_platform_enrollments_revocation_shape"),
        ),
        sa.ForeignKeyConstraint(
            ["redeemed_user_id"],
            ["app_users.user_id"],
            name=op.f("fk_platform_enrollments_redeemed_user_id_app_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("enrollment_id", name=op.f("pk_platform_enrollments")),
        sa.UniqueConstraint(
            "redeemed_user_id", name=op.f("uq_platform_enrollments_redeemed_user_id")
        ),
        sa.UniqueConstraint("token_hash", name=op.f("uq_platform_enrollments_token_hash")),
    )


def downgrade() -> None:
    op.drop_table("platform_enrollments")
    postgresql.ENUM(name="platform_enrollment_status").drop(op.get_bind(), checkfirst=False)
