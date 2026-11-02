"""POST /api/v1/tenants/{id}/verify · /resume — 실제 PostgreSQL + AWS 대역 (#533 B).

보장을 둘로 가른다.
  - AWS 대역(tenant_admin.verify_connection 패치): 호출 여부 · 인자 · 결과만 정한다. 실 AWS · 신뢰 정책은 보지 않는다.
    결과 5갈래의 분류 · 전송 오류는 test_tenant_verification.py 가 고정한다 — 여기서 다시 세지 않는다.
  - 실 DB: 저장된 행 · 응답 · GET 일치, 권한 재판정, AWS 중 변경에 대한 거절, 거절 시 무기록.
  - 트랜잭션 분리는 db 픽스처의 SAVEPOINT 로는 볼 수 없어(바깥 트랜잭션이 항상 열려 있다) 독립 commit 세션과
    별도 연결로 확인한다(committed_client).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
import tenant_admin
from auth import sessions
from auth.http import get_clock
from config import TenantSettings, get_tenant_settings
from db import models
from db.repositories import auth as auth_repo
from db.repositories import tenants as tenants_repo
from db.session import get_db
from schemas.api.auth import PlatformRole, UserRole
from schemas.api.tenants import (
    TenantStatus,
    TenantVerificationResult,
    TenantVerifyResponse,
)
from sqlalchemy import delete, event, select, text, update
from sqlalchemy.orm import Session, sessionmaker
from tenant_verification import VerificationOutcome

BASE = "/api/v1/tenants"
ACCOUNT = "123456789012"
OTHER = "111122223333"
ROLE = f"arn:aws:iam::{ACCOUNT}:role/vigilantis-access"
REGIONS = ["ap-northeast-2", "us-east-1"]
NOW = datetime(2026, 10, 22, 5, 0, tzinfo=UTC)
MISSING = "00000000-0000-0000-0000-000000000000"
R = TenantVerificationResult
OK = VerificationOutcome(R.OK)
READ_DENIED = VerificationOutcome(R.READ_DENIED, "AccessDenied")
ROUTES = ["verify", "resume"]
# 라우트가 받는 시작 상태
START = {"verify": TenantStatus.PENDING_CONNECTION, "resume": TenantStatus.SUSPENDED}
SKEW = timedelta(minutes=2)  # AWS 대역이 흘려보내는 시간 — 관측 시각이 AWS 뒤에 찍히는지 본다


# ----- 픽스처 · 헬퍼 -----------------------------------------------------------------

@pytest.fixture()
def onboarding(client_pg):
    def configure(ready: bool = True) -> None:
        client_pg.app.dependency_overrides[get_tenant_settings] = lambda: TenantSettings(
            TENANT_ONBOARDING_TEMPLATE_URL="https://templates.example.com/t.yaml" if ready else None,
            PLATFORM_PRINCIPAL_ARN="arn:aws:iam::999988887777:role/vigilantis-platform" if ready else None,
        )

    configure(True)
    return configure


class FakeAws:
    """verify_connection 대역 + 멈춰 둔 시계. during 은 AWS 를 기다리는 사이 다른 쪽에서 일어난 일을 흉내 낸다.

    시계는 AWS 호출 전까지 start 에 서 있다가, 호출되면 observed_at(= start + SKEW)로 건너뛴다 —
    관측 시각이 AWS 뒤에 찍히는지가 정확한 값으로 드러난다."""

    def __init__(self, app, monkeypatch) -> None:
        self.outcome = OK
        self.calls: list[tuple] = []
        self.during = None
        self.start = datetime.now(UTC)
        self.observed_at = self.start + SKEW
        self.now = self.start
        app.dependency_overrides[get_clock] = lambda: (lambda: self.now)
        monkeypatch.setattr(tenant_admin, "verify_connection", self)

    def __call__(self, access, *, expected_account_id, region):
        self.calls.append((access, expected_account_id, region))
        self.now = self.observed_at
        if self.during is not None:
            self.during()
        return self.outcome


@pytest.fixture()
def aws(client_pg, monkeypatch):
    return FakeAws(client_pg.app, monkeypatch)


@pytest.fixture()
def admin(login_as, onboarding, aws):
    return login_as(UserRole.ADMIN, platform_role=PlatformRole.CENTRAL_ADMIN)


@pytest.fixture()
def manager(login_as, onboarding, aws):
    return login_as(UserRole.ADMIN, platform_role=PlatformRole.TENANT_MANAGER)


def _seed(db, status=TenantStatus.PENDING_CONNECTION, account=ACCOUNT) -> models.Tenant:
    tenant = tenants_repo.create_tenant(db, name="학원", aws_account_id=account, regions=REGIONS, now=NOW)
    if status is not TenantStatus.PENDING_CONNECTION:
        assert tenants_repo.record_verification(
            db, tenant.tenant_id, result=R.OK, reason_code=None, now=NOW)
    if status is TenantStatus.SUSPENDED:
        assert tenants_repo.suspend_tenant(db, tenant.tenant_id, reason="계약 만료", now=NOW)
    db.commit()
    return tenant


def _stored(db, tenant_id):
    db.expire_all()
    row = tenants_repo.get_tenant(db, tenant_id)
    return None if row is None else row.tenant


def _snapshot(db, tenant_id):
    t = _stored(db, tenant_id)
    return None if t is None else (
        t.status, t.last_verification_result, t.last_verified_at, t.last_verification_reason_code,
        t.suspended_at, t.suspended_reason, t.updated_at)


def _post(client, route, tenant_id):
    return client.post(f"{BASE}/{tenant_id}/{route}")


def _code(resp) -> str:
    return resp.json()["error"]["code"]


def _change(db, tenant_id, **values):
    db.execute(update(models.Tenant).where(models.Tenant.tenant_id == tenant_id).values(**values))
    db.commit()


# ----- ① 저장된 결과 · 응답 · GET -------------------------------------------------------

@pytest.mark.parametrize("outcome", [
    OK,
    VerificationOutcome(R.ASSUME_DENIED, "AccessDenied"),
    VerificationOutcome(R.ACCOUNT_MISMATCH, "CallerAccountMismatch"),
    READ_DENIED,
    VerificationOutcome(R.UNREACHABLE, "ThrottlingException"),
])
def test_verify_pending_stores_each_outcome_and_matches_get(client_pg, db, admin, aws, outcome):
    tenant = _seed(db)
    aws.outcome = outcome

    resp = _post(client_pg, "verify", tenant.tenant_id)

    assert resp.status_code == 200
    body = TenantVerifyResponse.model_validate(resp.json())
    expected = TenantStatus.ACTIVE if outcome is OK else TenantStatus.PENDING_CONNECTION
    assert body.tenant.status is expected
    assert (body.verification.result, body.verification.reason_code) == (outcome.result, outcome.reason_code)
    # 관측 시각은 AWS 를 기다린 뒤에 찍는다(응답의 UtcDateTime 은 초 단위로 자른다)
    assert body.verification.verified_at == aws.observed_at.replace(microsecond=0)
    stored = _stored(db, tenant.tenant_id)
    assert (stored.status, stored.last_verification_result, stored.last_verification_reason_code) == (
        expected, outcome.result, outcome.reason_code)
    assert stored.last_verified_at.replace(microsecond=0) == body.verification.verified_at
    assert client_pg.get(f"{BASE}/{tenant.tenant_id}").json() == resp.json()["tenant"]


@pytest.mark.parametrize("outcome,reason", [(OK, None), (READ_DENIED, "AccessDenied")])
def test_verify_active_never_changes_status(client_pg, db, admin, aws, outcome, reason):
    tenant = _seed(db, TenantStatus.ACTIVE)
    aws.outcome = outcome

    resp = _post(client_pg, "verify", tenant.tenant_id)

    assert resp.status_code == 200
    body = TenantVerifyResponse.model_validate(resp.json())
    assert body.tenant.status is TenantStatus.ACTIVE  # 실패해도 자동 정지하지 않는다
    assert (body.verification.result, body.verification.reason_code) == (outcome.result, reason)
    stored = _stored(db, tenant.tenant_id)
    assert (stored.status, stored.last_verification_result) == (TenantStatus.ACTIVE, outcome.result)
    assert client_pg.get(f"{BASE}/{tenant.tenant_id}").json() == resp.json()["tenant"]


def test_resume_ok_activates_and_clears_suspension(client_pg, db, admin, aws):
    tenant = _seed(db, TenantStatus.SUSPENDED)

    resp = _post(client_pg, "resume", tenant.tenant_id)

    assert resp.status_code == 200
    body = TenantVerifyResponse.model_validate(resp.json())
    assert body.tenant.status is TenantStatus.ACTIVE
    assert (body.tenant.suspended_at, body.tenant.suspended_reason) == (None, None)
    stored = _stored(db, tenant.tenant_id)
    assert (stored.status, stored.suspended_at, stored.suspended_reason) == (TenantStatus.ACTIVE, None, None)
    assert client_pg.get(f"{BASE}/{tenant.tenant_id}").json() == resp.json()["tenant"]


def test_failed_resume_keeps_suspension_and_records_the_failure(client_pg, db, admin, aws):
    tenant = _seed(db, TenantStatus.SUSPENDED)
    before = _stored(db, tenant.tenant_id).suspended_at
    aws.outcome = VerificationOutcome(R.UNREACHABLE, "ThrottlingException")

    resp = _post(client_pg, "resume", tenant.tenant_id)

    assert resp.status_code == 200
    body = TenantVerifyResponse.model_validate(resp.json())
    assert (body.tenant.status, body.tenant.suspended_reason) == (TenantStatus.SUSPENDED, "계약 만료")
    assert (body.verification.result, body.verification.reason_code) == (R.UNREACHABLE, "ThrottlingException")
    stored = _stored(db, tenant.tenant_id)
    assert (stored.status, stored.suspended_at) == (TenantStatus.SUSPENDED, before)
    assert stored.last_verification_result is R.UNREACHABLE
    assert client_pg.get(f"{BASE}/{tenant.tenant_id}").json() == resp.json()["tenant"]


@pytest.mark.parametrize("route", ROUTES)
def test_aws_gets_the_stored_snapshot_and_the_first_region(client_pg, db, admin, aws, route):
    tenant = _seed(db, START[route])

    assert _post(client_pg, route, tenant.tenant_id).status_code == 200

    (access, account, region), = aws.calls
    assert (access.tenant_id, access.access_role_arn, access.external_id) == (
        tenant.tenant_id, ROLE, tenant.external_id)
    assert (account, region) == (ACCOUNT, REGIONS[0])


def test_ok_on_pending_needs_no_onboarding_settings_but_a_failure_does(client_pg, db, admin, aws, onboarding):
    onboarding(False)
    ok, failed = _seed(db), _seed(db, account=OTHER)
    assert _post(client_pg, "verify", ok.tenant_id).status_code == 200  # ACTIVE 응답에는 링크가 없다
    assert _stored(db, ok.tenant_id).status is TenantStatus.ACTIVE

    aws.outcome = READ_DENIED
    before = _snapshot(db, failed.tenant_id)
    resp = _post(client_pg, "verify", failed.tenant_id)  # 여전히 PENDING 이라 링크가 필요하다
    assert resp.status_code == 503 and _code(resp) == "TENANT_ONBOARDING_UNAVAILABLE"
    assert _snapshot(db, failed.tenant_id) == before  # 관측도 남지 않는다


# ----- ② AWS 를 부르기 전의 거절 -----------------------------------------------------------

@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("role", [UserRole.ADMIN, UserRole.VIEWER])
def test_customer_account_is_forbidden_without_aws(client_pg, db, login_as, make_tenant, onboarding, aws, route, role):
    tenant = _seed(db, START[route])
    login_as(role, tenant_id=make_tenant())
    resp = _post(client_pg, route, tenant.tenant_id)
    assert resp.status_code == 403 and _code(resp) == "AUTH_FORBIDDEN"
    assert aws.calls == []


@pytest.mark.parametrize("route", ROUTES)
def test_unauthenticated_is_401_without_aws(client_pg, db, onboarding, aws, route):
    tenant = _seed(db, START[route])
    resp = client_pg.post(f"{BASE}/{tenant.tenant_id}/{route}", headers={"Cookie": ""})
    assert resp.status_code == 401
    assert aws.calls == []


@pytest.mark.parametrize("route", ROUTES)
def test_manager_outside_scope_is_404_without_aws(client_pg, db, manager, aws, route):
    mine, other = _seed(db, START[route]), _seed(db, START[route], OTHER)
    auth_repo.replace_assigned_tenants(db, user_id=manager.user_id, tenant_ids=(mine.tenant_id,), now=NOW)
    db.commit()
    before = _snapshot(db, other.tenant_id)

    unassigned = _post(client_pg, route, other.tenant_id)

    assert unassigned.status_code == 404 and _code(unassigned) == "TENANT_NOT_FOUND"
    assert aws.calls == [] and _snapshot(db, other.tenant_id) == before
    assert _post(client_pg, route, mine.tenant_id).status_code == 200  # 담당 고객은 된다
    assert len(aws.calls) == 1


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("tenant_id", [MISSING, "not-a-uuid"])
def test_unknown_or_malformed_id_is_404_without_aws(client_pg, db, admin, aws, route, tenant_id):
    resp = _post(client_pg, route, tenant_id)
    assert resp.status_code == 404 and _code(resp) == "TENANT_NOT_FOUND"
    assert aws.calls == []


@pytest.mark.parametrize("route,status", [
    ("verify", TenantStatus.SUSPENDED),
    ("resume", TenantStatus.PENDING_CONNECTION),
    ("resume", TenantStatus.ACTIVE),
])
def test_status_outside_the_route_is_409_without_aws(client_pg, db, admin, aws, route, status):
    tenant = _seed(db, status)
    before = _snapshot(db, tenant.tenant_id)
    resp = _post(client_pg, route, tenant.tenant_id)
    assert resp.status_code == 409 and _code(resp) == "TENANT_STATE_CONFLICT"
    assert aws.calls == [] and _snapshot(db, tenant.tenant_id) == before


# ----- ③ AWS 를 기다리는 사이의 변경 --------------------------------------------------------

# 입력(역할 · ExternalId · 계정 · 첫 리전) 변경은 이 SAVEPOINT 세션으로 본다. 상태 · 삭제 · 권한 회수처럼
# 다른 연결이 끼어드는 경합은 아래 ⑥ 의 독립 commit 세션으로 본다.

@pytest.mark.parametrize("values", [
    {"access_role_arn": f"arn:aws:iam::{ACCOUNT}:role/other-role"},
    {"external_id": "f" * 32},
    {"aws_account_id": OTHER, "access_role_arn": f"arn:aws:iam::{OTHER}:role/vigilantis-access"},
    {"regions": [REGIONS[1], REGIONS[0]]},  # regions[0] 이 달라진다
], ids=["role", "external_id", "account", "first_region"])
@pytest.mark.parametrize("route", ROUTES)
def test_verification_input_changed_during_aws_is_409_and_persists_no_observation(
    client_pg, db, admin, aws, route, values
):
    tenant = _seed(db, START[route])
    seen = {}

    def during():
        _change(db, tenant.tenant_id, **values)
        seen["row"] = _snapshot(db, tenant.tenant_id)

    aws.during = during
    resp = _post(client_pg, route, tenant.tenant_id)

    assert resp.status_code == 409 and _code(resp) == "TENANT_STATE_CONFLICT"
    assert _snapshot(db, tenant.tenant_id) == seen["row"]


def test_a_change_beyond_the_first_region_does_not_block_the_observation(client_pg, db, admin, aws):
    tenant = _seed(db)
    aws.during = lambda: _change(db, tenant.tenant_id, regions=[REGIONS[0], "eu-west-1"])  # 확인한 리전은 그대로
    assert _post(client_pg, "verify", tenant.tenant_id).status_code == 200
    assert _stored(db, tenant.tenant_id).status is TenantStatus.ACTIVE


# ----- ⑤ 조건부 갱신이 0행일 때 -------------------------------------------------------------

@pytest.mark.parametrize("route,write", [("verify", "record_verification"), ("resume", "resume_tenant")])
def test_zero_row_write_is_409_while_the_tenant_exists(client_pg, db, admin, aws, monkeypatch, route, write):
    tenant = _seed(db, START[route])
    before = _snapshot(db, tenant.tenant_id)
    monkeypatch.setattr(tenants_repo, write, lambda *a, **k: False)

    resp = _post(client_pg, route, tenant.tenant_id)

    assert resp.status_code == 409 and _code(resp) == "TENANT_STATE_CONFLICT"
    assert _snapshot(db, tenant.tenant_id) == before


@pytest.mark.parametrize("route,write", [("verify", "record_verification"), ("resume", "resume_tenant")])
def test_zero_row_write_is_404_when_the_tenant_is_gone(client_pg, db, admin, aws, monkeypatch, route, write):
    tenant = _seed(db, START[route])

    def vanish(session, tenant_id, **_):
        session.execute(delete(models.Tenant).where(models.Tenant.tenant_id == tenant_id))
        return False

    monkeypatch.setattr(tenants_repo, write, vanish)
    resp = _post(client_pg, route, tenant.tenant_id)
    assert resp.status_code == 404 and _code(resp) == "TENANT_NOT_FOUND"


# ----- ⑥ AWS 를 기다리는 동안 트랜잭션 · 잠금이 없다, commit 은 실제로 남는다 ----------------------

@pytest.fixture()
def committed_client(pg_engine, client, committed_auth):
    """요청마다 독립 commit 세션을 쓰는 클라이언트 — 운영의 get_db 와 같은 모양."""
    factory = sessionmaker(bind=pg_engine, autoflush=False, expire_on_commit=False)

    def get_db_override():
        session = factory()
        try:
            yield session
        finally:
            session.close()

    client.app.dependency_overrides[get_db] = get_db_override
    client.app.dependency_overrides[get_tenant_settings] = lambda: TenantSettings(
        TENANT_ONBOARDING_TEMPLATE_URL="https://templates.example.com/t.yaml",
        PLATFORM_PRINCIPAL_ARN="arn:aws:iam::999988887777:role/vigilantis-platform",
    )
    client.admin = committed_auth.make()
    client.headers.update(client.admin.headers)
    client.factory = factory
    return client


@pytest.fixture()
def committed_aws(committed_client, monkeypatch):
    return FakeAws(committed_client.app, monkeypatch)


def _elsewhere(client, write):
    """AWS 를 기다리는 사이 다른 연결(독립 세션)에서 commit 되는 변경."""
    with client.factory() as other:
        write(other)
        other.commit()


def _fresh(client, tenant_id):
    """요청 세션과 무관한 새 세션으로 읽은 저장 값 — 지워졌으면 None."""
    with client.factory() as other:
        t = other.get(models.Tenant, tenant_id)
        return None if t is None else (
            t.status, t.last_verification_result, t.last_verified_at, t.last_verification_reason_code,
            t.suspended_at, t.suspended_reason, t.updated_at)


def _assigned_manager(client, committed_auth, tenant_id):
    seed = committed_auth.make(
        UserRole.ADMIN, platform_role=PlatformRole.TENANT_MANAGER, assigned_tenant_ids=(tenant_id,))
    client.headers.update(seed.headers)
    return seed


@pytest.mark.parametrize("route", ROUTES)
def test_assigned_manager_completes_and_response_row_and_get_agree(
    committed_client, committed_auth, committed_aws, route
):
    tenant_id = committed_auth.make_tenant(START[route])
    _assigned_manager(committed_client, committed_auth, tenant_id)

    resp = _post(committed_client, route, tenant_id)

    assert resp.status_code == 200, resp.text
    body = TenantVerifyResponse.model_validate(resp.json())
    assert body.tenant.status is TenantStatus.ACTIVE
    assert body.verification.verified_at == committed_aws.observed_at.replace(microsecond=0)
    stored = _fresh(committed_client, tenant_id)  # 다른 연결에서 본다 — commit 된 값
    assert stored[:2] == (TenantStatus.ACTIVE, R.OK)
    assert stored[2].replace(microsecond=0) == body.verification.verified_at
    assert stored[4:6] == (None, None)
    assert committed_client.get(f"{BASE}/{tenant_id}").json() == resp.json()["tenant"]


def _suspend(other, tenant_id):
    assert tenants_repo.suspend_tenant(other, tenant_id, reason="그 사이 정지", now=NOW)


def _resume_elsewhere(other, tenant_id):
    assert tenants_repo.resume_tenant(other, tenant_id, result=R.OK, reason_code=None, now=NOW)


def _verified_elsewhere(other, tenant_id):
    assert tenants_repo.record_verification(other, tenant_id, result=R.OK, reason_code=None, now=NOW)


def test_overlapping_verifications_do_not_move_the_last_verified_time_backwards(
    committed_client, committed_auth, committed_aws, pg_engine
):
    tenant_id = committed_auth.make_tenant(TenantStatus.ACTIVE)
    second_at = committed_aws.observed_at + timedelta(seconds=1)
    last_at = second_at + timedelta(seconds=1)
    overtaking = []

    def before_lock(conn, cursor, statement, parameters, context, executemany):
        if (
            statement != "LOCK TABLE app_users IN EXCLUSIVE MODE"
            or len(committed_aws.calls) != 1
            or overtaking
        ):
            return
        # 첫 요청의 AWS 응답 뒤, 쓰기 잠금을 얻기 전에 둘째 요청이 먼저 commit한다.
        overtaking.append(None)
        committed_aws.observed_at = second_at
        committed_aws.outcome = READ_DENIED
        response = _post(committed_client, "verify", tenant_id)
        assert response.status_code == 200, response.text
        overtaking[0] = TenantVerifyResponse.model_validate(response.json())
        assert _fresh(committed_client, tenant_id)[2] == second_at
        committed_aws.now = last_at

    event.listen(pg_engine, "before_cursor_execute", before_lock)
    try:
        response = _post(committed_client, "verify", tenant_id)
    finally:
        event.remove(pg_engine, "before_cursor_execute", before_lock)

    assert response.status_code == 200, response.text
    body = TenantVerifyResponse.model_validate(response.json())
    assert len(overtaking) == 1
    assert body.verification.verified_at >= overtaking[0].verification.verified_at
    assert body.verification.result is R.OK
    stored = _fresh(committed_client, tenant_id)
    assert stored[:3] == (TenantStatus.ACTIVE, R.OK, last_at)
    assert stored[2].replace(microsecond=0) == body.verification.verified_at
    assert committed_client.get(f"{BASE}/{tenant_id}").json() == response.json()["tenant"]


@pytest.mark.parametrize("route,start,change", [
    ("verify", TenantStatus.ACTIVE, _suspend),
    ("verify", TenantStatus.PENDING_CONNECTION, _verified_elsewhere),
    ("resume", TenantStatus.SUSPENDED, _resume_elsewhere),
])
def test_status_changed_on_another_connection_is_409_and_keeps_its_row(
    committed_client, committed_auth, committed_aws, route, start, change
):
    tenant_id = committed_auth.make_tenant(start)
    committed_aws.outcome = READ_DENIED
    seen = {}

    def during():
        _elsewhere(committed_client, lambda other: change(other, tenant_id))
        seen["row"] = _fresh(committed_client, tenant_id)

    committed_aws.during = during
    resp = _post(committed_client, route, tenant_id)

    assert resp.status_code == 409 and _code(resp) == "TENANT_STATE_CONFLICT"
    assert _fresh(committed_client, tenant_id) == seen["row"]  # 우리 관측(READ_DENIED)이 덮어쓰지 않았다


def test_resume_after_another_resume_and_re_suspend_is_409_and_keeps_the_newer_suspension(
    committed_client, committed_auth, committed_aws
):
    # 상태가 SUSPENDED 로 돌아와도 처음 본 정지가 아니다 — 같은 사유라도 정지 시각이 새로 찍힌 정지다
    tenant_id = committed_auth.make_tenant(TenantStatus.SUSPENDED)
    original = _fresh(committed_client, tenant_id)
    seen = {}  # 바깥 요청의 AWS 결과는 기본 OK — 새 정지를 풀어 버리는 쪽이 위험한 경우다

    def during():
        def cycle(other):
            assert tenants_repo.resume_tenant(other, tenant_id, result=R.OK, reason_code=None, now=NOW)
            assert tenants_repo.suspend_tenant(
                other, tenant_id, reason=original[5], now=datetime.now(UTC) + timedelta(hours=1))

        _elsewhere(committed_client, cycle)
        seen["row"] = _fresh(committed_client, tenant_id)

    committed_aws.during = during
    resp = _post(committed_client, "resume", tenant_id)

    assert seen["row"][0] is TenantStatus.SUSPENDED
    assert seen["row"][4] > original[4] and seen["row"][5] == original[5]
    assert resp.status_code == 409 and _code(resp) == "TENANT_STATE_CONFLICT"
    assert _fresh(committed_client, tenant_id) == seen["row"]  # 새 정지와 그 사이의 OK 기록이 그대로다


def test_tenant_deleted_on_another_connection_is_404(committed_client, committed_auth, committed_aws):
    tenant_id = committed_auth.make_tenant(TenantStatus.PENDING_CONNECTION)
    committed_aws.during = lambda: _elsewhere(
        committed_client, lambda other: tenants_repo.delete_tenant(other, tenant_id))

    resp = _post(committed_client, "verify", tenant_id)

    assert resp.status_code == 404 and _code(resp) == "TENANT_NOT_FOUND"
    assert _fresh(committed_client, tenant_id) is None


def _revoke_session(other, seed):
    sessions.revoke_session(other, seed.token, now=datetime.now(UTC))


def _demote(other, seed):
    other.get(models.AppUser, seed.user_id).platform_role = PlatformRole.TENANT_MANAGER


def _unassign(other, seed):
    auth_repo.replace_assigned_tenants(other, user_id=seed.user_id, tenant_ids=(), now=NOW)


@pytest.mark.parametrize("route,revoke,assigned,status,code", [
    ("verify", _revoke_session, False, 401, "AUTH_REQUIRED"),
    ("resume", _demote, False, 404, "TENANT_NOT_FOUND"),  # 강등되어 배정이 없다
    ("verify", _unassign, True, 404, "TENANT_NOT_FOUND"),
], ids=["session_revoked", "demoted", "assignment_removed"])
def test_authority_removed_on_another_connection_writes_nothing(
    committed_client, committed_auth, committed_aws, route, revoke, assigned, status, code
):
    tenant_id = committed_auth.make_tenant(START[route])
    actor = (
        _assigned_manager(committed_client, committed_auth, tenant_id) if assigned
        else committed_client.admin
    )
    before = _fresh(committed_client, tenant_id)
    committed_aws.during = lambda: _elsewhere(committed_client, lambda other: revoke(other, actor))

    resp = _post(committed_client, route, tenant_id)

    assert resp.status_code == status and _code(resp) == code
    assert len(committed_aws.calls) == 1  # 첫 판정은 통과해 AWS 까지 갔다
    assert _fresh(committed_client, tenant_id) == before


@pytest.mark.parametrize("route", ROUTES)
def test_no_transaction_or_lock_is_held_while_waiting_for_aws_and_the_result_is_committed(
    committed_client, committed_auth, committed_aws, pg_engine, monkeypatch, route
):
    tenant_id = committed_auth.make_tenant(START[route])
    events: list[str] = []
    probe: dict = {}

    original = tenants_repo.read_tenant_for_share

    def spied(db, tid):
        events.append("share")
        return original(db, tid)

    monkeypatch.setattr(tenants_repo, "read_tenant_for_share", spied)

    def during():
        events.append("aws")
        with Session(pg_engine) as other:  # 앱과 다른 연결
            # 앱이 users 잠금(EXCLUSIVE)이나 테넌트 행 잠금을 쥐고 있으면 NOWAIT 이 즉시 실패한다
            other.execute(text("LOCK TABLE app_users IN EXCLUSIVE MODE NOWAIT"))
            other.execute(
                select(models.Tenant.tenant_id)
                .where(models.Tenant.tenant_id == tenant_id)
                .with_for_update(nowait=True)
            ).all()
            probe["idle_in_transaction"] = other.scalar(text(
                "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                "AND pid <> pg_backend_pid() AND state LIKE 'idle in transaction%'"))
            other.rollback()

    committed_aws.during = during

    resp = _post(committed_client, route, tenant_id)

    assert resp.status_code == 200, resp.text
    assert probe == {"idle_in_transaction": 0}
    assert events == ["aws", "share"]  # 테넌트 행 잠금은 AWS 뒤, 쓰기 직전에만 잡는다
    body = TenantVerifyResponse.model_validate(resp.json())
    stored = _fresh(committed_client, tenant_id)  # 별도 세션에서 읽는다 — commit 된 값만 보인다
    assert stored[:2] == (TenantStatus.ACTIVE, R.OK)
    assert stored[2].replace(microsecond=0) == body.verification.verified_at
