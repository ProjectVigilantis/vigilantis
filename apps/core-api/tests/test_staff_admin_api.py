"""플랫폼 직원 관리 API (#492 PR 5 · #572 1번) — 중앙 관리자 전용, 대상은 PLATFORM 뿐, 자기 변경 · 마지막 중앙 관리자 보호.

(#572 1번) 직원 이메일 초대 · 재발송: 가짜 Cognito 어댑터(SDK 호출 기록)로 Cognito 호출 순서 · 중복 409 · 부분 실패를
고정하고, 만든 계정이 운영 CLI(bootstrap_admin --platform)의 결과와 같은지 실제 CLI 실행 결과와 대조한다.
"""

from __future__ import annotations

import io
import logging
from datetime import UTC, datetime

import boto3
import pytest
from auth import bootstrap_admin
from auth.cognito import (
    AuthProviderUnavailableError,
    AuthRateLimitedError,
    CognitoUser,
    UserStateConflictError,
)
from botocore.stub import Stubber
from config import AuthSettings
from db import models
from db.repositories import auth as auth_repo
from schemas.api.auth import PlatformRole, UserRole
from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError, SQLAlchemyError
from tests.test_user_admin_api import FORCE, ISSUER, FakeAdapter, use_adapter

BASE = "/api/v1/staff"
CENTRAL = PlatformRole.CENTRAL_ADMIN
MANAGER = PlatformRole.TENANT_MANAGER
MISSING = "00000000-0000-4000-8000-000000000000"
NEW_EMAIL = "new-staff@example.com"
ITEM_KEYS = {"user_id", "email", "platform_role", "status", "last_login_at"}


@pytest.fixture()
def tenant(make_tenant) -> str:
    return make_tenant()


@pytest.fixture(autouse=True)
def central_session(login_as):
    """기본 platform_session 은 TENANT_MANAGER 다 — 이 파일의 행위자는 명시적으로 CENTRAL_ADMIN 으로 로그인한다."""
    return login_as(UserRole.ADMIN, platform_role=CENTRAL)


@pytest.fixture(autouse=True)
def adapter(client_pg):
    """모든 테스트에 가짜 Cognito 어댑터를 건다 — 부르지 말아야 할 경로가 불렀으면 503 대신 calls 에 남는다."""
    fake = FakeAdapter()
    fake.create_result = CognitoUser(NEW_EMAIL, "sub-staff-new", NEW_EMAIL, FORCE)
    use_adapter(client_pg, fake)
    return fake


def invite(client_pg, email=NEW_EMAIL):
    return client_pg.post(BASE, json={"email": email})


def resend(client_pg, user_id):
    return client_pg.post(f"{BASE}/{user_id}/invite/resend")


def user_row(db, user_id):
    db.expire_all()
    return db.get(models.AppUser, user_id)


def user_count(db):
    return db.execute(text("SELECT count(*) FROM app_users")).scalar_one()


def staff(make_auth_session, platform_role=MANAGER):
    return make_auth_session(UserRole.ADMIN, platform_role=platform_role)


def assigned(db, user_id):
    from db.repositories import auth as auth_repo

    return sorted(auth_repo.list_assigned_tenant_ids(db, user_id=user_id))


def assign(client_pg, user_id, ids):
    return client_pg.put(f"{BASE}/{user_id}/tenants", json={"tenant_ids": ids})


def events(db, target_id):
    rows = db.scalars(
        select(models.UserAdminEvent)
        .where(models.UserAdminEvent.target_user_id == target_id)
        .order_by(models.UserAdminEvent.occurred_at, models.UserAdminEvent.event_id)
    ).all()
    return [(r.action.value, r.before_state, r.after_state) for r in rows]


def db_state(db, user_id):
    return db.execute(
        text("SELECT platform_role::text, status::text FROM app_users WHERE user_id = :u"),
        {"u": user_id},
    ).one()


REQUESTS = [
    ("GET", "", None),
    ("POST", "", {"email": NEW_EMAIL}),
    ("POST", "", {"email": "bad"}),
    ("POST", "/{id}/invite/resend", None),
    ("GET", "/{id}/events", None),
    ("PUT", "/{id}/role", {"platform_role": "CENTRAL_ADMIN"}),
    ("PUT", "/{id}/role", {"platform_role": "NOPE"}),
    ("PUT", "/{id}/status", {"status": "DISABLED"}),
    ("PUT", "/{id}/status", {}),
    ("GET", "/{id}/tenants", None),
    ("PUT", "/{id}/tenants", {"tenant_ids": []}),
    ("PUT", "/{id}/tenants", {"tenant_ids": ["bad"]}),
]


