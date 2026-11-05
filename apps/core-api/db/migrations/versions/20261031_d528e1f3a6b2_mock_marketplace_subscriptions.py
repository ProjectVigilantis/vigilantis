"""모의 Marketplace 계약 · 이벤트 + tenants 구독 투영의 원천 — 고객 구독 관리 (Post-MVP T-16 · #528)

1. tenants 에 투영의 원천 2칸: subscription_source(MANUAL · MOCK_MARKETPLACE · APP_FREE · NULL = 등록 안 됨)와
   entitlement_ends_at(모의 계약 투영의 권리 끝 시각). 계약 모양을 CHECK 로 거울한다.
2. mock_marketplace_agreements — 유료 권리의 권위 있는 원천(서버 안의 모의 공급자). 테넌트마다 진행 중
   (PENDING · ACTIVE) 계약은 하나다(부분 유일 인덱스).
3. mock_marketplace_events — 모의 공급자의 전이와 같은 트랜잭션에 남는 내구 이벤트(중복 제거 키 유일).

**기존 행은 그대로 둔다.** 구독 칸(옛 요금제 이름 · 기간 · 예약 · 기준일 · 요금)을 고치지 않고, 구독 정보가
있는 행의 원천은 운영자 입력(MANUAL)으로 둔다 — Marketplace 구매를 소급해 지어내지 않는다. 비어 있는 행은
NULL(등록 안 됨) 그대로다. tenant_subscription_history 의 행도 그대로다(읽기 전용 옛 기록).
이미 적용된 리비전(a6d2f8c1b347 · b528d7e4c913 · c528a4d9e710)은 고치지 않고 그 뒤에 붙인다.

downgrade 는 모의 계약 · 이벤트와 원천 칸을 버린다 — 구독 칸 자체는 남는다.

Revision ID: d528e1f3a6b2
Revises: c528a4d9e710
Create Date: 2026-10-31 10:00:00.000000
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "d528e1f3a6b2"
down_revision = "c528a4d9e710"
branch_labels = None
depends_on = None

_UUID = sa.Uuid(as_uuid=False)
_SOURCES = ("MANUAL", "MOCK_MARKETPLACE", "APP_FREE")
_AGREEMENT_STATUSES = ("PENDING", "ACTIVE", "FAILED", "EXPIRED")
_EVENT_KINDS = ("ACTIVATED", "ACTIVATION_FAILED", "RENEWAL_DISABLED", "RENEWED", "EXPIRED")

_NO_SUBSCRIPTION_FACTS = (
    "plan IS NULL AND period_start IS NULL AND period_end IS NULL AND NOT cancel_at_period_end"
    " AND billing_anchor_day IS NULL AND amount_minor IS NULL AND currency IS NULL"
)

# db.models._SUBSCRIPTION_SOURCE_SHAPE 와 같은 식이다(이름은 ck_tenants_<이름>)
_TENANT_CHECKS = {
    "source_iff_subscription": f"(subscription_source IS NULL) = ({_NO_SUBSCRIPTION_FACTS})",
    "entitlement_end_with_mock": (
        "(subscription_source IS NOT DISTINCT FROM 'MOCK_MARKETPLACE') = (entitlement_ends_at IS NOT NULL)"
    ),
    "mock_projection_shape": (
        "subscription_source IS DISTINCT FROM 'MOCK_MARKETPLACE' OR (plan IN ('Business', 'Enterprise')"
        " AND period_start IS NOT NULL AND period_end IS NOT NULL AND amount_minor IS NULL)"
    ),
    "app_free_is_free": "subscription_source IS DISTINCT FROM 'APP_FREE' OR plan = 'Free'",
}

# db.models._MOCK_AGREEMENT_SHAPE 와 같은 식이다(이름은 ck_mock_marketplace_agreements_<이름>)
_AGREEMENT_CHECKS = {
    "source_is_mock": "source = 'MOCK_MARKETPLACE'",
    "buyer_ref_format": "buyer_ref ~ '^mock-buyer-[0-9a-f]{32}$'",
    "license_ref_format": "license_ref ~ '^mock-license-[0-9a-f]{32}$'",
    "plan_is_paid": "plan IN ('Business', 'Enterprise')",
    "anniversary_day_range": "anniversary_day IS NULL OR anniversary_day BETWEEN 1 AND 31",
    "renewal_disabled_shape": "auto_renew = (renewal_disabled_at IS NULL)",
    "activation_shape": (
        "(activated_at IS NULL) = (term_start IS NULL) AND (term_start IS NULL) = (term_end IS NULL)"
        " AND (term_end IS NULL) = (anniversary_day IS NULL) AND (activated_at IS NULL) = (term_number = 0)"
        " AND (status IN ('ACTIVE', 'EXPIRED')) = (activated_at IS NOT NULL)"
    ),
    "term_ordered": "term_end IS NULL OR term_end > term_start",
    "ended_shape": "(status IN ('PENDING', 'ACTIVE')) = (ended_at IS NULL)",
    "failure_shape": "(status = 'FAILED') = (failure_code IS NOT NULL)",
    "expired_shape": "status <> 'EXPIRED' OR (NOT auto_renew AND ended_at = term_end)",
    "revision_positive": "revision >= 1 AND term_number >= 0",
}


def _enum(name: str):
    return postgresql.ENUM(name=name, create_type=False)


def upgrade() -> None:
    bind = op.get_bind()
    postgresql.ENUM(*_SOURCES, name="subscription_source").create(bind, checkfirst=False)
    postgresql.ENUM(*_AGREEMENT_STATUSES, name="mock_agreement_status").create(bind, checkfirst=False)
    postgresql.ENUM(*_EVENT_KINDS, name="mock_marketplace_event_kind").create(bind, checkfirst=False)

    op.add_column("tenants", sa.Column("subscription_source", _enum("subscription_source"), nullable=True))
    op.add_column("tenants", sa.Column("entitlement_ends_at", sa.DateTime(timezone=True), nullable=True))
    # 구독 정보가 있는 기존 행 = 운영자 입력(이 변경 전의 기록). 값은 고치지 않는다
    op.execute(
        f"UPDATE tenants SET subscription_source = 'MANUAL'::subscription_source WHERE NOT ({_NO_SUBSCRIPTION_FACTS})"
    )
    for name, sql in _TENANT_CHECKS.items():
        op.create_check_constraint(op.f(f"ck_tenants_{name}"), "tenants", sql)

    op.create_table(
        "mock_marketplace_agreements",
        sa.Column("agreement_id", _UUID, nullable=False),
        sa.Column("tenant_id", _UUID, nullable=False),
        sa.Column("source", _enum("subscription_source"), nullable=False),
        sa.Column("buyer_ref", sa.String(length=64), nullable=False),
        sa.Column("license_ref", sa.String(length=64), nullable=False),
        sa.Column("plan", sa.String(length=50), nullable=False),
        sa.Column("status", _enum("mock_agreement_status"), nullable=False),
        sa.Column("auto_renew", sa.Boolean(), nullable=False),
        sa.Column("anniversary_day", sa.SmallInteger(), nullable=True),
        sa.Column("term_number", sa.Integer(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("requested_by_user_id", _UUID, nullable=False),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("term_start", sa.DateTime(timezone=True), nullable=True),
        sa.Column("term_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("renewal_disabled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failure_code", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        *(
            sa.CheckConstraint(sql, name=op.f(f"ck_mock_marketplace_agreements_{name}"))
            for name, sql in _AGREEMENT_CHECKS.items()
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.tenant_id"],
            name=op.f("fk_mock_marketplace_agreements_tenant_id_tenants"),
        ),
        sa.ForeignKeyConstraint(
            ["requested_by_user_id"], ["app_users.user_id"],
            name=op.f("fk_mock_marketplace_agreements_requested_by_user_id_app_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("agreement_id", name=op.f("pk_mock_marketplace_agreements")),
        sa.UniqueConstraint("license_ref", name=op.f("uq_mock_marketplace_agreements_license_ref")),
    )
    op.create_index(
        op.f("ix_mock_marketplace_agreements_tenant_id"), "mock_marketplace_agreements", ["tenant_id"]
    )
    op.create_index(
        op.f("ix_mock_marketplace_agreements_requested_by_user_id"),
        "mock_marketplace_agreements",
        ["requested_by_user_id"],
    )
    op.create_index(
        "uq_mock_marketplace_agreements_open_tenant",
        "mock_marketplace_agreements",
        ["tenant_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('PENDING', 'ACTIVE')"),
    )

    op.create_table(
        "mock_marketplace_events",
        sa.Column("event_id", _UUID, nullable=False),
        sa.Column("agreement_id", _UUID, nullable=False),
        sa.Column("source", _enum("subscription_source"), nullable=False),
        sa.Column("kind", _enum("mock_marketplace_event_kind"), nullable=False),
        sa.Column("dedupe_key", sa.String(length=200), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("source = 'MOCK_MARKETPLACE'", name=op.f("ck_mock_marketplace_events_source_is_mock")),
        sa.ForeignKeyConstraint(
            ["agreement_id"], ["mock_marketplace_agreements.agreement_id"],
            name=op.f("fk_mock_marketplace_events_agreement_id_mock_marketplace_agreements"),
        ),
        sa.PrimaryKeyConstraint("event_id", name=op.f("pk_mock_marketplace_events")),
        sa.UniqueConstraint("dedupe_key", name=op.f("uq_mock_marketplace_events_dedupe_key")),
    )
    op.create_index(
        op.f("ix_mock_marketplace_events_agreement_id"), "mock_marketplace_events", ["agreement_id"]
    )
    op.create_index(
        "ix_mock_marketplace_events_unprocessed",
        "mock_marketplace_events",
        ["created_at"],
        postgresql_where=sa.text("processed_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_mock_marketplace_events_unprocessed", table_name="mock_marketplace_events")
    op.drop_index(op.f("ix_mock_marketplace_events_agreement_id"), table_name="mock_marketplace_events")
    op.drop_table("mock_marketplace_events")
    op.drop_index("uq_mock_marketplace_agreements_open_tenant", table_name="mock_marketplace_agreements")
    op.drop_index(
        op.f("ix_mock_marketplace_agreements_requested_by_user_id"), table_name="mock_marketplace_agreements"
    )
    op.drop_index(op.f("ix_mock_marketplace_agreements_tenant_id"), table_name="mock_marketplace_agreements")
    op.drop_table("mock_marketplace_agreements")
    for name in reversed(_TENANT_CHECKS):
        op.drop_constraint(op.f(f"ck_tenants_{name}"), "tenants", type_="check")
    op.drop_column("tenants", "entitlement_ends_at")
    op.drop_column("tenants", "subscription_source")
    bind = op.get_bind()
    for name in ("mock_marketplace_event_kind", "mock_agreement_status", "subscription_source"):
        postgresql.ENUM(name=name).drop(bind, checkfirst=False)
