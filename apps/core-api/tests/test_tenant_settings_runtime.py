"""T-7 고객 설정 ↔ 알림 런타임 — 실제 PostgreSQL · 실제 dispatcher · 실제 설정 Workflow · SDK 대역.

네트워크 · AWS · 실제 비밀값 없음. Slack 은 HTTPSConnection 대역, SNS · Secrets Manager 는 botocore Stubber
또는 가짜 클라이언트다. 각 단계는 별도 세션 · commit 으로 돌린다(운영과 같은 트랜잭션 경계).

지키는 것: 재기동 없는 현재 설정 사용(발견 · claim) · 설정 변경 트랜잭션의 바인딩 무효화(대기 · 재시도는
옛 목적지로 가지 않음) · 새로 켠 채널 · 재기동 epoch 재사용 · routes 파일 가져오기(값 보존 · 해제 뒤 재가져오기와
대체 없음) · 이메일 세대 관측(확인 대기 → READY → 해지 감지)과 늦은 결과 차단 · 은퇴 자원 정리 · 비밀 원문 비노출.
"""

from __future__ import annotations

import threading
import time
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import boto3
import pytest
import tenant_settings
from auth import admin_service
from botocore.stub import Stubber
from config import get_auth_settings
from db import models
from db.repositories import notification_deliveries as deliveries_repo
from db.repositories import notification_events as events_repo
from db.repositories import tenant_settings as settings_repo
from notifications.channels import ManagedSlackSender, SlackSender, SnsSender
from notifications.contracts import EventSnapshot, Route
from notifications.dispatcher import DeliveryDispatcher
from notifications.routing import (
    CHANNEL_NOT_CONFIGURED,
    DESTINATION_INVALID,
    DESTINATION_SHARED,
    OWNERSHIP_MISSING,
    RouteRegistry,
)
from notifications.settings_reconciler import SettingsReconciler
from notifications.settings_source import (
    SettingsRouteSource,
    build_registry,
    destination_hash,
)
from schemas.api.incidents import RiskLevel
from schemas.api.settings import TenantSettingsUpdateRequest
from schemas.api.tenants import TenantVerificationResult
from services.aws.settings_notifications import (
    SettingsProviderError,
    SettingsResources,
    platform_region,
    secret_name,
    topic_name,
)
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.orm import Session, sessionmaker

PLATFORM_ACCOUNT = "999988887777"
WEBHOOK_A = "https://hooks.slack.com/services/TRUNA/BRUNA/runtimeTokenA"
WEBHOOK_A2 = "https://hooks.slack.com/services/TRUNA/BRUNA2/runtimeTokenA2"
WEBHOOK_B = "https://hooks.slack.com/services/TRUNB/BRUNB/runtimeTokenB"
LEGACY_TOPIC = "arn:aws:sns:ap-northeast-2:999999999999:customer-legacy"
ENV = {"SLACK_A": WEBHOOK_A}
BASE_URL = "https://console.vigilantis.example"
ADDRESS = "ops@example.com"


# --- 대역 -------------------------------------------------------------------------


class SlackWire:
    def __init__(self):
        self.posts = []

    def __call__(self, host, port, timeout):
        wire = self

        class Connection:
            sock = SimpleNamespace(settimeout=lambda _s: None)

            def connect(self):
                return None

            def request(self, method, path, body, headers):
                wire.posts.append(f"https://{host}{path}")

            def getresponse(self):
                return SimpleNamespace(status=200, getheader=lambda *_: None, read=lambda _n=-1: b"ok")

            def close(self):
                return None

        return Connection()


class SnsWire:
    def __init__(self):
        self.publishes = []

    def __call__(self, region, **_timeouts):
        return self

    def publish(self, TopicArn, Subject, Message):
        self.publishes.append(TopicArn)
        return {"MessageId": f"msg-{uuid.uuid4().hex}"}


class FakeSecrets:
    """Secrets Manager 대역 — 만든 값은 여기만 있고, 읽기는 정확한 (ARN, VersionId) 로만 된다."""

    def __init__(self):
        self.values = {}
        self.reads = []
        self.deleted = []

    def create_secret(self, *, Name, SecretString, ClientRequestToken, Description):
        arn = f"arn:aws:secretsmanager:{platform_region()}:{PLATFORM_ACCOUNT}:secret:{Name}-AbC123"
        version = str(uuid.uuid4())
        self.values[(arn, version)] = SecretString
        return {"ARN": arn, "Name": Name, "VersionId": version}

    def get_secret_value(self, *, SecretId, VersionId):
        self.reads.append((SecretId, VersionId))
        return {"SecretString": self.values[(SecretId, VersionId)]}

    def delete_secret(self, *, SecretId, ForceDeleteWithoutRecovery):
        self.deleted.append(SecretId)
        return {}


class Cloud:
    """SettingsResources 의 client_factory 대역 — secretsmanager 는 FakeSecrets, sns 는 Stubber 클라이언트."""

    def __init__(self):
        self.secrets = FakeSecrets()
        self.sns = boto3.session.Session().client(
            "sns", region_name=platform_region(),
            aws_access_key_id="testing", aws_secret_access_key="testing",
        )
        self.stub = Stubber(self.sns)
        self.stub.activate()

    def __call__(self, service, region, *, connect_timeout, read_timeout):
        return self.secrets if service == "secretsmanager" else self.sns


