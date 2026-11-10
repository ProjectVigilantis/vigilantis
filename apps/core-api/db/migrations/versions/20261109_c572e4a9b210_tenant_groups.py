"""고객사 그룹 — 관리자 직원계정의 담당 범위를 고객사 단위 배정에서 그룹 배정으로 옮긴다 (#572 2번 · 3번)

1. tenant_groups(group_id, name UNIQUE, created_at, updated_at) — 중앙 관리계정이 만드는 고객사 묶음(한 단계뿐).
2. tenants.group_id — NULL 허용 FK(tenant_groups, ON DELETE SET NULL) + 인덱스. 고객사는 최대 한 그룹에 들고,
   NULL 은 담당 그룹 없음(담당자 없음)이다. 그룹을 지우면 그 고객사들은 그룹 없음으로 돌아간다.
3. app_user_group_assignments(user_id, group_id) — TENANT_MANAGER 의 담당 그룹. 복합 PK, 두 FK 모두 ON DELETE
   CASCADE(사용자 · 그룹 삭제가 배정을 남기지 않는다). 한 직원에게 여러 그룹, 한 그룹을 여러 직원이 맡는다.
4. user_event_action 에 GROUP_ASSIGNMENTS_CHANGED 추가. TENANT_ASSIGNMENTS_CHANGED 는 남긴다 — 이력은
   append-only 이고 PG 는 enum 값을 지우지 못한다.
5. app_user_tenant_assignments(고객사 단위 직접 배정)를 지운다. **직접 배정이 1건이라도 남아 있으면 upgrade 를
   거절한다(RuntimeError)** — 그룹으로 옮기는 규칙이 정해지지 않았으므로 지우거나 무시하지 않는다. 0건이면 변환
   없이 도입한다. 남아 있으면 운영자가 배정 목록을 보관하고 비운 뒤 upgrade 하고, 그룹을 만들어 고객사를 넣고
   그 직원에게 그룹을 배정해 범위를 다시 준다.

downgrade 는 그룹 · 그룹 배정 · tenants.group_id 를 버린다(손실). app_user_tenant_assignments 는 b492e7d1a605 와
같은 모양의 빈 테이블로 되살리고, 그룹 배정을 고객사 배정으로 풀어 옮기지 않는다 — 담당자는 배정 없음(고객사
접근 없음)으로 돌아간다(범위를 넓히지 않는다). GROUP_ASSIGNMENTS_CHANGED 값과 그 이력 행은 남는다
(다시 upgrade 해도 IF NOT EXISTS 라 안전하다).

Revision ID: c572e4a9b210
Revises: c597d1e2f3a4
Create Date: 2026-11-09 10:00:00.000000
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "c572e4a9b210"
down_revision = "c597d1e2f3a4"
branch_labels = None
depends_on = None

_UUID = sa.Uuid(as_uuid=False)
_GROUPS = "tenant_groups"
_GROUP_ASSIGNMENTS = "app_user_group_assignments"
_TENANT_ASSIGNMENTS = "app_user_tenant_assignments"


def upgrade() -> None:
    remaining = op.get_bind().execute(
        sa.text(f"SELECT count(*) FROM {_TENANT_ASSIGNMENTS}")
    ).scalar_one()
    if remaining:
        raise RuntimeError(
            f"{_TENANT_ASSIGNMENTS} 에 고객사 단위 직접 배정 {remaining}건이 남아 있어 upgrade 를 멈춘다. "
            "이 리비전은 직접 배정을 지우거나 무시하지 않는다 — upgrade 전에 운영자가 그 배정을 그룹으로 옮길 방법을 "
            "정하고(배정 목록 보관 → 직접 배정 비움), upgrade 뒤 그룹을 만들어 고객사를 넣고 그 직원에게 그룹을 배정한다."
        )
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE user_event_action ADD VALUE IF NOT EXISTS 'GROUP_ASSIGNMENTS_CHANGED'")
    op.create_table(
        _GROUPS,
        sa.Column("group_id", _UUID, nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("group_id", name=op.f("pk_tenant_groups")),
        sa.UniqueConstraint("name", name=op.f("uq_tenant_groups_name")),
    )
    op.add_column("tenants", sa.Column("group_id", _UUID, nullable=True))
    op.create_foreign_key(
        op.f("fk_tenants_group_id_tenant_groups"),
        "tenants",
        _GROUPS,
        ["group_id"],
        ["group_id"],
        ondelete="SET NULL",
    )
    op.create_index(op.f("ix_tenants_group_id"), "tenants", ["group_id"], unique=False)
    op.create_table(
        _GROUP_ASSIGNMENTS,
        sa.Column("user_id", _UUID, nullable=False),
        sa.Column("group_id", _UUID, nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["app_users.user_id"],
            name=op.f("fk_app_user_group_assignments_user_id_app_users"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["group_id"],
            ["tenant_groups.group_id"],
            name=op.f("fk_app_user_group_assignments_group_id_tenant_groups"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("user_id", "group_id", name=op.f("pk_app_user_group_assignments")),
    )
    op.create_index(
        op.f("ix_app_user_group_assignments_group_id"), _GROUP_ASSIGNMENTS, ["group_id"], unique=False
    )
    op.drop_index(op.f("ix_app_user_tenant_assignments_tenant_id"), table_name=_TENANT_ASSIGNMENTS)
    op.drop_table(_TENANT_ASSIGNMENTS)


def downgrade() -> None:
    # b492e7d1a605 와 같은 모양의 빈 테이블 — 그룹 배정을 고객사 배정으로 풀어 옮기지 않는다(범위를 넓히지 않는다)
    op.create_table(
        _TENANT_ASSIGNMENTS,
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
        op.f("ix_app_user_tenant_assignments_tenant_id"), _TENANT_ASSIGNMENTS, ["tenant_id"], unique=False
    )
    op.drop_index(op.f("ix_app_user_group_assignments_group_id"), table_name=_GROUP_ASSIGNMENTS)
    op.drop_table(_GROUP_ASSIGNMENTS)
    op.drop_index(op.f("ix_tenants_group_id"), table_name="tenants")
    op.drop_constraint(op.f("fk_tenants_group_id_tenant_groups"), "tenants", type_="foreignkey")
    op.drop_column("tenants", "group_id")
    op.drop_table(_GROUPS)
