"""#493 PR2 발송 dispatcher — 실제 PostgreSQL·Alembic head, 실제 routing·sender + 전송 계층 대역.

네트워크·AWS·실제 비밀값은 쓰지 않는다. Slack은 HTTPSConnection 대역, SNS는 client 대역이다.
시간 경과(임대 만료·재시도 기한·관문 대기)는 DB 행 시각을 앞당겨 결정적으로 흉내 낸다.
호스트 시계(사건 created_at)와 DB 시계(activated_at)의 차이를 없애려고, 활성화 시점이 주제가
아닌 테스트는 activate()로 활성화를 1분 앞당기고, 과거 사건은 1시간 전 시각으로 만든다.
"""

import asyncio
import json
import re
import threading
import time
import uuid
from contextlib import contextmanager
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
from sqlalchemy import create_engine, delete, event, func, insert, select, text, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

TENANT_A = "11111111-1111-4111-8111-111111111111"
TENANT_B = "22222222-2222-4222-8222-222222222222"
TENANT_MISSING = "33333333-3333-4333-8333-333333333333"  # 형식은 유효하지만 Tenant 행이 없다
ACCOUNT_A = "111111111111"
ACCOUNT_B = "222222222222"
ACCOUNT_OTHER = "333333333333"  # 어느 테넌트 설정에도 없는 계정 — 불일치 흉내용
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
        self.wire.posts.append({"url": f"https://{self.host}{path}", "body": body.decode()})
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


TENANT_ACCOUNTS = {TENANT_A: ACCOUNT_A, TENANT_B: ACCOUNT_B}


def _tenant_role(account):
    return f"arn:aws:iam::{account}:role/vigilantis-test"


def _drop_owned_tenants(engine):
    """이 파일이 심은 테넌트 행만 지운다."""
    with engine.begin() as connection:
        connection.execute(
            delete(models.Tenant).where(models.Tenant.tenant_id.in_(list(TENANT_ACCOUNTS)))
        )


def _seed_owned_tenants(engine):
    _drop_owned_tenants(engine)
    with Session(engine) as db:
        for tenant, account in TENANT_ACCOUNTS.items():
            db.add(models.Tenant(
                tenant_id=tenant, name=f"테스트 {account}", aws_account_id=account,
                access_role_arn=_tenant_role(account), external_id=f"external-{tenant}",
                regions=[REGION],
            ))
        db.commit()


@pytest.fixture()
def sessions(pg_engine):
    """발송 표는 전역 anti-join 대상이라 테스트마다 비운다. 세션은 각 단계가 따로 연다.

    권위 있는 Tenant(ACCOUNT_A/B)를 실제 모델·제약으로 심고, 정리는 발송 표를 비운 뒤 이 행만 지운다.
    """
    _wipe(pg_engine)
    _seed_owned_tenants(pg_engine)
    yield sessionmaker(bind=pg_engine, autoflush=False, expire_on_commit=False)
    _wipe(pg_engine)
    _drop_owned_tenants(pg_engine)


def delete_tenant(engine, tenant):
    with engine.begin() as connection:
        connection.execute(delete(models.Tenant).where(models.Tenant.tenant_id == tenant))


def move_tenant_account(engine, tenant, account):
    """권위 계정을 바꾼다(역할 ARN은 CHECK 때문에 함께 옮긴다)."""
    with engine.begin() as connection:
        connection.execute(
            update(models.Tenant).where(models.Tenant.tenant_id == tenant)
            .values(aws_account_id=account, access_role_arn=_tenant_role(account))
        )


BROKEN_TENANT = {"missing": "TENANT_NOT_FOUND", "mismatch": "TENANT_ACCOUNT_MISMATCH"}


def break_tenant(engine, kind, tenant):
    if kind == "missing":
        delete_tenant(engine, tenant)
    else:
        move_tenant_account(engine, tenant, ACCOUNT_OTHER)


TRANSITION_REQUIRED = "NOTIFICATION_TENANT_TRANSITION_REQUIRED"


def refuse(dispatcher, code):
    with pytest.raises(ValueError) as failure:
        dispatcher.initialize()
    assert str(failure.value) == code


def snapshot(engine):
    """발송 관련 표 전체의 행 값(시각 포함) — 거부·실패가 아무것도 바꾸지 않았음을 전체 동등으로 본다."""
    with engine.connect() as connection:
        return {
            model.__tablename__: sorted(
                (tuple(row) for row in connection.execute(select(model.__table__))), key=repr,
            )
            for model in (
                models.NotificationRoute, models.NotificationLane, models.NotificationDelivery,
            )
        }