@pytest.fixture()
def world(pg_engine, committed_auth):
    sessions = sessionmaker(bind=pg_engine, autoflush=False, expire_on_commit=False)
    _wipe(pg_engine)
    a, b = committed_auth.make_tenant(), committed_auth.make_tenant()
    admin = committed_auth.make()
    actor = admin_service.Actor(
        token=admin.token,
        idle_seconds=get_auth_settings().AUTH_SESSION_IDLE_SECONDS,
        clock=lambda: datetime.now(UTC),
    )
    cloud = Cloud()
    resources = SettingsResources(cloud)
    with Session(pg_engine) as db:
        accounts = {t: db.get(models.Tenant, t).aws_account_id for t in (a, b)}
    yield SimpleNamespace(
        engine=pg_engine, sessions=sessions, a=a, b=b, accounts=accounts, actor=actor,
        cloud=cloud, resources=resources, slack=SlackWire(), sns=SnsWire(),
    )
    cloud.stub.assert_no_pending_responses()
    _wipe(pg_engine)


def _wipe(engine):
    with engine.begin() as connection:
        for model in (
            models.NotificationDelivery, models.NotificationRoute, models.NotificationLane,
            models.NotificationEvent, models.TenantSettingsRecord, models.TenantSettingsCleanup,
            models.NotificationLegacyRoute,
        ):
            connection.execute(delete(model))


def _legacy(w):
    return RouteRegistry.from_mapping({"routes": [{
        "tenant_id": w.a, "aws_account_id": w.accounts[w.a],
        "slack_webhook_env": "SLACK_A", "sns_topic_arn": LEGACY_TOPIC,
    }]}, ENV)


def _dispatcher(w, legacy=None, environ=ENV):
    senders = {
        "SLACK": ManagedSlackSender(SlackSender(connection_factory=w.slack), w.resources.read_slack_secret),
        "SNS": SnsSender(client_factory=w.sns),
    }
    return DeliveryDispatcher(
        w.sessions, SettingsRouteSource(legacy, environ=environ), senders,
        public_base_url=BASE_URL, poll_seconds=0.05, batch_size=20, max_attempts=3,
        backoff_seconds=1, max_backoff_seconds=60, lease_seconds=30,
    )


def _patch(w, tenant_id, body, *, enabled=True):
    with w.sessions() as db:
        return tenant_settings.update_settings(
            db, w.actor, tenant_id, TenantSettingsUpdateRequest.model_validate(body),
            resources=lambda: w.resources,
            options=tenant_settings.WriteOptions(
                notifications_enabled=enabled, retire_secret_delay_seconds=180,
            ),
        )


def _event(w, tenant_id) -> str:
    account = w.accounts[tenant_id]
    with w.sessions() as db:
        row = events_repo.add(
            db, incident_id=str(uuid.uuid4()), execution_id=None, related_execution_id=None,
            kind="HIGH_INCIDENT_CREATED", phase="CREATED", outcome=None,
            target_arn=f"arn:aws:ec2:ap-northeast-2:{account}:instance/i-0{uuid.uuid4().hex[:16]}",
            aws_account_id=account, region="ap-northeast-2", risk_level=RiskLevel.HIGH,
            trigger_source=None, runbook_id=None, execution_status=None, reason_code=None,
            occurred_at=datetime.now(UTC),
        )
        db.commit()
        return row.event_id


def _activate_now(w):
    """호스트 시계(사건)와 DB 시계(활성화) 차이를 없앤다 — 지금 있는 바인딩의 활성화를 1분 앞당긴다."""
    with w.engine.begin() as connection:
        connection.execute(update(models.NotificationRoute).values(
            activated_at=models.NotificationRoute.activated_at - timedelta(minutes=1),
        ))


def _open(w):
    with w.engine.begin() as connection:
        connection.execute(text(
            "UPDATE notification_lanes SET available_at = now(), cooldown_until = now()"
        ))
        connection.execute(text(
            "UPDATE notification_deliveries SET next_attempt_at = now() WHERE state = 'PENDING'"
        ))


def _deliveries(w, event_id):
    with Session(w.engine) as db:
        return {row.channel: row for row in deliveries_repo.list_by_event(db, event_id)}


def _bindings(w, tenant_id):
    with Session(w.engine) as db:
        return {
            row.channel: (row.fingerprint, row.route_epoch)
            for row in db.scalars(select(models.NotificationRoute).where(
                models.NotificationRoute.aws_account_id == w.accounts[tenant_id]))
        }


def _record(w, tenant_id):
    with Session(w.engine) as db:
        return settings_repo.get(db, tenant_id)


def _drain(dispatcher, channel):
    while dispatcher.process_one(channel):
        pass


# --- routes 파일 가져오기 · 재기동 ------------------------------------------------------


