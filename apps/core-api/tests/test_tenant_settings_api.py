"""GET/PATCH /api/v1/tenants/{tenant_id}/settings (T-7) — 실제 PostgreSQL · 실제 세션 · 실제 어댑터.

Secrets Manager 는 SettingsResources 에 꽂은 가짜 SDK 클라이언트다(네트워크 · AWS 없음).
지키는 것: 범위(전체 · 배정 · 자기 고객) · 역할(403 이 404 · 422 보다 먼저) · 잠금 안 재판정 · 정지 409 ·
PATCH 의미(생략 유지 · 전체 교체 · [] · null 거절 · 빈 PATCH 거절 · 채널 null 해제) · 이메일 세대 규칙 ·
Slack 비밀 준비 · 교체 · 정리와 원문 비노출 · 바인딩 무효화 · 알림 비활성 시 AWS 호출 없음.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

import pytest
import tenant_settings
from auth import sessions
from db import models
from db.repositories import auth as auth_repo
from db.repositories import tenant_settings as settings_repo
from routers.tenant_settings import get_settings_resources, get_write_options
from schemas.api.auth import PlatformRole, UserRole
from schemas.api.tenants import TenantStatus
from services.aws.settings_notifications import (
    SettingsResources,
    platform_region,
    secret_name,
    topic_name,
)
from services.rule_engine import default_protected_tags
from sqlalchemy import func, select, update

BASE = "/api/v1/tenants"
ACCOUNT = "999988887777"
WEBHOOK = "https://hooks.slack.com/services/TAPI/BAPI/ApiPrivateToken"
WEBHOOK_2 = "https://hooks.slack.com/services/TAPI/BAPI2/ApiPrivateToken2"
ADDRESS = "ops@example.com"
RULES = [{"key": "Env", "values": ["Prod", "live"]}, {"key": "tier", "values": ["gold"]}]


class FakeSecrets:
    """Secrets Manager 클라이언트 대역 — 받은 이름으로 ARN 을 만들고 값은 여기에만 남긴다."""

    def __init__(self):
        self.values: dict[str, tuple[str, str]] = {}
        self.calls: list[str] = []
        self.fail: Exception | None = None

    def create_secret(self, *, Name, SecretString, ClientRequestToken, Description):
        self.calls.append("create_secret")
        if self.fail is not None:
            raise self.fail
        assert 32 <= len(ClientRequestToken) <= 64
        arn = f"arn:aws:secretsmanager:{platform_region()}:{ACCOUNT}:secret:{Name}-AbC123"
        version = str(uuid.uuid4())
        self.values[arn] = (version, SecretString)
        return {"ARN": arn, "Name": Name, "VersionId": version}


@pytest.fixture()
def secrets(client_pg):
    fake = FakeSecrets()
    created = []

    def factory(service, region, *, connect_timeout, read_timeout):
        created.append(service)
        assert service == "secretsmanager" and region == platform_region()
        return fake

    resources = SettingsResources(factory)
    client_pg.app.dependency_overrides[get_settings_resources] = lambda: (lambda: resources)
    client_pg.app.dependency_overrides[get_write_options] = lambda: tenant_settings.WriteOptions(
        notifications_enabled=True, retire_secret_delay_seconds=180,
    )
    fake.created = created
    return fake


@pytest.fixture()
def notifications_disabled(client_pg, secrets):
    client_pg.app.dependency_overrides[get_write_options] = lambda: tenant_settings.WriteOptions(
        notifications_enabled=False, retire_secret_delay_seconds=180,
    )
    return secrets


def _url(tenant_id: str) -> str:
    return f"{BASE}/{tenant_id}/settings"


def _code(resp) -> str:
    return resp.json()["error"]["code"]


def _row(db, tenant_id):
    db.expire_all()
    return settings_repo.get(db, tenant_id)


def _cleanups(db):
    db.expire_all()
    return db.scalars(
        select(models.TenantSettingsCleanup).order_by(models.TenantSettingsCleanup.created_at)
    ).all()


def _bindings(db, tenant_id):
    db.expire_all()
    account = db.get(models.Tenant, tenant_id).aws_account_id
    return {
        row.channel: (row.fingerprint, row.route_epoch)
        for row in db.scalars(
            select(models.NotificationRoute).where(models.NotificationRoute.aws_account_id == account)
        )
    }


def _seed_settings(db, tenant_id, **values):
    record = settings_repo.lock_existing(db, tenant_id)
    if record is None:
        record = settings_repo.new_record(tenant_id, updated_at=datetime(2026, 1, 1, tzinfo=UTC))
        db.add(record)
    for name, value in values.items():
        setattr(record, name, value)
    db.commit()
    return record


def _ready_email(db, tenant_id, address=ADDRESS):
    generation = str(uuid.uuid4())
    region = platform_region()
    topic = f"arn:aws:sns:{region}:{ACCOUNT}:{topic_name(tenant_id, generation)}"
    _seed_settings(
        db, tenant_id, email_source="MANAGED", email_address=address, email_generation=generation,
        email_region=region, email_status="READY", email_topic_arn=topic,
        email_subscription_arn=f"{topic}:{uuid.uuid4()}", email_next_check_at=func.now(),
        email_checked_at=func.now(),
    )
    return generation, topic


def _assert_no_secret(resp, caplog, *values):
    for value in values:
        assert value not in resp.text
        assert value not in caplog.text
        for token in value.rsplit("/", 1)[-1:]:
            assert token not in resp.text and token not in caplog.text


# ----- 조회 · 범위 ------------------------------------------------------------------


def test_get_returns_effective_defaults_without_creating_a_row(client_pg, db, make_tenant):
    tenant_id = make_tenant()
    resp = client_pg.get(_url(tenant_id.upper()))
    assert resp.status_code == 200
    body = resp.json()
    assert body["tenant_id"] == tenant_id
    assert body["protected_tags"] == default_protected_tags()
    assert body["notifications"] == {
        "slack_configured": False, "legacy_sns_configured": False,
        "email_address": None, "email_status": "UNSET",
    }
    created = db.get(models.Tenant, tenant_id).created_at
    assert body["updated_at"] == created.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert _row(db, tenant_id) is None


@pytest.mark.parametrize("kind", ["central", "manager", "tenant-ADMIN", "tenant-OPERATOR", "tenant-VIEWER"])
def test_readers_in_scope_get_settings(client_pg, login_as, make_tenant, kind):
    tenant_id = make_tenant()
    if kind == "central":
        login_as(UserRole.ADMIN, platform_role=PlatformRole.CENTRAL_ADMIN)
    elif kind == "manager":
        login_as(UserRole.ADMIN, platform_role=PlatformRole.TENANT_MANAGER, assigned_tenant_ids=(tenant_id,))
    else:
        login_as(UserRole[kind.split("-")[1]], tenant_id=tenant_id)
    assert client_pg.get(_url(tenant_id)).status_code == 200


@pytest.mark.parametrize("kind", ["manager-unassigned", "manager-other", "tenant-other"])
def test_out_of_scope_is_the_same_404_as_missing(client_pg, login_as, make_tenant, kind):
    tenant_id, other = make_tenant(), make_tenant()
    if kind == "manager-unassigned":
        login_as(UserRole.ADMIN, platform_role=PlatformRole.TENANT_MANAGER)
    elif kind == "manager-other":
        login_as(UserRole.ADMIN, platform_role=PlatformRole.TENANT_MANAGER, assigned_tenant_ids=(other,))
    else:
        login_as(UserRole.ADMIN, tenant_id=other)
    for target in (tenant_id, str(uuid.uuid4()), "not-a-uuid"):
        resp = client_pg.get(_url(target))
        assert resp.status_code == 404 and _code(resp) == "TENANT_NOT_FOUND"
        resp = client_pg.patch(_url(target), json={"protected_tags": []})
        assert resp.status_code == 404 and _code(resp) == "TENANT_NOT_FOUND"


def test_suspended_customer_can_read_but_nobody_writes(client_pg, db, login_as, make_tenant, secrets):
    tenant_id = make_tenant(TenantStatus.SUSPENDED)
    for login in (
        lambda: login_as(UserRole.ADMIN, tenant_id=tenant_id),
        lambda: login_as(UserRole.ADMIN, platform_role=PlatformRole.CENTRAL_ADMIN),
    ):
        login()
        assert client_pg.get(_url(tenant_id)).status_code == 200
        resp = client_pg.patch(_url(tenant_id), json={"notifications": {"slack_webhook_url": WEBHOOK}})
        assert resp.status_code == 409 and _code(resp) == "TENANT_SUSPENDED"
    assert secrets.calls == [] and _row(db, tenant_id) is None


def test_pending_tenant_settings_write_is_409_and_tenant_stays_deletable(client_pg, db, make_tenant):
    tenant_id = make_tenant(TenantStatus.PENDING_CONNECTION)
    assert client_pg.get(_url(tenant_id)).status_code == 200

    resp = client_pg.patch(_url(tenant_id), json={"protected_tags": []})

    assert resp.status_code == 409 and _code(resp) == "TENANT_STATE_CONFLICT"
    assert _row(db, tenant_id) is None
    assert client_pg.delete(f"{BASE}/{tenant_id}").status_code == 204


# ----- 쓰기 역할 · 잠금 안 재판정 ------------------------------------------------------


@pytest.mark.parametrize("role", [UserRole.OPERATOR, UserRole.VIEWER])
def test_customer_operator_and_viewer_are_forbidden_before_validation(
    client_pg, db, login_as, make_tenant, secrets, role,
):
    tenant_id = make_tenant()
    login_as(role, tenant_id=tenant_id)
    for body in ({"protected_tags": []}, {}, {"protected_tags": None}):
        resp = client_pg.patch(_url(tenant_id), json=body)
        assert resp.status_code == 403 and _code(resp) == "AUTH_FORBIDDEN"
    assert _row(db, tenant_id) is None and secrets.calls == []


@pytest.mark.parametrize("kind", ["customer-admin", "manager", "central"])
def test_writers_in_scope_save(client_pg, db, login_as, make_tenant, secrets, kind):
    tenant_id = make_tenant()
    if kind == "customer-admin":
        login_as(UserRole.ADMIN, tenant_id=tenant_id)
    elif kind == "manager":
        login_as(UserRole.ADMIN, platform_role=PlatformRole.TENANT_MANAGER, assigned_tenant_ids=(tenant_id,))
    resp = client_pg.patch(_url(tenant_id), json={"protected_tags": RULES})
    assert resp.status_code == 200 and resp.json()["protected_tags"] == RULES
    assert _row(db, tenant_id).protected_tags == RULES


def _inside_lock(monkeypatch, change):
    original = auth_repo.lock_users_table

    def wrapped(session):
        change(session)
        original(session)

    monkeypatch.setattr(auth_repo, "lock_users_table", wrapped)


@pytest.mark.parametrize("change,expected", [
    ("session-revoked", (401, "AUTH_REQUIRED")),
    ("assignment-revoked", (404, "TENANT_NOT_FOUND")),
    ("demoted-to-viewer", (403, "AUTH_FORBIDDEN")),
    ("tenant-suspended", (409, "TENANT_SUSPENDED")),
])
def test_revalidation_inside_the_lock_writes_nothing_and_releases_the_staged_secret(
    client_pg, db, login_as, make_tenant, secrets, monkeypatch, change, expected,
):
    tenant_id = make_tenant()
    if change == "assignment-revoked":
        actor = login_as(UserRole.ADMIN, platform_role=PlatformRole.TENANT_MANAGER, assigned_tenant_ids=(tenant_id,))
    else:
        actor = login_as(UserRole.ADMIN, tenant_id=tenant_id)
    now = datetime.now(UTC)
    changes = {
        "session-revoked": lambda s: sessions.revoke_session(s, actor.token, now=now),
        "assignment-revoked": lambda s: auth_repo.replace_assigned_tenants(
            s, user_id=actor.user_id, tenant_ids=(), now=now),
        "demoted-to-viewer": lambda s: s.execute(
            update(models.AppUser).where(models.AppUser.user_id == actor.user_id).values(role=UserRole.VIEWER)),
        "tenant-suspended": lambda s: s.execute(
            update(models.Tenant).where(models.Tenant.tenant_id == tenant_id).values(
                status=TenantStatus.SUSPENDED, suspended_at=now, suspended_reason="동시 정지")),
    }
    _inside_lock(monkeypatch, changes[change])
    before_row = _row(db, tenant_id)
    resp = client_pg.patch(_url(tenant_id), json={
        "protected_tags": [], "notifications": {"slack_webhook_url": WEBHOOK, "email_address": ADDRESS},
    })
    assert (resp.status_code, _code(resp)) == expected
    assert before_row is None and _row(db, tenant_id) is None
    # 비밀은 잠금 전에 만들었다 — 저장하지 않았으니 바로 정리 차례다
    [staged] = _cleanups(db)
    assert (staged.purpose, staged.resource_kind, staged.attempts) == ("STAGING", "SECRET", 0)
    assert db.scalar(select(staged.not_before <= func.now()))
    assert staged.resource_name.startswith(secret_name(tenant_id, str(uuid.uuid4())).rsplit("-", 1)[0])
    assert secrets.calls == ["create_secret"]


# ----- 보호 태그 의미 -----------------------------------------------------------------


def test_protected_tags_omit_replace_clear_and_null(client_pg, db, make_tenant, secrets):
    tenant_id = make_tenant()
    saved = client_pg.patch(_url(tenant_id), json={"protected_tags": RULES}).json()
    assert saved["protected_tags"] == RULES
    # 같은 값 재저장은 설정 변경이 아니다(updated_at 유지)
    again = client_pg.patch(_url(tenant_id), json={"protected_tags": RULES}).json()
    assert again["updated_at"] == saved["updated_at"]
    # 생략은 유지
    kept = client_pg.patch(_url(tenant_id), json={"notifications": {"email_address": ADDRESS}}).json()
    assert kept["protected_tags"] == RULES
    # [] 는 태그 보호 없음(기본 정책으로 돌아가지 않는다)
    cleared = client_pg.patch(_url(tenant_id), json={"protected_tags": []}).json()
    assert cleared["protected_tags"] == [] and _row(db, tenant_id).protected_tags == []
    assert settings_repo.load_protected_tag_policies(db, [tenant_id]) == {tenant_id: []}
    for body in ({"protected_tags": None}, {}, {"notifications": {}}, {"notifications": None},
                 {"protected_tags": [{"key": "env", "values": ["a"]}, {"key": "ENV", "values": ["b"]}]}):
        resp = client_pg.patch(_url(tenant_id), json=body)
        assert resp.status_code == 422 and _code(resp) == "REQUEST_VALIDATION_FAILED"
    assert _row(db, tenant_id).protected_tags == []


# ----- Slack -----------------------------------------------------------------------


def test_slack_secret_is_staged_saved_by_reference_and_never_echoed(
    client_pg, db, make_tenant, secrets, caplog,
):
    caplog.set_level(logging.DEBUG)
    tenant_id = make_tenant()
    resp = client_pg.patch(_url(tenant_id), json={"notifications": {"slack_webhook_url": WEBHOOK}})
    assert resp.status_code == 200
    assert resp.json()["notifications"]["slack_configured"] is True
    [(arn, (version, value))] = secrets.values.items()
    assert value == WEBHOOK
    row = _row(db, tenant_id)
    assert (row.slack_source, row.slack_secret_ref, row.slack_secret_version) == ("MANAGED", arn, version)
    assert WEBHOOK not in repr({c.key: getattr(row, c.key) for c in models.TenantSettingsRecord.__table__.columns})
    assert _cleanups(db) == []  # 준비 기록은 저장과 같은 commit 에서 사용 중이 됐다
    assert set(_bindings(db, tenant_id)) == {"SLACK"}
    _assert_no_secret(resp, caplog, WEBHOOK)
    assert arn not in resp.text and arn not in caplog.text


def test_same_slack_url_is_not_restaged(client_pg, db, make_tenant, secrets):
    tenant_id = make_tenant()
    first = client_pg.patch(_url(tenant_id), json={"notifications": {"slack_webhook_url": WEBHOOK}}).json()
    binding = _bindings(db, tenant_id)
    again = client_pg.patch(_url(tenant_id), json={"notifications": {"slack_webhook_url": WEBHOOK}}).json()
    assert secrets.calls == ["create_secret"]
    assert again["updated_at"] == first["updated_at"] and _bindings(db, tenant_id) == binding


def test_slack_replacement_and_clear_retire_the_old_secret_and_binding(client_pg, db, make_tenant, secrets):
    tenant_id = make_tenant()
    client_pg.patch(_url(tenant_id), json={"notifications": {"slack_webhook_url": WEBHOOK}})
    first_arn = _row(db, tenant_id).slack_secret_ref
    first_binding = _bindings(db, tenant_id)["SLACK"]
    client_pg.patch(_url(tenant_id), json={"notifications": {"slack_webhook_url": WEBHOOK_2}})
    second_arn = _row(db, tenant_id).slack_secret_ref
    assert second_arn != first_arn
    second_binding = _bindings(db, tenant_id)["SLACK"]
    assert second_binding[0] != first_binding[0] and second_binding[1] != first_binding[1]
    [retired] = _cleanups(db)
    assert (retired.purpose, retired.resource_name) == ("RETIRED", first_arn)
    assert db.scalar(select(retired.not_before > func.now()))  # 진행 중 전송이 읽을 수 있게 유예한다

    resp = client_pg.patch(_url(tenant_id), json={"notifications": {"slack_webhook_url": None}})
    assert resp.json()["notifications"]["slack_configured"] is False
    row = _row(db, tenant_id)
    assert (row.slack_source, row.slack_secret_ref) == ("CLEARED", None)
    assert sorted(c.resource_name for c in _cleanups(db)) == sorted([first_arn, second_arn])
    assert "SLACK" not in _bindings(db, tenant_id)


def test_provider_failure_saves_nothing_and_hides_provider_text(client_pg, db, make_tenant, secrets, caplog):
    from botocore.exceptions import ClientError

    tenant_id = make_tenant()
    secrets.fail = ClientError({"Error": {"Code": "AccessDeniedException", "Message": WEBHOOK}}, "CreateSecret")
    resp = client_pg.patch(_url(tenant_id), json={
        "protected_tags": [], "notifications": {"slack_webhook_url": WEBHOOK},
    })
    assert resp.status_code == 500 and _code(resp) == "INTERNAL_ERROR"
    assert _row(db, tenant_id) is None  # 응답이 실패면 일부 저장도 없다
    [staged] = _cleanups(db)
    assert staged.purpose == "STAGING" and db.scalar(select(staged.not_before <= func.now()))
    _assert_no_secret(resp, caplog, WEBHOOK)


def test_disabled_notifications_make_no_provider_call(client_pg, db, make_tenant, notifications_disabled):
    tenant_id = make_tenant()
    resp = client_pg.patch(_url(tenant_id), json={"notifications": {"slack_webhook_url": WEBHOOK}})
    assert resp.status_code == 500 and _code(resp) == "INTERNAL_ERROR"
    assert _row(db, tenant_id) is None and _cleanups(db) == []
    # 이메일 · 해제 · 태그는 DB 목표만 바꾼다(관측 · 정리는 알림 런타임 몫)
    resp = client_pg.patch(_url(tenant_id), json={
        "protected_tags": [], "notifications": {"email_address": ADDRESS, "slack_webhook_url": None},
    })
    assert resp.status_code == 200
    assert resp.json()["notifications"]["email_status"] == "SETUP_PENDING"
    assert notifications_disabled.created == [] and notifications_disabled.calls == []


# ----- 이메일 -----------------------------------------------------------------------


def test_new_address_starts_a_generation_and_same_address_keeps_progress(client_pg, db, make_tenant, secrets):
    tenant_id = make_tenant()
    resp = client_pg.patch(_url(tenant_id), json={"notifications": {"email_address": ADDRESS}})
    assert resp.json()["notifications"] == {
        "slack_configured": False, "legacy_sns_configured": False,
        "email_address": ADDRESS, "email_status": "SETUP_PENDING",
    }
    row = _row(db, tenant_id)
    generation, updated = row.email_generation, row.updated_at
    assert row.email_region == platform_region() and "SNS" not in _bindings(db, tenant_id)
    _seed_settings(db, tenant_id, email_status="CONFIRMATION_PENDING",
                   email_topic_arn=f"arn:aws:sns:{platform_region()}:{ACCOUNT}:{topic_name(tenant_id, str(generation))}",
                   email_subscription_arn="arn:aws:sns:x:1:t:sub")
    again = client_pg.patch(_url(tenant_id), json={"notifications": {"email_address": ADDRESS}}).json()
    assert again["notifications"]["email_status"] == "CONFIRMATION_PENDING"
    row = _row(db, tenant_id)
    assert (row.email_generation, row.updated_at) == (generation, updated)
    assert _cleanups(db) == [] and secrets.calls == []


def test_same_address_after_failure_retries_with_a_new_generation(client_pg, db, make_tenant, secrets):
    tenant_id = make_tenant()
    generation, topic = _ready_email(db, tenant_id)
    _seed_settings(db, tenant_id, email_status="SETUP_FAILED", email_failure_code="SUBSCRIPTION_REMOVED")
    resp = client_pg.patch(_url(tenant_id), json={"notifications": {"email_address": ADDRESS}})
    assert resp.json()["notifications"]["email_status"] == "SETUP_PENDING"
    row = _row(db, tenant_id)
    assert row.email_generation != generation and row.email_topic_arn is None
    [retired] = _cleanups(db)
    assert (retired.resource_kind, retired.resource_name) == ("SNS_TOPIC", topic)


def test_address_change_retires_ready_route_in_the_same_commit(client_pg, db, make_tenant, secrets):
    tenant_id = make_tenant()
    _, topic = _ready_email(db, tenant_id)
    record = _row(db, tenant_id)
    from notifications.settings_source import sync_tenant

    settings_repo.lock_notification_config(db)
    sync_tenant(db, tenant_id, db.get(models.Tenant, tenant_id).aws_account_id, record)
    db.commit()
    assert "SNS" in _bindings(db, tenant_id)
    resp = client_pg.patch(_url(tenant_id), json={"notifications": {"email_address": "new@example.com"}})
    assert resp.json()["notifications"] == {
        "slack_configured": False, "legacy_sns_configured": False,
        "email_address": "new@example.com", "email_status": "SETUP_PENDING",
    }
    assert "SNS" not in _bindings(db, tenant_id)  # 이전 READY 를 새 주소가 승계하지 않는다
    assert [(c.resource_kind, c.resource_name) for c in _cleanups(db)] == [("SNS_TOPIC", topic)]


LEGACY_TOPIC_ARN = "arn:aws:sns:ap-northeast-2:999999999999:customer-a"


def _legacy_sns(db, tenant_id):
    """routes 파일에서 가져온 외부 토픽(가져오기와 같은 행 모양) + 그 바인딩."""
    from notifications.settings_source import sync_tenant

    record = _seed_settings(db, tenant_id, email_source="LEGACY_TOPIC", email_legacy_topic_arn=LEGACY_TOPIC_ARN)
    settings_repo.lock_notification_config(db)
    sync_tenant(db, tenant_id, db.get(models.Tenant, tenant_id).aws_account_id, _row(db, tenant_id))
    db.commit()
    assert "SNS" in _bindings(db, tenant_id)
    return record


def test_legacy_topic_is_reported_without_revealing_it(client_pg, db, make_tenant, secrets):
    tenant_id = make_tenant()
    _legacy_sns(db, tenant_id)
    resp = client_pg.get(_url(tenant_id))
    assert resp.status_code == 200
    # 외부 토픽이 발송 중임을 알린다. 주소 · 상태를 지어내지 않고 ARN · 토픽 이름을 싣지 않는다
    assert resp.json()["notifications"] == {
        "slack_configured": False, "legacy_sns_configured": True,
        "email_address": None, "email_status": "UNSET",
    }
    assert LEGACY_TOPIC_ARN not in resp.text and "customer-a" not in resp.text


def test_partial_patches_keep_the_legacy_topic(client_pg, db, make_tenant, secrets):
    tenant_id = make_tenant()
    _legacy_sns(db, tenant_id)
    binding = _bindings(db, tenant_id)["SNS"]
    for body in ({"protected_tags": []}, {"notifications": {"slack_webhook_url": WEBHOOK}}):
        resp = client_pg.patch(_url(tenant_id), json=body)
        assert resp.status_code == 200 and resp.json()["notifications"]["legacy_sns_configured"] is True
    row = _row(db, tenant_id)
    assert (row.email_source, row.email_legacy_topic_arn) == ("LEGACY_TOPIC", LEGACY_TOPIC_ARN)
    assert _bindings(db, tenant_id)["SNS"] == binding  # 같은 발송 바인딩(epoch) 유지
    assert _cleanups(db) == []


def test_email_null_clears_and_never_deletes_an_external_legacy_topic(client_pg, db, make_tenant, secrets):
    tenant_id = make_tenant()
    _legacy_sns(db, tenant_id)
    resp = client_pg.patch(_url(tenant_id), json={"notifications": {"email_address": None}})
    assert resp.json()["notifications"] == {
        "slack_configured": False, "legacy_sns_configured": False,
        "email_address": None, "email_status": "UNSET",
    }
    row = _row(db, tenant_id)
    assert (row.email_source, row.email_legacy_topic_arn) == ("CLEARED", None)
    assert "SNS" not in _bindings(db, tenant_id)  # 같은 commit 에서 발송을 멈춘다
    assert _cleanups(db) == []  # 외부 토픽은 삭제 대기에 넣지 않는다
    again = client_pg.get(_url(tenant_id)).json()["notifications"]
    assert again["legacy_sns_configured"] is False


def test_new_email_replaces_the_legacy_topic_without_touching_it(client_pg, db, make_tenant, secrets):
    tenant_id = make_tenant()
    _legacy_sns(db, tenant_id)
    resp = client_pg.patch(_url(tenant_id), json={"notifications": {"email_address": ADDRESS}})
    assert resp.json()["notifications"] == {
        "slack_configured": False, "legacy_sns_configured": False,
        "email_address": ADDRESS, "email_status": "SETUP_PENDING",
    }
    row = _row(db, tenant_id)
    assert (row.email_source, row.email_legacy_topic_arn, row.email_status) == (
        "MANAGED", None, "SETUP_PENDING",
    )
    assert "SNS" not in _bindings(db, tenant_id)  # 새 주소는 READY 전까지 보내지 않고 옛 토픽으로도 보내지 않는다
    assert _cleanups(db) == [] and secrets.calls == []  # 외부 토픽 · 다른 자원 무변경


def test_never_started_generation_queues_no_cleanup(client_pg, db, make_tenant, secrets):
    tenant_id = make_tenant()
    client_pg.patch(_url(tenant_id), json={"notifications": {"email_address": ADDRESS}})
    client_pg.patch(_url(tenant_id), json={"notifications": {"email_address": None}})
    assert _cleanups(db) == []
