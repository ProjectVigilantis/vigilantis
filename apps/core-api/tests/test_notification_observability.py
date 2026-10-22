"""#493 PR3: 막힌 알림 설정의 관측성. 안전한 계정·채널·사유만 남기고 건별 소음은 없다. DB가 필요 없다."""

import json
import logging
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from notifications.dispatcher import DeliveryDispatcher
from notifications.routing import (
    CHANNEL_NOT_CONFIGURED,
    OWNERSHIP_CONFLICT,
    OWNERSHIP_MISSING,
    SECRET_MISSING,
    RouteRegistry,
)
from notifications.runtime import warn_blocked_routes

WEBHOOK = "https://hooks.slack.com/services/T0AAA/B0BBB/secretToken123"
ACCOUNT_A = "111111111111"
ACCOUNT_B = "222222222222"
ACCOUNT_UNDECLARED = "333333333333"
TENANT_A = "11111111-1111-4111-8111-111111111111"
TENANT_B = "22222222-2222-4222-8222-222222222222"
TENANT_C = "33333333-3333-4333-8333-333333333333"
TENANT_D = "44444444-4444-4444-8444-444444444444"
ROUTE_LOGGER = "vigilantis.notifications"
DISPATCH_LOGGER = "vigilantis.notifications.dispatcher"


def _registry(routes, environ=None):
    return RouteRegistry.from_mapping({"routes": routes}, environ or {})


def _records(caplog, name, logger_name):
    return [r for r in caplog.records if r.getMessage() == name and r.name == logger_name]


def _dispatcher(registry, senders=("SLACK", "SNS")):
    return DeliveryDispatcher(
        lambda: None, registry, {c: object() for c in senders}, public_base_url="https://example.com",
    )


def _event(account):
    now = datetime.now(UTC)
    return SimpleNamespace(
        event_id=f"evt-{account}", aws_account_id=account,
        target_arn=f"arn:aws:ec2:ap-northeast-2:{account}:instance/i-1",
        created_at=now, occurred_at=now,
    )


def test_secret_missing_warns_once_with_safe_fields(caplog):
    registry = _registry([
        {"tenant_id": TENANT_A, "aws_account_id": ACCOUNT_A, "slack_webhook_env": "HOOK_A"},
    ])
    with caplog.at_level(logging.WARNING, logger=ROUTE_LOGGER):
        warn_blocked_routes(registry)
    (record,) = _records(caplog, "notification_route_blocked", ROUTE_LOGGER)
    assert (record.aws_account_id, record.channel, record.reason) == (ACCOUNT_A, "SLACK", SECRET_MISSING)
    assert "HOOK_A" not in caplog.text and TENANT_A not in caplog.text


def test_ownership_conflict_warns_for_account_only(caplog):
    registry = _registry([
        {"tenant_id": TENANT_A, "aws_account_id": ACCOUNT_A},
        {"tenant_id": TENANT_B, "aws_account_id": ACCOUNT_A},
    ])
    with caplog.at_level(logging.WARNING, logger=ROUTE_LOGGER):
        warn_blocked_routes(registry)
    (record,) = _records(caplog, "notification_route_blocked", ROUTE_LOGGER)
    assert (record.aws_account_id, record.channel, record.reason) == (ACCOUNT_A, None, OWNERSHIP_CONFLICT)
    assert TENANT_A not in caplog.text and TENANT_B not in caplog.text


def test_healthy_single_channel_config_is_silent(caplog):
    registry = _registry(
        [
            {"tenant_id": TENANT_A, "aws_account_id": ACCOUNT_A, "slack_webhook_env": "HOOK_A"},
            {"tenant_id": TENANT_B, "aws_account_id": ACCOUNT_B},  # 채널을 일부러 비운 계정
        ],
        {"HOOK_A": WEBHOOK},
    )
    with caplog.at_level(logging.DEBUG):
        warn_blocked_routes(registry)
    assert [r for r in caplog.records if r.name.startswith(ROUTE_LOGGER)] == []
    assert WEBHOOK not in caplog.text


def test_blocked_routes_warn_once_per_account_channel_and_never_leak_webhook(caplog):
    registry = _registry(
        [
            {"tenant_id": TENANT_A, "aws_account_id": ACCOUNT_A, "slack_webhook_env": "HOOK_A"},
            {"tenant_id": TENANT_B, "aws_account_id": ACCOUNT_B, "slack_webhook_env": "HOOK_B"},
        ],
        {"HOOK_A": WEBHOOK},  # B 만 비밀 누락
    )
    with caplog.at_level(logging.WARNING, logger=ROUTE_LOGGER):
        warn_blocked_routes(registry)
    records = _records(caplog, "notification_route_blocked", ROUTE_LOGGER)
    assert [(r.aws_account_id, r.channel, r.reason) for r in records] == [(ACCOUNT_B, "SLACK", SECRET_MISSING)]
    assert WEBHOOK not in caplog.text