def test_legacy_routes_are_imported_once_keeping_values_and_bindings(world):
    w = world
    legacy = _legacy(w)
    old = DeliveryDispatcher(
        w.sessions, legacy, {}, public_base_url=BASE_URL, lease_seconds=30,
    )
    old.initialize()  # 기존(파일 원천) 배포가 만든 바인딩
    before = _bindings(w, w.a)

    dispatcher = _dispatcher(w, legacy)
    dispatcher.initialize()
    record = _record(w, w.a)
    assert (record.slack_source, record.slack_secret_ref) == ("LEGACY_ENV", "SLACK_A")
    assert (record.email_source, record.email_legacy_topic_arn) == ("LEGACY_TOPIC", LEGACY_TOPIC)
    assert _bindings(w, w.a) == before  # 같은 목적지 — epoch 유지(대기 중 발송이 막히지 않는다)
    _activate_now(w)

    event_id = _event(w, w.a)
    assert dispatcher.discover() == 2
    _drain(dispatcher, "SLACK")
    _drain(dispatcher, "SNS")
    assert {c: r.state for c, r in _deliveries(w, event_id).items()} == {
        "SLACK": "SUCCEEDED", "SNS": "SUCCEEDED",
    }
    assert w.slack.posts == [WEBHOOK_A] and w.sns.publishes == [LEGACY_TOPIC]

    # 재기동은 이미 정한 채널을 다시 가져오지 않고 바인딩도 그대로다
    updated = record.updated_at
    _dispatcher(w, legacy).initialize()
    assert _record(w, w.a).updated_at == updated and _bindings(w, w.a) == before


def test_explicit_clear_is_final_and_has_no_fallback(world):
    w = world
    legacy = _legacy(w)
    _dispatcher(w, legacy).initialize()
    with w.sessions() as db:
        imported = tenant_settings.read_settings(db, _admin_user(w, db), w.a)
    assert imported.notifications.legacy_sns_configured is True
    cleared = _patch(w, w.a, {"notifications": {"slack_webhook_url": None, "email_address": None}})
    assert cleared.notifications.slack_configured is False
    assert cleared.notifications.legacy_sns_configured is False
    assert _bindings(w, w.a) == {}

    dispatcher = _dispatcher(w, legacy)  # 재기동 — 파일에 라우트가 남아 있다
    dispatcher.initialize()
    record = _record(w, w.a)
    assert (record.slack_source, record.email_source) == ("CLEARED", "CLEARED")
    with w.sessions() as db:
        restarted = tenant_settings.read_settings(db, _admin_user(w, db), w.a)
    assert restarted.notifications.legacy_sns_configured is False  # 다시 가져오지 않는다
    event_id = _event(w, w.a)
    dispatcher.discover()
    assert {c: (r.state, r.blocked_reason) for c, r in _deliveries(w, event_id).items()} == {
        "SLACK": ("BLOCKED", CHANNEL_NOT_CONFIGURED), "SNS": ("BLOCKED", CHANNEL_NOT_CONFIGURED),
    }
    assert w.slack.posts == [] and w.sns.publishes == []
    assert w.cloud.secrets.deleted == []  # 외부 비밀 · 토픽은 지우지 않는다
    with Session(w.engine) as db:
        assert db.scalars(select(models.TenantSettingsCleanup)).all() == []


# --- 재기동 없는 반영 · 바인딩 무효화 -----------------------------------------------------


def test_settings_change_blocks_queued_delivery_and_new_events_use_the_new_secret(world, caplog):
    w = world
    caplog.set_level("DEBUG")
    dispatcher = _dispatcher(w, _legacy(w))
    dispatcher.initialize()
    _activate_now(w)
    queued = _event(w, w.a)
    dispatcher.discover("SLACK")
    assert _deliveries(w, queued)["SLACK"].state == "PENDING"

    _patch(w, w.a, {"notifications": {"slack_webhook_url": WEBHOOK_A2}})  # poll 전에 바뀐다
    _open(w)
    assert dispatcher.process_one("SLACK") is True
    stored = _deliveries(w, queued)["SLACK"]
    assert (stored.state, stored.blocked_reason) == ("BLOCKED", "ROUTE_CHANGED")
    assert w.slack.posts == []  # 옛 목적지로도, 새 목적지로도 보내지 않는다

    _activate_now(w)
    fresh = _event(w, w.a)
    dispatcher.discover("SLACK")
    _open(w)
    _drain(dispatcher, "SLACK")
    assert _deliveries(w, fresh)["SLACK"].state == "SUCCEEDED"
    record = _record(w, w.a)
    assert w.slack.posts == [WEBHOOK_A2]
    assert w.cloud.secrets.reads == [(record.slack_secret_ref, record.slack_secret_version)]
    for secret in (WEBHOOK_A, WEBHOOK_A2, "runtimeTokenA2"):
        assert secret not in caplog.text


def test_newly_enabled_channel_works_without_restart(world):
    w = world
    dispatcher = _dispatcher(w)
    dispatcher.initialize()
    before = _event(w, w.b)
    dispatcher.discover("SLACK")
    assert _deliveries(w, before)["SLACK"].blocked_reason == CHANNEL_NOT_CONFIGURED

    _patch(w, w.b, {"notifications": {"slack_webhook_url": WEBHOOK_B}})
    _activate_now(w)
    after = _event(w, w.b)
    dispatcher.discover("SLACK")
    _drain(dispatcher, "SLACK")
    assert _deliveries(w, after)["SLACK"].state == "SUCCEEDED"
    assert w.slack.posts == [WEBHOOK_B]


