"""app_users.platform_role + 담당 고객사 배정 — 플랫폼 계정 등급 (#492 PR 5)

1. app_user_platform_role(CENTRAL_ADMIN · TENANT_MANAGER) + app_users.platform_role
   (NULL 허용, 서버 기본값 없음).
2. 백필: 기존 PLATFORM → CENTRAL_ADMIN, TENANT · UNASSIGNED → NULL. 이전 스키마의 PLATFORM 은 모든 고객사에
   미치는 전역 권한이었으므로 같은 범위로 옮긴다(넓히지도 좁히지도 않는다). 새로 만들거나 지정하는 PLATFORM 은
   애플리케이션이 배정 없는 TENANT_MANAGER 로 만든다.
3. CHECK (account_type = 'PLATFORM') = (platform_role IS NOT NULL).
4. app_user_tenant_assignments(user_id, tenant_id) — TENANT_MANAGER 의 담당 고객사. 복합 PK, 두 FK 모두
   ON DELETE CASCADE(고객사 삭제가 배정을 남기지 않는다). 백필 없음 — 암묵 배정을 만들지 않는다.
5. user_event_action 에 PLATFORM_ROLE_CHANGED · TENANT_ASSIGNMENTS_CHANGED 추가.

ck_app_users_platform_role_is_admin(PLATFORM ⇒ role=ADMIN)은 호환 제약으로 남긴다 — 권한 근거가 아니다.
행 · Cognito 식별자(issuer, cognito_sub)는 건드리지 않는다.

이 리비전의 이전 미게시 초안(STAFF 를 포함한 3등급 · 배정 테이블 없음)을 이미 적용한 DB 는 지원하지 않는다 —
같은 리비전 ID 라 upgrade 가 아무것도 하지 않는다. 해당 DB 는 보존하고, 별도로 승인된 전환 절차 없이
이 코드로 upgrade · downgrade · stamp 하거나 새 앱을 연결하지 않는다. 폐기 가능한 일회용 검증 DB 만
다시 만들 수 있다.

downgrade 는 등급과 배정을 버린다(손실). 이전 스키마의 PLATFORM 은 전역 권한이라, 배정으로 범위가 제한된
TENANT_MANAGER 를 PLATFORM 으로 남기면 모든 고객사로 권한이 넓어진다 — 그래서 TENANT_MANAGER 는
UNASSIGNED(접근 없음)로 내리고 배정 테이블은 지운다. CENTRAL_ADMIN 은 PLATFORM(같은 전역 범위)으로 남는다.
다시 upgrade 해도 내린 계정 · 배정은 되살리지 않는다(UNASSIGNED 그대로, 배정 테이블은 비어 있다) —
운영 CLI 지정과 중앙 관리자의 배정으로 다시 준다. PG 는 enum 값을 지우지 못하고 user_admin_events 는
append-only 라 두 이력 값은 남긴다(upgrade 는 IF NOT EXISTS 라 다시 적용해도 안전하다).

Revision ID: b492e7d1a605
Revises: c492d6e8f4a2
Create Date: 2026-10-29 10:00:00.000000
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "b492e7d1a605"
down_revision = "c492d6e8f4a2"
branch_labels = None
depends_on = None

_UUID = sa.Uuid(as_uuid=False)
_ROLES = ("CENTRAL_ADMIN", "TENANT_MANAGER")
_EVENT_ACTIONS = ("PLATFORM_ROLE_CHANGED", "TENANT_ASSIGNMENTS_CHANGED")
_ASSIGNMENTS = "app_user_tenant_assignments"


def upgrade() -> None:
    with op.get_context().autocommit_block():
        for value in _EVENT_ACTIONS:
            op.execute(f"ALTER TYPE user_event_action ADD VALUE IF NOT EXISTS '{value}'")
    bind = op.get_bind()
    postgresql.ENUM(*_ROLES, name="app_user_platform_role").create(bind, checkfirst=False)
    platform_role = postgresql.ENUM(*_ROLES, name="app_user_platform_role", create_type=False)
    op.add_column("app_users", sa.Column("platform_role", platform_role, nullable=True))
    op.execute(
        "UPDATE app_users SET platform_role = 'CENTRAL_ADMIN'::app_user_platform_role "
        "WHERE account_type = 'PLATFORM'"
    )
    op.create_check_constraint(
        op.f("ck_app_users_platform_role_by_account_type"),
        "app_users",
        "(account_type = 'PLATFORM') = (platform_role IS NOT NULL)",
    )
    op.create_table(
        _ASSIGNMENTS,
        sa.Column("user_id", _UUID, nullable=False),
        sa.Column("tenant_id", _UUID, nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["app_users.user_id"],
            name=op.f("fk_app_user_tenant_assignments_user_id_app_users"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.tenant_id"],
            name=op.f("fk_app_user_tenant_assignments_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "user_id", "tenant_id", name=op.f("pk_app_user_tenant_assignments")
        ),
    )
    op.create_index(
        op.f("ix_app_user_tenant_assignments_tenant_id"), _ASSIGNMENTS, ["tenant_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_app_user_tenant_assignments_tenant_id"), table_name=_ASSIGNMENTS)
    op.drop_table(_ASSIGNMENTS)
    op.drop_constraint(
        op.f("ck_app_users_platform_role_by_account_type"), "app_users", type_="check"
    )
    # 배정 범위의 TENANT_MANAGER 를 전역 PLATFORM 으로 남기지 않는다 — 접근 없음으로 내린다(손실, 복원 없음)
    op.execute(
        "UPDATE app_users SET account_type = 'UNASSIGNED' WHERE platform_role = 'TENANT_MANAGER'"
    )
    op.drop_column("app_users", "platform_role")
    postgresql.ENUM(name="app_user_platform_role").drop(op.get_bind(), checkfirst=False)