def test_undeclared_account_warns_once_across_events_and_channels(caplog):
    registry = _registry([{"tenant_id": TENANT_A, "aws_account_id": ACCOUNT_A}])
    dispatcher = _dispatcher(registry)
    with caplog.at_level(logging.WARNING, logger=DISPATCH_LOGGER):
        rows = [
            dispatcher._materialize(_event(ACCOUNT_UNDECLARED), channel, {}, {})
            for channel in ("SLACK", "SNS", "SLACK", "SNS")
        ]
    assert {row["state"] for row in rows} == {"BLOCKED"}
    assert {row["blocked_reason"] for row in rows} == {OWNERSHIP_MISSING}
    assert {row["tenant_id"] for row in rows} == {None}
    (record,) = _records(caplog, "notification_account_ownership_missing", DISPATCH_LOGGER)
    assert (record.aws_account_id, record.reason) == (ACCOUNT_UNDECLARED, OWNERSHIP_MISSING)


def test_startup_wiring_warns_through_build_notification_runtime(caplog, monkeypatch, tmp_path):
    import db.session as db_session
    from notifications.runtime import build_notification_runtime

    routes = tmp_path / "routes.json"
    routes.write_text(json.dumps({"routes": [
        {"tenant_id": TENANT_A, "aws_account_id": ACCOUNT_A, "slack_webhook_env": "HOOK_A"},
        {"tenant_id": TENANT_B, "aws_account_id": ACCOUNT_B},
        {"tenant_id": TENANT_C, "aws_account_id": "444444444444"},
        {"tenant_id": TENANT_D, "aws_account_id": "444444444444"},
    ]}), encoding="utf-8")
    monkeypatch.delenv("HOOK_A", raising=False)
    monkeypatch.setattr(db_session, "get_session_factory", lambda: (lambda: None))
    settings = SimpleNamespace(
        NOTIFICATION_ENABLED=True, NOTIFICATION_ROUTES_FILE=str(routes),
        NOTIFICATION_PUBLIC_BASE_URL="https://example.com",
        NOTIFICATION_MAX_BACKOFF_SECONDS=300, NOTIFICATION_BACKOFF_SECONDS=2,
        NOTIFICATION_LEASE_SECONDS=120, NOTIFICATION_CONNECT_TIMEOUT_SECONDS=3,
        NOTIFICATION_READ_TIMEOUT_SECONDS=5, NOTIFICATION_POLL_SECONDS=1,
        NOTIFICATION_BATCH_SIZE=20, NOTIFICATION_MAX_ATTEMPTS=5,
    )
    with caplog.at_level(logging.WARNING, logger=ROUTE_LOGGER):
        runtime = build_notification_runtime(settings)
    records = _records(caplog, "notification_route_blocked", ROUTE_LOGGER)
    assert [(r.aws_account_id, r.channel, r.reason) for r in records] == [
        (ACCOUNT_A, "SLACK", SECRET_MISSING),
        ("444444444444", None, OWNERSHIP_CONFLICT),
    ]
    assert runtime.senders == {}


def test_each_undeclared_account_warns_separately(caplog):
    dispatcher = _dispatcher(_registry([]))
    with caplog.at_level(logging.WARNING, logger=DISPATCH_LOGGER):
        for account in (ACCOUNT_A, ACCOUNT_B, ACCOUNT_A):
            dispatcher._materialize(_event(account), "SLACK", {}, {})
    records = _records(caplog, "notification_account_ownership_missing", DISPATCH_LOGGER)
    assert [r.aws_account_id for r in records] == [ACCOUNT_A, ACCOUNT_B]


def test_many_undeclared_accounts_are_all_observed(caplog):
    dispatcher = _dispatcher(_registry([]))
    accounts = [f"{n:012d}" for n in range(1, 1201)]
    with caplog.at_level(logging.WARNING, logger=DISPATCH_LOGGER):
        for account in accounts:
            dispatcher._materialize(_event(account), "SLACK", {}, {})
    records = _records(caplog, "notification_account_ownership_missing", DISPATCH_LOGGER)
    assert [r.aws_account_id for r in records] == accounts


@pytest.mark.parametrize("missing_channel", ["SLACK", "SNS"])
def test_intentionally_absent_channel_logs_nothing_per_event(caplog, missing_channel):
    registry = _registry([{"tenant_id": TENANT_A, "aws_account_id": ACCOUNT_A}])
    dispatcher = _dispatcher(registry)
    tenant_accounts = {TENANT_A: ACCOUNT_A}
    with caplog.at_level(logging.DEBUG):
        rows = [
            dispatcher._materialize(_event(ACCOUNT_A), missing_channel, {}, tenant_accounts)
            for _ in range(3)
        ]
    assert {row["blocked_reason"] for row in rows} == {CHANNEL_NOT_CONFIGURED}
    assert {row["state"] for row in rows} == {"BLOCKED"}
    assert [r for r in caplog.records if r.name.startswith(ROUTE_LOGGER)] == []