@pytest.mark.parametrize("actor_role", [MANAGER])
@pytest.mark.parametrize("method,path,body", REQUESTS)
def test_non_central_platform_actor_is_forbidden(
    client_pg, db, login_as, make_auth_session, adapter, actor_role, method, path, body
):
    target = staff(make_auth_session)
    login_as(UserRole.ADMIN, platform_role=actor_role)
    before, count = db_state(db, target.user_id), user_count(db)
    resp = client_pg.request(method, BASE + path.replace("{id}", target.user_id), json=body)
    assert resp.status_code == 403
    assert (db_state(db, target.user_id), user_count(db)) == (before, count)
    assert adapter.calls == []  # 권한 판정이 본문 검증 · Cognito 보다 먼저다


@pytest.mark.parametrize("method,path,body", REQUESTS)
def test_tenant_admin_is_forbidden(
    client_pg, db, login_as, make_auth_session, tenant, adapter, method, path, body
):
    target = staff(make_auth_session)
    login_as(UserRole.ADMIN, tenant_id=tenant)
    count = user_count(db)
    resp = client_pg.request(method, BASE + path.replace("{id}", target.user_id), json=body)
    assert resp.status_code == 403
    assert (user_count(db), adapter.calls) == (count, [])


def test_unauthenticated_is_401(client_pg, db, make_auth_session, adapter):
    target = staff(make_auth_session)
    anonymous = {"Cookie": ""}
    assert client_pg.get(BASE, headers=anonymous).status_code == 401
    assert client_pg.post(BASE, json={"email": NEW_EMAIL}, headers=anonymous).status_code == 401
    assert resend_as(client_pg, target.user_id, anonymous).status_code == 401
    assert adapter.calls == []


def resend_as(client_pg, user_id, headers):
    return client_pg.post(f"{BASE}/{user_id}/invite/resend", headers=headers)


def test_central_lists_only_platform_accounts(client_pg, db, make_auth_session, tenant):
    member = staff(make_auth_session, MANAGER)
    tenant_user = make_auth_session(UserRole.VIEWER, tenant_id=tenant)
    resp = client_pg.get(BASE)
    assert resp.status_code == 200
    items = {i["user_id"]: i for i in resp.json()["items"]}
    assert items[member.user_id]["platform_role"] == "TENANT_MANAGER"
    assert set(items[member.user_id]) == {"user_id", "email", "platform_role", "status", "last_login_at"}
    assert tenant_user.user_id not in items


def test_central_changes_role_and_records_event(client_pg, db, make_auth_session):
    target = staff(make_auth_session)
    for role in (CENTRAL, MANAGER):
        resp = client_pg.put(f"{BASE}/{target.user_id}/role", json={"platform_role": role.value})
        assert resp.status_code == 204
        assert db_state(db, target.user_id)[0] == role.value
    log = events(db, target.user_id)
    assert [e[0] for e in log] == ["PLATFORM_ROLE_CHANGED"] * 2
    assert log[0][1] == {"platform_role": "TENANT_MANAGER"}
    assert log[0][2] == {"platform_role": "CENTRAL_ADMIN"}


def test_same_role_is_409(client_pg, make_auth_session):
    target = staff(make_auth_session)
    resp = client_pg.put(f"{BASE}/{target.user_id}/role", json={"platform_role": "TENANT_MANAGER"})
    assert resp.status_code == 409


def test_staff_role_is_removed(client_pg, make_auth_session):
    target = staff(make_auth_session)
    assert client_pg.put(f"{BASE}/{target.user_id}/role", json={"platform_role": "STAFF"}).status_code == 422
    assert client_pg.put(f"{BASE}/{target.user_id}/role", json={"platform_role": "VIEWER"}).status_code == 422


def test_cannot_change_own_role_or_status(client_pg, db, central_session):
    me = central_session.user_id
    assert client_pg.put(f"{BASE}/{me}/role", json={"platform_role": "TENANT_MANAGER"}).status_code == 403
    assert client_pg.put(f"{BASE}/{me}/status", json={"status": "DISABLED"}).status_code == 403
    assert db_state(db, me) == ("CENTRAL_ADMIN", "ACTIVE")