def put_delivery(engine, event_id, channel, state, **over):
    """저장소를 거치지 않고 (사건, 채널) 발송 행 1개를 CHECK를 지키는 모양으로 직접 심는다."""
    values = {
        "delivery_id": str(uuid.uuid4()), "event_id": event_id, "channel": channel,
        "state": state, "tenant_id": TENANT_A, "aws_account_id": ACCOUNT_A,
        "route_fingerprint": "f" * 64, "route_epoch": str(uuid.uuid4()),
    }
    receipt = {"SLACK": "SLACK_POSTED", "SNS": "SNS_ACCEPTED"}[channel]
    values |= {
        "PENDING": {"attempts": 0, "next_attempt_at": func.now()},
        "IN_FLIGHT": {
            "attempts": 1, "claim_token": str(uuid.uuid4()),
            "lease_expires_at": func.now() + timedelta(seconds=300),
        },
        "SUCCEEDED": {"attempts": 1, "finished_at": func.now(), "receipt_kind": receipt},
        "FAILED": {
            "attempts": 1, "finished_at": func.now(), "last_category": "PERMANENT",
            "final_reason": "PERMANENT", "last_error_code": "legacy",
        },
        "BLOCKED": {"blocked_reason": "ROUTE_CHANGED", "finished_at": func.now()},
    }[state] | over
    with engine.begin() as connection:
        connection.execute(insert(models.NotificationDelivery).values(**values))
    return values["delivery_id"]


@contextmanager
def tenant_lookup_failing(engine):
    """권위 Tenant 표를 읽는 모든 문장을 DB 오류로 만든다(다른 표 문장은 그대로)."""
    def fail(_connection, _cursor, statement, parameters, _context, _executemany):
        if re.search(r"\btenants\b", statement):
            raise OperationalError(statement, parameters, Exception("injected db failure"))

    event.listen(engine, "before_cursor_execute", fail)
    try:
        yield
    finally:
        event.remove(engine, "before_cursor_execute", fail)


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


def db_clock(engine):
    """관문이 비교하는 PostgreSQL 서버 시계의 지금 시각(호스트 시계와 섞지 않는다)."""
    with engine.connect() as connection:
        return connection.execute(text("SELECT clock_timestamp()")).scalar_one()


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
            TENANT_A, ACCOUNT_A,
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
                assert (row.state, row.attempts, row.tenant_id) == ("SUCCEEDED", 1, TENANT_A)
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
        # 클라이언트 접속만 센다 — autovacuum 등 서버 내부 작업은 앱 세션·트랜잭션이 아니다
        with observer.connect() as connection:
            busy = connection.execute(text(
                "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database()"
                " AND pid <> pg_backend_pid() AND backend_type = 'client backend'"
                " AND state <> 'idle'"
            )).scalar_one()
        return pg_engine.pool.checkedout(), busy

    with sessions() as held:  # 대조군: 앱 세션이 트랜잭션을 연 채면 관찰기가 잡는다
        held.execute(text("SELECT 1"))
        assert observe() == (1, 1)

    slack, sns = SlackWire(), SnsWire()
    slack.on_request = sns.on_publish = lambda: seen.append(observe())
    dispatcher = make_dispatcher(
        sessions, registry(entry(TENANT_A, ACCOUNT_A, slack="SLACK_A", sns=TOPIC_A)),
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
        entry(TENANT_A, ACCOUNT_A, slack="SLACK_A"),
        entry(TENANT_B, ACCOUNT_B, slack="SLACK_B"),
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
        entry(TENANT_A, ACCOUNT_A, slack="SLACK_A2"),
        entry(TENANT_B, ACCOUNT_B, slack="SLACK_B"),
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
    configured = registry(entry(TENANT_A, ACCOUNT_A, sns=TOPIC_A))
    wire = SnsWire()
    dispatcher = make_dispatcher(sessions, configured, sns=wire)
    activate(dispatcher, pg_engine)
    dispatcher.initialize()  # 같은 설정 재등록은 epoch·활성화 시각을 바꾸지 않는다
    before = route_row(pg_engine, ACCOUNT_A, "SNS")
    pending = commit_event(sessions)
    dispatcher.discover()

    removed = registry(entry(TENANT_A, ACCOUNT_A))
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
        sessions, registry(entry(TENANT_A, ACCOUNT_A, slack="SLACK_A", sns=TOPIC_A)),
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
            entry(TENANT_A, ACCOUNT_A, slack="SLACK_A", sns=TOPIC_A),
            entry(TENANT_B, ACCOUNT_B, slack="SLACK_B", sns=TOPIC_B),
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
        sessions, registry(entry(TENANT_A, ACCOUNT_A, sns=TOPIC_A)), sns=SnsWire(),
        batch_size=2,
    )
    activate(dispatcher, pg_engine)
    for _ in range(5):
        commit_event(sessions)
    assert [dispatcher.discover() for _ in range(4)] == [4, 4, 2, 0]


def test_late_committed_older_event_is_still_discovered(sessions, pg_engine):
    dispatcher = make_dispatcher(
        sessions, registry(entry(TENANT_A, ACCOUNT_A, sns=TOPIC_A)), sns=SnsWire(),
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
    routes = registry(entry(TENANT_A, ACCOUNT_A, slack="SLACK_A", sns=TOPIC_A))
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
    routes = registry(entry(TENANT_A, ACCOUNT_A, sns=TOPIC_A))
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
        entry(TENANT_A, ACCOUNT_A, slack="SLACK_A"),
        entry(TENANT_B, ACCOUNT_B, slack="SLACK_B"),
    )
    wire = SlackWire()
    sent_at = []  # 전송 계층 경계에서 관문과 같은 DB 시계로 잰다
    wire.on_request = lambda: sent_at.append(db_clock(pg_engine))
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
    times = sorted(sent_at)
    assert len(times) == 3
    assert all(later - earlier >= timedelta(seconds=1) for earlier, later in zip(times, times[1:]))
    assert {rows(pg_engine, e)["SLACK"].state for e in events} == {"SUCCEEDED"}


