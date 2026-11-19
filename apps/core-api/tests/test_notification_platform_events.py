"""#589 알림 사건별 종합 기록 — 실제 DB의 종결·동시성·롤백, 외부 발송은 전송 계층 대역."""

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from db import models
from db.repositories import notification_events as events_repo
from db.repositories import platform_events as activity_repo
from notifications import dispatcher as dispatcher_module
from notifications import routing
from notifications.contracts import SendResult
from schemas.platform_events import (
    PlatformActorKind,
    PlatformEventAction,
    PlatformEventOutcome,
    PlatformEventRecord,
)
from schemas.runbooks import TriggerSource
from sqlalchemy import Text, cast, func, select, update
from sqlalchemy import event as sql_event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from tests.platform_event_faults import failing_platform_event_write

from tests import test_notification_delivery as d

sessions = d.sessions

def _activities(engine, event_id):
    with Session(engine) as db:
        return list(db.scalars(
            select(models.PlatformEvent).where(
                models.PlatformEvent.action == "NOTIFICATION_SEND",
                models.PlatformEvent.target_id == event_id,
            )
        ))


def _ready(sessions, engine, **options):
    dispatcher = d.make_dispatcher(
        sessions, d.registry(d.entry(d.TENANT_A, d.ACCOUNT_A, slack="SLACK_A", sns=d.TOPIC_A)),
        **({"slack": d.SlackWire(), "sns": d.SnsWire()} | options),
    )
    d.activate(dispatcher, engine)
    return dispatcher


def _accepted(channel):
    return SendResult(
        receipt_kind={"SLACK": "SLACK_POSTED", "SNS": "SNS_ACCEPTED"}[channel],
        receipt_ref="provider-receipt",
    )


@pytest.mark.parametrize("channel", ["SLACK", "SNS"])
@pytest.mark.parametrize(
    "failure",
    [
        {"state": "BLOCKED", "blocked_reason": reason}
        for reason in (
            routing.ACCOUNT_MISSING,
            routing.ACCOUNT_INVALID,
            routing.TARGET_ARN_INVALID,
            routing.ARN_ACCOUNT_MISMATCH,
            routing.OWNERSHIP_MISSING,
            routing.OWNERSHIP_CONFLICT,
            routing.CHANNEL_UNAVAILABLE,
            routing.SECRET_MISSING,
            routing.DESTINATION_INVALID,
            routing.DESTINATION_CONFLICT,
            routing.DESTINATION_SHARED,
            dispatcher_module.ROUTE_CHANGED,
            dispatcher_module.TENANT_NOT_FOUND,
            dispatcher_module.TENANT_ACCOUNT_MISMATCH,
        )
    ] + [
        {"state": "FAILED", "final_reason": reason, "last_category": category}
        for reason in ("PERMANENT", "EXHAUSTED")
        for category in ("TRANSIENT", "PERMANENT", "UNKNOWN")
    ],
)
def test_aggregated_failure_reasons_satisfy_activity_record_contract(channel, failure):
    deliveries = [
        models.NotificationDelivery(channel=channel, **failure),
        models.NotificationDelivery(
            channel="SNS" if channel == "SLACK" else "SLACK", state="SUCCEEDED",
        ),
    ]
    outcome, details = dispatcher_module._event_outcome(deliveries)
    record = PlatformEventRecord(
        action=PlatformEventAction.NOTIFICATION_SEND,
        actor_kind=PlatformActorKind.SYSTEM,
        outcome=outcome,
        target_id=str(uuid.uuid4()),
        details=details,
    )
    assert record.outcome is PlatformEventOutcome.FAILED