def test_restart_reuses_managed_bindings(world):
    w = world
    _dispatcher(w).initialize()
    _patch(w, w.b, {"notifications": {"slack_webhook_url": WEBHOOK_B}})
    binding = _bindings(w, w.b)
    _dispatcher(w).initialize()
    assert _bindings(w, w.b) == binding


# --- 이메일 관측 ---------------------------------------------------------------------


def _reconciler(w, resources=None, *, batch_size=1, lease_seconds=30):
    """기본은 한 번에 한 단계 — 각 단계의 결과를 따로 본다."""
    return SettingsReconciler(
        w.sessions, resources or w.resources, batch_size=batch_size, lease_seconds=lease_seconds,
        confirm_poll_seconds=60, ready_check_seconds=900, backoff_seconds=1, max_backoff_seconds=60,
    )


def _make_email_due(w):
    with w.engine.begin() as connection:
        connection.execute(text(
            "UPDATE tenant_settings SET email_next_check_at = now() WHERE email_next_check_at IS NOT NULL"
        ))


def _attributes(topic, pending):
    # 확인된 구독은 공식 예시처럼 PendingConfirmation 을 싣지 않는다 — 확인은 토픽 구독 목록으로만 판정한다
    values = {"TopicArn": topic, "Protocol": "email", "Endpoint": ADDRESS}
    if pending:
        values["PendingConfirmation"] = "true"
    return {"Attributes": values}


def test_email_reaches_ready_only_after_recipient_confirmation_and_unsubscribe_is_detected(world):
    w = world
    dispatcher = _dispatcher(w)
    dispatcher.initialize()
    _patch(w, w.b, {"notifications": {"email_address": ADDRESS}})
    record = _record(w, w.b)
    name = topic_name(w.b, record.email_generation)
    topic = f"arn:aws:sns:{platform_region()}:{PLATFORM_ACCOUNT}:{name}"
    subscription = f"{topic}:{uuid.uuid4()}"
    stub = w.cloud.stub
    stub.add_response("create_topic", {"TopicArn": topic}, {"Name": name})
    stub.add_response("subscribe", {"SubscriptionArn": subscription}, {
        "TopicArn": topic, "Protocol": "email", "Endpoint": ADDRESS, "ReturnSubscriptionArn": True,
    })
    stub.add_response("get_subscription_attributes", _attributes(topic, True), {"SubscriptionArn": subscription})
    stub.add_response("get_subscription_attributes", _attributes(topic, False), {"SubscriptionArn": subscription})
    stub.add_response("list_subscriptions_by_topic", {"Subscriptions": [{
        "SubscriptionArn": subscription, "Owner": PLATFORM_ACCOUNT, "Protocol": "email",
        "Endpoint": ADDRESS, "TopicArn": topic,
    }]}, {"TopicArn": topic})
    reconciler = _reconciler(w)

    statuses = []
    for _ in range(4):
        assert reconciler.reconcile_emails() == 1
        statuses.append(_record(w, w.b).email_status)
        if statuses[-1] != "READY":
            assert "SNS" not in _bindings(w, w.b)  # READY 전에는 라우트가 없다
        _make_email_due(w)
    assert statuses == ["SETUP_PENDING", "CONFIRMATION_PENDING", "CONFIRMATION_PENDING", "READY"]
    ready = _record(w, w.b)
    assert (ready.email_topic_arn, ready.email_subscription_arn) == (topic, subscription)
    assert "SNS" in _bindings(w, w.b)

    _activate_now(w)
    event_id = _event(w, w.b)
    dispatcher.discover("SNS")
    _drain(dispatcher, "SNS")
    assert _deliveries(w, event_id)["SNS"].state == "SUCCEEDED" and w.sns.publishes == [topic]

    # 수신자가 구독을 해지했다 — READY 를 내리고 같은 commit 에서 라우트를 뺀다
    stub.add_client_error("get_subscription_attributes", "NotFound", "gone")
    assert reconciler.reconcile_emails() == 1
    failed = _record(w, w.b)
    assert (failed.email_status, failed.email_failure_code) == ("SETUP_FAILED", "SUBSCRIPTION_REMOVED")
    assert "SNS" not in _bindings(w, w.b)
    later = _event(w, w.b)
    dispatcher.discover("SNS")
    assert _deliveries(w, later)["SNS"].blocked_reason == CHANNEL_NOT_CONFIGURED
    assert w.sns.publishes == [topic]
    with w.sessions() as db:
        shown = tenant_settings.read_settings(db, _admin_user(w, db), w.b)
    assert (shown.notifications.email_address, shown.notifications.email_status.value) == (
        ADDRESS, "SETUP_FAILED",
    )


def _admin_user(w, db):
    from auth import service

    return service.resolve_current_user(
        db, w.actor.token, now=datetime.now(UTC), idle_seconds=w.actor.idle_seconds, touch=False,
    )