def test_central_can_demote_and_disable_another_central(client_pg, db, make_auth_session):
    """복수 중앙 관리자 — 행위자(활성 중앙)가 남으므로 다른 중앙 관리자는 내릴 수 있다."""
    other = staff(make_auth_session, CENTRAL)
    assert client_pg.put(f"{BASE}/{other.user_id}/status", json={"status": "DISABLED"}).status_code == 204
    assert client_pg.put(f"{BASE}/{other.user_id}/role", json={"platform_role": "TENANT_MANAGER"}).status_code == 204
    assert db_state(db, other.user_id) == ("TENANT_MANAGER", "DISABLED")


def test_disable_revokes_sessions_and_management_immediately(client_pg, db, make_auth_session, tenant):
    target = staff(make_auth_session, MANAGER)
    users = "/api/v1/users"
    assert client_pg.get(users, headers=target.headers).status_code == 200
    resp = client_pg.put(f"{BASE}/{target.user_id}/status", json={"status": "DISABLED"})
    assert resp.status_code == 204
    assert db_state(db, target.user_id)[1] == "DISABLED"
    assert client_pg.get(users, headers=target.headers).status_code == 401
    assert events(db, target.user_id)[-1][0] == "STATUS_CHANGED"
    assert client_pg.put(f"{BASE}/{target.user_id}/status", json={"status": "ACTIVE"}).status_code == 204


def test_manager_cannot_use_staff_powers_even_with_assignment(client_pg, db, make_auth_session, tenant):
    target = staff(make_auth_session)
    assert assign(client_pg, target.user_id, [tenant]).status_code == 204
    resp = client_pg.get(BASE, headers=target.headers)
    assert resp.status_code == 403
    resp = client_pg.put(
        f"{BASE}/{target.user_id}/tenants", json={"tenant_ids": []}, headers=target.headers
    )
    assert resp.status_code == 403
    assert assigned(db, target.user_id) == [tenant]


def test_assign_get_replace_and_clear(client_pg, db, make_auth_session, make_tenant):
    target = staff(make_auth_session)
    a, b, c = make_tenant(), make_tenant(), make_tenant()
    assert client_pg.get(f"{BASE}/{target.user_id}/tenants").json() == {"tenant_ids": []}
    assert assign(client_pg, target.user_id, [b, a]).status_code == 204
    assert client_pg.get(f"{BASE}/{target.user_id}/tenants").json() == {"tenant_ids": sorted([a, b])}
    assert assign(client_pg, target.user_id, [c, a]).status_code == 204
    assert assigned(db, target.user_id) == sorted([a, c])
    assert assign(client_pg, target.user_id, []).status_code == 204
    assert client_pg.get(f"{BASE}/{target.user_id}/tenants").json() == {"tenant_ids": []}
    assert assigned(db, target.user_id) == []
    log = events(db, target.user_id)
    assert [e[0] for e in log] == ["TENANT_ASSIGNMENTS_CHANGED"] * 3
    assert log[0][1] == {"tenant_ids": []}
    assert log[0][2] == {"tenant_ids": sorted([a, b])}
    assert log[2][1] == {"tenant_ids": sorted([a, c])}
    assert log[2][2] == {"tenant_ids": []}


def test_assignment_event_failure_rolls_back_replacement(
    client_pg, db, make_auth_session, make_tenant, monkeypatch
):
    from db.repositories import auth as auth_repo
    from sqlalchemy.exc import SQLAlchemyError

    target = staff(make_auth_session)
    kept, other = make_tenant(), make_tenant()
    assert assign(client_pg, target.user_id, [kept]).status_code == 204
    events_before = len(events(db, target.user_id))
    replaced = []
    real_replace = auth_repo.replace_assigned_tenants

    def spy_replace(*args, **kwargs):
        real_replace(*args, **kwargs)
        replaced.append(kwargs["tenant_ids"])

    def boom(*args, **kwargs):
        raise SQLAlchemyError("secret-detail postgres://user:pw@host")

    with monkeypatch.context() as patch:
        patch.setattr(auth_repo, "replace_assigned_tenants", spy_replace)
        patch.setattr(auth_repo, "add_user_event", boom)
        resp = assign(client_pg, target.user_id, [other])
    assert replaced == [(other,)]  # 교체가 일어난 뒤 이력 추가에서 실패했다
    assert resp.status_code == 500
    assert resp.json()["error"]["code"] == "INTERNAL_ERROR"
    assert "secret-detail" not in resp.text
    db.expire_all()
    assert assigned(db, target.user_id) == [kept]
    assert len(events(db, target.user_id)) == events_before