# --- 중단·임대 회수·늦은 결과 ---------------------------------------------------------


def test_crash_during_discovery_leaves_nothing_and_next_process_sends_once(sessions, pg_engine):
    routes = registry(entry(TENANT_A, ACCOUNT_A, sns=TOPIC_A))

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
    routes = registry(entry(TENANT_A, ACCOUNT_A, sns=TOPIC_A))
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
    routes = registry(entry(TENANT_A, ACCOUNT_A, sns=TOPIC_A))
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
    routes = registry(entry(TENANT_A, ACCOUNT_A, sns=TOPIC_A))
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
    routes = registry(entry(TENANT_A, ACCOUNT_A, slack="SLACK_A"))
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
    routes = registry(entry(TENANT_A, ACCOUNT_A, sns=TOPIC_A))
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
    old_token = rows(pg_engine, event_id)["SNS"].claim_token

    second_entered, second_release = threading.Event(), threading.Event()
    second_wire = SnsWire({"MessageId": "reclaimed-result"})
    second_wire.on_publish = lambda: (second_entered.set(), second_release.wait(10))
    second = make_dispatcher(sessions, routes, sns=second_wire)
    expire_leases(pg_engine)
    second.process_one("SNS")
    make_due(pg_engine)
    second_outcome = []
    replacement = threading.Thread(target=lambda: second_outcome.append(second.process_one("SNS")))
    replacement.start()
    assert second_entered.wait(10)
    in_flight = rows(pg_engine, event_id)["SNS"]
    assert (in_flight.state, in_flight.attempts) == ("IN_FLIGHT", 2)
    assert in_flight.claim_token not in (None, old_token)

    # 교체 시도가 아직 전송 중일 때 이전 작업자의 늦은 결과는 이전 토큰이라 막힌다
    release.set()
    worker.join(10)
    assert outcome == [True]
    row = rows(pg_engine, event_id)["SNS"]
    assert (row.state, row.claim_token, row.attempts, row.receipt_ref, row.updated_at) == (
        "IN_FLIGHT", in_flight.claim_token, 2, None, in_flight.updated_at,
    )

    second_release.set()
    replacement.join(10)
    assert second_outcome == [True]
    row = rows(pg_engine, event_id)["SNS"]
    assert (row.state, row.receipt_ref, row.attempts) == ("SUCCEEDED", "reclaimed-result", 2)


@pytest.mark.parametrize(("response", "state", "error_code", "cooldown"), [
    ((200, {}, b"ok"), "SUCCEEDED", None, 0),
    ((429, {"Retry-After": "120"}, b""), "PENDING", "slack_http_429", 120),
], ids=["success", "429"])
def test_result_after_lease_expiry_is_kept_when_nobody_reclaimed_first(
    sessions, pg_engine, response, state, error_code, cooldown,
):
    """임대가 지났어도 회수 전에 돌아온 결과는 반영한다 — 회수·재전송으로 바꾸지 않는다."""
    routes = registry(entry(TENANT_A, ACCOUNT_A, slack="SLACK_A"))
    entered, release = threading.Event(), threading.Event()
    wire = SlackWire(response)
    wire.on_request = lambda: (entered.set(), release.wait(10))
    dispatcher = make_dispatcher(sessions, routes, slack=wire)
    activate(dispatcher, pg_engine)
    event_id = commit_event(sessions)
    dispatcher.discover()
    worker = threading.Thread(target=dispatcher.process_one, args=("SLACK",))
    worker.start()
    assert entered.wait(10)
    expire_leases(pg_engine)
    release.set()
    worker.join(10)
    row = rows(pg_engine, event_id)["SLACK"]
    assert (row.state, row.last_error_code, row.attempts, row.claim_token) == (
        state, error_code, 1, None,
    )
    spacing, waited = lane_waits(pg_engine)
    assert 0 < spacing <= 1  # 관문 보유자로서 결과 확정 뒤 1초 간격으로 연다
    assert cooldown - 1 < waited <= cooldown  # 429 대기는 남고 성공은 대기를 만들지 않는다
    assert len(wire.posts) == 1


