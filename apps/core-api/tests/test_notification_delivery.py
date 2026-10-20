"""#493 PR2 발송 dispatcher — 실제 PostgreSQL·Alembic head, 실제 routing·sender + 전송 계층 대역.

네트워크·AWS·실제 비밀값은 쓰지 않는다. Slack은 HTTPSConnection 대역, SNS는 client 대역이다.
시간 경과(임대 만료·재시도 기한·관문 대기)는 DB 행 시각을 앞당겨 결정적으로 흉내 낸다.
호스트 시계(사건 created_at)와 DB 시계(activated_at)의 차이를 없애려고, 활성화 시점이 주제가
아닌 테스트는 activate()로 활성화를 1분 앞당기고, 과거 사건은 1시간 전 시각으로 만든다.
"""

import asyncio
import json
import threading
import time
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import incident_intake
import pytest
from botocore.exceptions import ClientError
from db import models
from db.repositories import assets as assets_repo
from db.repositories import notification_deliveries as deliveries_repo
from db.repositories import notification_events as events_repo
from notifications import dispatcher as dispatcher_module
from notifications.channels import SlackSender, SnsSender
from notifications.dispatcher import DeliveryDispatcher
from notifications.routing import RouteRegistry
from schemas.api.assets import AssetType
from schemas.api.incidents import RiskLevel
from schemas.intake import SecOpsIncidentIntake
from sqlalchemy import create_engine, delete, func, select, text, update
from sqlalchemy.orm import Session, sessionmaker

ACCOUNT_A = "111111111111"
ACCOUNT_B = "222222222222"
REGION = "ap-northeast-2"
WEBHOOK_A = "https://hooks.slack.com/services/TFAKEA/BFAKEA/fakeTokenA"
WEBHOOK_A2 = "https://hooks.slack.com/services/TFAKEA/BFAKEA2/fakeTokenA2"
WEBHOOK_B = "https://hooks.slack.com/services/TFAKEB/BFAKEB/fakeTokenB"
TOPIC_A = "arn:aws:sns:ap-northeast-2:999999999999:customer-a"
TOPIC_B = "arn:aws:sns:ap-northeast-2:999999999999:customer-b"
ENV = {"SLACK_A": WEBHOOK_A, "SLACK_A2": WEBHOOK_A2, "SLACK_B": WEBHOOK_B}
BASE_URL = "https://console.vigilantis.example"
CRASH_BEFORE_SEND = object()
CRASH_AFTER_ACCEPT = object()


class _Crash(BaseException):
    """프로세스 중단 흉내 — dispatcher의 except Exception에 걸리지 않는다."""


# --- 전송 계층 대역 -------------------------------------------------------------