class ChangingCloud:
    """CreateTopic 이 도는 동안(트랜잭션 밖) 사용자가 주소를 바꾼다 — 늦은 결과는 반영되면 안 된다."""

    def __init__(self, w):
        self.w = w
        self.created = []
        self.deleted = []

    def create_topic(self, *, region, name):
        self.created.append(name)
        _patch(self.w, self.w.b, {"notifications": {"email_address": "new@example.com"}})
        return f"arn:aws:sns:{region}:{PLATFORM_ACCOUNT}:{name}"

    def delete_topic(self, *, region, name):
        self.deleted.append(name)

    def close(self):
        return None


def test_stale_generation_result_is_fenced_and_its_topic_is_cleaned_after_the_lease(world):
    w = world
    _patch(w, w.b, {"notifications": {"email_address": ADDRESS}})
    old_generation = _record(w, w.b).email_generation
    cloud = ChangingCloud(w)
    reconciler = _reconciler(w, cloud)
    assert reconciler.reconcile_emails() == 1  # 주소 변경 PATCH 가 SDK 호출 중에 commit 됐다(잠금 없음)
    record = _record(w, w.b)
    assert record.email_address == "new@example.com" and record.email_generation != old_generation
    assert (record.email_status, record.email_topic_arn) == ("SETUP_PENDING", None)
    with Session(w.engine) as db:
        [cleanup] = db.scalars(select(models.TenantSettingsCleanup)).all()
        # 진행 중 임대가 끝난 뒤에야 지운다
        assert db.scalar(select(cleanup.not_before > func.now() + timedelta(seconds=29)))
    assert cleanup.resource_name == topic_name(w.b, old_generation)
    assert reconciler.run_cleanups() == 0
    with w.engine.begin() as connection:
        connection.execute(text("UPDATE tenant_settings_cleanups SET not_before = now()"))
    assert reconciler.run_cleanups() == 1
    assert cloud.deleted == [topic_name(w.b, old_generation)]


class FlakyCleanup:
    def __init__(self):
        self.calls = 0

    def delete_secret(self, *, region, name):
        self.calls += 1
        if self.calls == 1:
            raise SettingsProviderError("TRANSIENT", "secretsmanager_Throttling")

    def close(self):
        return None


def test_retired_secret_cleanup_retries_until_done(world):
    w = world
    _patch(w, w.b, {"notifications": {"slack_webhook_url": WEBHOOK_B}})
    _patch(w, w.b, {"notifications": {"slack_webhook_url": None}})
    with w.engine.begin() as connection:
        connection.execute(text("UPDATE tenant_settings_cleanups SET not_before = now()"))
    cloud = FlakyCleanup()
    reconciler = _reconciler(w, cloud)
    assert reconciler.run_cleanups() == 1
    with Session(w.engine) as db:
        [row] = db.scalars(select(models.TenantSettingsCleanup)).all()
        assert (row.attempts, row.last_error_code) == (1, "secretsmanager_Throttling")
    with w.engine.begin() as connection:
        connection.execute(text("UPDATE tenant_settings_cleanups SET not_before = now()"))
    assert reconciler.run_cleanups() == 1 and cloud.calls == 2
    with Session(w.engine) as db:
        assert db.scalars(select(models.TenantSettingsCleanup)).all() == []


# --- 라우트 해석 규칙(DB 없음) -----------------------------------------------------------


def _managed(tenant_id, url, staging=None):
    name = secret_name(tenant_id, staging or str(uuid.uuid4()))
    return SimpleNamespace(
        slack_source="MANAGED",
        slack_secret_ref=f"arn:aws:secretsmanager:ap-northeast-2:{PLATFORM_ACCOUNT}:secret:{name}-AbC123",
        slack_secret_version=str(uuid.uuid4()), slack_destination_hash=destination_hash(url),
        email_source=None,
    )


def _snapshot_event(account):
    now = datetime.now(UTC)
    return EventSnapshot(
        event_id="e", incident_id="i", execution_id=None, related_execution_id=None,
        target_arn=f"arn:aws:ec2:ap-northeast-2:{account}:instance/i-1", aws_account_id=account,
        region="ap-northeast-2", kind="HIGH_INCIDENT_CREATED", phase="CREATED", outcome=None,
        trigger_source=None, runbook_id=None, execution_status=None, reason_code=None,
        occurred_at=now, created_at=now,
    )


def test_shared_destination_and_foreign_secret_are_blocked():
    a, b, c = (str(uuid.uuid4()) for _ in range(3))
    rows = [
        (a, "111111111111", _managed(a, WEBHOOK_A)),
        (b, "222222222222", _managed(b, WEBHOOK_A)),  # 다른 비밀이지만 같은 URL
        (c, "333333333333", _managed(a, WEBHOOK_B)),  # 다른 테넌트 소유 비밀
    ]
    registry, candidates = build_registry(rows, {})
    assert registry.routes == ()
    assert registry.resolve(_snapshot_event("111111111111"), "SLACK").blocked_reason == DESTINATION_SHARED
    assert registry.resolve(_snapshot_event("222222222222"), "SLACK").blocked_reason == DESTINATION_SHARED
    assert registry.resolve(_snapshot_event("333333333333"), "SLACK").blocked_reason == DESTINATION_INVALID
    # 공유로 막힌 라우트도 바인딩 후보에는 남는다(공유가 풀리면 재기동 없이 다시 쓴다)
    assert {r.tenant_id for r in candidates} == {a, b}
    assert all(WEBHOOK_A not in repr(r) for r in candidates)