def test_same_assignment_is_409_without_event(client_pg, db, make_auth_session, tenant):
    target = staff(make_auth_session)
    assert assign(client_pg, target.user_id, []).status_code == 409
    assert assign(client_pg, target.user_id, [tenant]).status_code == 204
    assert assign(client_pg, target.user_id, [tenant]).status_code == 409
    assert len(events(db, target.user_id)) == 1


def test_duplicate_unknown_and_malformed_ids_change_nothing(client_pg, db, make_auth_session, tenant):
    target = staff(make_auth_session)
    assert assign(client_pg, target.user_id, [tenant]).status_code == 204
    assert assign(client_pg, target.user_id, [tenant, tenant]).status_code == 422
    assert assign(client_pg, target.user_id, [tenant.upper(), tenant]).status_code == 422
    assert assign(client_pg, target.user_id, ["not-a-uuid"]).status_code == 422
    resp = client_pg.put(f"{BASE}/{target.user_id}/tenants", json={"tenant_ids": [], "extra": 1})
    assert resp.status_code == 422
    resp = assign(client_pg, target.user_id, [tenant, MISSING])
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "TENANT_NOT_FOUND"
    assert assigned(db, target.user_id) == [tenant]
    assert len(events(db, target.user_id)) == 1


def test_assignment_allowed_for_suspended_customer_without_changing_accounts(
    client_pg, db, make_auth_session, make_tenant
):
    from db.models import TenantStatus

    suspended = make_tenant(TenantStatus.SUSPENDED)
    target = staff(make_auth_session)
    before = db.execute(
        text("SELECT account_type::text, tenant_id FROM app_users WHERE user_id = :u"),
        {"u": target.user_id},
    ).one()
    assert assign(client_pg, target.user_id, [suspended]).status_code == 204
    assert assigned(db, target.user_id) == [suspended]
    assert db.execute(
        text("SELECT account_type::text, tenant_id FROM app_users WHERE user_id = :u"),
        {"u": target.user_id},
    ).one() == before


def test_central_and_non_platform_targets_cannot_be_assigned(client_pg, db, make_auth_session, tenant):
    central = staff(make_auth_session, CENTRAL)
    assert assign(client_pg, central.user_id, [tenant]).status_code == 409
    assert assigned(db, central.user_id) == []
    customer = make_auth_session(UserRole.VIEWER, tenant_id=tenant)
    for uid in (customer.user_id, MISSING, "not-a-uuid"):
        for resp in (assign(client_pg, uid, [tenant]), client_pg.get(f"{BASE}/{uid}/tenants")):
            assert resp.status_code == 404
            assert resp.json()["error"]["code"] == "USER_NOT_FOUND"
    assert assigned(db, customer.user_id) == []


def test_role_change_clears_assignments_and_never_resurrects(client_pg, db, make_auth_session, tenant):
    target = staff(make_auth_session)
    assert assign(client_pg, target.user_id, [tenant]).status_code == 204
    assert client_pg.put(f"{BASE}/{target.user_id}/role", json={"platform_role": "CENTRAL_ADMIN"}).status_code == 204
    assert assigned(db, target.user_id) == []
    assert client_pg.put(f"{BASE}/{target.user_id}/role", json={"platform_role": "TENANT_MANAGER"}).status_code == 204
    assert client_pg.get(f"{BASE}/{target.user_id}/tenants").json() == {"tenant_ids": []}
    log = events(db, target.user_id)
    assert sorted(e[0] for e in log) == ["PLATFORM_ROLE_CHANGED"] * 2 + ["TENANT_ASSIGNMENTS_CHANGED"] * 2
    cleared = [e for e in log if e[0] == "TENANT_ASSIGNMENTS_CHANGED" and e[1] == {"tenant_ids": [tenant]}]
    assert [e[2] for e in cleared] == [{"tenant_ids": []}]


