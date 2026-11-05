"""#528 모의 Marketplace 병합 리비전 2개 — 실제 PostgreSQL/Alembic.

- f528a565e001: #528(e528b3c7d914)와 #515·#552·#565 가지(a565c8d1e901).
- f528f565e001: 그 #528 head 와 T-7 고객 설정 head(f565c8d1a9e3 · #576).

지키는 것: 부모 2개(둘 다 그대로) · 단일 head · 각 부모에만 올라간 DB 가 head 로 올라가 다른 가지의
테이블을 얻고 자기 가지의 기록(구독 투영 · 옛 구독 보존 · 모의 계약 · 이벤트 / 고객 설정 · 정리 대기 ·
파일 바인딩 증거 / 두 가지가 함께 가진 신청 결정)을 보존 · ORM 일치.
테스트마다 고유 이름의 일회용 DB 를 만들고 지운다(공용 pg_engine DB 의 head 를 건드리지 않는다).
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from config import get_settings
from db import models
from sqlalchemy import create_engine, inspect, text

MERGE = "f528a565e001"
MARKETPLACE = "e528b3c7d914"
DECISIONS = "a565c8d1e901"
SETTINGS_MERGE = "f528f565e001"  # MERGE(#528 head)와 SETTINGS_HEAD 를 잇는 병합
SETTINGS_HEAD = "f565c8d1a9e3"  # #576 head — T-7 병합(f8c1a9e4f515)과 DECISIONS 를 잇는다
MARKETPLACE_TABLES = {"tenant_subscription_history", "mock_marketplace_agreements", "mock_marketplace_events"}
DEV_TABLES = {"totp_recovery_operations", "signup_requests", "signup_request_decisions"}
T7_TABLES = {"tenant_settings", "tenant_settings_cleanups", "notification_legacy_routes"}
CONFIG_PATH = Path(__file__).resolve().parents[2] / "alembic.ini"
TENANT_ID = "44444444-4444-4444-8444-444444444444"
NOW = datetime(2026, 11, 3, 9, 0, tzinfo=UTC)


@pytest.fixture()
def merge_db(pg_engine, monkeypatch):
    name = f"vigilantis_mkmerge_{uuid.uuid4().hex[:8]}"
    admin = create_engine(pg_engine.url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    url = pg_engine.url.set(database=name)
    monkeypatch.setenv("DATABASE_URL", url.render_as_string(hide_password=False))
    get_settings.cache_clear()
    engine = create_engine(url)
    try:
        yield Config(str(CONFIG_PATH)), engine
    finally:
        engine.dispose()
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()
        get_settings.cache_clear()


def _assert_combined_head(config, engine):
    [head] = ScriptDirectory.from_config(config).get_heads()
    with engine.connect() as conn:
        assert conn.scalar(text("SELECT version_num FROM alembic_version")) == head
        assert (MARKETPLACE_TABLES | DEV_TABLES | T7_TABLES) <= set(inspect(conn).get_table_names())
        assert compare_metadata(MigrationContext.configure(conn), models.Base.metadata) == []


def _insert_buyer(conn) -> str:
    return conn.execute(
        text(
            "INSERT INTO app_users (user_id, issuer, cognito_sub, email, role, account_type, tenant_id, "
            "status, created_at, updated_at) VALUES (gen_random_uuid(), 'https://issuer.example/mk', "
            "'mk-buyer', 'buyer@example.com', CAST('ADMIN' AS app_user_role), "
            "CAST('TENANT' AS app_user_account_type), :t, CAST('ACTIVE' AS app_user_status), :n, :n) "
            "RETURNING user_id"
        ),
        {"t": TENANT_ID, "n": NOW},
    ).scalar_one()


def _insert_signup_decision(conn) -> tuple[str, str]:
    """승인된 신청 + 그 결정을 남긴 플랫폼 담당자. (request_id, 담당자 user_id)."""
    request_id = conn.execute(
        text(
            "INSERT INTO signup_requests (request_id, idempotency_key, company_name, contact_name, email, "
            "status, created_at, updated_at) VALUES (gen_random_uuid(), gen_random_uuid(), 'mk corp', "
            "'mk contact', 'applicant@example.com', CAST('APPROVED' AS signup_request_status), :n, :n) "
            "RETURNING request_id"
        ),
        {"n": NOW},
    ).scalar_one()
    operator = conn.execute(
        text(
            "INSERT INTO app_users (user_id, issuer, cognito_sub, email, role, account_type, platform_role, "
            "status, created_at, updated_at) VALUES (gen_random_uuid(), 'https://issuer.example/mk', "
            "'mk-operator', 'operator@example.com', CAST('ADMIN' AS app_user_role), "
            "CAST('PLATFORM' AS app_user_account_type), CAST('TENANT_MANAGER' AS app_user_platform_role), "
            "CAST('ACTIVE' AS app_user_status), :n, :n) RETURNING user_id"
        ),
        {"n": NOW},
    ).scalar_one()
    conn.execute(
        text(
            "INSERT INTO signup_request_decisions (request_id, actor_user_id, actor_email, decided_at, "
            "outcome) VALUES (:r, :a, 'operator@example.com', :n, CAST('APPROVED' AS signup_request_status))"
        ),
        {"r": request_id, "a": operator, "n": NOW},
    )
    return request_id, operator


def _assert_signup_decision_kept(conn, request_id, operator) -> None:
    assert conn.execute(
        text(
            "SELECT d.outcome::text, d.actor_user_id = :a FROM signup_request_decisions d "
            "JOIN signup_requests s USING (request_id) WHERE s.request_id = :r"
        ),
        {"r": request_id, "a": operator},
    ).one() == ("APPROVED", True)


def test_merge_revision_joins_both_unchanged_parents_into_the_single_head():
    script = ScriptDirectory.from_config(Config(str(CONFIG_PATH)))
    assert set(script.get_revision(MERGE).down_revision) == {MARKETPLACE, DECISIONS}
    assert set(script.get_revision(MARKETPLACE).down_revision) == {"d528e1f3a6b2", "d492a544e001"}
    assert script.get_revision(DECISIONS).down_revision == "f515a552e001"
    assert set(script.get_revision(SETTINGS_MERGE).down_revision) == {MERGE, SETTINGS_HEAD}
    assert set(script.get_revision(SETTINGS_HEAD).down_revision) == {"f8c1a9e4f515", DECISIONS}
    [head] = script.get_heads()  # head 를 병합으로 고정하지 않는다 — 뒤에 리비전이 붙어도 깨지지 않는다
    assert {MERGE, SETTINGS_MERGE} <= {rev.revision for rev in script.walk_revisions(base="base", head=head)}


def test_marketplace_only_database_upgrades_keeping_projection_and_agreement(merge_db):
    config, engine = merge_db
    command.upgrade(config, MARKETPLACE)
    with engine.begin() as conn:
        assert DEV_TABLES.isdisjoint(inspect(conn).get_table_names())
        conn.execute(
            text(
                "INSERT INTO tenants (tenant_id, name, status, aws_account_id, access_role_arn, external_id, "
                "regions, collect_vpc_ids, auto_action_enabled, plan, period_start, period_end, "
                "subscription_source, created_at, updated_at) "
                "VALUES (:t, 'mk-merge', 'PENDING_CONNECTION', '123456789012', "
                "'arn:aws:iam::123456789012:role/vigilantis-access', :ext, '[\"ap-northeast-2\"]'::jsonb, "
                "'[]'::jsonb, false, 'pilot', '2026-10-01', '2026-10-31', "
                "CAST('MANUAL' AS subscription_source), :n, :n)"
            ),
            {"t": TENANT_ID, "ext": "c" * 32, "n": NOW},
        )
        buyer = _insert_buyer(conn)
        conn.execute(
            text(
                "INSERT INTO mock_marketplace_agreements (agreement_id, tenant_id, source, buyer_ref, "
                "license_ref, plan, status, auto_renew, term_number, revision, requested_by_user_id, "
                "requested_at, created_at, updated_at) VALUES (gen_random_uuid(), :t, "
                "CAST('MOCK_MARKETPLACE' AS subscription_source), :buyer_ref, :license_ref, 'Business', "
                "CAST('PENDING' AS mock_agreement_status), true, 0, 1, :u, :n, :n, :n)"
            ),
            {
                "t": TENANT_ID,
                "buyer_ref": "mock-buyer-" + "a" * 32,
                "license_ref": "mock-license-" + "b" * 32,
                "u": buyer,
                "n": NOW,
            },
        )

    command.upgrade(config, "head")

    _assert_combined_head(config, engine)
    with engine.connect() as conn:
        assert conn.execute(
            text(
                "SELECT plan, period_start, period_end, subscription_source::text, entitlement_ends_at "
                "FROM tenants WHERE tenant_id = :t"
            ),
            {"t": TENANT_ID},
        ).one() == ("pilot", date(2026, 10, 1), date(2026, 10, 31), "MANUAL", None)
        assert conn.execute(
            text("SELECT status::text, plan FROM mock_marketplace_agreements WHERE tenant_id = :t"),
            {"t": TENANT_ID},
        ).one() == ("PENDING", "Business")


def test_decisions_only_database_upgrades_keeping_signup_decision(merge_db):
    config, engine = merge_db
    command.upgrade(config, DECISIONS)
    with engine.begin() as conn:
        assert MARKETPLACE_TABLES.isdisjoint(inspect(conn).get_table_names())
        request_id, operator = _insert_signup_decision(conn)

    command.upgrade(config, "head")

    _assert_combined_head(config, engine)
    with engine.connect() as conn:
        _assert_signup_decision_kept(conn, request_id, operator)


def test_marketplace_head_database_upgrades_keeping_projection_history_agreement_and_event(merge_db):
    # #528 head(MERGE)까지 올라간 DB — T-7 설정 가지만 더 실행한다. 진행 중 모의 계약의 투영 · 이벤트와
    # 옛 재개 규칙이 남긴 구독 보존 행을 고치지 않는다
    config, engine = merge_db
    command.upgrade(config, MERGE)
    term_start = datetime(2026, 11, 2, 15, 0, tzinfo=UTC)
    term_end = datetime(2026, 12, 2, 15, 0, tzinfo=UTC)
    with engine.begin() as conn:
        assert T7_TABLES.isdisjoint(inspect(conn).get_table_names())
        conn.execute(
            text(
                "INSERT INTO tenants (tenant_id, name, status, aws_account_id, access_role_arn, external_id, "
                "regions, collect_vpc_ids, auto_action_enabled, last_verification_result, last_verified_at, "
                "plan, period_start, period_end, subscription_source, entitlement_ends_at, created_at, "
                "updated_at) VALUES (:t, 'mk-settings-merge', 'ACTIVE', '123456789012', "
                "'arn:aws:iam::123456789012:role/vigilantis-access', :ext, '[\"ap-northeast-2\"]'::jsonb, "
                "'[]'::jsonb, false, 'OK', :n, 'Business', '2026-11-03', '2026-12-02', "
                "CAST('MOCK_MARKETPLACE' AS subscription_source), :end, :n, :n)"
            ),
            {"t": TENANT_ID, "ext": "d" * 32, "end": term_end, "n": NOW},
        )
        conn.execute(
            text(
                "INSERT INTO tenant_subscription_history (history_id, tenant_id, plan, period_start, "
                "period_end, cancel_at_period_end, archived_at) "
                "VALUES (gen_random_uuid(), :t, 'pilot', '2026-01-01', '2026-01-31', false, :n)"
            ),
            {"t": TENANT_ID, "n": NOW},
        )
        buyer = _insert_buyer(conn)
        agreement_id = conn.execute(
            text(
                "INSERT INTO mock_marketplace_agreements (agreement_id, tenant_id, source, buyer_ref, "
                "license_ref, plan, status, auto_renew, anniversary_day, term_number, revision, "
                "requested_by_user_id, requested_at, activated_at, term_start, term_end, created_at, "
                "updated_at) VALUES (gen_random_uuid(), :t, CAST('MOCK_MARKETPLACE' AS subscription_source), "
                ":buyer_ref, :license_ref, 'Business', CAST('ACTIVE' AS mock_agreement_status), true, 3, 1, 2, "
                ":u, :n, :start, :start, :end, :n, :n) RETURNING agreement_id"
            ),
            {
                "t": TENANT_ID,
                "buyer_ref": "mock-buyer-" + "e" * 32,
                "license_ref": "mock-license-" + "f" * 32,
                "u": buyer,
                "start": term_start,
                "end": term_end,
                "n": NOW,
            },
        ).scalar_one()
        conn.execute(
            text(
                "INSERT INTO mock_marketplace_events (event_id, agreement_id, source, kind, dedupe_key, "
                "occurred_at, created_at, processed_at) VALUES (gen_random_uuid(), :a, "
                "CAST('MOCK_MARKETPLACE' AS subscription_source), "
                "CAST('ACTIVATED' AS mock_marketplace_event_kind), :key, :start, :start, :n)"
            ),
            {"a": agreement_id, "key": f"{agreement_id}:ACTIVATED:1", "start": term_start, "n": NOW},
        )
        request_id, operator = _insert_signup_decision(conn)

    command.upgrade(config, "head")

    _assert_combined_head(config, engine)
    with engine.connect() as conn:
        assert conn.execute(
            text(
                "SELECT status::text, plan, period_start, period_end, subscription_source::text, "
                "entitlement_ends_at FROM tenants WHERE tenant_id = :t"
            ),
            {"t": TENANT_ID},
        ).one() == ("ACTIVE", "Business", date(2026, 11, 3), date(2026, 12, 2), "MOCK_MARKETPLACE", term_end)
        assert conn.execute(
            text("SELECT plan, period_start, period_end FROM tenant_subscription_history WHERE tenant_id = :t"),
            {"t": TENANT_ID},
        ).one() == ("pilot", date(2026, 1, 1), date(2026, 1, 31))
        assert conn.execute(
            text(
                "SELECT status::text, term_number, revision, term_end FROM mock_marketplace_agreements "
                "WHERE agreement_id = :a"
            ),
            {"a": agreement_id},
        ).one() == ("ACTIVE", 1, 2, term_end)
        assert conn.execute(
            text("SELECT kind::text, processed_at FROM mock_marketplace_events WHERE agreement_id = :a"),
            {"a": agreement_id},
        ).one() == ("ACTIVATED", NOW)
        _assert_signup_decision_kept(conn, request_id, operator)


def test_settings_head_database_upgrades_keeping_settings_and_manual_subscription(merge_db):
    # #576 head(SETTINGS_HEAD)까지 올라간 DB — #528 가지 전체를 실행한다. 고객 설정 · 정리 대기 · 파일 바인딩
    # 증거를 보존하고, 운영자가 입력한 옛 구독은 값 그대로 원천 MANUAL 이 된다(모의 구매를 지어내지 않는다)
    config, engine = merge_db
    command.upgrade(config, SETTINGS_HEAD)
    with engine.begin() as conn:
        assert MARKETPLACE_TABLES.isdisjoint(inspect(conn).get_table_names())
        conn.execute(
            text(
                "INSERT INTO tenants (tenant_id, name, status, aws_account_id, access_role_arn, external_id, "
                "regions, collect_vpc_ids, auto_action_enabled, plan, period_start, period_end, created_at, "
                "updated_at) VALUES (:t, 'settings-merge', 'PENDING_CONNECTION', '123456789012', "
                "'arn:aws:iam::123456789012:role/vigilantis-access', :ext, '[\"ap-northeast-2\"]'::jsonb, "
                "'[]'::jsonb, false, 'pilot', '2026-10-01', '2026-10-31', :n, :n)"
            ),
            {"t": TENANT_ID, "ext": "a" * 32, "n": NOW},
        )
        conn.execute(
            text(
                "INSERT INTO tenant_settings (tenant_id, protected_tags, updated_at, slack_source, "
                "slack_secret_ref) VALUES (:t, CAST(:tags AS jsonb), :n, 'LEGACY_ENV', 'SLACK_WEBHOOK_MK')"
            ),
            {"t": TENANT_ID, "tags": '[{"key": "Env", "values": ["Prod"]}]', "n": NOW},
        )
        conn.execute(
            text(
                "INSERT INTO tenant_settings_cleanups (cleanup_id, tenant_id, resource_kind, purpose, region, "
                "resource_name, not_before) VALUES (gen_random_uuid(), :t, 'SECRET', 'RETIRED', "
                "'ap-northeast-2', 'vigilantis/tenant-slack/mk-retired', :n)"
            ),
            {"t": TENANT_ID, "n": NOW},
        )
        conn.execute(
            text(
                "INSERT INTO notification_legacy_routes (aws_account_id, channel, tenant_id, fingerprint) "
                "VALUES ('210987654321', 'SLACK', 'deleted-tenant', :fp)"
            ),
            {"fp": "0" * 64},
        )
        request_id, operator = _insert_signup_decision(conn)

    command.upgrade(config, "head")

    _assert_combined_head(config, engine)
    with engine.connect() as conn:
        assert conn.execute(
            text(
                "SELECT plan, period_start, period_end, subscription_source::text, entitlement_ends_at "
                "FROM tenants WHERE tenant_id = :t"
            ),
            {"t": TENANT_ID},
        ).one() == ("pilot", date(2026, 10, 1), date(2026, 10, 31), "MANUAL", None)
        assert conn.scalar(text("SELECT count(*) FROM mock_marketplace_agreements")) == 0
        assert conn.execute(
            text(
                "SELECT protected_tags->0->>'key', slack_source, slack_secret_ref FROM tenant_settings "
                "WHERE tenant_id = :t"
            ),
            {"t": TENANT_ID},
        ).one() == ("Env", "LEGACY_ENV", "SLACK_WEBHOOK_MK")
        assert conn.execute(
            text("SELECT resource_kind, purpose, resource_name FROM tenant_settings_cleanups WHERE tenant_id = :t"),
            {"t": TENANT_ID},
        ).one() == ("SECRET", "RETIRED", "vigilantis/tenant-slack/mk-retired")
        assert conn.scalar(
            text("SELECT fingerprint FROM notification_legacy_routes WHERE tenant_id = 'deleted-tenant'")
        ) == "0" * 64
        _assert_signup_decision_kept(conn, request_id, operator)
