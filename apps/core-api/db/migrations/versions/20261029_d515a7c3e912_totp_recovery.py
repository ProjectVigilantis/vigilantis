"""TOTP 분실 복구 — 인증 세대 · 복구 차단 · 복구 작업 (#515)

1. auth_session_revoke_reason 에 TOTP_RESET 추가.
2. app_users.auth_generation(INTEGER, 기본 0) · app_users.mfa_recovery_state(app_user_mfa_recovery_state,
   기본 NONE). 기존 사용자는 복구 이력이 없으므로 세대 0 · NONE 이다. 서버 기본값은 이 칸을 모르는
   쓰기(raw INSERT)를 위해 남긴다.
3. totp_recovery_operations — 호출자가 고른 operation_id 하나에 복구 1건. 차단과 같은 commit 에 생긴다.
4. totp_provider_mutations — 신원(issuer, sub)별 Cognito TOTP 변경 울타리. 풀리지 않은(resolution NULL) 행은
   신원당 1개(부분 유일 인덱스)이며, 제공자 변경 호출 전에 commit 된다.

downgrade 는 차단 중이거나 새 등록을 기다리는 사용자(mfa_recovery_state <> NONE)가 있으면 거절한다 —
이전 스키마는 그 차단을 표현하지 못해, 그대로 내리면 제공자 삭제가 확인되지 않은 옛 TOTP 로그인이 다시 열린다.
풀리지 않은 울타리가 있어도 거절한다 — 결과 불명인 제공자 변경이 이전 코드의 등록 · 삭제와 겹칠 수 있다.
TOTP_RESET 으로 폐기된 세션은 ADMIN_REVOKED 로 바꾼다(이전 코드가 읽을 수 있게). PG 는 enum 값을 지우지
못하므로 TOTP_RESET 값은 남는다(upgrade 는 IF NOT EXISTS 라 다시 적용해도 안전하다). 복구 작업 기록은 버린다.

Revision ID: d515a7c3e912
Revises: d492a544e001
Create Date: 2026-10-29 12:00:00.000000
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "d515a7c3e912"
down_revision = "d492a544e001"
branch_labels = None
depends_on = None

_UUID = sa.Uuid(as_uuid=False)
_STATES = ("NONE", "RESET_PENDING", "ENROLLMENT_REQUIRED")
_STATUSES = (
    "PROVIDER_PENDING",
    "COMPLETED",
    "PROVIDER_NOT_FOUND",
    "PROVIDER_REJECTED",
    "PROVIDER_UNKNOWN",
)
_KINDS = ("RESET", "ENROLLMENT")
_RESOLUTIONS = ("PROVIDER_RETURNED", "OPERATOR_RECONCILED")


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE auth_session_revoke_reason ADD VALUE IF NOT EXISTS 'TOTP_RESET'")
    bind = op.get_bind()
    postgresql.ENUM(*_STATES, name="app_user_mfa_recovery_state").create(bind, checkfirst=False)
    postgresql.ENUM(*_STATUSES, name="totp_recovery_status").create(bind, checkfirst=False)
    state = postgresql.ENUM(*_STATES, name="app_user_mfa_recovery_state", create_type=False)
    status = postgresql.ENUM(*_STATUSES, name="totp_recovery_status", create_type=False)
    op.add_column(
        "app_users",
        sa.Column("auth_generation", sa.Integer(), server_default=sa.text("0"), nullable=False),
    )
    op.add_column(
        "app_users",
        sa.Column("mfa_recovery_state", state, server_default="NONE", nullable=False),
    )
    op.create_table(
        "totp_recovery_operations",
        sa.Column("operation_id", _UUID, nullable=False),
        sa.Column("target_user_id", _UUID, nullable=False),
        sa.Column("actor_user_id", _UUID, nullable=True),
        sa.Column("identity_verified", sa.Boolean(), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("status", status, nullable=False),
        sa.Column("revoked_session_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finalized_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "identity_verified", name=op.f("ck_totp_recovery_operations_identity_verified")
        ),
        sa.CheckConstraint(
            "generation > 0", name=op.f("ck_totp_recovery_operations_generation_positive")
        ),
        sa.CheckConstraint(
            "revoked_session_count >= 0",
            name=op.f("ck_totp_recovery_operations_revoked_count_nonnegative"),
        ),
        sa.CheckConstraint(
            "(status = 'PROVIDER_PENDING') = (finalized_at IS NULL)",
            name=op.f("ck_totp_recovery_operations_finalized_shape"),
        ),
        sa.ForeignKeyConstraint(
            ["actor_user_id"],
            ["app_users.user_id"],
            name=op.f("fk_totp_recovery_operations_actor_user_id_app_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["target_user_id"],
            ["app_users.user_id"],
            name=op.f("fk_totp_recovery_operations_target_user_id_app_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("operation_id", name=op.f("pk_totp_recovery_operations")),
        sa.UniqueConstraint(
            "target_user_id",
            "generation",
            name=op.f("uq_totp_recovery_operations_target_user_id_generation"),
        ),
    )
    postgresql.ENUM(*_KINDS, name="totp_provider_mutation_kind").create(bind, checkfirst=False)
    postgresql.ENUM(*_RESOLUTIONS, name="totp_provider_mutation_resolution").create(
        bind, checkfirst=False
    )
    kind = postgresql.ENUM(*_KINDS, name="totp_provider_mutation_kind", create_type=False)
    resolution = postgresql.ENUM(
        *_RESOLUTIONS, name="totp_provider_mutation_resolution", create_type=False
    )
    op.create_table(
        "totp_provider_mutations",
        sa.Column("mutation_id", _UUID, nullable=False),
        sa.Column("issuer", sa.String(), nullable=False),
        sa.Column("cognito_sub", sa.String(), nullable=False),
        sa.Column("kind", kind, nullable=False),
        sa.Column("operation_id", _UUID, nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolution", resolution, nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reconciled_by", _UUID, nullable=True),
        sa.CheckConstraint(
            "(kind = 'RESET') = (operation_id IS NOT NULL)",
            name=op.f("ck_totp_provider_mutations_operation_by_kind"),
        ),
        sa.CheckConstraint(
            "(resolution IS NULL) = (resolved_at IS NULL)",
            name=op.f("ck_totp_provider_mutations_resolution_shape"),
        ),
        sa.CheckConstraint(
            "reconciled_by IS NULL OR resolution IS NOT DISTINCT FROM 'OPERATOR_RECONCILED'",
            name=op.f("ck_totp_provider_mutations_reconciler_shape"),
        ),
        sa.ForeignKeyConstraint(
            ["operation_id"],
            ["totp_recovery_operations.operation_id"],
            name=op.f("fk_totp_provider_mutations_operation_id_totp_recovery_operations"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["reconciled_by"],
            ["app_users.user_id"],
            name=op.f("fk_totp_provider_mutations_reconciled_by_app_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("mutation_id", name=op.f("pk_totp_provider_mutations")),
        sa.UniqueConstraint("operation_id", name=op.f("uq_totp_provider_mutations_operation_id")),
    )
    op.create_index(
        "uq_totp_provider_mutations_unresolved_identity",
        "totp_provider_mutations",
        ["issuer", "cognito_sub"],
        unique=True,
        postgresql_where=sa.text("resolution IS NULL"),
    )


def downgrade() -> None:
    bind = op.get_bind()
    blocked = bind.execute(
        sa.text("SELECT count(*) FROM app_users WHERE mfa_recovery_state <> 'NONE'")
    ).scalar_one()
    if blocked:
        raise RuntimeError(
            "TOTP 복구 차단 중이거나 새 등록을 기다리는 사용자가 있어 내릴 수 없다 "
            "— 이전 스키마는 그 로그인 차단을 표현하지 못한다"
        )
    unresolved = bind.execute(
        sa.text("SELECT count(*) FROM totp_provider_mutations WHERE resolution IS NULL")
    ).scalar_one()
    if unresolved:
        raise RuntimeError(
            "풀리지 않은 TOTP 신원 변경 울타리가 있어 내릴 수 없다 "
            "— 결과 불명인 Cognito 변경이 이전 코드의 등록 · 삭제와 겹칠 수 있다"
        )
    op.execute(
        "UPDATE auth_sessions SET revoke_reason = 'ADMIN_REVOKED' "
        "WHERE revoke_reason = 'TOTP_RESET'"
    )
    op.drop_index(
        "uq_totp_provider_mutations_unresolved_identity", table_name="totp_provider_mutations"
    )
    op.drop_table("totp_provider_mutations")
    postgresql.ENUM(name="totp_provider_mutation_resolution").drop(bind, checkfirst=False)
    postgresql.ENUM(name="totp_provider_mutation_kind").drop(bind, checkfirst=False)
    op.drop_table("totp_recovery_operations")
    op.drop_column("app_users", "mfa_recovery_state")
    op.drop_column("app_users", "auth_generation")
    postgresql.ENUM(name="totp_recovery_status").drop(bind, checkfirst=False)
    postgresql.ENUM(name="app_user_mfa_recovery_state").drop(bind, checkfirst=False)