def test_assignment_by_demoted_actor_is_rejected_and_writes_nothing(
    client_pg, db, login_as, make_auth_session, tenant
):
    target = staff(make_auth_session)
    actor = login_as(UserRole.ADMIN, platform_role=CENTRAL)
    db.execute(
        text("UPDATE app_users SET platform_role = 'TENANT_MANAGER' WHERE user_id = :u"),
        {"u": actor.user_id},
    )
    assert assign(client_pg, target.user_id, [tenant]).status_code == 403
    assert assigned(db, target.user_id) == []
    assert events(db, target.user_id) == []


@pytest.mark.parametrize("kind", ["tenant", "unknown", "malformed"])
def test_non_platform_or_missing_targets_are_opaque_404(client_pg, db, make_auth_session, tenant, kind):
    ids = {
        "tenant": make_auth_session(UserRole.VIEWER, tenant_id=tenant).user_id,
        "unknown": "00000000-0000-4000-8000-000000000000",
        "malformed": "not-a-uuid",
    }
    uid = ids[kind]
    for method, path, body in (
        ("PUT", "/role", {"platform_role": "CENTRAL_ADMIN"}),
        ("PUT", "/status", {"status": "DISABLED"}),
        ("GET", "/events", None),
    ):
        resp = client_pg.request(method, f"{BASE}/{uid}{path}", json=body)
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "USER_NOT_FOUND"


def test_customer_users_endpoints_never_reach_platform_targets(client_pg, make_auth_session):
    target = staff(make_auth_session)
    for method, path, body in (
        ("PUT", "/role", {"role": "VIEWER"}),
        ("PUT", "/status", {"status": "DISABLED"}),
        ("POST", "/sessions/revoke", None),
        ("GET", "/events", None),
    ):
        assert client_pg.request(method, f"/api/v1/users/{target.user_id}{path}", json=body).status_code == 404


def test_staff_schemas_are_closed_and_validate_enums():
    from datetime import UTC, datetime

    from pydantic import ValidationError
    from schemas.api.staff import (
        StaffItem,
        StaffResponse,
        StaffRoleRequest,
        StaffStatusRequest,
        StaffTenantsRequest,
    )

    for bad in ({"tenant_ids": [MISSING, MISSING]}, {"tenant_ids": ["x"]}, {"tenant_ids": [], "a": 1}, {}):
        with pytest.raises(ValidationError):
            StaffTenantsRequest(**bad)
    assert StaffTenantsRequest(tenant_ids=[]).tenant_ids == []
    assert StaffRoleRequest(platform_role="TENANT_MANAGER").platform_role is MANAGER
    assert StaffStatusRequest(status="DISABLED").status.value == "DISABLED"
    for bad in ({"platform_role": "STAFF"}, {"platform_role": "TENANT_MANAGER", "role": "ADMIN"}, {}):
        with pytest.raises(ValidationError):
            StaffRoleRequest(**bad)
    for bad in ({"status": "PENDING"}, {"status": "ACTIVE", "extra": 1}, {}):
        with pytest.raises(ValidationError):
            StaffStatusRequest(**bad)
    item = {
        "user_id": "u1", "email": "a@example.com", "platform_role": "TENANT_MANAGER",
        "status": "ACTIVE", "last_login_at": datetime.now(UTC),
    }
    assert StaffResponse(items=[item]).items[0].platform_role is MANAGER
    for bad in ({**item, "tenant_id": None}, {**item, "platform_role": "NOPE"}, {**item, "email": "bad"}):
        with pytest.raises(ValidationError):
            StaffItem(**bad)


def test_invite_request_schema_is_closed():
    from pydantic import ValidationError
    from schemas.api.staff import StaffInviteRequest

    assert StaffInviteRequest(email=NEW_EMAIL).email == NEW_EMAIL
    for bad in (
        {},
        {"email": "bad"},
        {"email": NEW_EMAIL, "platform_role": "CENTRAL_ADMIN"},
        {"email": NEW_EMAIL, "tenant_id": MISSING},
        {"email": NEW_EMAIL, "recover_existing": True},
    ):
        with pytest.raises(ValidationError):
            StaffInviteRequest(**bad)


# --- 초대 · 재발송 (#572 1번) -------------------------------------------------------


