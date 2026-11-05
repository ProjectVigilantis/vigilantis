"""고객 구독 마이그레이션 (Post-MVP T-16 · #528) — 실제 PostgreSQL/Alembic.

① a6d2f8c1b347(tenants 구독 칸 + tenant_subscription_history): 기존 테넌트의 구독은 그대로 남는다 — 옛 요금제
   이름(basic · pilot)과 기간을 고치지 않고, NULL 구독을 Free 로 채우지 않으며, 새 칸은 예약 없음 · 기준일 없음 ·
   요금 미정이다(지어낸 값이 없다). 이 리비전의 부모는 그대로이고, 단일 head 는 이 리비전과 #492 가지를 잇는
   병합 리비전들을 지난다.
② d528e1f3a6b2(모의 Marketplace 계약 · 이벤트 + 투영 원천): 이미 적용된 c528a4d9e710 뒤에 붙고, #492 등급 ·
   감사 기록 병합(d492a544e001)과는 빈 병합(e528b3c7d914)으로 만난다. 옛 #528 head(c528)까지만 적용된 DB 가 그대로
   올라간다. 구독 정보가 있는 기존 행의 원천은 운영자 입력(MANUAL)이고 값은 그대로다 — Marketplace 구매를 소급해
   만들지 않는다. 비어 있는 행은 등록 안 됨(NULL) 그대로이고 옛 재개 이력은 보존된다. 기존 행은 계약 처리 대상이
   아니다. 올린 뒤 ORM 과 같다.
옛 DB 는 공용 DB 를 내려서 만들지 않는다 — head 에서 내리면 #492 담당 배정(b492e7d1a605 · 손실 downgrade)까지
내려간다. 테스트마다 빈 일회용 DB 를 그 리비전까지만 올린다.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
import subscription_workflow
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from config import get_settings
from db import models
from schemas.subscriptions import SubscriptionSource
from sqlalchemy import column, create_engine, func, insert, inspect, select, table, text
from sqlalchemy.orm import Session

REVISION = "a6d2f8c1b347"
PARENT = "f514a6c3d201"
SUBSCRIPTION_MERGE = "b528d7e4c913"  # 이 리비전 + #492 가지의 빈 병합
ACCOUNT_SCOPE_MERGE = "c492d6e8f4a2"
AUDIT_MERGE = "c528a4d9e710"  # 구독 + 감사 기록 가지의 빈 병합 — 이미 적용된 마지막 리비전
MOCK_MARKETPLACE = "d528e1f3a6b2"
PLATFORM_AUDIT_MERGE = "d492a544e001"  # #492 등급 · 담당 배정 + 감사 기록 가지의 빈 병합
HEAD_MERGE = "e528b3c7d914"  # 이 가지(d528) + d492 의 빈 병합
NEW_COLUMNS = {"cancel_at_period_end", "billing_anchor_day", "amount_minor", "currency"}
SOURCE_COLUMNS = {"subscription_source", "entitlement_ends_at"}
MOCK_TABLES = {"mock_marketplace_agreements", "mock_marketplace_events"}

# 옛 리비전에는 ORM 이 아니라 그때의 칸으로 넣는다
_OLD_TENANTS = table("tenants", *(column(name) for name in (
    "tenant_id", "name", "status", "aws_account_id", "access_role_arn", "external_id", "regions",
    "collect_vpc_ids", "auto_action_enabled", "last_verification_result", "last_verified_at",
    "plan", "period_start", "period_end", "created_at", "updated_at")))


def _old_row(account: str, **subscription) -> dict:
    return dict(
        tenant_id=str(uuid.uuid4()), name="학원", status=text("'ACTIVE'"), aws_account_id=account,
        access_role_arn=f"arn:aws:iam::{account}:role/vigilantis-access", external_id=uuid.uuid4().hex,
        regions=text("'[\"ap-northeast-2\"]'::jsonb"), collect_vpc_ids=text("'[]'::jsonb"),
        auto_action_enabled=True, last_verification_result=text("'OK'"), last_verified_at=text("now()"),
        created_at=text("now()"), updated_at=text("now()"),
        plan=None, period_start=None, period_end=None,
    ) | subscription


@pytest.fixture()
def scratch_db(pg_engine, monkeypatch):
    """(Config, engine) — 같은 서버의 새 빈 DB. 끝나면 DROP."""
    name = f"vigilantis_sub_{uuid.uuid4().hex[:8]}"
    admin = create_engine(pg_engine.url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    url = pg_engine.url.set(database=name)
    monkeypatch.setenv("DATABASE_URL", url.render_as_string(hide_password=False))
    get_settings.cache_clear()
    engine = create_engine(url)
    try:
        yield Config(str(Path(__file__).resolve().parents[2] / "alembic.ini")), engine
    finally:
        engine.dispose()
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()
        get_settings.cache_clear()


def test_existing_subscriptions_survive_unchanged_and_are_not_renewed(scratch_db):
    config, engine = scratch_db
    script = ScriptDirectory.from_config(config)
    [head] = script.get_heads()
    assert SUBSCRIPTION_MERGE in {rev.revision for rev in script.walk_revisions("base", head)}
    assert script.get_revision(REVISION).down_revision == PARENT
    assert set(script.get_revision(SUBSCRIPTION_MERGE).down_revision) == {REVISION, ACCOUNT_SCOPE_MERGE}

    # 이미 지난 기간을 가진 옛 행 — 마이그레이션 뒤에도 갱신 · 전환 대상이 되면 안 된다
    legacy = {
        "basic": _old_row("200000000001", plan="basic", period_start=date(2026, 1, 1), period_end=date(2026, 1, 31)),
        "business": _old_row("200000000002", plan="Business", period_start=date(2026, 9, 1),
                             period_end=date(2026, 9, 30)),
        "unregistered": _old_row("200000000003"),
    }
    command.upgrade(config, PARENT)
    assert NEW_COLUMNS.isdisjoint(c["name"] for c in inspect(engine).get_columns("tenants"))
    assert "tenant_subscription_history" not in inspect(engine).get_table_names()
    with engine.begin() as conn:
        for row in legacy.values():
            conn.execute(insert(_OLD_TENANTS).values(**row))
    command.upgrade(config, "head")
    ids = {name: row["tenant_id"] for name, row in legacy.items()}
    with Session(engine) as session:
        stored = {
            name: session.execute(
                select(models.Tenant.plan, models.Tenant.period_start, models.Tenant.period_end,
                       models.Tenant.cancel_at_period_end, models.Tenant.billing_anchor_day,
                       models.Tenant.amount_minor, models.Tenant.currency)
                .where(models.Tenant.tenant_id == tenant_id)).one()
            for name, tenant_id in ids.items()
        }
        assert {name: tuple(row) for name, row in stored.items()} == {
            "basic": ("basic", date(2026, 1, 1), date(2026, 1, 31), False, None, None, None),
            "business": ("Business", date(2026, 9, 1), date(2026, 9, 30), False, None, None, None),
            # NULL = 미등록 — Free 로 채우지 않는다
            "unregistered": (None, None, None, False, None, None, None),
        }
        # 원천은 운영자 입력 · 미등록 그대로다 — 계약을 지어내지 않으니 계약 처리 · 투영 대상이 아니다
        sources = dict(session.execute(
            select(models.Tenant.tenant_id, models.Tenant.subscription_source)
            .where(models.Tenant.tenant_id.in_(list(ids.values())))).all())
        assert {name: sources[tenant_id] for name, tenant_id in ids.items()} == {
            "basic": SubscriptionSource.MANUAL, "business": SubscriptionSource.MANUAL, "unregistered": None}
        assert session.scalar(select(func.count()).select_from(models.MockMarketplaceAgreement)) == 0
        assert not any(
            subscription_workflow.reconcile_tenant_subscription(session, tenant_id, now=datetime.now(UTC))
            for tenant_id in ids.values())
        assert session.execute(text("SELECT count(*) FROM tenant_subscription_history")).scalar_one() == 0
    with engine.connect() as conn:
        assert compare_metadata(MigrationContext.configure(conn), models.Base.metadata) == []
        constraints = {c["name"] for c in inspect(conn).get_check_constraints("tenants")}
        assert {
            "ck_tenants_amount_with_currency", "ck_tenants_amount_range", "ck_tenants_currency_format",
            "ck_tenants_billing_anchor_day_range", "ck_tenants_billing_anchor_with_period",
            "ck_tenants_cancel_with_period_end",
        } <= constraints


# c528a4d9e710 의 tenants 모양(구독 칸 7개 포함) · 옛 재개 이력
_APPLIED_TENANTS = table("tenants", *(column(c.name) for c in _OLD_TENANTS.c), *(
    column(name) for name in ("cancel_at_period_end", "billing_anchor_day", "amount_minor", "currency")))
_APPLIED_HISTORY = table("tenant_subscription_history", *(column(name) for name in (
    "history_id", "tenant_id", "plan", "period_start", "period_end", "cancel_at_period_end", "archived_at")))


def _applied_row(account: str, **subscription) -> dict:
    return _old_row(account) | {
        "cancel_at_period_end": False, "billing_anchor_day": None, "amount_minor": None, "currency": None,
    } | subscription


def test_mock_marketplace_revision_keeps_applied_subscriptions_and_history(scratch_db):
    config, engine = scratch_db
    script = ScriptDirectory.from_config(config)
    [head] = script.get_heads()
    # 이미 적용된 리비전을 고치지 않고 그 뒤에 붙고, #492 가지와는 빈 병합으로 만난다(어느 부모도 고치지 않는다)
    assert script.get_revision(MOCK_MARKETPLACE).down_revision == AUDIT_MERGE
    assert set(script.get_revision(HEAD_MERGE).down_revision) == {MOCK_MARKETPLACE, PLATFORM_AUDIT_MERGE}
    assert {MOCK_MARKETPLACE, HEAD_MERGE} <= {rev.revision for rev in script.walk_revisions("base", head)}

    applied = {
        "manual-scheduled": _applied_row(
            "200000000011", plan="Business", period_start=date(2026, 10, 31), period_end=date(2026, 11, 29),
            cancel_at_period_end=True, billing_anchor_day=31, amount_minor=990000, currency="KRW"),
        "free": _applied_row("200000000012", plan="Free"),
        "dates-without-plan": _applied_row("200000000013", period_end=date(2026, 12, 31)),
        "unregistered": _applied_row("200000000014"),
    }
    ids = {name: row["tenant_id"] for name, row in applied.items()}
    history_id = str(uuid.uuid4())
    # 검증 DB 처럼 옛 #528 head(c528)까지만 적용된 DB — #492 등급 · 담당 배정 가지(b492 → d492)는 아직 없다
    command.upgrade(config, AUDIT_MERGE)
    with engine.connect() as conn:
        assert set(conn.execute(text("SELECT version_num FROM alembic_version")).scalars()) == {AUDIT_MERGE}
    assert SOURCE_COLUMNS.isdisjoint(c["name"] for c in inspect(engine).get_columns("tenants"))
    assert (MOCK_TABLES | {"app_user_tenant_assignments"}).isdisjoint(inspect(engine).get_table_names())
    with engine.begin() as conn:
        for row in applied.values():
            conn.execute(insert(_APPLIED_TENANTS).values(**row))
        conn.execute(insert(_APPLIED_HISTORY).values(
            history_id=history_id, tenant_id=ids["free"], plan="Business", period_start=date(2026, 9, 1),
            period_end=date(2026, 9, 30), cancel_at_period_end=False, archived_at=text("now()")))
    command.upgrade(config, "head")
    with Session(engine) as session:
        stored = {
            name: tuple(session.execute(
                select(models.Tenant.plan, models.Tenant.period_start, models.Tenant.period_end,
                       models.Tenant.cancel_at_period_end, models.Tenant.billing_anchor_day,
                       models.Tenant.amount_minor, models.Tenant.currency, models.Tenant.subscription_source,
                       models.Tenant.entitlement_ends_at)
                .where(models.Tenant.tenant_id == tenant_id)).one())
            for name, tenant_id in ids.items()
        }
        assert stored == {
            "manual-scheduled": ("Business", date(2026, 10, 31), date(2026, 11, 29), True, 31, 990000, "KRW",
                                 SubscriptionSource.MANUAL, None),
            "free": ("Free", None, None, False, None, None, None, SubscriptionSource.MANUAL, None),
            "dates-without-plan": (None, None, date(2026, 12, 31), False, None, None, None,
                                   SubscriptionSource.MANUAL, None),
            "unregistered": (None, None, None, False, None, None, None, None, None),
        }
        [history] = session.execute(
            select(models.TenantSubscriptionHistory.history_id, models.TenantSubscriptionHistory.plan)
            .where(models.TenantSubscriptionHistory.tenant_id == ids["free"])).all()
        assert tuple(history) == (history_id, "Business")
        assert session.scalar(select(func.count()).select_from(models.MockMarketplaceAgreement)) == 0
    with engine.connect() as conn:
        assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == head
        assert compare_metadata(MigrationContext.configure(conn), models.Base.metadata) == []
