"""고객사 관리의 플랫폼 활동 로그(#589 B) — 실제 PostgreSQL 행으로 본다.

지키는 것
  - 생성 · 수정 · 자동 조치 켜고 끄기 · 정지 · 연결 확인 · 재개 · 삭제가 각각 한 행을 남긴다. 행위자는 요청한 사람,
    소속과 대상은 그 고객사다.
  - 수정은 값이 실제로 바뀐 칸만 싣는다. 바뀐 것이 없으면 행이 없다. 자동 조치는 전 · 후 값으로 따로 남는다.
  - 상태가 바뀐 쓰기는 같은 트랜잭션이다 — 로그를 못 쓰면 쓰기도 되돌아간다. 거절(409 · 503)은 행을 남기지 않는다.
  - 연결 확인 · 재개의 상태 충돌(409)만은 요청 트랜잭션 밖에 DENIED 로 남는다 — 요청이 되돌려져도 남고,
    요청이 쥔 잠금을 기다리지 않는다. 따로 쓰기가 실패해도 409 는 그대로다.
  - 행에는 이름 · 계정 · 역할 ARN · 정지 사유 원문이 들어가지 않는다.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
import tenant_admin
from auth.http import get_clock
from config import TenantSettings, get_tenant_settings
from db import models
from db.repositories import platform_events as platform_events_repo
from db.repositories import tenants as tenants_repo
from fastapi.testclient import TestClient
from schemas.api.auth import AccountType, PlatformRole, UserRole
from schemas.api.tenants import TenantStatus, TenantVerificationResult
from schemas.platform_events import (
    PlatformActorKind,
    PlatformEventAction,
    PlatformEventCategory,
    PlatformEventOutcome,
    PlatformEventTargetType,
)
from sqlalchemy import create_engine, func, select, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker
from tenant_verification import VerificationOutcome
from tests.group_scope import assign_tenants

BASE = "/api/v1/tenants"
ACCOUNT = "123456789012"
NOW = datetime(2026, 10, 22, 5, 0, tzinfo=UTC)
CREATE_BODY = {"name": "학원", "aws_account_id": ACCOUNT, "regions": ["ap-northeast-2", "us-east-1"]}
REASON = "계약 만료 — 담당자 홍길동 요청"
A = PlatformEventAction
S, D, F = PlatformEventOutcome.SUCCEEDED, PlatformEventOutcome.DENIED, PlatformEventOutcome.FAILED
R = TenantVerificationResult
OK = VerificationOutcome(R.OK)
READ_DENIED = VerificationOutcome(R.READ_DENIED, "AccessDenied")
BUSINESS = {"plan": "Business", "period_start": "2026-11-01", "period_end": "2026-11-30"}


# ----- 픽스처 · 헬퍼 -----------------------------------------------------------------


def _ready() -> TenantSettings:
    return TenantSettings(
        TENANT_ONBOARDING_TEMPLATE_URL="https://templates.example.com/vigilantis-access.yaml",
        PLATFORM_PRINCIPAL_ARN="arn:aws:iam::999988887777:role/vigilantis-platform",
    )


@pytest.fixture()
def onboarding(client_pg):
    def configure(ready: bool = True) -> None:
        client_pg.app.dependency_overrides[get_tenant_settings] = (
            _ready if ready else (lambda: TenantSettings())
        )

    configure(True)
    return configure


class FakeAws:
    """tenant_admin.verify_connection 대역. during 은 AWS 를 기다리는 사이 다른 쪽에서 일어난 일을 흉내 낸다."""

    def __init__(self, monkeypatch) -> None:
        self.outcome = OK
        self.during = None
        monkeypatch.setattr(tenant_admin, "verify_connection", self)

    def __call__(self, access, *, expected_account_id, regions):
        if self.during is not None:
            self.during()
        return self.outcome


@pytest.fixture()
def aws(monkeypatch):
    return FakeAws(monkeypatch)


@pytest.fixture()
def admin(client_pg, platform_session, onboarding, aws):
    """client_pg 의 기본 세션 — PLATFORM(CENTRAL_ADMIN)."""
    return platform_session


def _seed(db, status=TenantStatus.ACTIVE, account=ACCOUNT) -> str:
    tenant = tenants_repo.create_tenant(
        db, name="학원", aws_account_id=account, regions=["ap-northeast-2"], now=NOW
    )
    if status is not TenantStatus.PENDING_CONNECTION:
        assert tenants_repo.record_verification(
            db, tenant.tenant_id, result=R.OK, reason_code=None, now=NOW
        )
    if status is TenantStatus.SUSPENDED:
        assert tenants_repo.suspend_tenant(db, tenant.tenant_id, reason=REASON, now=NOW)
    db.commit()
    return tenant.tenant_id


def _events(db, tenant_id=None, action=None):
    db.expire_all()
    query = select(models.PlatformEvent).where(
        models.PlatformEvent.category == PlatformEventCategory.TENANT_ADMIN
    )
    if tenant_id is not None:
        query = query.where(models.PlatformEvent.tenant_id == tenant_id)
    if action is not None:
        query = query.where(models.PlatformEvent.action == action.value)
    return db.scalars(query.order_by(models.PlatformEvent.occurred_at, models.PlatformEvent.action)).all()


def _stored(db, tenant_id):
    db.expire_all()
    return db.get(models.Tenant, tenant_id)


def _code(resp) -> str:
    return resp.json()["error"]["code"]


def _is_tenant_row(event, seed, tenant_id, action, outcome=S) -> None:
    """고객사 관리 행의 공통 모양 — 사람 행위자 · 소속과 대상이 그 고객사."""
    assert (event.action, event.outcome) == (action.value, outcome)
    assert event.category is PlatformEventCategory.TENANT_ADMIN
    assert event.actor_kind is PlatformActorKind.USER
    assert (event.actor_user_id, event.session_id) == (seed.user_id, seed.session_id)
    assert (event.actor_account_type, event.actor_platform_role) == (
        AccountType.PLATFORM, seed.platform_role,
    )
    assert (event.tenant_id, event.target_type, event.target_id) == (
        tenant_id, PlatformEventTargetType.TENANT, tenant_id,
    )


def _failing_after_write(monkeypatch, action):
    """그 행동의 이벤트 행을 쓴 직후 DB 오류 — 업무 쓰기와 이벤트가 함께 되돌아가는지 본다."""
    original = platform_events_repo.record_platform_event

    def fail(db, act, *args, **kwargs):
        row = original(db, act, *args, **kwargs)
        if act is action:
            raise OperationalError("event write", {}, Exception("not exposed"))
        return row

    monkeypatch.setattr(platform_events_repo, "record_platform_event", fail)


# ----- 생성 --------------------------------------------------------------------------


def test_create_records_one_row_for_the_new_tenant(client_pg, db, admin):
    resp = client_pg.post(BASE, json=CREATE_BODY, headers={"X-Request-ID": "req-create-1"})
    assert resp.status_code == 201
    tenant_id = resp.json()["tenant"]["tenant_id"]

    [event] = _events(db, tenant_id)
    _is_tenant_row(event, admin, tenant_id, A.TENANT_CREATE)
    assert (event.details, event.request_id) == ({}, "req-create-1")
    # 이름 · 계정 · 역할 ARN · ExternalId 는 행에 없다
    dump = f"{event.details}{event.target_id}"
    assert not any(text in dump for text in ("학원", ACCOUNT, "vigilantis-access", resp.json()["external_id"]))


@pytest.mark.parametrize("refusal", ["account_conflict", "no_onboarding_settings"])
def test_refused_create_records_nothing(client_pg, db, admin, onboarding, refusal):
    if refusal == "account_conflict":
        _seed(db)
    else:
        onboarding(False)
    resp = client_pg.post(BASE, json=CREATE_BODY)
    assert resp.status_code in (409, 503)
    assert _events(db, action=A.TENANT_CREATE) == []


def test_create_is_undone_when_the_event_cannot_be_written(client_pg, db, admin, monkeypatch):
    _failing_after_write(monkeypatch, A.TENANT_CREATE)
    assert client_pg.post(BASE, json=CREATE_BODY).status_code == 500
    assert db.scalar(select(func.count()).select_from(models.Tenant)) == 0
    assert _events(db) == []


def test_manager_create_records_the_manager_as_actor(client_pg, db, login_as, onboarding, make_group):
    group_id = make_group()
    manager = login_as(UserRole.ADMIN, platform_role=PlatformRole.TENANT_MANAGER)
    from db.repositories import auth as auth_repo

    auth_repo.replace_assigned_groups(db, user_id=manager.user_id, group_ids=(group_id,), now=NOW)
    db.commit()

    resp = client_pg.post(BASE, json={**CREATE_BODY, "group_id": group_id})
    assert resp.status_code == 201
    [event] = _events(db, resp.json()["tenant"]["tenant_id"])
    assert (event.actor_user_id, event.actor_platform_role) == (
        manager.user_id, PlatformRole.TENANT_MANAGER,
    )


# ----- 수정 · 자동 조치 ----------------------------------------------------------------


def test_update_records_only_the_fields_whose_value_changed(client_pg, db, admin):
    tenant_id = _seed(db)
    resp = client_pg.patch(
        f"{BASE}/{tenant_id}",
        # 이름은 지금 값과 같다 — 보냈지만 바뀌지 않았다
        json={"name": "학원", "regions": ["us-east-1", "ap-northeast-2"], "collect_vpc_ids": ["vpc-0a1b2c3d"]},
    )
    assert resp.status_code == 200
    [event] = _events(db, tenant_id)
    _is_tenant_row(event, admin, tenant_id, A.TENANT_UPDATE)
    assert event.details == {"changed_fields": ["collect_vpc_ids", "regions"]}  # 이름순 · 값은 없다


def test_update_with_the_same_values_records_nothing(client_pg, db, admin):
    tenant_id = _seed(db)
    resp = client_pg.patch(
        f"{BASE}/{tenant_id}",
        json={"name": "학원", "regions": ["ap-northeast-2"], "auto_action_enabled": True},
    )
    assert resp.status_code == 200
    assert _events(db, tenant_id) == []


def test_region_order_is_a_change(client_pg, db, admin):
    # 첫 리전이 온보딩 링크의 스택 리전이다 — 순서만 바뀌어도 바뀐 것이다
    tenant_id = _seed(db)
    client_pg.patch(f"{BASE}/{tenant_id}", json={"regions": ["ap-northeast-2", "us-east-1"]})
    client_pg.patch(f"{BASE}/{tenant_id}", json={"regions": ["us-east-1", "ap-northeast-2"]})
    assert [e.details for e in _events(db, tenant_id)] == [{"changed_fields": ["regions"]}] * 2


def test_subscription_change_is_recorded_once_and_not_on_a_repeat(client_pg, db, admin):
    tenant_id = _seed(db)
    for _ in range(2):  # 같은 구독을 다시 보내면 바뀐 것이 없다
        assert client_pg.patch(f"{BASE}/{tenant_id}", json={"subscription": BUSINESS}).status_code == 200
    assert [e.details for e in _events(db, tenant_id)] == [{"changed_fields": ["subscription"]}]

    assert client_pg.patch(f"{BASE}/{tenant_id}", json={"subscription": None}).status_code == 200
    assert len(_events(db, tenant_id, A.TENANT_UPDATE)) == 2


def test_auto_action_toggle_is_its_own_row_with_before_and_after(client_pg, db, admin):
    tenant_id = _seed(db)
    assert _stored(db, tenant_id).auto_action_enabled is True
    client_pg.patch(f"{BASE}/{tenant_id}", json={"auto_action_enabled": False})
    client_pg.patch(f"{BASE}/{tenant_id}", json={"auto_action_enabled": False})  # 이미 꺼져 있다 — 행 없음
    client_pg.patch(f"{BASE}/{tenant_id}", json={"auto_action_enabled": True})

    events = _events(db, tenant_id)
    assert [(e.action, e.details) for e in events] == [
        ("TENANT_AUTO_ACTION_CHANGE", {"before": "ENABLED", "after": "DISABLED"}),
        ("TENANT_AUTO_ACTION_CHANGE", {"before": "DISABLED", "after": "ENABLED"}),
    ]
    for event in events:
        _is_tenant_row(event, admin, tenant_id, A.TENANT_AUTO_ACTION_CHANGE)


def test_one_patch_changing_both_leaves_two_rows_of_the_same_request(client_pg, db, admin):
    tenant_id = _seed(db)
    resp = client_pg.patch(
        f"{BASE}/{tenant_id}",
        json={"name": "새 이름", "auto_action_enabled": False},
        headers={"X-Request-ID": "req-patch-both"},
    )
    assert resp.status_code == 200
    events = _events(db, tenant_id)
    assert {e.action: e.details for e in events} == {
        "TENANT_AUTO_ACTION_CHANGE": {"before": "ENABLED", "after": "DISABLED"},
        "TENANT_UPDATE": {"changed_fields": ["name"]},
    }
    assert len(events) == 2
    assert {e.request_id for e in events} == {"req-patch-both"}
    assert "새 이름" not in "".join(str(e.details) for e in events)


def test_update_is_undone_when_the_event_cannot_be_written(client_pg, db, admin, monkeypatch):
    tenant_id = _seed(db)
    _failing_after_write(monkeypatch, A.TENANT_AUTO_ACTION_CHANGE)
    assert client_pg.patch(f"{BASE}/{tenant_id}", json={"name": "새 이름", "auto_action_enabled": False}).status_code == 500
    stored = _stored(db, tenant_id)
    assert (stored.name, stored.auto_action_enabled) == ("학원", True)
    assert _events(db, tenant_id) == []


def test_refused_update_records_nothing(client_pg, db, admin, onboarding):
    tenant_id = _seed(db, TenantStatus.PENDING_CONNECTION)
    onboarding(False)  # 연결 전 상세는 온보딩 링크를 싣는다 — 설정이 없으면 503 이고 쓰기도 남지 않는다
    assert client_pg.patch(f"{BASE}/{tenant_id}", json={"name": "새 이름"}).status_code == 503
    assert _stored(db, tenant_id).name == "학원"
    assert _events(db, tenant_id) == []


# ----- 정지 --------------------------------------------------------------------------


def test_suspend_records_the_transition_without_the_reason_text(client_pg, db, admin):
    tenant_id = _seed(db)
    resp = client_pg.post(f"{BASE}/{tenant_id}/suspend", json={"reason": REASON})
    assert resp.status_code == 200

    [event] = _events(db, tenant_id)
    _is_tenant_row(event, admin, tenant_id, A.TENANT_SUSPEND)
    assert event.details == {"before": "ACTIVE", "after": "SUSPENDED"}
    assert _stored(db, tenant_id).suspended_reason == REASON  # 원문은 고객사 행에만 있다


@pytest.mark.parametrize("status", [TenantStatus.PENDING_CONNECTION, TenantStatus.SUSPENDED])
def test_refused_suspend_records_nothing(client_pg, db, admin, status):
    tenant_id = _seed(db, status)
    resp = client_pg.post(f"{BASE}/{tenant_id}/suspend", json={"reason": REASON})
    assert (resp.status_code, _code(resp)) == (409, "TENANT_STATE_CONFLICT")
    assert _events(db, tenant_id) == []


def test_suspend_is_undone_when_the_event_cannot_be_written(client_pg, db, admin, monkeypatch):
    tenant_id = _seed(db)
    _failing_after_write(monkeypatch, A.TENANT_SUSPEND)
    assert client_pg.post(f"{BASE}/{tenant_id}/suspend", json={"reason": REASON}).status_code == 500
    assert _stored(db, tenant_id).status is TenantStatus.ACTIVE
    assert _events(db, tenant_id) == []


# ----- 연결 확인 · 재개 ----------------------------------------------------------------

ROUTES = [
    ("verify", TenantStatus.PENDING_CONNECTION, A.TENANT_VERIFY),
    ("resume", TenantStatus.SUSPENDED, A.TENANT_RESUME),
]


@pytest.mark.parametrize("route,start,action", ROUTES)
def test_successful_check_records_ok(client_pg, db, admin, aws, route, start, action):
    tenant_id = _seed(db, start)
    assert client_pg.post(f"{BASE}/{tenant_id}/{route}").status_code == 200

    [event] = _events(db, tenant_id)
    _is_tenant_row(event, admin, tenant_id, action)
    assert event.details == {"result": "OK"}
    assert _stored(db, tenant_id).status is TenantStatus.ACTIVE


@pytest.mark.parametrize("route,start,action", ROUTES)
def test_failed_check_is_stored_and_recorded_as_failed(client_pg, db, admin, aws, route, start, action):
    # 실패한 확인도 고객사 행에 결과로 남는 쓰기다 — 같은 트랜잭션의 FAILED 한 행
    tenant_id = _seed(db, start)
    aws.outcome = READ_DENIED
    assert client_pg.post(f"{BASE}/{tenant_id}/{route}").status_code == 200

    [event] = _events(db, tenant_id)
    _is_tenant_row(event, admin, tenant_id, action, F)
    assert event.details == {"result": "READ_DENIED", "reason_code": "AccessDenied"}
    stored = _stored(db, tenant_id)
    assert (stored.status, stored.last_verification_result) == (start, R.READ_DENIED)


def test_a_reason_outside_the_log_format_falls_back_to_the_result_name(client_pg, db, admin, aws):
    # 고객사 행의 사유 칸은 형식이 없다 — 활동 로그 형식 밖의 값이 확인 결과까지 되돌리면 안 된다
    tenant_id = _seed(db, TenantStatus.PENDING_CONNECTION)
    aws.outcome = VerificationOutcome(R.ASSUME_DENIED, "weird code (with spaces)")
    assert client_pg.post(f"{BASE}/{tenant_id}/verify").status_code == 200

    [event] = _events(db, tenant_id)
    assert event.details == {"result": "ASSUME_DENIED", "reason_code": "ASSUME_DENIED"}
    assert _stored(db, tenant_id).last_verification_reason_code == "weird code (with spaces)"


@pytest.mark.parametrize("route,wrong,action", [
    ("verify", TenantStatus.SUSPENDED, A.TENANT_VERIFY),
    ("resume", TenantStatus.ACTIVE, A.TENANT_RESUME),
])
def test_conflict_before_the_check_is_denied_without_a_result(
    client_pg, db, admin, aws, route, wrong, action
):
    tenant_id = _seed(db, wrong)
    aws.during = lambda: pytest.fail("받지 않는 상태면 AWS 에 묻지 않는다")
    resp = client_pg.post(f"{BASE}/{tenant_id}/{route}")
    assert (resp.status_code, _code(resp)) == (409, "TENANT_STATE_CONFLICT")

    [event] = _events(db, tenant_id)
    _is_tenant_row(event, admin, tenant_id, action, D)
    assert event.details == {"reason_code": "TENANT_STATE_CONFLICT"}


def test_conflict_after_the_check_is_denied_with_the_result_it_got(client_pg, db, admin, aws):
    tenant_id = _seed(db)  # ACTIVE — verify 를 받는다
    aws.outcome = READ_DENIED

    def suspended_meanwhile():
        db.execute(update(models.Tenant).where(models.Tenant.tenant_id == tenant_id).values(
            status=TenantStatus.SUSPENDED, suspended_at=NOW + timedelta(hours=1), suspended_reason=REASON,
        ))
        db.commit()

    aws.during = suspended_meanwhile
    resp = client_pg.post(f"{BASE}/{tenant_id}/verify")
    assert (resp.status_code, _code(resp)) == (409, "TENANT_STATE_CONFLICT")

    [event] = _events(db, tenant_id)
    _is_tenant_row(event, admin, tenant_id, A.TENANT_VERIFY, D)
    assert event.details == {"reason_code": "TENANT_STATE_CONFLICT", "result": "READ_DENIED"}
    assert _stored(db, tenant_id).last_verification_result is R.OK  # 우리 관측은 쓰이지 않았다


def test_tenant_deleted_during_the_check_records_nothing(client_pg, db, admin, aws):
    tenant_id = _seed(db, TenantStatus.PENDING_CONNECTION)

    def deleted_meanwhile():
        assert tenants_repo.delete_tenant(db, tenant_id)
        db.commit()

    aws.during = deleted_meanwhile
    resp = client_pg.post(f"{BASE}/{tenant_id}/verify")
    assert (resp.status_code, _code(resp)) == (404, "TENANT_NOT_FOUND")
    assert _events(db, tenant_id) == []


def test_check_is_undone_when_the_event_cannot_be_written(client_pg, db, admin, aws, monkeypatch):
    tenant_id = _seed(db, TenantStatus.PENDING_CONNECTION)
    _failing_after_write(monkeypatch, A.TENANT_VERIFY)
    assert client_pg.post(f"{BASE}/{tenant_id}/verify").status_code == 500
    stored = _stored(db, tenant_id)
    assert (stored.status, stored.last_verification_result) == (TenantStatus.PENDING_CONNECTION, None)
    assert _events(db, tenant_id) == []


def test_detached_write_failure_keeps_the_original_409(client_pg, db, admin, aws):
    import db.session as db_session

    tenant_id = _seed(db, TenantStatus.SUSPENDED)

    def unavailable():
        raise RuntimeError("detached session unavailable")

    client_pg.app.dependency_overrides[db_session.get_session_factory] = lambda: unavailable
    resp = client_pg.post(f"{BASE}/{tenant_id}/verify")
    assert (resp.status_code, _code(resp)) == (409, "TENANT_STATE_CONFLICT")
    assert _events(db, tenant_id) == []


def test_manager_outside_the_scope_gets_403_and_no_tenant_row(client_pg, db, login_as, onboarding, aws):
    # 403 은 고객사 관리 행이 아니다(권한 거절은 ACCESS_FORBIDDEN 이 따로 맡는다)
    tenant_id, other = _seed(db), _seed(db, account="111122223333")
    manager = login_as(UserRole.ADMIN, platform_role=PlatformRole.TENANT_MANAGER)
    assign_tenants(db, user_id=manager.user_id, tenant_ids=(other,), now=NOW)
    db.commit()
    for call in (
        lambda: client_pg.post(f"{BASE}/{tenant_id}/verify"),
        lambda: client_pg.post(f"{BASE}/{tenant_id}/suspend", json={"reason": REASON}),
        lambda: client_pg.patch(f"{BASE}/{tenant_id}", json={"name": "x"}),
        lambda: client_pg.delete(f"{BASE}/{tenant_id}"),
    ):
        assert call().status_code == 403
    assert _events(db, tenant_id) == []


# ----- 삭제 --------------------------------------------------------------------------


def test_delete_leaves_the_row_after_the_tenant_is_gone(client_pg, db, admin):
    created = client_pg.post(BASE, json=CREATE_BODY).json()["tenant"]["tenant_id"]
    assert client_pg.delete(f"{BASE}/{created}").status_code == 204
    assert _stored(db, created) is None

    create, remove = _events(db, created)  # 생성 행도 고객사와 함께 사라지지 않는다
    _is_tenant_row(create, admin, created, A.TENANT_CREATE)
    _is_tenant_row(remove, admin, created, A.TENANT_DELETE)
    assert remove.details == {}


def test_refused_delete_records_nothing(client_pg, db, admin):
    tenant_id = _seed(db)  # 연결된 적 있는 고객사는 지우지 않는다
    resp = client_pg.delete(f"{BASE}/{tenant_id}")
    assert (resp.status_code, _code(resp)) == (409, "TENANT_STATE_CONFLICT")
    assert _events(db, tenant_id) == []


def test_delete_is_undone_when_the_event_cannot_be_written(client_pg, db, admin, monkeypatch):
    tenant_id = _seed(db, TenantStatus.PENDING_CONNECTION)
    _failing_after_write(monkeypatch, A.TENANT_DELETE)
    assert client_pg.delete(f"{BASE}/{tenant_id}").status_code == 500
    assert _stored(db, tenant_id) is not None
    assert _events(db, tenant_id) == []


# ----- 따로 남기기 — 독립 세션(운영과 같은 갈래) ----------------------------------------------


def _committed_app(sessions):
    """요청마다 독립 세션, 따로 남기기도 독립 세션 — 테스트 트랜잭션 밖이다."""
    import api_usage
    import db.session as db_session
    from config import get_settings
    from main import create_app

    get_settings.cache_clear()
    app = create_app()

    def _get_db():
        session = sessions()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[db_session.get_db] = _get_db
    app.dependency_overrides[db_session.get_session_factory] = lambda: sessions
    app.dependency_overrides[api_usage.get_usage_session_factory] = lambda: sessions
    app.dependency_overrides[get_tenant_settings] = _ready
    app.dependency_overrides[get_clock] = lambda: (lambda: datetime.now(UTC))
    return app


@pytest.mark.parametrize("after_the_check", [False, True])
def test_conflict_commits_apart_from_the_request_without_waiting(
    pg_engine, committed_auth, aws, after_the_check
):
    """요청이 쥔 사용자 표 잠금을 놓지 않고 따로 쓰면 FK 확인이 그 잠금을 기다린다 — 5초 뒤 실패해 행이 없다."""
    engine = create_engine(pg_engine.url, connect_args={"options": "-c lock_timeout=5000"})
    sessions = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    tenant_id = committed_auth.make_tenant(
        TenantStatus.ACTIVE if after_the_check else TenantStatus.SUSPENDED
    )
    seed = committed_auth.make()
    if after_the_check:
        def suspended_meanwhile():
            with sessions() as other:
                assert tenants_repo.suspend_tenant(other, tenant_id, reason=REASON, now=datetime.now(UTC))
                other.commit()

        aws.during = suspended_meanwhile
    try:
        with TestClient(_committed_app(sessions), headers=seed.headers) as client:
            resp = client.post(f"{BASE}/{tenant_id}/verify")

        assert (resp.status_code, _code(resp)) == (409, "TENANT_STATE_CONFLICT")
        with sessions() as check:
            [event] = _events(check, tenant_id)
            _is_tenant_row(event, seed, tenant_id, A.TENANT_VERIFY, D)
            assert event.details == {"reason_code": "TENANT_STATE_CONFLICT"} | (
                {"result": "OK"} if after_the_check else {}
            )
    finally:
        engine.dispose()  # 사람 행 · 고객사는 committed_auth 정리가 지운다


# ----- 도입 신청 승인으로 만든 고객사 ------------------------------------------------------


def test_signup_approval_leaves_a_create_row_by_the_approver(
    client_pg, client_pg_anon, db, admin, allowed_origin
):
    """승인은 자기 트랜잭션 안에서 같은 생성 본체를 지난다 — 생성 행의 행위자는 승인한 직원이다."""
    import uuid

    signup = client_pg_anon.post(
        "/api/v1/signup-requests",
        json={"company_name": "Acme", "contact_name": "Kim", "email": "kim@example.com",
              "phone": "010-1234-5678", "note": "note"},
        headers={"Origin": allowed_origin, "Idempotency-Key": str(uuid.uuid4())},
    )
    request_id = signup.json()["request_id"]
    resp = client_pg.post(
        f"/api/v1/signup-requests/{request_id}/approve",
        json={"aws_account_id": ACCOUNT, "regions": ["ap-northeast-2"]},
    )
    assert resp.status_code == 200
    tenant_id = resp.json()["tenant"]["tenant"]["tenant_id"]

    [event] = _events(db, tenant_id)
    _is_tenant_row(event, admin, tenant_id, A.TENANT_CREATE)


def test_refused_signup_approval_leaves_no_create_row(client_pg, client_pg_anon, db, admin, allowed_origin):
    import uuid

    _seed(db)  # 같은 계정의 고객사가 이미 있다 → 승인 409, 승인 트랜잭션과 함께 생성 행도 없다
    signup = client_pg_anon.post(
        "/api/v1/signup-requests",
        json={"company_name": "Acme", "contact_name": "Kim", "email": "kim@example.com",
              "phone": "010-1234-5678", "note": "note"},
        headers={"Origin": allowed_origin, "Idempotency-Key": str(uuid.uuid4())},
    )
    resp = client_pg.post(
        f"/api/v1/signup-requests/{signup.json()['request_id']}/approve",
        json={"aws_account_id": ACCOUNT, "regions": ["ap-northeast-2"]},
    )
    assert resp.status_code == 409
    assert _events(db, action=A.TENANT_CREATE) == []


# ----- 기록 계약 — TENANT_UPDATE 의 details -------------------------------------------------


def _update_record(details):
    import uuid

    from schemas.platform_events import PlatformEventRecord

    tenant = str(uuid.uuid4())
    return PlatformEventRecord(
        action=A.TENANT_UPDATE,
        actor_kind=PlatformActorKind.USER,
        actor_user_id=str(uuid.uuid4()),
        actor_account_type=AccountType.PLATFORM,
        actor_platform_role=PlatformRole.CENTRAL_ADMIN,
        tenant_id=tenant,
        target_id=tenant,
        details=details,
    )


def test_update_details_take_sorted_unique_known_field_names():
    record = _update_record({"changed_fields": ["collect_vpc_ids", "name", "regions", "subscription"]})
    assert record.details == {"changed_fields": ["collect_vpc_ids", "name", "regions", "subscription"]}


@pytest.mark.parametrize("details", [
    {},                                                 # 바뀐 칸 없이는 남기지 않는다
    {"changed_fields": []},
    {"changed_fields": ["regions", "name"]},            # 이름순
    {"changed_fields": ["name", "name"]},               # 중복
    {"changed_fields": ["auto_action_enabled"]},        # 따로 남는다(TENANT_AUTO_ACTION_CHANGE)
    {"changed_fields": ["aws_account_id"]},             # PATCH 가 받지 않는 칸
    {"changed_fields": ["name"], "name": "새 이름"},     # 값은 싣지 않는다
])
def test_update_details_outside_the_contract_are_rejected(details):
    with pytest.raises(ValueError):
        _update_record(details)


def test_every_patch_field_is_either_an_update_field_or_the_auto_action_row():
    """PATCH 가 받는 칸이 늘면 이 표가 깨진다 — 새 칸을 TENANT_UPDATE 에 실을지 따로 남길지 정하게 한다."""
    from typing import get_args

    from schemas.api.tenants import TenantUpdateRequest
    from schemas.platform_events import TenantUpdateField

    assert set(get_args(TenantUpdateField)) | {"auto_action_enabled"} == set(
        TenantUpdateRequest.model_fields
    )
    assert set(tenant_admin._PLAIN_UPDATE_FIELDS) | {"subscription"} == set(get_args(TenantUpdateField))