def _managed_route(url=WEBHOOK_A):
    tenant = str(uuid.uuid4())
    record = _managed(tenant, url)
    return Route(
        tenant, "111111111111", "SLACK", "f" * 64, record.slack_secret_ref, region="ap-northeast-2",
        secret_version=record.slack_secret_version, destination_hash=record.slack_destination_hash,
    )


@pytest.mark.parametrize(("failure", "expected"), [
    (SettingsProviderError("TRANSIENT", "secretsmanager_Throttling"), ("TRANSIENT", "slack_secret_lookup_failed")),
    (SettingsProviderError("NOT_FOUND", "secretsmanager_not_found"), ("PERMANENT", "slack_secret_unavailable")),
    (RuntimeError(WEBHOOK_A), ("TRANSIENT", "slack_secret_lookup_failed")),
    ("other-url", ("PERMANENT", "slack_secret_mismatch")),
])
def test_managed_slack_sender_never_sends_an_unverified_secret(failure, expected):
    wire = SlackWire()

    def read(_arn, _version):
        if isinstance(failure, str):
            return WEBHOOK_B
        raise failure

    sender = ManagedSlackSender(SlackSender(connection_factory=wire), read)
    result = sender.send(_snapshot_event("111111111111"), _managed_route(), BASE_URL)
    assert (result.failure_category, result.error_code) == expected
    assert wire.posts == []


def test_managed_slack_sender_caches_immutable_versions():
    wire, reads = SlackWire(), []
    route = _managed_route()

    def read(arn, version):
        reads.append((arn, version))
        return WEBHOOK_A

    sender = ManagedSlackSender(SlackSender(connection_factory=wire), read)
    for _ in range(2):
        assert sender.send(_snapshot_event("111111111111"), route, BASE_URL).succeeded
    assert reads == [(route.destination, route.secret_version)] and wire.posts == [WEBHOOK_A] * 2


def test_runtime_builds_without_routes_file_and_touches_no_db_or_aws(monkeypatch):
    import db.session as db_session
    from notifications.runtime import build_notification_runtime

    monkeypatch.setattr(db_session, "get_session_factory", lambda: (lambda: pytest.fail("DB opened")))
    runtime = build_notification_runtime(SimpleNamespace(
        NOTIFICATION_ENABLED=True, NOTIFICATION_ROUTES_FILE="",
        NOTIFICATION_PUBLIC_BASE_URL="https://example.com",
        NOTIFICATION_MAX_BACKOFF_SECONDS=300, NOTIFICATION_BACKOFF_SECONDS=2,
        NOTIFICATION_LEASE_SECONDS=120, NOTIFICATION_CONNECT_TIMEOUT_SECONDS=3,
        NOTIFICATION_READ_TIMEOUT_SECONDS=5, NOTIFICATION_POLL_SECONDS=1,
        NOTIFICATION_BATCH_SIZE=20, NOTIFICATION_MAX_ATTEMPTS=5,
    ))
    assert set(runtime.senders) == {"SLACK", "SNS"} and runtime.reconciler is not None
    assert runtime.reconciler._resources._clients == {}


def test_rejected_setup_fails_with_a_generic_code(world, caplog):
    w = world
    caplog.set_level("DEBUG")
    _patch(w, w.b, {"notifications": {"email_address": ADDRESS}})
    w.cloud.stub.add_client_error("create_topic", "AuthorizationError", f"denied for {ADDRESS}")
    assert _reconciler(w).reconcile_emails() == 1
    record = _record(w, w.b)
    assert (record.email_status, record.email_failure_code) == ("SETUP_FAILED", "PROVIDER_REJECTED")
    assert ADDRESS not in caplog.text


def test_transient_setup_error_keeps_pending_and_backs_off(world, caplog):
    w = world
    caplog.set_level("DEBUG")
    _patch(w, w.b, {"notifications": {"email_address": ADDRESS}})
    w.cloud.stub.add_client_error("create_topic", "Throttling", f"slow down {ADDRESS}")
    reconciler = _reconciler(w)
    assert reconciler.reconcile_emails() == 1
    record = _record(w, w.b)
    assert (record.email_status, record.email_attempts) == ("SETUP_PENDING", 1)
    assert reconciler.reconcile_emails() == 0  # backoff 중
    assert ADDRESS not in caplog.text


# --- 관측 · 정리 임대(SDK 직전 claim · 만료 결과 차단) ------------------------------------------


class StepCloud:
    """CreateTopic · 삭제 대역. 호출마다(트랜잭션 밖에서) on_call 을 부른다."""

    def __init__(self, on_call=None):
        self.on_call = on_call
        self.calls = []

    def create_topic(self, *, region, name):
        self.calls.append(name)
        if self.on_call is not None:
            self.on_call()
        return f"arn:aws:sns:{region}:{PLATFORM_ACCOUNT}:{name}"

    def delete_secret(self, *, region, name):
        self.calls.append(name)
        if self.on_call is not None:
            self.on_call()

    def close(self):
        return None


def _leased(w):
    with Session(w.engine) as db:
        return set(map(str, db.scalars(select(models.TenantSettingsRecord.tenant_id).where(
            models.TenantSettingsRecord.email_lease_token.is_not(None)))))