class SlackWire:
    """http.client.HTTPSConnection 대역. 요청을 기록하고 정해 둔 응답을 준다(기본 200 ok)."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.posts = []
        self.on_request = None
        self._lock = threading.Lock()

    def __call__(self, host, port, timeout):
        return _SlackConnection(self, host)

    def next_response(self):
        with self._lock:
            return self.responses.pop(0) if self.responses else (200, {}, b"ok")


class _SlackConnection:
    def __init__(self, wire, host):
        self.wire, self.host = wire, host
        self.sock = SimpleNamespace(settimeout=lambda _seconds: None)
        self._response = None

    def connect(self):
        return None

    def request(self, method, path, body, headers):
        response = self.wire.next_response()
        if response is CRASH_BEFORE_SEND:
            raise _Crash
        self.wire.posts.append({
            "url": f"https://{self.host}{path}", "body": body.decode(), "at": time.monotonic(),
        })
        if self.wire.on_request is not None:
            self.wire.on_request()
        if response is CRASH_AFTER_ACCEPT:
            raise _Crash
        self._response = response

    def getresponse(self):
        status, headers, body = self._response
        return SimpleNamespace(
            status=status,
            getheader=lambda name, default=None: headers.get(name, default),
            read=lambda limit=-1: body if limit < 0 else body[:limit],
        )

    def close(self):
        return None


class SnsWire:
    """boto3 SNS client 대역(팩토리 겸 client). 기본 응답은 수락(MessageId)."""

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.publishes = []
        self.on_publish = None

    def __call__(self, region, **_timeouts):
        return self

    def publish(self, TopicArn, Subject, Message):
        outcome = self.outcomes.pop(0) if self.outcomes else None
        if outcome is CRASH_BEFORE_SEND:
            raise _Crash
        self.publishes.append({"topic": TopicArn, "body": Message})
        if self.on_publish is not None:
            self.on_publish()
        if outcome is CRASH_AFTER_ACCEPT:
            raise _Crash
        if isinstance(outcome, Exception):
            raise outcome
        return outcome or {"MessageId": f"msg-{uuid.uuid4().hex}"}


def _throttled():
    return ClientError({"Error": {"Code": "Throttling", "Message": "raw provider text"}}, "Publish")


# --- 준비 ---------------------------------------------------------------------


def _wipe(engine):
    with engine.begin() as connection:
        for model in (
            models.NotificationDelivery, models.NotificationRoute,
            models.NotificationLane, models.NotificationEvent,
        ):
            connection.execute(delete(model))


@pytest.fixture()
def sessions(pg_engine):
    """발송 표는 전역 anti-join 대상이라 테스트마다 비운다. 세션은 각 단계가 따로 연다."""
    _wipe(pg_engine)
    yield sessionmaker(bind=pg_engine, autoflush=False, expire_on_commit=False)
    _wipe(pg_engine)


def entry(tenant, account, slack=None, sns=None):
    value = {"tenant_id": tenant, "aws_account_id": account}
    if slack is not None:
        value["slack_webhook_env"] = slack
    if sns is not None:
        value["sns_topic_arn"] = sns
    return value


def registry(*entries):
    return RouteRegistry.from_mapping({"routes": list(entries)}, ENV)


def make_dispatcher(sessions, routes, *, slack=None, sns=None, **options):
    senders = {}
    if slack is not None:
        senders["SLACK"] = SlackSender(connection_factory=slack)
    if sns is not None:
        senders["SNS"] = SnsSender(client_factory=sns)
    settings = {
        "public_base_url": BASE_URL, "poll_seconds": 0.05, "batch_size": 20, "max_attempts": 3,
        "backoff_seconds": 1, "max_backoff_seconds": 60, "lease_seconds": 30,
    } | options
    return DeliveryDispatcher(sessions, routes, senders, **settings)


def activate(dispatcher, engine):
    dispatcher.initialize()
    with engine.begin() as connection:
        connection.execute(update(models.NotificationRoute).values(
            activated_at=models.NotificationRoute.activated_at - timedelta(minutes=1),
        ))


def commit_event(sessions, account=ACCOUNT_A, *, created_shift=None, occurred_shift=timedelta(0)):
    """PR1 저장소로 사건 1건을 commit한다. shift는 호스트 현재 시각 기준 이동."""
    instance = "i-0" + uuid.uuid4().hex[:16]
    with sessions() as db:
        row = events_repo.add(
            db, incident_id=str(uuid.uuid4()), execution_id=None, related_execution_id=None,
            kind="HIGH_INCIDENT_CREATED", phase="CREATED", outcome=None,
            target_arn=f"arn:aws:ec2:{REGION}:{account}:instance/{instance}"
            if account else f"arn:aws:ec2:{REGION}:{ACCOUNT_A}:instance/{instance}",
            aws_account_id=account, region=REGION, risk_level=RiskLevel.HIGH,
            trigger_source=None, runbook_id=None, execution_status=None, reason_code=None,
            occurred_at=datetime.now(UTC) + occurred_shift,
        )
        if created_shift is not None:
            row.created_at = datetime.now(UTC) + created_shift
        db.commit()
        return row.event_id


def rows(engine, event_id):
    with Session(engine) as db:
        return {row.channel: row for row in deliveries_repo.list_by_event(db, event_id)}


def route_row(engine, account, channel):
    with Session(engine) as db:
        return deliveries_repo.get_route(db, account, channel)


def make_due(engine):
    with engine.begin() as connection:
        connection.execute(text(
            "UPDATE notification_deliveries SET next_attempt_at = now() WHERE state = 'PENDING'"
        ))


def open_lane(engine):
    with engine.begin() as connection:
        connection.execute(text(
            "UPDATE notification_lanes SET available_at = now(), cooldown_until = now()"
        ))


def expire_leases(engine):
    with engine.begin() as connection:
        connection.execute(text(
            "UPDATE notification_deliveries SET lease_expires_at = now() - interval '1 second'"
            " WHERE state = 'IN_FLIGHT'"
        ))


def lane_waits(engine):
    """(간격 관문까지 남은 초, 공급자 대기까지 남은 초)."""
    with engine.connect() as connection:
        spacing, cooldown = connection.execute(text(
            "SELECT extract(epoch FROM available_at - now()),"
            " extract(epoch FROM cooldown_until - now())"
            " FROM notification_lanes WHERE channel = 'SLACK'"
        )).one()
    return float(spacing), float(cooldown)


def lane_wait_seconds(engine):
    return max(lane_waits(engine))


def retry_delay(row):
    return row.next_attempt_at - row.updated_at


def _seeded_high_intake(sessions, account):
    instance = "i-0" + uuid.uuid4().hex[:16]
    arn = f"arn:aws:ec2:{REGION}:{account}:instance/{instance}"
    threat_id = str(uuid.uuid4())
    with sessions() as db:
        run = assets_repo.start_collection_run(
            db, account_id=account, region=REGION, mode="localstack",
            lookback_days=14, period_seconds=3600,
        )
        assets_repo.upsert_asset(
            db, arn=arn, asset_type=AssetType.EC2, resource_id=instance, account_id=account,
            region=REGION, spec={"instance_type": "t3.micro"},
            collection_run_id=run.collection_run_id, collected_at=datetime.now(UTC),
        )
        db.commit()
        run_id = run.collection_run_id
    intake = SecOpsIncidentIntake.model_validate({
        "title": "SSH 브루트포스 시도",
        "threat_event": {
            "threat_event_id": threat_id, "source_event_id": f"evt-{threat_id}",
            "event_type": "SSH_BRUTE_FORCE", "target_arn": arn,
            "occurred_at": datetime.now(UTC).isoformat(),
            "payload": {"source_ip": "203.0.113.10", "failed_attempt_count": 120,
                        "window_seconds": 300},
            "deduplication_key": f"SSH_BRUTE_FORCE:{instance}:203.0.113.10",
            "collected_at": datetime.now(UTC).isoformat(),
        },
        "initial_risk": {
            "threat_event_id": threat_id, "initial_risk_level": "HIGH",
            "response_mode": "PRE_MITIGATION_0_5S", "reason_codes": ["RISK_SSH_BRUTEFORCE"],
        },
    })
    with sessions() as db:
        outcome = incident_intake.create_incident_from_intake(db, intake)
    return outcome.incident_id, arn, threat_id, run_id


def _remove_intake_rows(engine, incident_id, arn, threat_id, run_id):
    with engine.begin() as connection:
        connection.execute(delete(models.Evidence).where(models.Evidence.incident_id == incident_id))
        connection.execute(delete(models.Incident).where(models.Incident.incident_id == incident_id))
        connection.execute(
            delete(models.ThreatEvent).where(models.ThreatEvent.threat_event_id == threat_id)
        )
        connection.execute(delete(models.Asset).where(models.Asset.arn == arn))
        connection.execute(
            delete(models.CollectionRun).where(models.CollectionRun.collection_run_id == run_id)
        )


# --- 업무 commit → 발견 → 실제 sender → 별도 세션 결과 ---------------------------------


@pytest.mark.parametrize("channels", [("SLACK",), ("SNS",), ("SLACK", "SNS")])
def test_committed_high_intake_is_sent_once_per_configured_channel(sessions, pg_engine, channels):
    slack, sns = SlackWire(), SnsWire()
    dispatcher = make_dispatcher(
        sessions,
        registry(entry(
            "customer-a", ACCOUNT_A,
            slack="SLACK_A" if "SLACK" in channels else None,
            sns=TOPIC_A if "SNS" in channels else None,
        )),
        slack=slack if "SLACK" in channels else None,
        sns=sns if "SNS" in channels else None,
    )
    activate(dispatcher, pg_engine)
    incident_id, *intake_rows = _seeded_high_intake(sessions, ACCOUNT_A)
    try:
        with Session(pg_engine) as db:
            [event] = events_repo.list_by_incident(db, incident_id)
        assert event.aws_account_id == ACCOUNT_A

        assert dispatcher.discover() == 2  # 두 채널 모두 행을 남긴다
        assert dispatcher.discover() == 0
        for channel in channels:
            assert dispatcher.process_one(channel) is True
            assert dispatcher.process_one(channel) is False  # 종결 뒤 다시 보내지 않는다

        stored = rows(pg_engine, event.event_id)
        for channel in ("SLACK", "SNS"):
            row = stored[channel]
            if channel in channels:
                assert (row.state, row.attempts, row.tenant_id) == ("SUCCEEDED", 1, "customer-a")
                assert row.receipt_kind == {"SLACK": "SLACK_POSTED", "SNS": "SNS_ACCEPTED"}[channel]
            else:
                assert (row.state, row.blocked_reason) == ("BLOCKED", "CHANNEL_NOT_CONFIGURED")
        assert [p["url"] for p in slack.posts] == ([WEBHOOK_A] if "SLACK" in channels else [])
        assert [p["topic"] for p in sns.publishes] == ([TOPIC_A] if "SNS" in channels else [])
        for body in [p["body"] for p in slack.posts] + [p["body"] for p in sns.publishes]:
            assert event.event_id in body and f"{BASE_URL}/incidents/{incident_id}" in body
        if "SNS" in channels:
            assert stored["SNS"].receipt_ref.startswith("msg-")
    finally:
        _remove_intake_rows(pg_engine, incident_id, *intake_rows)


def test_no_session_or_transaction_is_open_during_external_calls(sessions, pg_engine):
    observer = create_engine(pg_engine.url)
    seen = []

    def observe():
        with observer.connect() as connection:
            busy = connection.execute(text(
                "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database()"
                " AND pid <> pg_backend_pid() AND state <> 'idle'"
            )).scalar_one()
        seen.append((pg_engine.pool.checkedout(), busy))

    slack, sns = SlackWire(), SnsWire()
    slack.on_request = sns.on_publish = observe
    dispatcher = make_dispatcher(
        sessions, registry(entry("customer-a", ACCOUNT_A, slack="SLACK_A", sns=TOPIC_A)),
        slack=slack, sns=sns,
    )
    activate(dispatcher, pg_engine)
    commit_event(sessions)
    dispatcher.discover()
    assert dispatcher.process_one("SLACK") and dispatcher.process_one("SNS")
    observer.dispose()
    assert seen == [(0, 0), (0, 0)]


# --- 고객·수신처 경계, 바인딩 변경, 소급 발송 없음 ------------------------------------------


def test_routes_never_cross_customers_and_changed_destination_gets_no_old_events(
    sessions, pg_engine,
):
    original = registry(
        entry("customer-a", ACCOUNT_A, slack="SLACK_A"),
        entry("customer-b", ACCOUNT_B, slack="SLACK_B"),
    )
    first_wire = SlackWire()
    first = make_dispatcher(sessions, original, slack=first_wire)
    activate(first, pg_engine)
    event_a, event_b = commit_event(sessions, ACCOUNT_A), commit_event(sessions, ACCOUNT_B)
    first.discover()
    assert first.process_one("SLACK")
    open_lane(pg_engine)
    assert first.process_one("SLACK")
    sent = {p["url"]: p["body"] for p in first_wire.posts}
    assert set(sent) == {WEBHOOK_A, WEBHOOK_B}
    assert event_a in sent[WEBHOOK_A] and event_b not in sent[WEBHOOK_A]
    assert event_b in sent[WEBHOOK_B] and event_a not in sent[WEBHOOK_B]

    pending_a = commit_event(sessions, ACCOUNT_A)
    first.discover()
    old_a, old_b = route_row(pg_engine, ACCOUNT_A, "SLACK"), route_row(pg_engine, ACCOUNT_B, "SLACK")

    # 재기동: A의 웹훅만 바뀐다. 같은 fingerprint인 B는 epoch·활성화 시각을 유지한다.
    history_a = commit_event(
        sessions, ACCOUNT_A, created_shift=timedelta(hours=-1), occurred_shift=timedelta(hours=-1),
    )
    changed = registry(
        entry("customer-a", ACCOUNT_A, slack="SLACK_A2"),
        entry("customer-b", ACCOUNT_B, slack="SLACK_B"),
    )
    second_wire = SlackWire()
    second = make_dispatcher(sessions, changed, slack=second_wire)
    second.initialize()
    new_a, new_b = route_row(pg_engine, ACCOUNT_A, "SLACK"), route_row(pg_engine, ACCOUNT_B, "SLACK")
    assert (new_b.route_epoch, new_b.activated_at) == (old_b.route_epoch, old_b.activated_at)
    assert new_a.route_epoch != old_a.route_epoch and new_a.fingerprint != old_a.fingerprint
    assert new_a.activated_at > old_a.activated_at

    open_lane(pg_engine)
    second.discover()
    assert second.process_one("SLACK") is True
    assert rows(pg_engine, pending_a)["SLACK"].blocked_reason == "ROUTE_CHANGED"
    assert rows(pg_engine, history_a)["SLACK"].blocked_reason == "NOT_ACTIVATED"
    assert second.process_one("SLACK") is False
    assert second_wire.posts == []  # 바뀐 수신처로 과거·대기 사건을 보내지 않는다

    # 옛 설정으로 남은 프로세스도 새 epoch 행을 옛 수신처로 보내지 못한다
    stale_target = commit_event(sessions, ACCOUNT_A, occurred_shift=timedelta(minutes=2),
                                created_shift=timedelta(minutes=2))
    second.discover()
    assert rows(pg_engine, stale_target)["SLACK"].state == "PENDING"
    assert first.process_one("SLACK") is True
    assert rows(pg_engine, stale_target)["SLACK"].blocked_reason == "ROUTE_CHANGED"
    assert len(first_wire.posts) == 2

    fresh_a = commit_event(sessions, ACCOUNT_A, occurred_shift=timedelta(minutes=2),
                           created_shift=timedelta(minutes=2))
    fresh_b = commit_event(sessions, ACCOUNT_B)
    second.discover()
    assert second.process_one("SLACK")
    open_lane(pg_engine)
    assert second.process_one("SLACK")
    sent = {p["url"]: p["body"] for p in second_wire.posts}
    assert set(sent) == {WEBHOOK_A2, WEBHOOK_B}
    assert fresh_a in sent[WEBHOOK_A2] and fresh_b in sent[WEBHOOK_B]


def test_removed_then_readded_binding_gets_new_epoch_and_old_pending_stays_blocked(
    sessions, pg_engine,
):
    configured = registry(entry("customer-a", ACCOUNT_A, sns=TOPIC_A))
    wire = SnsWire()
    dispatcher = make_dispatcher(sessions, configured, sns=wire)
    activate(dispatcher, pg_engine)
    dispatcher.initialize()  # 같은 설정 재등록은 epoch·활성화 시각을 바꾸지 않는다
    before = route_row(pg_engine, ACCOUNT_A, "SNS")
    pending = commit_event(sessions)
    dispatcher.discover()

    removed = registry(entry("customer-a", ACCOUNT_A))
    make_dispatcher(sessions, removed).initialize()
    assert route_row(pg_engine, ACCOUNT_A, "SNS") is None

    readded = make_dispatcher(sessions, configured, sns=wire)
    readded.initialize()
    after = route_row(pg_engine, ACCOUNT_A, "SNS")
    assert after.fingerprint == before.fingerprint
    assert after.route_epoch != before.route_epoch and after.activated_at > before.activated_at
    assert readded.process_one("SNS") is True
    assert rows(pg_engine, pending)["SNS"].blocked_reason == "ROUTE_CHANGED"
    assert wire.publishes == []


def test_adding_settings_later_never_sends_history_and_blocked_rows_stay_blocked(
    sessions, pg_engine,
):
    hour = timedelta(hours=1)
    old_both = commit_event(sessions, created_shift=-hour, occurred_shift=-hour)
    old_created = commit_event(sessions, created_shift=-hour, occurred_shift=hour)
    old_occurred = commit_event(sessions, created_shift=hour, occurred_shift=-hour)
    new = commit_event(sessions, created_shift=hour, occurred_shift=hour)
    no_account = commit_event(sessions, None, created_shift=hour, occurred_shift=hour)
    unowned_b = commit_event(sessions, ACCOUNT_B, created_shift=hour, occurred_shift=hour)

    slack, sns = SlackWire(), SnsWire()
    dispatcher = make_dispatcher(
        sessions, registry(entry("customer-a", ACCOUNT_A, slack="SLACK_A", sns=TOPIC_A)),
        slack=slack, sns=sns,
    )
    dispatcher.initialize()  # 실제 DB 활성화 시각 — 앞당기지 않는다
    assert dispatcher.discover() == 12
    for event_id in (old_both, old_created, old_occurred):
        assert {c: r.blocked_reason for c, r in rows(pg_engine, event_id).items()} == {
            "SLACK": "NOT_ACTIVATED", "SNS": "NOT_ACTIVATED",
        }
    assert {r.blocked_reason for r in rows(pg_engine, no_account).values()} == {"ACCOUNT_MISSING"}
    assert {r.blocked_reason for r in rows(pg_engine, unowned_b).values()} == {"OWNERSHIP_MISSING"}
    assert dispatcher.process_one("SLACK") and dispatcher.process_one("SNS")
    assert {r.state for r in rows(pg_engine, new).values()} == {"SUCCEEDED"}

    # 설정을 고쳐도(B 추가) 이미 BLOCKED인 행은 그대로이고 아무것도 다시 보내지 않는다
    fixed = make_dispatcher(
        sessions,
        registry(
            entry("customer-a", ACCOUNT_A, slack="SLACK_A", sns=TOPIC_A),
            entry("customer-b", ACCOUNT_B, slack="SLACK_B", sns=TOPIC_B),
        ),
        slack=slack, sns=sns,
    )
    fixed.initialize()
    open_lane(pg_engine)
    assert fixed.discover() == 0
    assert not fixed.process_one("SLACK") and not fixed.process_one("SNS")
    assert {r.blocked_reason for r in rows(pg_engine, unowned_b).values()} == {"OWNERSHIP_MISSING"}
    assert len(slack.posts) == len(sns.publishes) == 1


# --- 발견: 상한·동시성·늦은 commit -------------------------------------------------------


def test_discovery_is_bounded_per_channel(sessions, pg_engine):
    dispatcher = make_dispatcher(
        sessions, registry(entry("customer-a", ACCOUNT_A, sns=TOPIC_A)), sns=SnsWire(),
        batch_size=2,
    )
    activate(dispatcher, pg_engine)
    for _ in range(5):
        commit_event(sessions)
    assert [dispatcher.discover() for _ in range(4)] == [4, 4, 2, 0]


def test_late_committed_older_event_is_still_discovered(sessions, pg_engine):
    dispatcher = make_dispatcher(
        sessions, registry(entry("customer-a", ACCOUNT_A, sns=TOPIC_A)), sns=SnsWire(),
    )
    activate(dispatcher, pg_engine)
    with sessions() as late:
        older = events_repo.add(
            late, incident_id=str(uuid.uuid4()), execution_id=None, related_execution_id=None,
            kind="HIGH_INCIDENT_CREATED", phase="CREATED", outcome=None,
            target_arn=f"arn:aws:ec2:{REGION}:{ACCOUNT_A}:instance/i-0late",
            aws_account_id=ACCOUNT_A, region=REGION, risk_level=RiskLevel.HIGH,
            trigger_source=None, runbook_id=None, execution_status=None, reason_code=None,
            occurred_at=datetime.now(UTC),
        )
        newer = commit_event(sessions)
        assert dispatcher.discover("SNS") == 1
        late.commit()
    assert rows(pg_engine, newer)["SNS"].state == "PENDING"
    assert dispatcher.discover("SNS") == 1  # cursor가 없어 먼저 생성된 늦은 commit도 찾는다
    assert rows(pg_engine, older.event_id)["SNS"].state == "PENDING"


def test_concurrent_discovery_creates_exactly_one_row_per_event_channel(sessions, pg_engine):
    routes = registry(entry("customer-a", ACCOUNT_A, slack="SLACK_A", sns=TOPIC_A))
    workers = [
        make_dispatcher(sessions, routes, slack=SlackWire(), sns=SnsWire(), batch_size=100)
        for _ in range(4)
    ]
    activate(workers[0], pg_engine)
    events = [commit_event(sessions) for _ in range(30)]
    barrier = threading.Barrier(len(workers))
    created = []

    def discover(worker):
        barrier.wait()
        created.append(worker.discover())

    threads = [threading.Thread(target=discover, args=(w,)) for w in workers]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    assert sum(created) == 60
    with Session(pg_engine) as db:
        count = db.scalar(select(func.count()).select_from(models.NotificationDelivery))
    assert count == 60 and all(len(rows(pg_engine, e)) == 2 for e in events)


def test_concurrent_claims_send_each_delivery_once(sessions, pg_engine):
    routes = registry(entry("customer-a", ACCOUNT_A, sns=TOPIC_A))
    wire = SnsWire()
    workers = [make_dispatcher(sessions, routes, sns=wire) for _ in range(4)]
    activate(workers[0], pg_engine)
    events = [commit_event(sessions) for _ in range(12)]
    workers[0].discover()
    barrier = threading.Barrier(len(workers))

    def drain(worker):
        barrier.wait()
        while worker.process_one("SNS"):
            pass

    threads = [threading.Thread(target=drain, args=(w,)) for w in workers]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    sent = [e for e in events for p in wire.publishes if e in p["body"]]
    assert sorted(sent) == sorted(events)
    stored = [rows(pg_engine, e)["SNS"] for e in events]
    assert {(row.state, row.attempts) for row in stored} == {("SUCCEEDED", 1)}


def test_competing_processes_keep_slack_calls_at_least_one_second_apart(sessions, pg_engine):
    routes = registry(
        entry("customer-a", ACCOUNT_A, slack="SLACK_A"),
        entry("customer-b", ACCOUNT_B, slack="SLACK_B"),
    )
    wire = SlackWire()
    workers = [make_dispatcher(sessions, routes, slack=wire) for _ in range(2)]
    activate(workers[0], pg_engine)
    events = [commit_event(sessions, account) for account in (ACCOUNT_A, ACCOUNT_B, ACCOUNT_A)]
    workers[0].discover()
    deadline = time.monotonic() + 15

    def drain(worker):
        while len(wire.posts) < len(events) and time.monotonic() < deadline:
            if not worker.process_one("SLACK"):
                time.sleep(0.02)

    threads = [threading.Thread(target=drain, args=(w,)) for w in workers]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(20)
    times = sorted(p["at"] for p in wire.posts)
    assert len(times) == 3
    assert all(later - earlier >= 1.0 for earlier, later in zip(times, times[1:]))
    assert {rows(pg_engine, e)["SLACK"].state for e in events} == {"SUCCEEDED"}


# --- 중단·임대 회수·늦은 결과 ---------------------------------------------------------


def test_crash_during_discovery_leaves_nothing_and_next_process_sends_once(sessions, pg_engine):
    routes = registry(entry("customer-a", ACCOUNT_A, sns=TOPIC_A))

    class CrashingRegistry:
        @property
        def routes(self):
            return routes.routes

        def resolve(self, event, channel):
            raise _Crash

    crashed = make_dispatcher(sessions, CrashingRegistry(), sns=SnsWire())
    activate(crashed, pg_engine)
    event_id = commit_event(sessions)
    with pytest.raises(_Crash):
        crashed.discover()
    assert rows(pg_engine, event_id) == {}

    wire = SnsWire()
    restarted = make_dispatcher(sessions, routes, sns=wire)
    restarted.initialize()
    assert restarted.discover() == 2 and restarted.process_one("SNS")
    assert len(wire.publishes) == 1 and rows(pg_engine, event_id)["SNS"].state == "SUCCEEDED"


def test_crash_after_claim_is_recovered_as_unknown_and_retried(sessions, pg_engine):
    routes = registry(entry("customer-a", ACCOUNT_A, sns=TOPIC_A))
    crashed = make_dispatcher(sessions, routes, sns=SnsWire(CRASH_BEFORE_SEND))
    activate(crashed, pg_engine)
    event_id = commit_event(sessions)
    crashed.discover()
    with pytest.raises(_Crash):
        crashed.process_one("SNS")
    row = rows(pg_engine, event_id)["SNS"]
    assert (row.state, row.attempts) == ("IN_FLIGHT", 1)

    wire = SnsWire()
    restarted = make_dispatcher(sessions, routes, sns=wire)
    assert restarted.process_one("SNS") is False  # 임대가 살아 있는 동안 다시 보내지 않는다
    expire_leases(pg_engine)
    assert restarted.process_one("SNS") is False  # 회수 후 재시도 기한을 기다린다
    row = rows(pg_engine, event_id)["SNS"]
    assert (row.state, row.last_category, row.last_error_code) == (
        "PENDING", "UNKNOWN", "lease_expired",
    )
    assert retry_delay(row) == timedelta(seconds=1)
    make_due(pg_engine)
    assert restarted.process_one("SNS") is True
    row = rows(pg_engine, event_id)["SNS"]
    assert (row.state, row.attempts) == ("SUCCEEDED", 2) and len(wire.publishes) == 1


def test_crash_after_acceptance_resends_but_never_beyond_max_attempts(sessions, pg_engine):
    routes = registry(entry("customer-a", ACCOUNT_A, sns=TOPIC_A))
    wire = SnsWire(CRASH_AFTER_ACCEPT, CRASH_AFTER_ACCEPT)
    dispatcher = make_dispatcher(sessions, routes, sns=wire, max_attempts=2)
    activate(dispatcher, pg_engine)
    event_id = commit_event(sessions)
    dispatcher.discover()
    for _ in range(2):
        with pytest.raises(_Crash):
            dispatcher.process_one("SNS")
        expire_leases(pg_engine)
        dispatcher.process_one("SNS")  # 회수
        make_due(pg_engine)
    row = rows(pg_engine, event_id)["SNS"]
    assert (row.state, row.final_reason, row.last_category, row.attempts) == (
        "FAILED", "EXHAUSTED", "UNKNOWN", 2,
    )
    assert len(wire.publishes) == 2  # 수락 뒤 중단은 중복 가능 — 최대 max_attempts회
    assert dispatcher.process_one("SNS") is False and len(wire.publishes) == 2


def test_recovered_crash_counts_toward_exhaustion(sessions, pg_engine):
    routes = registry(entry("customer-a", ACCOUNT_A, sns=TOPIC_A))
    wire = SnsWire(CRASH_BEFORE_SEND, _throttled())
    dispatcher = make_dispatcher(sessions, routes, sns=wire, max_attempts=2)
    activate(dispatcher, pg_engine)
    event_id = commit_event(sessions)
    dispatcher.discover()
    with pytest.raises(_Crash):
        dispatcher.process_one("SNS")
    expire_leases(pg_engine)
    dispatcher.process_one("SNS")
    make_due(pg_engine)
    assert dispatcher.process_one("SNS") is True
    row = rows(pg_engine, event_id)["SNS"]
    assert (row.state, row.final_reason, row.last_category, row.last_error_code, row.attempts) == (
        "FAILED", "EXHAUSTED", "TRANSIENT", "sns_Throttling", 2,
    )


def test_slack_crash_keeps_the_lane_closed_until_its_hold_expires(sessions, pg_engine):
    routes = registry(entry("customer-a", ACCOUNT_A, slack="SLACK_A"))
    crashed = make_dispatcher(sessions, routes, slack=SlackWire(CRASH_AFTER_ACCEPT), lease_seconds=30)
    activate(crashed, pg_engine)
    event_id = commit_event(sessions)
    crashed.discover()
    with pytest.raises(_Crash):
        crashed.process_one("SLACK")
    wire = SlackWire()
    restarted = make_dispatcher(sessions, routes, slack=wire)
    expire_leases(pg_engine)
    restarted.process_one("SLACK")
    make_due(pg_engine)
    assert 25 < lane_wait_seconds(pg_engine) <= 30
    assert restarted.process_one("SLACK") is False and wire.posts == []
    open_lane(pg_engine)
    assert restarted.process_one("SLACK") is True
    assert rows(pg_engine, event_id)["SLACK"].state == "SUCCEEDED" and len(wire.posts) == 1


def test_stale_result_after_lease_reclaim_is_fenced(sessions, pg_engine):
    routes = registry(entry("customer-a", ACCOUNT_A, sns=TOPIC_A))
    entered, release = threading.Event(), threading.Event()
    slow = SnsWire({"MessageId": "late-result"})
    slow.on_publish = lambda: (entered.set(), release.wait(10))
    first = make_dispatcher(sessions, routes, sns=slow)
    activate(first, pg_engine)
    event_id = commit_event(sessions)
    first.discover()
    outcome = []
    worker = threading.Thread(target=lambda: outcome.append(first.process_one("SNS")))
    worker.start()
    assert entered.wait(10)

    second_wire = SnsWire({"MessageId": "reclaimed-result"})
    second = make_dispatcher(sessions, routes, sns=second_wire)
    expire_leases(pg_engine)
    second.process_one("SNS")
    make_due(pg_engine)
    assert second.process_one("SNS") is True
    release.set()
    worker.join(10)
    assert outcome == [True]
    row = rows(pg_engine, event_id)["SNS"]
    assert (row.state, row.receipt_ref, row.attempts) == ("SUCCEEDED", "reclaimed-result", 2)


def test_terminal_rows_are_never_reset_or_resent(sessions, pg_engine):
    routes = registry(entry("customer-a", ACCOUNT_A, slack="SLACK_A"))
    wire = SlackWire((200, {}, b"ok"), (404, {}, b"no_service"))
    dispatcher = make_dispatcher(sessions, routes, slack=wire)
    activate(dispatcher, pg_engine)
    succeeded, failed = commit_event(sessions), commit_event(sessions)
    blocked = commit_event(sessions, None)
    dispatcher.discover()
    assert dispatcher.process_one("SLACK")
    open_lane(pg_engine)
    assert dispatcher.process_one("SLACK")
    snapshot = {e: rows(pg_engine, e)["SLACK"] for e in (succeeded, failed, blocked)}
    assert [snapshot[e].state for e in (succeeded, failed, blocked)] == [
        "SUCCEEDED", "FAILED", "BLOCKED",
    ]
    assert (snapshot[failed].final_reason, snapshot[failed].last_error_code) == (
        "PERMANENT", "slack_http_404",
    )

    with sessions() as db:
        for event_id, row in snapshot.items():
            any_token = str(uuid.uuid4())
            assert not deliveries_repo.settle(db, row.delivery_id, any_token,
                                              deliveries_repo.succeeded("SLACK_POSTED", "x"))
            assert not deliveries_repo.recover_expired(
                db, row.delivery_id, deliveries_repo.retry("UNKNOWN", "x", 0),
            )
            deliveries_repo.start_attempt(db, row.delivery_id, any_token, 30)
            deliveries_repo.block_pending(db, row.delivery_id, "ROUTE_CHANGED")
        db.commit()
    make_dispatcher(sessions, registry(entry("customer-a", ACCOUNT_A, slack="SLACK_A2")),
                    slack=wire).initialize()
    for _ in range(2):
        expire_leases(pg_engine)
        make_due(pg_engine)
        open_lane(pg_engine)
        dispatcher.discover()
        assert dispatcher.process_one("SLACK") is False
    for event_id, before in snapshot.items():
        after = rows(pg_engine, event_id)["SLACK"]
        assert (after.state, after.attempts, after.updated_at, after.blocked_reason) == (
            before.state, before.attempts, before.updated_at, before.blocked_reason,
        )
    assert len(wire.posts) == 2


TERMINAL = {"SUCCEEDED", "FAILED", "BLOCKED"}


def test_removed_channel_closes_pending_and_crashed_rows_without_sending(sessions, pg_engine):
    both = registry(entry("customer-a", ACCOUNT_A, slack="SLACK_A", sns=TOPIC_A))
    sns = SnsWire(CRASH_BEFORE_SEND)
    first = make_dispatcher(sessions, both, slack=SlackWire(), sns=sns)
    activate(first, pg_engine)
    crashed = commit_event(sessions)
    first.discover()
    with pytest.raises(_Crash):
        first.process_one("SNS")
    waiting = commit_event(sessions)
    first.discover()
    with pg_engine.begin() as connection:  # 백오프 중(기한 전)인 행도 정리 대상이다
        connection.execute(text(
            "UPDATE notification_deliveries SET next_attempt_at = now() + interval '1 hour'"
            " WHERE state = 'PENDING' AND channel = 'SNS'"
        ))
    expire_leases(pg_engine)
    assert rows(pg_engine, crashed)["SNS"].state == "IN_FLIGHT"

    # 재기동: SNS를 설정에서 뺐고 Slack 발신기만 있다. 실제 채널 스레드로 돈다.
    wire = SlackWire()
    restarted = make_dispatcher(
        sessions, registry(entry("customer-a", ACCOUNT_A, slack="SLACK_A")), slack=wire,
    )

    def settled():
        return all(
            row.state in TERMINAL
            for event_id in (crashed, waiting) for row in rows(pg_engine, event_id).values()
        )

    async def run():
        await restarted.start()
        try:
            assert await asyncio.to_thread(_wait_for, settled)
        finally:
            await restarted.stop()

    asyncio.run(run())
    assert route_row(pg_engine, ACCOUNT_A, "SNS") is None
    recovered, pending = rows(pg_engine, crashed)["SNS"], rows(pg_engine, waiting)["SNS"]
    assert (recovered.state, recovered.blocked_reason, recovered.attempts) == (
        "BLOCKED", "CHANNEL_NOT_CONFIGURED", 1,
    )
    assert (recovered.last_category, recovered.last_error_code) == ("UNKNOWN", "lease_expired")
    assert (pending.state, pending.blocked_reason, pending.attempts) == (
        "BLOCKED", "CHANNEL_NOT_CONFIGURED", 0,
    )
    assert sns.publishes == [] and sns.outcomes == []  # 외부 호출·새 시도 없음
    assert {(rows(pg_engine, e)["SLACK"].state, rows(pg_engine, e)["SLACK"].attempts)
            for e in (crashed, waiting)} == {("SUCCEEDED", 1)}  # 다른 채널은 그대로 보낸다
    assert sorted(p["url"] for p in wire.posts) == [WEBHOOK_A, WEBHOOK_A]


def test_route_without_sender_blocks_pending_row_instead_of_claiming(sessions, pg_engine):
    routes = registry(entry("customer-a", ACCOUNT_A, sns=TOPIC_A))
    sns = SnsWire()
    first = make_dispatcher(sessions, routes, sns=sns)
    activate(first, pg_engine)
    event_id = commit_event(sessions)
    first.discover()

    no_sender = make_dispatcher(sessions, routes)  # 설정 라우트는 남았지만 발신기가 없다
    no_sender.initialize()
    assert route_row(pg_engine, ACCOUNT_A, "SNS") is not None
    assert no_sender.process_one("SNS") is True
    assert no_sender.process_one("SNS") is False
    row = rows(pg_engine, event_id)["SNS"]
    assert (row.state, row.blocked_reason, row.attempts) == ("BLOCKED", "CHANNEL_NOT_CONFIGURED", 0)
    assert sns.publishes == []


def test_lower_attempt_limit_after_restart_closes_rows_without_sending(sessions, pg_engine):
    routes = registry(entry("customer-a", ACCOUNT_A, sns=TOPIC_A))
    not_found = ClientError({"Error": {"Code": "NotFound", "Message": "raw"}}, "Publish")
    first = make_dispatcher(  # 긴 백오프 — 재시도 행이 다음 사건보다 먼저 기한이 되지 않게
        sessions, routes, max_attempts=5, backoff_seconds=60,
        sns=SnsWire(_throttled(), _throttled(), _throttled(), None, not_found, _throttled()),
    )
    activate(first, pg_engine)

    def committed_and_processed(dispatcher):
        event_id = commit_event(sessions)
        dispatcher.discover()
        assert dispatcher.process_one("SNS")
        return event_id

    over = committed_and_processed(first)
    for _ in range(2):
        make_due(pg_engine)
        assert first.process_one("SNS")
    succeeded = committed_and_processed(first)
    permanent = committed_and_processed(first)
    under = committed_and_processed(first)
    exhausted = committed_and_processed(
        make_dispatcher(sessions, routes, sns=SnsWire(_throttled()), max_attempts=1),
    )
    uncategorized = commit_event(sessions)
    first.discover()
    with pg_engine.begin() as connection:  # 범주 없이 시도 4회가 남은 PENDING
        connection.execute(text(
            "UPDATE notification_deliveries SET attempts = 4 WHERE event_id = :event_id"
        ), {"event_id": uncategorized})
    assert (rows(pg_engine, over)["SNS"].state, rows(pg_engine, over)["SNS"].attempts) == (
        "PENDING", 3,
    )
    terminal = {e: rows(pg_engine, e)["SNS"] for e in (succeeded, permanent, exhausted)}
    assert [(r.state, r.final_reason) for r in terminal.values()] == [
        ("SUCCEEDED", None), ("FAILED", "PERMANENT"), ("FAILED", "EXHAUSTED"),
    ]
    with sessions() as db:  # 한도 미달 행·종결 행은 이 전이에 걸리지 않는다
        assert not deliveries_repo.exhaust_pending(db, rows(pg_engine, under)["SNS"].delivery_id, 3)
        for row in terminal.values():
            assert not deliveries_repo.exhaust_pending(db, row.delivery_id, 1)
        db.rollback()

    wire = SnsWire()
    restarted = make_dispatcher(sessions, routes, sns=wire, max_attempts=3)
    restarted.initialize()
    make_due(pg_engine)
    while restarted.process_one("SNS"):
        pass
    closed, fallback = rows(pg_engine, over)["SNS"], rows(pg_engine, uncategorized)["SNS"]
    assert (closed.state, closed.final_reason, closed.attempts) == ("FAILED", "EXHAUSTED", 3)
    assert (closed.last_category, closed.last_error_code) == ("TRANSIENT", "sns_Throttling")
    assert (fallback.state, fallback.final_reason, fallback.attempts) == ("FAILED", "EXHAUSTED", 4)
    assert (fallback.last_category, fallback.last_error_code) == ("UNKNOWN", None)
    sent = rows(pg_engine, under)["SNS"]
    assert (sent.state, sent.attempts) == ("SUCCEEDED", 2)
    assert len(wire.publishes) == 1 and under in wire.publishes[0]["body"]
    for event_id, before in terminal.items():
        after = rows(pg_engine, event_id)["SNS"]
        assert (after.state, after.attempts, after.updated_at, after.final_reason,
                after.last_category, after.last_error_code) == (
            before.state, before.attempts, before.updated_at, before.final_reason,
            before.last_category, before.last_error_code,
        )


# --- 실패 분류·백오프·429·UNKNOWN·채널 격리 --------------------------------------------


def test_transient_failures_back_off_then_exhaust(sessions, pg_engine):
    routes = registry(entry("customer-a", ACCOUNT_A, sns=TOPIC_A))
    wire = SnsWire(_throttled(), _throttled(), _throttled())
    dispatcher = make_dispatcher(sessions, routes, sns=wire, max_attempts=3, backoff_seconds=2,
                                 max_backoff_seconds=3)
    activate(dispatcher, pg_engine)
    event_id = commit_event(sessions)
    dispatcher.discover()
    delays = []
    for _ in range(2):
        assert dispatcher.process_one("SNS")
        row = rows(pg_engine, event_id)["SNS"]
        assert (row.state, row.last_category, row.last_error_code) == (
            "PENDING", "TRANSIENT", "sns_Throttling",
        )
        delays.append(retry_delay(row))
        assert dispatcher.process_one("SNS") is False  # 기한 전에는 다시 보내지 않는다
        make_due(pg_engine)
    assert delays == [timedelta(seconds=2), timedelta(seconds=3)]  # 2·4초 → 상한 3초
    assert dispatcher.process_one("SNS")
    row = rows(pg_engine, event_id)["SNS"]
    assert (row.state, row.final_reason, row.attempts) == ("FAILED", "EXHAUSTED", 3)
    assert len(wire.publishes) == 3


def test_slack_429_cooldown_covers_the_whole_lane_and_survives_restart(sessions, pg_engine):
    routes = registry(
        entry("customer-a", ACCOUNT_A, slack="SLACK_A", sns=TOPIC_A),
        entry("customer-b", ACCOUNT_B, slack="SLACK_B"),
    )
    first = make_dispatcher(sessions, routes, slack=SlackWire((429, {"Retry-After": "120"}, b"")),
                            sns=SnsWire(), max_backoff_seconds=60)
    activate(first, pg_engine)
    event_a, event_b = commit_event(sessions, ACCOUNT_A), commit_event(sessions, ACCOUNT_B)
    first.discover()
    assert first.process_one("SLACK")
    limited = [e for e in (event_a, event_b) if rows(pg_engine, e)["SLACK"].attempts == 1]
    [limited_row] = [rows(pg_engine, e)["SLACK"] for e in limited]
    assert (limited_row.state, limited_row.last_error_code) == ("PENDING", "slack_http_429")
    assert retry_delay(limited_row) == timedelta(seconds=120)  # 상한(60초)으로 자르지 않는다
    assert 119 < lane_wait_seconds(pg_engine) <= 120

    wire = SlackWire()
    sns = SnsWire()
    restarted = make_dispatcher(sessions, routes, slack=wire, sns=sns)
    restarted.initialize()
    make_due(pg_engine)
    assert restarted.process_one("SLACK") is False and wire.posts == []  # 다른 고객 B도 대기
    assert restarted.process_one("SNS") is True  # SNS는 Slack 대기와 무관하다
    assert rows(pg_engine, event_a)["SNS"].state == "SUCCEEDED"
    open_lane(pg_engine)
    assert restarted.process_one("SLACK") is True and len(wire.posts) == 1


@pytest.mark.parametrize(("b_response", "b_state"), [
    ((200, {}, b"ok"), "SUCCEEDED"),
    ((404, {}, b"no_service"), "FAILED"),
    ((429, {}, b""), "PENDING"),  # Retry-After 없는 TRANSIENT
], ids=["success", "permanent", "transient"])
def test_stale_429_cooldown_survives_the_current_holders_release(
    sessions, pg_engine, b_response, b_state,
):
    """A 임대 만료 → B가 관문 보유 → A의 늦은 429(120초) → B 해제. 120초 대기가 남아야 한다."""
    routes = registry(
        entry("customer-a", ACCOUNT_A, slack="SLACK_A"),
        entry("customer-b", ACCOUNT_B, slack="SLACK_B"),
    )
    a_entered, a_release, b_entered, b_release = (threading.Event() for _ in range(4))
    a_wire = SlackWire((429, {"Retry-After": "120"}, b""))
    a_wire.on_request = lambda: (a_entered.set(), a_release.wait(10))
    b_wire = SlackWire(b_response)
    b_wire.on_request = lambda: (b_entered.set(), b_release.wait(10))
    a = make_dispatcher(sessions, routes, slack=a_wire)
    b = make_dispatcher(sessions, routes, slack=b_wire)
    activate(a, pg_engine)
    a_event = commit_event(sessions, ACCOUNT_A)
    a.discover()
    b_event = commit_event(sessions, ACCOUNT_B)
    a.discover()

    a_thread = threading.Thread(target=a.process_one, args=("SLACK",))
    a_thread.start()
    assert a_entered.wait(10)
    a_token = rows(pg_engine, a_event)["SLACK"].claim_token
    expire_leases(pg_engine)  # A의 임대와 관문 보유 시간이 지난 것으로 만든다
    open_lane(pg_engine)
    b_thread = threading.Thread(target=b.process_one, args=("SLACK",))
    b_thread.start()
    assert b_entered.wait(10)
    recovered, b_row = rows(pg_engine, a_event)["SLACK"], rows(pg_engine, b_event)["SLACK"]
    assert (recovered.state, recovered.claim_token, recovered.attempts) == ("PENDING", None, 1)
    assert b_row.state == "IN_FLIGHT" and b_row.claim_token not in (None, a_token)

    a_release.set()  # 늦은 429: 결과는 막히지만 공급자 대기는 남는다
    a_thread.join(10)
    assert rows(pg_engine, a_event)["SLACK"].last_error_code == "lease_expired"
    assert 119 < lane_waits(pg_engine)[1] <= 120
    b_release.set()
    b_thread.join(10)
    assert rows(pg_engine, b_event)["SLACK"].state == b_state
    spacing, cooldown = lane_waits(pg_engine)
    assert 0 < spacing <= 1  # 보유자 B는 임대 길이가 아니라 1초 간격으로 연다
    assert 118 < cooldown <= 120  # B의 해제가 A의 공급자 대기를 줄이지 않는다

    wire = SlackWire()
    restarted = make_dispatcher(sessions, routes, slack=wire)
    restarted.initialize()
    assert 118 < lane_waits(pg_engine)[1] <= 120  # 재기동 뒤에도 유지
    make_due(pg_engine)
    with pg_engine.begin() as connection:  # 1초 간격은 지난 것으로 — 공급자 대기만 남긴다
        connection.execute(text("UPDATE notification_lanes SET available_at = now()"))
    assert restarted.process_one("SLACK") is False and wire.posts == []
    with pg_engine.begin() as connection:  # 120초가 지난 것으로
        connection.execute(text(
            "UPDATE notification_lanes SET cooldown_until = now() - interval '1 second'"
        ))
    assert restarted.process_one("SLACK") is True and len(wire.posts) == 1
    assert len(a_wire.posts) == len(b_wire.posts) == 1


def test_unknown_results_and_sender_exceptions_retry_within_bounds(sessions, pg_engine, caplog):
    secret = "https://hooks.slack.com/services/LEAK/LEAK/leaked-secret"

    class RaisingSender:
        def send(self, event, route, public_base_url):
            raise RuntimeError(secret)

        def close(self):
            return None

    routes = registry(entry("customer-a", ACCOUNT_A, slack="SLACK_A", sns=TOPIC_A))
    slack = SlackWire((503, {}, b"down"))
    dispatcher = DeliveryDispatcher(
        sessions, routes,
        {"SLACK": SlackSender(connection_factory=slack), "SNS": RaisingSender()},
        public_base_url=BASE_URL, max_attempts=2, backoff_seconds=1, lease_seconds=30,
    )
    activate(dispatcher, pg_engine)
    event_id = commit_event(sessions)
    dispatcher.discover()
    caplog.set_level("INFO")
    assert dispatcher.process_one("SLACK") and dispatcher.process_one("SNS")
    stored = rows(pg_engine, event_id)
    assert (stored["SLACK"].state, stored["SLACK"].last_category, stored["SLACK"].last_error_code) \
        == ("PENDING", "UNKNOWN", "slack_http_503")
    assert (stored["SNS"].state, stored["SNS"].last_category, stored["SNS"].last_error_code) == (
        "PENDING", "UNKNOWN", "sender_raised",
    )
    make_due(pg_engine)
    open_lane(pg_engine)
    assert dispatcher.process_one("SLACK") and dispatcher.process_one("SNS")
    stored = rows(pg_engine, event_id)
    assert (stored["SLACK"].state, stored["SLACK"].attempts) == ("SUCCEEDED", 2)
    assert (stored["SNS"].state, stored["SNS"].final_reason) == ("FAILED", "EXHAUSTED")
    assert "notification_sender_raised" in caplog.messages
    assert secret not in caplog.text and "leaked-secret" not in caplog.text


def _wait_for(predicate, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def test_running_channel_threads_are_isolated_from_each_others_failures(
    sessions, pg_engine, caplog,
):
    routes = registry(entry("customer-a", ACCOUNT_A, slack="SLACK_A", sns=TOPIC_A))

    class SnsBrokenRegistry:
        @property
        def routes(self):
            return routes.routes

        def resolve(self, event, channel):
            if channel == "SNS":
                raise RuntimeError("SELECT secret FROM somewhere")
            return routes.resolve(event, channel)

    wire = SlackWire()
    dispatcher = make_dispatcher(sessions, SnsBrokenRegistry(), slack=wire, sns=SnsWire())
    activate(dispatcher, pg_engine)
    epoch = route_row(pg_engine, ACCOUNT_A, "SLACK").route_epoch
    event_id = commit_event(sessions)
    caplog.set_level("INFO")

    async def run():
        await dispatcher.start()
        try:
            assert await asyncio.to_thread(
                _wait_for, lambda: rows(pg_engine, event_id).get("SLACK") is not None
                and rows(pg_engine, event_id)["SLACK"].state == "SUCCEEDED",
            )
        finally:
            await dispatcher.stop()

    asyncio.run(run())
    assert route_row(pg_engine, ACCOUNT_A, "SLACK").route_epoch == epoch  # start가 epoch 유지
    assert "SNS" not in rows(pg_engine, event_id) and len(wire.posts) == 1
    failures = [r for r in caplog.records if r.getMessage() == "notification_delivery_loop_failed"]
    assert failures and {r.channel for r in failures} == {"SNS"}
    assert "secret" not in caplog.text
    assert not [t for t in threading.enumerate() if t.name.startswith("notification-")]


# --- 시작·종료·취소 -----------------------------------------------------------------


def test_stop_waits_for_in_flight_result_even_when_the_caller_is_cancelled(sessions, pg_engine):
    entered, release = threading.Event(), threading.Event()
    wire = SlackWire()
    wire.on_request = lambda: (entered.set(), release.wait(10))
    dispatcher = make_dispatcher(
        sessions, registry(entry("customer-a", ACCOUNT_A, slack="SLACK_A")), slack=wire,
    )
    activate(dispatcher, pg_engine)
    event_id = commit_event(sessions)

    async def run():
        await dispatcher.start()
        assert await asyncio.to_thread(entered.wait, 10)
        stopping = asyncio.create_task(dispatcher.stop())
        await asyncio.sleep(0.2)
        assert not stopping.done()
        stopping.cancel()
        await asyncio.sleep(0.2)
        assert not stopping.done()  # 취소돼도 전송 스레드가 끝날 때까지 돌아오지 않는다
        assert rows(pg_engine, event_id)["SLACK"].state == "IN_FLIGHT"
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await stopping

    asyncio.run(run())
    row = rows(pg_engine, event_id)["SLACK"]
    assert (row.state, row.attempts) == ("SUCCEEDED", 1)
    assert not [t for t in threading.enumerate() if t.name.startswith("notification-")]


def test_initialize_failure_is_sanitized_and_starts_no_threads(sessions, pg_engine, caplog):
    secret = "postgresql://user:hunter2@db/x SELECT webhook"

    def broken_sessions():
        raise RuntimeError(secret)

    routes = registry(entry("customer-a", ACCOUNT_A, slack="SLACK_A"))
    dispatcher = make_dispatcher(broken_sessions, routes, slack=SlackWire())
    caplog.set_level("INFO")

    async def run():
        with pytest.raises(RuntimeError) as failure:
            await dispatcher.start()
        await dispatcher.stop()
        return failure.value

    error = asyncio.run(run())
    assert str(error) == "NOTIFICATION_DISPATCHER_INITIALIZE_FAILED"
    assert error.__cause__ is None and error.__context__ is None
    assert "notification_dispatcher_initialize_failed" in caplog.messages
    assert "hunter2" not in caplog.text and secret not in caplog.text
    assert not [t for t in threading.enumerate() if t.name.startswith("notification-")]
    assert route_row(pg_engine, ACCOUNT_A, "SLACK") is None


def test_partial_worker_start_stops_started_workers(sessions, pg_engine, monkeypatch):
    real_thread = threading.Thread

    class FailingSnsThread(real_thread):
        def start(self):
            if self.name == "notification-sns":
                raise RuntimeError("cannot start thread")
            super().start()

    monkeypatch.setattr(dispatcher_module.threading, "Thread", FailingSnsThread)
    dispatcher = make_dispatcher(
        sessions, registry(entry("customer-a", ACCOUNT_A, slack="SLACK_A")), slack=SlackWire(),
    )

    async def run():
        with pytest.raises(RuntimeError, match="cannot start thread"):
            await dispatcher.start()

    asyncio.run(run())
    assert not [t for t in threading.enumerate() if t.name.startswith("notification-")]
    monkeypatch.setattr(dispatcher_module.threading, "Thread", real_thread)

    async def restart():
        await dispatcher.start()
        await dispatcher.stop()

    asyncio.run(restart())  # 실패한 시작 뒤에도 다시 시작·정지할 수 있다


def test_real_app_lifespan_runs_the_dispatcher_against_postgres(
    sessions, pg_engine, tmp_path, monkeypatch,
):
    """main.create_app → build_notification_runtime → 실제 DeliveryDispatcher 스레드.

    바꾸는 것은 세션 팩토리(격리 DB)와 sender의 전송 계층뿐이다.
    """
    import db.session as db_session
    import main
    from config import get_settings
    from fastapi.testclient import TestClient
    from notifications import channels

    slack, sns = SlackWire(), SnsWire()

    class WiredSlackSender(channels.SlackSender):
        def __init__(self, **timeouts):
            super().__init__(connection_factory=slack, **timeouts)

    class WiredSnsSender(channels.SnsSender):
        def __init__(self, **timeouts):
            super().__init__(client_factory=sns, **timeouts)

    monkeypatch.setattr(channels, "SlackSender", WiredSlackSender)
    monkeypatch.setattr(channels, "SnsSender", WiredSnsSender)
    monkeypatch.setattr(db_session, "get_session_factory", lambda: sessions)
    routes_file = tmp_path / "routes.json"
    routes_file.write_text(json.dumps({"routes": [{
        "tenant_id": "customer-a", "aws_account_id": ACCOUNT_A,
        "slack_webhook_env": "NOTIFICATION_TEST_SLACK_A", "sns_topic_arn": TOPIC_A,
    }]}), encoding="utf-8")
    monkeypatch.setenv("NOTIFICATION_TEST_SLACK_A", WEBHOOK_A)
    monkeypatch.setenv("NOTIFICATION_ROUTES_FILE", str(routes_file))
    monkeypatch.setenv("NOTIFICATION_PUBLIC_BASE_URL", BASE_URL)
    monkeypatch.setenv("NOTIFICATION_POLL_SECONDS", "0.1")
    monkeypatch.setenv("NOTIFICATION_ENABLED", "true")  # conftest의 강제 비활성화 뒤 명시적 opt-in
    get_settings.cache_clear()
    intake_rows = None
    try:
        with TestClient(main.create_app()) as client:
            assert client.get("/health").status_code == 200
            activated = route_row(pg_engine, ACCOUNT_A, "SLACK").activated_at
            assert route_row(pg_engine, ACCOUNT_A, "SNS") is not None
            # 사건 created_at(호스트 시계)이 DB 활성화 시각 뒤에 오게 한다
            assert _wait_for(lambda: datetime.now(UTC) > activated)
            intake_rows = _seeded_high_intake(sessions, ACCOUNT_A)
            assert _wait_for(lambda: len(slack.posts) == 1 and len(sns.publishes) == 1)
        assert not [t for t in threading.enumerate() if t.name.startswith("notification-")]
        with Session(pg_engine) as db:
            [event] = events_repo.list_by_incident(db, intake_rows[0])
        stored = rows(pg_engine, event.event_id)
        assert {c: (r.state, r.attempts) for c, r in stored.items()} == {
            "SLACK": ("SUCCEEDED", 1), "SNS": ("SUCCEEDED", 1),
        }
        assert slack.posts[0]["url"] == WEBHOOK_A and sns.publishes[0]["topic"] == TOPIC_A
    finally:
        get_settings.cache_clear()
        if intake_rows is not None:
            _remove_intake_rows(pg_engine, *intake_rows)