def test_success_is_one_system_event_linked_back_to_the_incident(sessions, pg_engine):
    dispatcher = _ready(sessions, pg_engine)
    incident_id, *intake_rows = d._seeded_high_intake(sessions, d.ACCOUNT_A)
    try:
        with sessions() as db:
            [event] = events_repo.list_by_incident(db, incident_id)
        dispatcher.discover()
        assert dispatcher.process_one("SLACK")
        assert _activities(pg_engine, event.event_id) == []
        assert dispatcher.process_one("SNS")
        [activity] = _activities(pg_engine, event.event_id)
        assert (activity.actor_kind, activity.target_type, activity.outcome) == (
            "SYSTEM", "NOTIFICATION", "SUCCEEDED",
        )
        assert activity.tenant_id == d.TENANT_A
        assert activity.details == {}
        with sessions() as db:
            linked_incident = db.scalar(
                select(models.NotificationEvent.incident_id).join(
                    models.PlatformEvent,
                    models.PlatformEvent.target_id == cast(models.NotificationEvent.event_id, Text),
                ).where(models.PlatformEvent.event_id == activity.event_id)
            )
        assert linked_incident == incident_id

        restarted = _ready(sessions, pg_engine)
        assert restarted.discover() == 0
        assert not restarted.process_one("SLACK")
        assert not restarted.process_one("SNS")
        assert len(_activities(pg_engine, event.event_id)) == 1
    finally:
        d._remove_intake_rows(pg_engine, incident_id, *intake_rows)


def test_same_incident_has_separate_activity_rows_for_distinct_notifications(sessions, pg_engine):
    dispatcher = _ready(sessions, pg_engine)
    first = d.commit_event(sessions)
    with sessions() as db:
        original = events_repo.get(db, first)
        second = events_repo.add(
            db, incident_id=original.incident_id, execution_id=None, related_execution_id=None,
            kind="PROTECTED_TARGET_ESCALATION", phase="CREATED", outcome=None,
            target_arn=original.target_arn, aws_account_id=original.aws_account_id,
            region=original.region, risk_level=original.risk_level,
            trigger_source=TriggerSource.PRE_MITIGATION_0_5S, runbook_id=None,
            execution_status=None, reason_code="PROTECTED_TAG", occurred_at=original.occurred_at,
        ).event_id
        db.commit()
    dispatcher.discover()
    for _ in range(2):
        d.open_lane(pg_engine)
        assert dispatcher.process_one("SLACK")
        assert dispatcher.process_one("SNS")
    assert len(_activities(pg_engine, first)) == len(_activities(pg_engine, second)) == 1


def test_missing_channel_and_retry_do_not_prematurely_finalize(sessions, pg_engine):
    sns = d.SnsWire(d._throttled())
    dispatcher = _ready(sessions, pg_engine, sns=sns)
    event_id = d.commit_event(sessions)
    dispatcher.discover("SLACK")
    assert dispatcher.process_one("SLACK")
    assert _activities(pg_engine, event_id) == []
    dispatcher.discover("SNS")
    assert dispatcher.process_one("SNS")
    assert d.rows(pg_engine, event_id)["SNS"].state == "PENDING"
    assert _activities(pg_engine, event_id) == []
    d.make_due(pg_engine)
    assert dispatcher.process_one("SNS")
    [activity] = _activities(pg_engine, event_id)
    assert activity.outcome == "SUCCEEDED"
    assert d.rows(pg_engine, event_id)["SNS"].attempts == 2


def test_representative_failure_uses_channel_order_not_completion_order(sessions, pg_engine):
    dispatcher = _ready(sessions, pg_engine, max_attempts=1)
    event_id = d.commit_event(sessions)
    dispatcher.discover()
    slack, sns = dispatcher._claim("SLACK"), dispatcher._claim("SNS")
    dispatcher._finish(sns, SendResult(failure_category="UNKNOWN", error_code="provider-raw"))
    assert _activities(pg_engine, event_id) == []
    dispatcher._finish(slack, SendResult(failure_category="PERMANENT", error_code="provider-raw"))
    [activity] = _activities(pg_engine, event_id)
    assert activity.outcome == "FAILED"
    assert activity.details == {"reason_code": "SLACK:PERMANENT:PERMANENT"}
    assert d.rows(pg_engine, event_id)["SNS"].last_category == "UNKNOWN"