CLI_POOL = "ap-northeast-2_cli"
CLI_SETTINGS = AuthSettings(
    _env_file=None,
    COGNITO_REGION="ap-northeast-2",
    COGNITO_USER_POOL_ID=CLI_POOL,
    COGNITO_CLIENT_ID="client-id",
    COGNITO_CLIENT_SECRET="client-secret-zzz-9",
)


def cli_create_staff(db, email, sub):
    """운영 CLI 로 직원 1명을 만든다(botocore Stubber — 실 AWS 호출 없음). 비교 기준이 되는 행을 돌려준다."""
    client = boto3.client(
        "cognito-idp", region_name="ap-northeast-2", aws_access_key_id="test", aws_secret_access_key="test"
    )
    with Stubber(client) as stubber:
        stubber.add_response(
            "admin_create_user",
            {"User": {"Username": email, "UserStatus": FORCE, "Attributes": [{"Name": "sub", "Value": sub}]}},
            {
                "UserPoolId": CLI_POOL,
                "Username": email,
                "UserAttributes": [{"Name": "email", "Value": email}],
                "DesiredDeliveryMediums": ["EMAIL"],
            },
        )
        code = bootstrap_admin.run(
            ["--platform", "--email", email, "--yes"],
            db=db, settings=CLI_SETTINGS, client=client, out=io.StringIO(),
        )
        stubber.assert_no_pending_responses()
    assert code == 0
    return auth_repo.get_user_by_identity(db, issuer=bootstrap_admin._issuer(CLI_SETTINGS), cognito_sub=sub)


def account_shape(row):
    """CLI 와 API 가 같아야 하는 칸 — 신원(issuer · sub · email)과 시각은 뺀다."""
    return (
        row.role, row.account_type, row.platform_role, row.tenant_id, row.status, row.email_verified_at
    )


def test_central_invites_staff_as_cli_does_and_records_event(client_pg, db, central_session, adapter):
    resp = invite(client_pg)
    assert resp.status_code == 201
    body = resp.json()
    assert set(body) == ITEM_KEYS
    assert (body["email"], body["platform_role"], body["status"], body["last_login_at"]) == (
        NEW_EMAIL, "TENANT_MANAGER", "ACTIVE", None,
    )
    assert adapter.calls == [("create", NEW_EMAIL, False)]  # 자동 연결(get) 없음

    row = user_row(db, body["user_id"])
    assert (row.issuer, row.cognito_sub, row.email) == (ISSUER, "sub-staff-new", NEW_EMAIL)
    assert assigned(db, row.user_id) == []
    reference = cli_create_staff(db, "cli-staff@example.com", "sub-cli")
    assert account_shape(row) == account_shape(reference)
    assert (row.account_type.value, row.platform_role.value, row.tenant_id) == ("PLATFORM", "TENANT_MANAGER", None)

    assert events(db, row.user_id) == [
        ("USER_INVITED", {}, {"platform_role": "TENANT_MANAGER", "status": "ACTIVE"})
    ]
    listed = client_pg.get(f"{BASE}/{row.user_id}/events").json()["items"]
    assert [(e["action"], e["actor_user_id"], e["target_user_id"]) for e in listed] == [
        ("USER_INVITED", central_session.user_id, row.user_id)
    ]
    assert body in client_pg.get(BASE).json()["items"]
    # 등급은 기존 경로로 바꾼다 — 초대 본문에는 등급 칸이 없다
    assert client_pg.put(f"{BASE}/{row.user_id}/role", json={"platform_role": "CENTRAL_ADMIN"}).status_code == 204


def test_invite_existing_cognito_email_is_409_without_db_write(client_pg, db, adapter):
    adapter.create_error = UserStateConflictError()
    count = user_count(db)
    resp = invite(client_pg, "customer-admin@example.com")
    assert (resp.status_code, resp.json()["error"]["code"]) == (409, "USER_STATE_CONFLICT")
    assert adapter.calls == [("create", "customer-admin@example.com", False)]  # 자동 연결(get) 없음
    assert user_count(db) == count


