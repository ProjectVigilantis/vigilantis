"""app_user_feature_permissions — 고객사 직원계정(OPERATOR)의 기능별 권한 (#572 5번)

1. app_user_feature_permission(VIEW · EXECUTE_ACTION · RESOLVE_INCIDENT · CREATE_INQUIRY).
2. app_user_feature_permissions(user_id, permission) — 복합 PK, app_users FK ON DELETE CASCADE.
   명시 부여 행만 권한이다. ADMIN 은 행 없이 전부, VIEWER 는 행을 두지 않는다(권한 판정은 역할을 먼저 본다).
3. 백필: 기존 TENANT OPERATOR 전원(ACTIVE · DISABLED 모두)에게 네 가지 전부. 이전 스키마의 OPERATOR 는 조치 실행 ·
   인시던트 종료 · 문의 등록 · 조회를 모두 할 수 있었으므로 같은 범위로 옮긴다(넓히지도 좁히지도 않는다).
   VIEWER 는 역할을 바꾸지 않고 행도 만들지 않는다 — 11/3 결정으로 대시보드 조회만 남는다(문의 등록은 닫힌다).
   새로 초대하는 OPERATOR 는 애플리케이션이 기본 권한 없이 만든다.
4. user_event_action 에 PERMISSIONS_CHANGED 추가.

downgrade 는 표와 enum 을 버린다(손실). 이전 앱은 OPERATOR 를 역할만으로 판정하므로 권한이 일부만 있던 OPERATOR 는
전부 할 수 있게 넓어진다 — 그래서 되돌릴 때는 되돌린 뒤 OPERATOR 의 역할을 다시 검토해야 한다. 다시 upgrade 하면 백필이
그 시점의 OPERATOR 전원에게 네 가지를 준다(이전 부여 내역은 되살리지 않는다). PG 는 enum 값을 지우지 못하고
user_admin_events 는 append-only 라 이력 값은 남긴다(upgrade 는 IF NOT EXISTS 라 다시 적용해도 안전하다).

Revision ID: c572a1b2d905
Revises: a1b7c4d2e905
Create Date: 2026-11-09 10:00:00.000000
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "c572a1b2d905"
down_revision = "a1b7c4d2e905"
branch_labels = None
depends_on = None

_UUID = sa.Uuid(as_uuid=False)
_PERMISSIONS = ("VIEW", "EXECUTE_ACTION", "RESOLVE_INCIDENT", "CREATE_INQUIRY")
_ENUM = "app_user_feature_permission"
_TABLE = "app_user_feature_permissions"


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE user_event_action ADD VALUE IF NOT EXISTS 'PERMISSIONS_CHANGED'")
    bind = op.get_bind()
    postgresql.ENUM(*_PERMISSIONS, name=_ENUM).create(bind, checkfirst=False)
    permission = postgresql.ENUM(*_PERMISSIONS, name=_ENUM, create_type=False)
    op.create_table(
        _TABLE,
        sa.Column("user_id", _UUID, nullable=False),
        sa.Column("permission", permission, nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["app_users.user_id"],
            name=op.f(f"fk_{_TABLE}_user_id_app_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("user_id", "permission", name=op.f(f"pk_{_TABLE}")),
    )
    # 기존 OPERATOR 는 지금 가능한 일을 그대로 유지한다 — 네 가지 전부
    op.execute(
        f"INSERT INTO {_TABLE} (user_id, permission) "
        f"SELECT u.user_id, p.permission::{_ENUM} "
        "FROM app_users u CROSS JOIN (VALUES "
        + ", ".join(f"('{value}')" for value in _PERMISSIONS)
        + ") AS p(permission) "
        "WHERE u.account_type = 'TENANT' AND u.role = 'OPERATOR'"
    )


def downgrade() -> None:
    op.drop_table(_TABLE)
    postgresql.ENUM(name=_ENUM).drop(op.get_bind(), checkfirst=False)