@pytest.mark.parametrize(
    "account,expected",
    [
        (None, "SLACK:ACCOUNT_MISSING"),
        (d.ACCOUNT_OTHER, "SLACK:OWNERSHIP_MISSING"),
        (d.ACCOUNT_A, "SLACK:CHANNEL_UNAVAILABLE"),
    ],
)
def test_blocked_targets_are_failures_even_without_a_send_attempt(sessions, pg_engine, account, expected):
    dispatcher = _ready(sessions, pg_engine, slack=None, sns=None)
    event_id = d.commit_event(sessions, account)
    dispatcher.discover()
    [activity] = _activities(pg_engine, event_id)
    assert (activity.outcome, activity.tenant_id, activity.details) == (
        "FAILED", None, {"reason_code": expected},
    )
    assert all(row.attempts == 0 for row in d.rows(pg_engine, event_id).values())


def test_partial_success_keeps_channel_details_outside_activity_log(sessions, pg_engine):
    dispatcher = _ready(sessions, pg_engine, sns=None)
    event_id = d.commit_event(sessions)
    dispatcher.discover()
    assert dispatcher.process_one("SLACK")
    [activity] = _activities(pg_engine, event_id)
    assert (activity.outcome, activity.details) == (
        "FAILED", {"reason_code": "SNS:CHANNEL_UNAVAILABLE"},
    )
    assert {c: r.state for c, r in d.rows(pg_engine, event_id).items()} == {
        "SLACK": "SUCCEEDED", "SNS": "BLOCKED",
    }


@pytest.mark.parametrize("excluded", ["unselected", "not_activated", "legacy", "old_terminal"])
def test_excluded_and_historical_notifications_are_not_logged(sessions, pg_engine, excluded):
    if excluded == "unselected":
        dispatcher = d.make_dispatcher(sessions, d.registry(d.entry(d.TENANT_A, d.ACCOUNT_A)))
        d.activate(dispatcher, pg_engine)
    else:
        dispatcher = _ready(sessions, pg_engine)
    event_id = d.commit_event(
        sessions, occurred_shift=timedelta(hours=-1) if excluded == "not_activated" else timedelta(0),
    )
    if excluded == "legacy":
        d.put_delivery(pg_engine, event_id, "SNS", "BLOCKED", blocked_reason="CHANNEL_NOT_CONFIGURED")
    elif excluded == "old_terminal":
        for channel in ("SLACK", "SNS"):
            d.put_delivery(pg_engine, event_id, channel, "SUCCEEDED")
    dispatcher.discover()
    dispatcher.process_one("SLACK")
    dispatcher.process_one("SNS")
    assert _activities(pg_engine, event_id) == []