def test_batch_claims_each_step_just_before_its_sdk_call(world):
    w = world
    _patch(w, w.a, {"notifications": {"email_address": "a@example.com"}})
    _patch(w, w.b, {"notifications": {"email_address": ADDRESS}})
    leased_during_calls = []
    cloud = StepCloud(on_call=lambda: leased_during_calls.append(_leased(w)))
    assert _reconciler(w, cloud, batch_size=2).reconcile_emails() == 2
    # 묶음 전체를 미리 잡지 않는다 — 호출마다 그 호출의 테넌트 하나만 임대 중이다
    assert leased_during_calls == [{w.a}, {w.b}]
    assert _leased(w) == set()
    assert all(_record(w, t).email_topic_arn is not None for t in (w.a, w.b))


def test_result_after_its_own_lease_expired_is_not_applied_and_the_step_is_redone(world):
    w = world
    _patch(w, w.b, {"notifications": {"email_address": ADDRESS}})

    def expire():  # 토큰은 그대로(재claim 없음) 임대만 지난다
        with w.engine.begin() as connection:
            connection.execute(text(
                "UPDATE tenant_settings SET email_lease_expires_at = clock_timestamp() - interval '1 second'"
                " WHERE tenant_id = :t"
            ), {"t": w.b})

    cloud = StepCloud(on_call=expire)
    reconciler = _reconciler(w, cloud)
    assert reconciler.reconcile_emails() == 1
    stale = _record(w, w.b)
    assert (stale.email_status, stale.email_topic_arn) == ("SETUP_PENDING", None)
    assert stale.email_lease_token is not None  # 만료된 채 남은 같은 토큰이어도 반영하지 않았다

    cloud.on_call = None
    assert reconciler.reconcile_emails() == 1  # 만료 임대는 새 토큰으로 다시 잡혀 같은 단계를 한다
    fresh = _record(w, w.b)
    assert fresh.email_topic_arn is not None and fresh.email_lease_token is None
    assert cloud.calls == [topic_name(w.b, fresh.email_generation)] * 2


def test_lease_expiring_while_waiting_for_the_lock_is_judged_by_db_clock_after_the_wait(world):
    w = world
    _patch(w, w.b, {"notifications": {"email_address": ADDRESS}})
    ready, threads = threading.Event(), []

    def hold_config_lock():
        # 설정 잠금을 쥔 다른 트랜잭션이 1초 뒤 끝난다. 그 안에서 임대를 0.3초 뒤 만료로 줄인다
        with w.engine.connect() as connection:
            transaction = connection.begin()
            connection.execute(
                text("SELECT pg_advisory_xact_lock(:key)"), {"key": settings_repo._CONFIG_LOCK_KEY},
            )
            connection.execute(text(
                "UPDATE tenant_settings SET email_lease_expires_at ="
                " clock_timestamp() + interval '300 milliseconds' WHERE tenant_id = :t"
            ), {"t": w.b})
            ready.set()
            time.sleep(1.0)
            transaction.commit()

    def start_holder():
        thread = threading.Thread(target=hold_config_lock)
        thread.start()
        threads.append(thread)
        assert ready.wait(5)

    cloud = StepCloud(on_call=start_holder)
    try:
        # 반영 트랜잭션은 임대가 살아 있을 때 시작해 잠금을 기다린다 — 시작 시각(now())이 아니라 대기 뒤 시계로 판정
        assert _reconciler(w, cloud).reconcile_emails() == 1
    finally:
        for thread in threads:
            thread.join(10)
    assert _record(w, w.b).email_topic_arn is None


def test_cleanup_claims_one_item_just_before_its_delete(world):
    w = world
    with w.sessions() as db:
        for _ in range(2):
            settings_repo.queue_cleanup(
                db, tenant_id=w.b, resource_kind="SECRET", purpose="RETIRED",
                region=platform_region(), resource_name=secret_name(w.b, str(uuid.uuid4())),
            )
        db.commit()

    def attempts():
        with Session(w.engine) as db:
            return sorted(db.scalars(select(models.TenantSettingsCleanup.attempts)))

    observed = []
    cloud = StepCloud(on_call=lambda: observed.append(attempts()))
    assert _reconciler(w, cloud, batch_size=2).run_cleanups() == 2
    assert observed == [[0, 1], [1]]  # 두 번째 항목은 자기 삭제 직전까지 잡히지 않는다
    assert attempts() == []


# --- 지워진 고객의 파일 바인딩(재기동 호환) ------------------------------------------------


WEBHOOK_X = "https://hooks.slack.com/services/TRUNX/BRUNX/runtimeTokenX"