def test_reclaim_and_late_result_race_is_decided_by_the_row_lock(sessions, pg_engine):
    """임대 만료 뒤 회수와 늦은 결과가 겹치면 행 잠금을 먼저 잡은 쪽 하나만 반영된다."""
    routes = registry(entry(TENANT_A, ACCOUNT_A, sns=TOPIC_A))
    entered, release = threading.Event(), threading.Event()
    wire = SnsWire({"MessageId": "late-result"})
    wire.on_publish = lambda: (entered.set(), release.wait(10))
    first = make_dispatcher(sessions, routes, sns=wire)
    activate(first, pg_engine)
    reclaimed = commit_event(sessions)
    first.discover()
    worker = threading.Thread(target=first.process_one, args=("SNS",))
    worker.start()
    assert entered.wait(10)
    expire_leases(pg_engine)
    observer = create_engine(pg_engine.url)

    def lock_waiters():
        with observer.connect() as connection:
            return connection.execute(text(
                "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database()"
                " AND wait_event_type = 'Lock'"
            )).scalar_one()

    # 회수가 먼저 잠근다(_recover와 같은 저장소 호출을 commit 전에 멈춤). 늦은 결과는 잠금을
    # 기다렸다가 회수 commit 뒤 IN_FLIGHT·토큰 조건을 다시 평가해 막힌다.
    with sessions() as reclaimer:
        [expired] = deliveries_repo.lock_expired(reclaimer, "SNS", 10)
        release.set()
        assert _wait_for(lambda: lock_waiters() == 1)
        assert deliveries_repo.recover_expired(
            reclaimer, expired.delivery_id, deliveries_repo.retry("UNKNOWN", "lease_expired", 60),
        )
        reclaimer.commit()
    worker.join(10)
    observer.dispose()
    row = rows(pg_engine, reclaimed)["SNS"]
    assert (row.state, row.last_error_code, row.receipt_ref, row.claim_token, row.attempts) == (
        "PENDING", "lease_expired", None, None, 1,
    )

    # 결과가 먼저 잠근다. 회수는 SKIP LOCKED로 건너뛰고, 그 뒤에는 닫을 IN_FLIGHT가 없다.
    settled_first = commit_event(sessions)
    first.discover()
    delivery_id, token = rows(pg_engine, settled_first)["SNS"].delivery_id, str(uuid.uuid4())
    with sessions() as db:
        deliveries_repo.start_attempt(db, delivery_id, token, 30)
        db.commit()
    expire_leases(pg_engine)
    with sessions() as settler, sessions() as reclaimer:
        assert deliveries_repo.settle(
            settler, delivery_id, token, deliveries_repo.succeeded("SNS_ACCEPTED", "late-result"),
        )
        assert deliveries_repo.lock_expired(reclaimer, "SNS", 10) == []
        settler.commit()
        assert not deliveries_repo.recover_expired(
            reclaimer, delivery_id, deliveries_repo.retry("UNKNOWN", "lease_expired", 60),
        )
        reclaimer.commit()
    row = rows(pg_engine, settled_first)["SNS"]
    assert (row.state, row.receipt_ref, row.attempts) == ("SUCCEEDED", "late-result", 1)
    assert len(wire.publishes) == 1