def test_invite_existing_app_identity_is_409_without_db_write(client_pg, db, make_auth_session, adapter):
    existing = staff(make_auth_session)
    row = user_row(db, existing.user_id)
    adapter.create_result = CognitoUser(row.email, row.cognito_sub, row.email, FORCE)
    count = user_count(db)
    resp = invite(client_pg, row.email)
    assert (resp.status_code, resp.json()["error"]["code"]) == (409, "USER_STATE_CONFLICT")
    assert (user_count(db), events(db, existing.user_id)) == (count, [])
    # 그 sub 는 다른 앱 계정의 로그인 신원이다 — 보상 삭제하지 않는다
    assert adapter.calls == [("create", row.email, False)]


@pytest.mark.parametrize("error,status", [(AuthProviderUnavailableError, 503), (AuthRateLimitedError, 429)])
def test_invite_provider_failure_leaves_no_row(client_pg, db, adapter, error, status):
    adapter.create_error = error()
    count = user_count(db)
    assert invite(client_pg).status_code == status
    assert user_count(db) == count
    assert adapter.calls == [("create", NEW_EMAIL, False)]  # 만들지 않았다(sub 모름) — 보상 삭제 없음


def test_invite_db_failure_is_sanitized_500_and_discards_created_cognito_user(client_pg, db, adapter, monkeypatch):
    def boom(*args, **kwargs):
        raise SQLAlchemyError("secret-detail postgres://user:pw@host")

    monkeypatch.setattr(auth_repo, "add_user_event", boom)
    open_at_delete = []
    original_delete = adapter.admin_delete_user

    def delete_spy(username):
        open_at_delete.append(db.in_transaction())
        return original_delete(username)

    adapter.admin_delete_user = delete_spy
    count = user_count(db)
    resp = invite(client_pg)
    assert (resp.status_code, resp.json()["error"]["code"]) == (500, "INTERNAL_ERROR")
    assert "secret-detail" not in resp.text
    # 만든 사용자를 sub 로 지운다(이메일이 아니다) — 신원 확인 트랜잭션은 Cognito 호출 전에 닫혔다
    assert adapter.calls == [("create", NEW_EMAIL, False), ("delete", "sub-staff-new")]
    assert open_at_delete == [False]
    assert user_count(db) == count


def test_invite_does_not_discard_when_commit_actually_landed(client_pg, db, adapter, monkeypatch):
    """commit 이 반영된 뒤 연결 오류로 500 이 된 경우 — (issuer, sub) 의 앱 계정이 있으므로 Cognito 사용자를 지우지 않는다."""
    real_commit = db.commit
    lost = []

    def commit_then_lose_connection():
        real_commit()
        if not lost and ("create", NEW_EMAIL, False) in adapter.calls:
            lost.append(True)
            raise OperationalError("COMMIT", {}, Exception("server closed the connection"))

    monkeypatch.setattr(db, "commit", commit_then_lose_connection)
    resp = invite(client_pg)
    assert (resp.status_code, resp.json()["error"]["code"]) == (500, "INTERNAL_ERROR")
    assert lost == [True]
    assert adapter.calls == [("create", NEW_EMAIL, False)]
    assert auth_repo.get_user_by_identity(db, issuer=ISSUER, cognito_sub="sub-staff-new") is not None


def test_invite_discard_failure_keeps_500_and_logs_only_sub_and_code(client_pg, db, adapter, monkeypatch, caplog):
    def boom(*args, **kwargs):
        raise SQLAlchemyError("secret-detail")

    monkeypatch.setattr(auth_repo, "add_user_event", boom)
    failure = AuthProviderUnavailableError()
    failure.provider_code = "InternalErrorException"
    adapter.delete_error = failure
    count = user_count(db)
    with caplog.at_level(logging.WARNING, logger="auth.staff_service"):
        resp = invite(client_pg)
    assert (resp.status_code, resp.json()["error"]["code"]) == (500, "INTERNAL_ERROR")
    assert adapter.calls == [("create", NEW_EMAIL, False), ("delete", "sub-staff-new")]
    assert user_count(db) == count
    expected = "staff invite compensation failed sub=sub-staff-new error_type=AuthProviderUnavailableError error_code=InternalErrorException"
    assert [(r.levelno, r.getMessage()) for r in caplog.records if r.name == "auth.staff_service"] == [
        (logging.WARNING, expected)
    ]