def test_deleted_legacy_tenant_survives_restarts_without_regaining_send_authority(world, committed_auth):
    w = world
    x = committed_auth.make_tenant()
    with Session(w.engine) as db:
        w.accounts[x] = db.get(models.Tenant, x).aws_account_id
    environ = {**ENV, "SLACK_X": WEBHOOK_X}
    legacy = RouteRegistry.from_mapping({"routes": [
        {"tenant_id": w.a, "aws_account_id": w.accounts[w.a], "slack_webhook_env": "SLACK_A"},
        {"tenant_id": x, "aws_account_id": w.accounts[x], "slack_webhook_env": "SLACK_X"},
    ]}, environ)
    # 파일 원천 배포가 두 고객을 바인딩했고 x 의 사건 하나가 대기 중이다
    old = DeliveryDispatcher(
        w.sessions, legacy, {"SLACK": SlackSender(connection_factory=w.slack)},
        public_base_url=BASE_URL, lease_seconds=30,
    )
    old.initialize()
    _activate_now(w)
    queued = _event(w, x)
    old.discover("SLACK")
    assert _deliveries(w, queued)["SLACK"].state == "PENDING"
    with w.engine.begin() as connection:
        connection.execute(delete(models.Tenant).where(models.Tenant.tenant_id == x))

    for _ in range(2):  # 업그레이드 첫 기동과 그다음 재기동 모두 API 를 멈추지 않는다
        dispatcher = _dispatcher(w, legacy, environ)
        dispatcher.initialize()
    assert "SLACK" not in _bindings(w, x)  # 바인딩 · 라우트는 되살리지 않는다(증거만 남는다)

    _activate_now(w)
    fresh_x, fresh_a = _event(w, x), _event(w, w.a)
    dispatcher.discover("SLACK")
    _open(w)
    _drain(dispatcher, "SLACK")
    for event_id in (queued, fresh_x):
        stored = _deliveries(w, event_id)["SLACK"]
        assert (stored.state, stored.blocked_reason) == ("BLOCKED", OWNERSHIP_MISSING)
    assert _deliveries(w, fresh_a)["SLACK"].state == "SUCCEEDED"
    assert w.slack.posts == [WEBHOOK_A]

    # 다른 고객이 그 계정을 이어받아 직접 설정해도 재기동이 멈추지 않고, 이어받은 고객의 라우트가 쓰인다
    from db.repositories import tenants as tenants_repo

    with w.sessions() as db:
        y = tenants_repo.create_tenant(
            db, name="takeover", aws_account_id=w.accounts[x], regions=["ap-northeast-2"],
            now=datetime.now(UTC),
        ).tenant_id
        # 설정 저장은 ACTIVE 만 받는다 — 연결 확인을 거친 정상 생애 주기로 맞춘다
        assert tenants_repo.record_verification(
            db, y, result=TenantVerificationResult.OK, reason_code=None, now=datetime.now(UTC),
        )
        db.commit()
    committed_auth._tenant_ids.append(y)  # world 정리(설정 행 삭제) 뒤 고객 행을 지운다
    w.accounts[y] = w.accounts[x]
    _patch(w, y, {"notifications": {"slack_webhook_url": WEBHOOK_B}})
    live = _bindings(w, y)["SLACK"]
    dispatcher = _dispatcher(w, legacy, environ)
    dispatcher.initialize()
    assert _bindings(w, y)["SLACK"] == live  # 증거가 살아 있는 라우트를 덮지 않는다
    _activate_now(w)
    taken_over = _event(w, y)
    dispatcher.discover("SLACK")
    _open(w)
    _drain(dispatcher, "SLACK")
    assert _deliveries(w, taken_over)["SLACK"].state == "SUCCEEDED"
    assert w.slack.posts == [WEBHOOK_A, WEBHOOK_B] and WEBHOOK_X not in w.slack.posts


def test_pending_legacy_tenant_is_deletable_through_the_workflow_and_restarts_without_delivery(world, committed_auth):
    from schemas.api.tenants import TenantStatus
    from tenant_admin import delete_tenant


    w = world
    p = committed_auth.make_tenant(status=TenantStatus.PENDING_CONNECTION)
    with Session(w.engine) as db:
        w.accounts[p] = db.get(models.Tenant, p).aws_account_id
    environ = {**ENV, "SLACK_P": WEBHOOK_X}
    legacy = RouteRegistry.from_mapping({"routes": [
        {"tenant_id": w.a, "aws_account_id": w.accounts[w.a], "slack_webhook_env": "SLACK_A"},
        {"tenant_id": p, "aws_account_id": w.accounts[p], "slack_webhook_env": "SLACK_P"},
    ]}, environ)
    _dispatcher(w, legacy, environ).initialize()  # 연결 전 고객의 파일 라우트도 설정 행으로 가져온다
    assert _record(w, p).slack_source == "LEGACY_ENV"
    bound = _bindings(w, p)
    assert "SLACK" in bound

    with w.sessions() as db:
        delete_tenant(db, w.actor, p)  # 가져온 설정 행이 지우기를 막지 않는다
    assert _record(w, p) is None
    assert _bindings(w, p) == bound  # 바인딩은 과거 증거로 남는다

    for _ in range(2):  # 같은 파일로 재기동해도 시작이 실패하지 않는다
        dispatcher = _dispatcher(w, legacy, environ)
        dispatcher.initialize()
    assert _record(w, p) is None
    _activate_now(w)
    fresh = _event(w, p)
    dispatcher.discover("SLACK")
    _open(w)
    _drain(dispatcher, "SLACK")
    stored = _deliveries(w, fresh).get("SLACK")
    assert stored is None or stored.state == "BLOCKED"
    assert WEBHOOK_X not in w.slack.posts