def test_terminal_rows_are_never_reset_or_resent(sessions, pg_engine):
    routes = registry(entry(TENANT_A, ACCOUNT_A, slack="SLACK_A"))
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
    make_dispatcher(sessions, registry(entry(TENANT_A, ACCOUNT_A, slack="SLACK_A2")),
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
    both = registry(entry(TENANT_A, ACCOUNT_A, slack="SLACK_A", sns=TOPIC_A))
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
        sessions, registry(entry(TENANT_A, ACCOUNT_A, slack="SLACK_A")), slack=wire,
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
    routes = registry(entry(TENANT_A, ACCOUNT_A, sns=TOPIC_A))
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
    routes = registry(entry(TENANT_A, ACCOUNT_A, sns=TOPIC_A))
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
    routes = registry(entry(TENANT_A, ACCOUNT_A, sns=TOPIC_A))
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
        entry(TENANT_A, ACCOUNT_A, slack="SLACK_A", sns=TOPIC_A),
        entry(TENANT_B, ACCOUNT_B, slack="SLACK_B"),
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
        entry(TENANT_A, ACCOUNT_A, slack="SLACK_A"),
        entry(TENANT_B, ACCOUNT_B, slack="SLACK_B"),
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

    routes = registry(entry(TENANT_A, ACCOUNT_A, slack="SLACK_A", sns=TOPIC_A))
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
    routes = registry(entry(TENANT_A, ACCOUNT_A, slack="SLACK_A", sns=TOPIC_A))

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
        sessions, registry(entry(TENANT_A, ACCOUNT_A, slack="SLACK_A")), slack=wire,
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

    routes = registry(entry(TENANT_A, ACCOUNT_A, slack="SLACK_A"))
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
        sessions, registry(entry(TENANT_A, ACCOUNT_A, slack="SLACK_A")), slack=SlackWire(),
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
        "tenant_id": TENANT_A, "aws_account_id": ACCOUNT_A,
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


# --- 권위 Tenant 바인딩(#506): 기동 검증·발견·claim·최초 활성화 전환 -----------------------------


@pytest.mark.parametrize("kind", list(BROKEN_TENANT))
def test_startup_rejects_missing_or_mismatched_tenant_before_any_write(
    sessions, pg_engine, kind, caplog,
):
    caplog.set_level("ERROR")
    break_tenant(pg_engine, kind, TENANT_B)  # 첫 항목(A)은 유효해도 하나라도 틀리면 전부 거부한다
    dispatcher = make_dispatcher(
        sessions,
        registry(
            entry(TENANT_A, ACCOUNT_A, slack="SLACK_A"),
            entry(TENANT_B, ACCOUNT_B, slack="SLACK_B"),
        ),
        slack=SlackWire(),
    )
    before = snapshot(pg_engine)
    assert before == {"notification_routes": [], "notification_lanes": [], "notification_deliveries": []}
    refuse(dispatcher, BROKEN_TENANT[kind])
    assert snapshot(pg_engine) == before  # 라우트·관문 어느 쪽도 쓰지 않는다
    logged = [r for r in caplog.records if r.getMessage() == "notification_tenant_binding_invalid"]
    assert [r.reason for r in logged] == [BROKEN_TENANT[kind]]
    for secret in (TENANT_A, TENANT_B, ACCOUNT_A, ACCOUNT_B, WEBHOOK_A, WEBHOOK_B):
        assert secret not in caplog.text


def test_startup_route_check_runs_before_the_legacy_transition_check(sessions, pg_engine):
    delete_tenant(pg_engine, TENANT_A)
    put_delivery(
        pg_engine, commit_event(sessions), "SNS", "PENDING", tenant_id="customer-a",
    )
    dispatcher = make_dispatcher(
        sessions, registry(entry(TENANT_A, ACCOUNT_A, sns=TOPIC_A)), sns=SnsWire(),
    )
    before = snapshot(pg_engine)
    refuse(dispatcher, "TENANT_NOT_FOUND")
    assert snapshot(pg_engine) == before


LEGACY_TENANTS = {
    "slug": "customer-a",
    "uuid-without-hyphens": TENANT_MISSING.replace("-", ""),
    "uppercase-uuid": "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA",
    "uuid-of-other-account": TENANT_B,  # Tenant B는 있지만 계정이 ACCOUNT_B라 ACCOUNT_A 행과 어긋난다
}


@pytest.mark.parametrize("state", ["PENDING", "IN_FLIGHT"])
@pytest.mark.parametrize("legacy", list(LEGACY_TENANTS))
def test_legacy_unfinished_delivery_refuses_startup_and_changes_nothing(
    sessions, pg_engine, state, legacy,
):
    first = make_dispatcher(
        sessions, registry(entry(TENANT_A, ACCOUNT_A, slack="SLACK_A", sns=TOPIC_A)),
        slack=SlackWire(), sns=SnsWire(),
    )
    activate(first, pg_engine)
    event_id = commit_event(sessions)
    put_delivery(pg_engine, event_id, "SNS", state, tenant_id=LEGACY_TENANTS[legacy])
    before = snapshot(pg_engine)
    old_route = route_row(pg_engine, ACCOUNT_A, "SLACK")

    # 설정이 바뀌어 있어도(수신처 변경·채널 제거) 동기화 전에 거부한다
    changed = make_dispatcher(
        sessions, registry(entry(TENANT_A, ACCOUNT_A, slack="SLACK_A2")), slack=SlackWire(),
    )
    refuse(changed, TRANSITION_REQUIRED)
    assert snapshot(pg_engine) == before
    kept = route_row(pg_engine, ACCOUNT_A, "SLACK")
    assert (kept.route_epoch, kept.activated_at) == (old_route.route_epoch, old_route.activated_at)
    assert route_row(pg_engine, ACCOUNT_A, "SNS") is not None


def test_first_enable_with_legacy_unfinished_delivery_writes_no_routes_or_lanes(
    sessions, pg_engine,
):
    put_delivery(pg_engine, commit_event(sessions), "SNS", "PENDING", tenant_id="customer-a")
    before = snapshot(pg_engine)
    assert before["notification_routes"] == [] and before["notification_lanes"] == []
    dispatcher = make_dispatcher(
        sessions, registry(entry(TENANT_A, ACCOUNT_A, slack="SLACK_A", sns=TOPIC_A)),
        slack=SlackWire(), sns=SnsWire(),
    )
    refuse(dispatcher, TRANSITION_REQUIRED)
    assert snapshot(pg_engine) == before


def test_legacy_in_flight_can_late_settle_after_refusal_but_is_fenced_after_reclaim(
    sessions, pg_engine,
):
    wire = SnsWire()
    dispatcher = make_dispatcher(
        sessions, registry(entry(TENANT_A, ACCOUNT_A, sns=TOPIC_A)), sns=wire,
    )
    settled_event = commit_event(sessions)
    put_delivery(pg_engine, settled_event, "SNS", "IN_FLIGHT", tenant_id="customer-a")
    refuse(dispatcher, TRANSITION_REQUIRED)
    live = rows(pg_engine, settled_event)["SNS"]
    assert live.state == "IN_FLIGHT"
    with sessions() as db:
        assert deliveries_repo.settle(
            db, live.delivery_id, live.claim_token,
            deliveries_repo.succeeded("SNS_ACCEPTED", "late-legacy"),
        ) is True
        db.commit()
    done = rows(pg_engine, settled_event)["SNS"]
    assert (done.state, done.receipt_ref, done.claim_token) == ("SUCCEEDED", "late-legacy", None)

    reclaimed_event = commit_event(sessions)
    put_delivery(pg_engine, reclaimed_event, "SNS", "IN_FLIGHT", tenant_id="customer-a")
    stale = rows(pg_engine, reclaimed_event)["SNS"]
    refuse(dispatcher, TRANSITION_REQUIRED)
    expire_leases(pg_engine)
    with sessions() as db:  # 이전 시도의 회수를 저장소로 직접 나타낸다(거부된 dispatcher는 돌리지 않는다)
        assert deliveries_repo.recover_expired(
            db, stale.delivery_id, deliveries_repo.retry("UNKNOWN", "lease_expired", 0),
        ) is True
        db.commit()
    after_reclaim = rows(pg_engine, reclaimed_event)["SNS"]
    assert (after_reclaim.state, after_reclaim.claim_token) == ("PENDING", None)
    with sessions() as db:
        assert deliveries_repo.settle(
            db, stale.delivery_id, stale.claim_token,
            deliveries_repo.succeeded("SNS_ACCEPTED", "stale"),
        ) is False
        db.commit()
    row = rows(pg_engine, reclaimed_event)["SNS"]
    assert (row.state, row.receipt_ref, row.claim_token) == ("PENDING", None, None)
    assert wire.publishes == []


def test_terminal_legacy_and_null_snapshot_history_is_untouched_and_not_replayed(
    sessions, pg_engine,
):
    legacy = {"tenant_id": "customer-a"}
    null_snapshot = {
        "tenant_id": None, "aws_account_id": None, "route_fingerprint": None, "route_epoch": None,
    }
    history = [
        ("SUCCEEDED", legacy), ("FAILED", legacy), ("BLOCKED", legacy), ("BLOCKED", null_snapshot),
    ]
    events = []
    for state, over in history:
        event_id = commit_event(sessions)
        events.append(event_id)
        for channel in ("SLACK", "SNS"):
            put_delivery(pg_engine, event_id, channel, state, **over)
    before = snapshot(pg_engine)["notification_deliveries"]
    slack, sns = SlackWire(), SnsWire()
    dispatcher = make_dispatcher(
        sessions, registry(entry(TENANT_A, ACCOUNT_A, slack="SLACK_A", sns=TOPIC_A)),
        slack=slack, sns=sns,
    )
    dispatcher.initialize()  # 종결 이력은 전환을 막지 않는다
    assert route_row(pg_engine, ACCOUNT_A, "SLACK") is not None
    assert dispatcher.discover() == 0
    assert dispatcher.process_one("SLACK") is False and dispatcher.process_one("SNS") is False
    assert snapshot(pg_engine)["notification_deliveries"] == before
    assert slack.posts == [] and sns.publishes == []


def test_canonical_unfinished_deliveries_with_matching_tenant_keep_initialize_idempotent(
    sessions, pg_engine,
):
    config = registry(entry(TENANT_A, ACCOUNT_A, slack="SLACK_A", sns=TOPIC_A))
    first = make_dispatcher(sessions, config, slack=SlackWire(), sns=SnsWire())
    activate(first, pg_engine)
    pending = commit_event(sessions)
    first.discover()
    route = route_row(pg_engine, ACCOUNT_A, "SNS")
    in_flight = commit_event(sessions)
    put_delivery(
        pg_engine, in_flight, "SNS", "IN_FLIGHT",
        route_fingerprint=route.fingerprint, route_epoch=route.route_epoch,
    )
    assert {r.state for r in rows(pg_engine, pending).values()} == {"PENDING"}
    before = snapshot(pg_engine)

    second = make_dispatcher(sessions, config, slack=SlackWire(), sns=SnsWire())
    second.initialize()
    second.initialize()
    assert snapshot(pg_engine) == before


def test_deleted_tenant_does_not_block_api_or_other_tenants(
    sessions, pg_engine, monkeypatch, capsys,
):
    import main
    from config import get_settings
    from fastapi.testclient import TestClient
    from notifications.runtime import NotificationRuntime

    config = registry(
        entry(TENANT_A, ACCOUNT_A, sns=TOPIC_A),
        entry(TENANT_B, ACCOUNT_B, sns=TOPIC_B),
    )
    activate(make_dispatcher(sessions, config, sns=SnsWire()), pg_engine)
    before = snapshot(pg_engine)["notification_routes"]
    delete_tenant(pg_engine, TENANT_A)
    wire = SnsWire()
    worker = make_dispatcher(sessions, config, sns=wire)
    runtime = NotificationRuntime(worker, worker._senders)
    monkeypatch.setattr(main, "build_notification_runtime", lambda _: runtime)
    monkeypatch.setenv("NOTIFICATION_ENABLED", "true")
    get_settings.cache_clear()
    try:
        with TestClient(main.create_app()) as client:
            assert client.get("/health").status_code == 200
            assert snapshot(pg_engine)["notification_routes"] == before
            deleted = commit_event(sessions, ACCOUNT_A)
            active = commit_event(sessions, ACCOUNT_B)
            assert _wait_for(lambda: rows(pg_engine, active).get("SNS") is not None
                             and rows(pg_engine, active)["SNS"].state == "SUCCEEDED")
            assert rows(pg_engine, deleted)["SNS"].state == "BLOCKED"
            assert rows(pg_engine, deleted)["SNS"].blocked_reason == "TENANT_NOT_FOUND"
        assert [p["topic"] for p in wire.publishes] == [TOPIC_B]
        assert not [t for t in threading.enumerate() if t.name.startswith("notification-")]
        # create_app의 setup_logging은 루트 핸들러를 교체한다. 실제 JSON 출력을 검증한다.
        output = capsys.readouterr().out
        logged = [
            record for line in output.splitlines()
            if (record := json.loads(line)).get("event") == "notification_tenant_binding_degraded"
        ]
        assert [(r["aws_account_id"], r["channel"], r["reason"]) for r in logged] == [
            (ACCOUNT_A, "SNS", "TENANT_NOT_FOUND"),
        ]
        for secret in (TENANT_A, TENANT_B, WEBHOOK_A, WEBHOOK_B):
            assert secret not in output
    finally:
        get_settings.cache_clear()


@pytest.mark.parametrize("changed", ["destination", "account"])
def test_existing_binding_does_not_allow_changed_invalid_configuration(
    sessions, pg_engine, changed,
):
    original = registry(entry(TENANT_A, ACCOUNT_A, slack="SLACK_A"))
    activate(make_dispatcher(sessions, original, slack=SlackWire()), pg_engine)
    if changed == "destination":
        delete_tenant(pg_engine, TENANT_A)
        config = registry(entry(TENANT_A, ACCOUNT_A, slack="SLACK_A2"))
        code = "TENANT_NOT_FOUND"
    else:
        move_tenant_account(pg_engine, TENANT_A, ACCOUNT_OTHER)
        config, code = original, "TENANT_ACCOUNT_MISMATCH"
    before = snapshot(pg_engine)
    refuse(make_dispatcher(sessions, config, slack=SlackWire()), code)
    assert snapshot(pg_engine) == before


@pytest.mark.parametrize("remove_route", [False, True])
def test_deleted_tenant_unfinished_rows_survive_restarts_without_resending(
    sessions, pg_engine, remove_route,
):
    config = registry(
        entry(TENANT_A, ACCOUNT_A, sns=TOPIC_A),
        entry(TENANT_B, ACCOUNT_B, sns=TOPIC_B),
    )
    first = make_dispatcher(sessions, config, sns=SnsWire())
    activate(first, pg_engine)
    pending = commit_event(sessions)
    first.discover("SNS")
    binding = route_row(pg_engine, ACCOUNT_A, "SNS")
    in_flight = commit_event(sessions)
    put_delivery(
        pg_engine, in_flight, "SNS", "IN_FLIGHT",
        route_fingerprint=binding.fingerprint, route_epoch=binding.route_epoch,
    )
    delete_tenant(pg_engine, TENANT_A)
    if remove_route:
        config = registry(entry(TENANT_B, ACCOUNT_B, sns=TOPIC_B))
    wire = SnsWire()
    before = snapshot(pg_engine)["notification_deliveries"]
    for _ in range(2):
        restarted = make_dispatcher(sessions, config, sns=wire)
        restarted.initialize()
        assert snapshot(pg_engine)["notification_deliveries"] == before
    if remove_route:
        assert route_row(pg_engine, ACCOUNT_A, "SNS") is None
    expire_leases(pg_engine)
    make_due(pg_engine)
    assert restarted.process_one("SNS") is True
    make_due(pg_engine)  # 회수된 시도의 backoff를 기다리지 않는다
    assert restarted.process_one("SNS") is True
    for event_id, attempts in ((pending, 0), (in_flight, 1)):
        row = rows(pg_engine, event_id)["SNS"]
        assert (row.state, row.attempts, row.claim_token) == ("BLOCKED", attempts, None)
        assert row.blocked_reason == ("OWNERSHIP_MISSING" if remove_route else "TENANT_NOT_FOUND")
    assert wire.publishes == []


@pytest.mark.parametrize("reclaim", [False, True])
def test_deleted_tenant_late_result_keeps_the_claim_token_fence(
    sessions, pg_engine, reclaim,
):
    config = registry(entry(TENANT_A, ACCOUNT_A, sns=TOPIC_A))
    first = make_dispatcher(sessions, config, sns=SnsWire())
    activate(first, pg_engine)
    event_id = commit_event(sessions)
    first.discover("SNS")
    claim = first._claim("SNS")
    assert claim is not None
    delete_tenant(pg_engine, TENANT_A)
    wire = SnsWire()
    restarted = make_dispatcher(sessions, config, sns=wire)
    restarted.initialize()
    if reclaim:
        expire_leases(pg_engine)
        restarted._recover("SNS")
    with sessions() as db:
        applied = deliveries_repo.settle(
            db, claim.delivery_id, claim.token,
            deliveries_repo.succeeded("SNS_ACCEPTED", "late-deleted"),
        )
        db.commit()
    assert applied is not reclaim
    if reclaim:
        make_due(pg_engine)
        assert restarted.process_one("SNS") is True
        assert rows(pg_engine, event_id)["SNS"].state == "BLOCKED"
    else:
        assert rows(pg_engine, event_id)["SNS"].state == "SUCCEEDED"
    assert wire.publishes == []


def test_missing_canonical_snapshot_does_not_prove_delivery_ownership(sessions, pg_engine):
    config = registry(entry(TENANT_A, ACCOUNT_A, sns=TOPIC_A))
    wire = SnsWire()
    event_id = commit_event(sessions)
    put_delivery(pg_engine, event_id, "SNS", "PENDING", tenant_id=TENANT_MISSING)
    restarted = make_dispatcher(sessions, config, sns=wire)
    restarted.initialize()  # UUID 형식만으로 발송 권한을 인정하는 것은 아니다
    make_due(pg_engine)
    assert restarted.process_one("SNS") is True
    row = rows(pg_engine, event_id)["SNS"]
    assert (row.state, row.blocked_reason, row.attempts) == ("BLOCKED", "ROUTE_CHANGED", 0)
    assert wire.publishes == []


def test_database_error_on_degraded_candidate_keeps_startup_fail_closed(sessions, pg_engine):
    config = registry(entry(TENANT_A, ACCOUNT_A, sns=TOPIC_A))
    activate(make_dispatcher(sessions, config, sns=SnsWire()), pg_engine)
    delete_tenant(pg_engine, TENANT_A)
    before = snapshot(pg_engine)
    with tenant_lookup_failing(pg_engine), pytest.raises(OperationalError):
        make_dispatcher(sessions, config, sns=SnsWire()).initialize()
    assert snapshot(pg_engine) == before


@pytest.mark.parametrize("kind", list(BROKEN_TENANT))
def test_discovery_blocks_events_whose_tenant_changed_after_startup(sessions, pg_engine, kind):
    wire = SnsWire()
    dispatcher = make_dispatcher(
        sessions,
        registry(entry(TENANT_A, ACCOUNT_A, sns=TOPIC_A), entry(TENANT_B, ACCOUNT_B, sns=TOPIC_B)),
        sns=wire,
    )
    activate(dispatcher, pg_engine)
    break_tenant(pg_engine, kind, TENANT_A)  # 기동 뒤 권위 매핑이 바뀐다
    event_a, event_b = commit_event(sessions, ACCOUNT_A), commit_event(sessions, ACCOUNT_B)
    dispatcher.discover("SNS")
    blocked = rows(pg_engine, event_a)["SNS"]
    assert (blocked.state, blocked.blocked_reason, blocked.tenant_id) == (
        "BLOCKED", BROKEN_TENANT[kind], None,
    )
    assert rows(pg_engine, event_b)["SNS"].tenant_id == TENANT_B
    while dispatcher.process_one("SNS"):
        pass
    assert [p["topic"] for p in wire.publishes] == [TOPIC_B]  # A 사건은 어느 수신처로도 가지 않는다
    assert event_a not in wire.publishes[0]["body"]


@pytest.mark.parametrize("kind", list(BROKEN_TENANT))
def test_claim_rechecks_tenant_after_discovery_and_never_calls_the_provider(
    sessions, pg_engine, kind,
):
    wire = SnsWire()
    dispatcher = make_dispatcher(
        sessions,
        registry(entry(TENANT_A, ACCOUNT_A, sns=TOPIC_A), entry(TENANT_B, ACCOUNT_B, sns=TOPIC_B)),
        sns=wire,
    )
    activate(dispatcher, pg_engine)
    event_a, event_b = commit_event(sessions, ACCOUNT_A), commit_event(sessions, ACCOUNT_B)
    dispatcher.discover("SNS")
    assert rows(pg_engine, event_a)["SNS"].state == "PENDING"
    break_tenant(pg_engine, kind, TENANT_A)  # discovery와 claim 사이에 바뀐다
    while dispatcher.process_one("SNS"):
        pass
    blocked = rows(pg_engine, event_a)["SNS"]
    assert (blocked.state, blocked.blocked_reason, blocked.attempts, blocked.claim_token) == (
        "BLOCKED", BROKEN_TENANT[kind], 0, None,
    )
    assert [p["topic"] for p in wire.publishes] == [TOPIC_B]
    assert rows(pg_engine, event_b)["SNS"].state == "SUCCEEDED"


def test_database_error_during_discovery_leaves_events_undiscovered_then_recovers(
    sessions, pg_engine,
):
    wire = SnsWire()
    dispatcher = make_dispatcher(
        sessions, registry(entry(TENANT_A, ACCOUNT_A, sns=TOPIC_A)), sns=wire,
    )
    activate(dispatcher, pg_engine)
    event_id = commit_event(sessions)
    with tenant_lookup_failing(pg_engine), pytest.raises(OperationalError):
        dispatcher.discover("SNS")
    assert rows(pg_engine, event_id) == {}  # 부분 행 없이 다음 poll이 다시 찾는다
    assert dispatcher.discover("SNS") == 1
    assert dispatcher.process_one("SNS") is True
    assert rows(pg_engine, event_id)["SNS"].state == "SUCCEEDED" and len(wire.publishes) == 1


def test_database_error_during_claim_keeps_state_and_attempts_then_recovers(sessions, pg_engine):
    wire = SnsWire(_throttled())
    dispatcher = make_dispatcher(
        sessions, registry(entry(TENANT_A, ACCOUNT_A, sns=TOPIC_A)), sns=wire,
    )
    activate(dispatcher, pg_engine)
    event_id = commit_event(sessions)
    dispatcher.discover("SNS")
    assert dispatcher.process_one("SNS") is True  # 일시 실패로 시도 1회가 쌓인 PENDING
    make_due(pg_engine)
    before = rows(pg_engine, event_id)["SNS"]
    assert (before.state, before.attempts) == ("PENDING", 1)

    with tenant_lookup_failing(pg_engine), pytest.raises(OperationalError):
        dispatcher.process_one("SNS")
    after = rows(pg_engine, event_id)["SNS"]
    assert (after.state, after.attempts, after.claim_token, after.lease_expires_at) == (
        "PENDING", 1, None, None,
    )
    assert len(wire.publishes) == 1  # 실패한 claim은 공급자를 부르지 않았다

    assert dispatcher.process_one("SNS") is True
    final = rows(pg_engine, event_id)["SNS"]
    assert (final.state, final.attempts) == ("SUCCEEDED", 2) and len(wire.publishes) == 2