def demote_during_create(db, central_session, adapter):
    """Cognito 생성 중에 행위자가 강등된다 — 잠금 안 재판정이 403 으로 끝난다."""
    original = adapter.admin_create_user

    def demote_then_create(email, *, resend=False):
        auth_repo.set_platform_role(
            db, user_id=central_session.user_id, platform_role=MANAGER, now=datetime.now(UTC)
        )
        db.commit()
        return original(email, resend=resend)

    adapter.admin_create_user = demote_then_create


def test_invite_by_demoted_actor_after_cognito_writes_nothing(client_pg, db, central_session, adapter):
    """SDK 뒤 잠금 안 재판정 — Cognito 는 이미 불렸지만 강등된 행위자는 403 이고 DB 는 쓰지 않는다.
    만든 Cognito 사용자는 보상 삭제한다."""
    demote_during_create(db, central_session, adapter)
    count = user_count(db)
    assert invite(client_pg).status_code == 403
    assert (user_count(db), adapter.calls) == (
        count, [("create", NEW_EMAIL, False), ("delete", "sub-staff-new")]
    )


def test_invite_unclassified_discard_failure_keeps_the_original_403(client_pg, db, central_session, adapter, caplog):
    """보상 삭제의 분류되지 않은 실패(SDK 응답 디코딩 오류 등)도 원래 실패를 바꾸지 않는다 — 경고 1줄만 남는다."""
    demote_during_create(db, central_session, adapter)
    adapter.delete_error = RuntimeError("PRIVATE-TEXT from a broken provider body")
    count = user_count(db)
    with caplog.at_level(logging.WARNING, logger="auth.staff_service"):
        resp = invite(client_pg)
    assert (resp.status_code, resp.json()["error"]["code"]) == (403, "AUTH_FORBIDDEN")
    assert (user_count(db), adapter.calls) == (
        count, [("create", NEW_EMAIL, False), ("delete", "sub-staff-new")]
    )
    assert [(r.levelno, r.getMessage()) for r in caplog.records if r.name == "auth.staff_service"] == [
        (logging.WARNING, "staff invite compensation failed sub=sub-staff-new error_type=RuntimeError error_code=None")
    ]
    assert "PRIVATE-TEXT" not in caplog.text


def test_central_resends_staff_invite_and_records_event(client_pg, db, make_auth_session, adapter):
    target = staff(make_auth_session)
    row = user_row(db, target.user_id)
    adapter.get_result = CognitoUser(row.email, row.cognito_sub, row.email, FORCE)
    assert resend(client_pg, target.user_id).status_code == 204
    assert adapter.calls == [("get", row.email), ("create", row.email, True)]
    assert events(db, target.user_id) == [("INVITE_RESENT", {}, {})]


@pytest.mark.parametrize("state", ["confirmed", "other_sub", "missing"])
def test_resend_without_pending_invite_is_409_without_resend(client_pg, db, make_auth_session, adapter, state):
    target = staff(make_auth_session)
    row = user_row(db, target.user_id)
    adapter.get_result = {
        "confirmed": CognitoUser(row.email, row.cognito_sub, row.email, "CONFIRMED"),
        "other_sub": CognitoUser(row.email, "someone-else", row.email, FORCE),
        "missing": None,
    }[state]
    resp = resend(client_pg, target.user_id)
    assert (resp.status_code, resp.json()["error"]["code"]) == (409, "USER_STATE_CONFLICT")
    assert adapter.calls == [("get", row.email)]
    assert events(db, target.user_id) == []


def test_resend_for_non_platform_or_missing_target_is_404_without_cognito(
    client_pg, make_auth_session, tenant, adapter
):
    customer = make_auth_session(UserRole.ADMIN, tenant_id=tenant)
    for user_id in (customer.user_id, MISSING, "not-a-uuid"):
        resp = resend(client_pg, user_id)
        assert (resp.status_code, resp.json()["error"]["code"]) == (404, "USER_NOT_FOUND")
    assert adapter.calls == []


def test_customer_invite_route_does_not_create_platform_accounts(client_pg, db, adapter):
    """`POST /api/v1/users` 는 PLATFORM 을 만들지 않는다 — tenant_id 없는 플랫폼 행위자 요청은 422(Cognito 미호출)."""
    count = user_count(db)
    resp = client_pg.post("/api/v1/users", json={"email": NEW_EMAIL, "role": "ADMIN"})
    assert resp.status_code == 422
    assert (user_count(db), adapter.calls) == (count, [])