@pytest.mark.parametrize(
    "incident_tenant,delivery_tenant,expected",
    [
        (d.TENANT_A, d.TENANT_A, d.TENANT_A),
        (d.TENANT_A, uuid.UUID(d.TENANT_A).hex, d.TENANT_A),
        (d.TENANT_A, d.TENANT_B, None),
        (d.TENANT_A, "legacy-tenant", None),
        (None, d.TENANT_A, None),
    ],
)
def test_tenant_conflicts_are_central_only_but_unbound_channels_are_not_conflicts(
    sessions, pg_engine, incident_tenant, delivery_tenant, expected,
):
    dispatcher = d.make_dispatcher(
        sessions, d.registry(d.entry(d.TENANT_A, d.ACCOUNT_A, slack="SLACK_A")), slack=d.SlackWire(),
    )
    d.activate(dispatcher, pg_engine)
    incident_id, *intake_rows = d._seeded_high_intake(sessions, d.ACCOUNT_A)
    try:
        with sessions() as db:
            db.execute(
                update(models.Incident)
                .where(models.Incident.incident_id == incident_id)
                .values(tenant_id=incident_tenant)
            )
            db.commit()
            [event] = events_repo.list_by_incident(db, incident_id)
        dispatcher.discover()
        claim = dispatcher._claim("SLACK")
        with pg_engine.begin() as connection:
            connection.execute(
                update(models.NotificationDelivery)
                .where(models.NotificationDelivery.delivery_id == claim.delivery_id)
                .values(tenant_id=delivery_tenant)
            )
        dispatcher._finish(claim, _accepted("SLACK"))
        [activity] = _activities(pg_engine, event.event_id)
        assert activity.tenant_id == expected
        assert d.rows(pg_engine, event.event_id)["SNS"].tenant_id is None
        filters = activity_repo.EventFilters(
            action=PlatformEventAction.NOTIFICATION_SEND, target_id=event.event_id,
        )
        with sessions() as db:
            manager = activity_repo.list_events(
                db, tenant_ids=(d.TENANT_A,), viewer_user_id=str(uuid.uuid4()),
                filters=filters, before=None, limit=10,
            )
            central = activity_repo.list_events(
                db, tenant_ids=None, viewer_user_id=str(uuid.uuid4()),
                filters=filters, before=None, limit=10,
            )
        assert len(manager) == (1 if expected else 0)
        assert len(central) == 1
    finally:
        d._remove_intake_rows(pg_engine, incident_id, *intake_rows)


@pytest.mark.parametrize("terminal_path", ["discover", "finish", "claim_block", "claim_exhaust", "recover"])
def test_activity_insert_failure_rolls_back_the_terminal_transition(
    sessions, pg_engine, terminal_path,
):
    routes = d.registry(d.entry(d.TENANT_A, d.ACCOUNT_A, sns=d.TOPIC_A))
    dispatcher = d.make_dispatcher(sessions, routes, sns=d.SnsWire(), max_attempts=1)
    d.activate(dispatcher, pg_engine)
    event_id = d.commit_event(sessions)
    claim = None
    if terminal_path == "discover":
        dispatcher.discover("SLACK")
        unavailable = d.make_dispatcher(sessions, routes)
        action = lambda: unavailable.discover("SNS")
        expected_state = None
    else:
        dispatcher.discover()
        expected_state = "PENDING"
        if terminal_path in ("finish", "recover"):
            claim = dispatcher._claim("SNS")
            expected_state = "IN_FLIGHT"
        if terminal_path == "finish":
            action = lambda: dispatcher._finish(claim, _accepted("SNS"))
        elif terminal_path == "recover":
            d.expire_leases(pg_engine)
            action = lambda: dispatcher._recover("SNS")
        elif terminal_path == "claim_block":
            unavailable = d.make_dispatcher(sessions, routes)
            action = lambda: unavailable.process_one("SNS")
        else:
            with pg_engine.begin() as connection:
                connection.execute(
                    update(models.NotificationDelivery)
                    .where(
                        models.NotificationDelivery.event_id == event_id,
                        models.NotificationDelivery.channel == "SNS",
                    )
                    .values(attempts=1, last_category="TRANSIENT")
                )
            action = lambda: dispatcher.process_one("SNS")

    with (
        failing_platform_event_write(only=PlatformEventAction.NOTIFICATION_SEND) as failure,
        pytest.raises(IntegrityError),
    ):
        action()
    assert failure.count == 1
    stored = d.rows(pg_engine, event_id).get("SNS")
    assert (stored.state if stored else None) == expected_state
    assert _activities(pg_engine, event_id) == []

    action()
    [activity] = _activities(pg_engine, event_id)
    assert activity.outcome == ("SUCCEEDED" if terminal_path == "finish" else "FAILED")
    if terminal_path == "recover":
        assert activity.details == {"reason_code": "SNS:EXHAUSTED:UNKNOWN"}
        dispatcher._finish(claim, _accepted("SNS"))
        assert len(_activities(pg_engine, event_id)) == 1
        assert d.rows(pg_engine, event_id)["SNS"].state == "FAILED"


def test_same_channel_discoverers_do_not_duplicate_activity(sessions, pg_engine):
    routes = d.registry(d.entry(d.TENANT_A, d.ACCOUNT_A, slack="SLACK_A", sns=d.TOPIC_A))
    workers = [d.make_dispatcher(sessions, routes) for _ in range(2)]
    d.activate(workers[0], pg_engine)
    event_id = d.commit_event(sessions)
    assert workers[0].discover("SLACK") == 1
    assert _activities(pg_engine, event_id) == []
    barrier = threading.Barrier(2, timeout=10)
    hook_hits = []

    def after_select(_connection, _cursor, statement, _parameters, _context, _many):
        if "FROM notification_events" in statement and "EXISTS" in statement:
            hook_hits.append(_connection)
            # INSERT 전 두 조회를 끝내야 늦은 발견자도 같은 사건을 고른다.
            barrier.wait()

    sql_event.listen(pg_engine, "after_cursor_execute", after_select)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(worker.discover, "SNS") for worker in workers]
            try:
                created = [future.result(timeout=20) for future in futures]
            finally:
                barrier.abort()
    finally:
        sql_event.remove(pg_engine, "after_cursor_execute", after_select)
    assert len(hook_hits) == 2
    assert sorted(created) == [0, 1]
    assert len(_activities(pg_engine, event_id)) == 1


@pytest.mark.parametrize("terminal_path", ["discover", "finish", "recover_batch"])
def test_concurrent_channel_commits_create_one_activity_per_event(
    sessions, pg_engine, terminal_path,
):
    dispatcher = _ready(sessions, pg_engine, max_attempts=1)
    event_ids = [
        d.commit_event(sessions, None if terminal_path == "discover" else d.ACCOUNT_A)
        for _ in range(2 if terminal_path == "recover_batch" else 1)
    ]
    if terminal_path == "finish":
        dispatcher.discover()
        claims = {channel: dispatcher._claim(channel) for channel in ("SLACK", "SNS")}

        def action(channel):
            dispatcher._finish(claims[channel], _accepted(channel))
    elif terminal_path == "recover_batch":
        for index, event_id in enumerate(event_ids):
            for channel in ("SLACK", "SNS"):
                age = index + 1 if channel == "SLACK" else 2 - index
                d.put_delivery(
                    pg_engine, event_id, channel, "IN_FLIGHT",
                    lease_expires_at=func.now() - timedelta(seconds=age),
                )

        def action(channel):
            dispatcher._recover(channel)
    else:
        def action(channel):
            dispatcher.discover(channel)

    barrier = threading.Barrier(2, timeout=10)
    writes = threading.local()

    def after_write(_connection, _cursor, statement, _parameters, context, _many):
        if terminal_path == "discover":
            relevant = statement.startswith("INSERT INTO notification_deliveries")
        else:
            relevant = (
                statement.startswith("UPDATE notification_deliveries")
                and context.compiled_parameters[0].get("state") in ("SUCCEEDED", "FAILED")
            )
        if relevant:
            writes.count = getattr(writes, "count", 0) + 1
            if writes.count == len(event_ids):
                # 서로 다른 채널의 쓰기와 FK 잠금까지 끝낸 뒤 종합 기록을 경합시킨다.
                barrier.wait()

    sql_event.listen(pg_engine, "after_cursor_execute", after_write)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(action, channel) for channel in ("SLACK", "SNS")]
            try:
                for future in futures:
                    future.result(timeout=20)
            finally:
                barrier.abort()
    finally:
        sql_event.remove(pg_engine, "after_cursor_execute", after_write)
    for event_id in event_ids:
        [activity] = _activities(pg_engine, event_id)
        assert activity.outcome == ("SUCCEEDED" if terminal_path == "finish" else "FAILED")
        if terminal_path == "recover_batch":
            assert activity.details == {"reason_code": "SLACK:EXHAUSTED:UNKNOWN"}
