"""플랫폼 계정 수명주기 CLI — botocore Stubber(실 AWS 호출 없음) + 실제 PostgreSQL.

등급(#492): 새 직원 · UNASSIGNED 지정은 담당 고객사 배정이 없는 TENANT_MANAGER, 중앙 관리자는 ACTIVE 중앙
관리자가 0명일 때만 명시 지정한다(기존 배정은 비운다). CLI 는 배정을 만들지 않는다.
"""

from __future__ import annotations

import io
import threading
import uuid
from datetime import UTC, datetime, timedelta

import boto3
import pytest
from auth import bootstrap_admin
from botocore.stub import Stubber
from config import AuthSettings
from db import models
from db.repositories import auth as auth_repo
from schemas.api.auth import AccountType, PlatformRole, UserRole
from sqlalchemy import text
from sqlalchemy.orm import Session

POOL = "ap-northeast-2_pool"
ISSUER = f"https://cognito-idp.ap-northeast-2.amazonaws.com/{POOL}"
EMAIL = "admin@example.com"
SUB = "11111111-2222-3333-4444-555555555555"
CLIENT_SECRET = "client-secret-zzz-9"
NOW = datetime(2026, 10, 19, 9, 0, tzinfo=UTC)
SETTINGS = AuthSettings(
    _env_file=None,
    COGNITO_REGION="ap-northeast-2",
    COGNITO_USER_POOL_ID=POOL,
    COGNITO_CLIENT_ID="client-id",
    COGNITO_CLIENT_SECRET=CLIENT_SECRET,
)


@pytest.fixture()
def cognito_client():
    client = boto3.client(
        "cognito-idp",
        region_name="ap-northeast-2",
        aws_access_key_id="test",
        aws_secret_access_key="test",
    )
    with Stubber(client) as stubber:
        client.stubber = stubber
        yield client
        stubber.assert_no_pending_responses()


def run(db, client, *argv):
    out = io.StringIO()
    code = bootstrap_admin.run(
        list(argv), db=db, settings=SETTINGS, client=client, now=lambda: NOW, out=out
    )
    return code, out.getvalue()


def users(db):
    return db.execute(
        text(
            "SELECT issuer, cognito_sub, email, role::text, account_type::text, tenant_id::text,"
            " platform_role::text FROM app_users"
        )
    ).all()


def seed_user(db, *, sub="other", email="x@example.com", role=UserRole.VIEWER,
              account_type=AccountType.UNASSIGNED, tenant_id=None,
              status=models.AppUserStatus.ACTIVE, platform_role=None, issuer=ISSUER):
    row = auth_repo.create_user(
        db, issuer=issuer, cognito_sub=sub, email=email, role=role,
        account_type=account_type, tenant_id=tenant_id, now=NOW, status=status,
        platform_role=platform_role,
    )
    db.commit()
    return row


def seed_platform(db, *, sub=SUB, email=EMAIL, platform_role=None, **kwargs):
    """플랫폼 계정 — platform_role 을 주지 않으면 운영 기본값과 같은 배정 없는 TENANT_MANAGER 다."""
    return seed_user(db, sub=sub, email=email, role=UserRole.ADMIN,
                     account_type=AccountType.PLATFORM, platform_role=platform_role, **kwargs)


def seed_tenant(db):
    tenant = models.Tenant(
        name="고객", aws_account_id="111111111111",
        access_role_arn="arn:aws:iam::111111111111:role/vigilantis-test",
        external_id=f"external-{uuid.uuid4()}", regions=["ap-northeast-2"],
    )
    db.add(tenant)
    db.commit()
    return tenant.tenant_id


def assignments(db):
    return db.execute(
        text("SELECT user_id::text, tenant_id::text FROM app_user_tenant_assignments ORDER BY 1, 2")
    ).all()


def seed_session(db, user, token="a"):
    db.add(models.AuthSession(
        token_hash=token * 64, user_id=user.user_id,
        mfa_step=models.AuthSessionMfaStep.SOFTWARE_TOKEN_MFA,
        created_at=NOW, last_activity_at=NOW, absolute_expires_at=NOW + timedelta(hours=8),
    ))
    db.commit()


def create_response(sub=SUB):
    return {
        "User": {
            "Username": EMAIL,
            "UserStatus": "FORCE_CHANGE_PASSWORD",
            "Attributes": [{"Name": "sub", "Value": sub}],
        }
    }


def get_response(sub=SUB):
    return {
        "Username": EMAIL,
        "UserStatus": "CONFIRMED",
        "UserAttributes": [{"Name": "sub", "Value": sub}],
    }


def test_preview_changes_nothing_and_calls_nothing(db, cognito_client):
    code, out = run(db, cognito_client, "--platform", "--email", EMAIL)
    assert code == 0
    assert "미리보기" in out
    assert users(db) == []  # Stubber 에 응답을 넣지 않았으므로 Cognito 호출이 있었다면 실패한다


@pytest.mark.parametrize("argv", [["--email", EMAIL], ["--email", EMAIL, "--yes"],
                                  ["--existing-cognito-user", "--email", EMAIL, "--yes"],
                                  ["--platform", "--yes"]])
def test_create_without_platform_flag_or_email_is_refused_with_zero_sdk_calls(db, cognito_client, argv):
    code, out = run(db, cognito_client, *argv)
    assert code == 2
    assert "--platform" in out or "--email" in out
    assert users(db) == []  # 스텁 응답 없음 → SDK 호출이 있었다면 실패한다


def test_yes_creates_cognito_user_and_admin_row(db, cognito_client):
    cognito_client.stubber.add_response(
        "admin_create_user",
        {
            "User": {
                "Username": EMAIL,
                "UserStatus": "FORCE_CHANGE_PASSWORD",
                "Attributes": [
                    {"Name": "sub", "Value": SUB},
                    {"Name": "email", "Value": EMAIL},
                ],
            }
        },
        {
            "UserPoolId": POOL,
            "Username": EMAIL,
            "UserAttributes": [{"Name": "email", "Value": EMAIL}],
            "DesiredDeliveryMediums": ["EMAIL"],
        },
    )
    code, out = run(db, cognito_client, "--platform", "--email", EMAIL, "--yes")
    assert code == 0
    # 새 직원은 배정 없는 TENANT_MANAGER(고객사 범위 없음) — role=ADMIN 은 호환 값일 뿐이다
    assert users(db) == [(ISSUER, SUB, EMAIL, "ADMIN", "PLATFORM", None, "TENANT_MANAGER")]
    assert assignments(db) == []
    row = auth_repo.get_user_by_identity(db, issuer=ISSUER, cognito_sub=SUB)
    assert f"user_id={row.user_id}" in out and "cognito_status=FORCE_CHANGE_PASSWORD" in out
    assert "platform_role=TENANT_MANAGER" in out
    for secret in (CLIENT_SECRET, "assword", "oken"):
        assert secret not in out


def test_additional_platform_account_is_allowed_when_users_exist(db, cognito_client):
    tenant_id = seed_tenant(db)
    seed_platform(db, sub="first", email="first@example.com", platform_role=PlatformRole.CENTRAL_ADMIN)
    seed_user(db, sub="member", email="m@example.com", role=UserRole.OPERATOR,
              account_type=AccountType.TENANT, tenant_id=tenant_id)
    cognito_client.stubber.add_response("admin_create_user", create_response())
    code, _ = run(db, cognito_client, "--platform", "--email", EMAIL, "--yes")
    assert code == 0
    assert (ISSUER, SUB, EMAIL, "ADMIN", "PLATFORM", None, "TENANT_MANAGER") in users(db)
    assert len(users(db)) == 3


def test_existing_identity_is_never_transferred_to_platform(db, cognito_client):
    tenant_id = seed_tenant(db)
    seed_user(db, sub=SUB, email=EMAIL, role=UserRole.OPERATOR,
              account_type=AccountType.TENANT, tenant_id=tenant_id)
    cognito_client.stubber.add_response("admin_get_user", get_response())
    code, out = run(db, cognito_client, "--platform", "--existing-cognito-user", "--email", EMAIL, "--yes")
    assert code == 2 and "이미 app_users 에 있다" in out
    assert users(db) == [(ISSUER, SUB, EMAIL, "OPERATOR", "TENANT", tenant_id, None)]


def test_designate_preview_changes_nothing_and_calls_nothing(db, cognito_client):
    target = seed_user(db)
    code, out = run(db, cognito_client, "--designate-unassigned-user-id", target.user_id)
    assert code == 0 and "미리보기" in out
    assert users(db)[0][3:] == ("VIEWER", "UNASSIGNED", None, None)


def test_designate_unassigned_makes_unassigned_manager_revokes_sessions_with_zero_sdk_calls(
    db, cognito_client
):
    target = seed_user(db, role=UserRole.ADMIN)  # 소속 없는 옛 ADMIN 도 배정 없는 담당자일 뿐이다
    seed_session(db, target)
    code, out = run(db, cognito_client, "--designate-unassigned-user-id", target.user_id, "--yes")
    assert code == 0 and "revoked_sessions=1" in out and "platform_role=TENANT_MANAGER" in out
    assert users(db)[0][3:] == ("ADMIN", "PLATFORM", None, "TENANT_MANAGER")
    assert assignments(db) == []
    assert auth_repo.count_active_central_admins(db) == 0
    reasons = db.execute(text("SELECT revoke_reason::text, revoked_at FROM auth_sessions")).all()
    assert reasons == [("ADMIN_REVOKED", NOW)]
    assert CLIENT_SECRET not in out


@pytest.mark.parametrize("case", ["tenant", "disabled", "platform", "missing", "bad-id"])
def test_designate_rejects_ineligible_with_zero_sdk_calls_and_no_change(db, cognito_client, case):
    seed = {
        "tenant": lambda: seed_user(db, role=UserRole.OPERATOR, account_type=AccountType.TENANT,
                                    tenant_id=seed_tenant(db)),
        "disabled": lambda: seed_user(db, status=models.AppUserStatus.DISABLED),
        "platform": lambda: seed_platform(db),
    }
    target_id = {"missing": str(uuid.uuid4()), "bad-id": "not-a-uuid"}.get(case)
    if target_id is None:
        target = seed[case]()
        seed_session(db, target)
        target_id = target.user_id
    before = users(db)
    code, out = run(db, cognito_client, "--designate-unassigned-user-id", target_id, "--yes")
    assert code == 2 and "거절" in out
    assert users(db) == before
    revoked = db.execute(text("SELECT count(*) FROM auth_sessions WHERE revoked_at IS NOT NULL")).scalar()
    assert revoked == 0
    if case == "disabled":
        assert "ACTIVE" in out
        assert db.execute(text("SELECT status::text FROM app_users")).scalar() == "DISABLED"


def test_designate_does_not_accept_email_platform_or_grade_flags(db, cognito_client):
    target = seed_user(db)
    for extra in (["--email", EMAIL], ["--platform"], ["--existing-cognito-user"], ["--central-admin"],
                  ["--bootstrap-central-admin-user-id", target.user_id]):
        code, _ = run(db, cognito_client, "--designate-unassigned-user-id", target.user_id, "--yes", *extra)
        assert code == 2
    assert users(db)[0][4:] == ("UNASSIGNED", None, None)


def test_existing_cognito_user_links_by_sub_from_admin_get_user(db, cognito_client):
    cognito_client.stubber.add_response(
        "admin_get_user",
        {
            "Username": EMAIL,
            "UserStatus": "CONFIRMED",
            "UserAttributes": [
                {"Name": "sub", "Value": SUB},
                {"Name": "email", "Value": EMAIL},
            ],
        },
        {"UserPoolId": POOL, "Username": EMAIL},
    )
    code, out = run(db, cognito_client, "--platform", "--existing-cognito-user", "--email", EMAIL, "--yes")
    assert code == 0
    assert users(db) == [(ISSUER, SUB, EMAIL, "ADMIN", "PLATFORM", None, "TENANT_MANAGER")]
    assert "cognito_status=CONFIRMED" in out
    assert CLIENT_SECRET not in out


@pytest.mark.parametrize("existing", [False, True])
def test_no_db_transaction_is_open_during_cognito_call(db, cognito_client, existing):
    seen = []
    method = "admin_get_user" if existing else "admin_create_user"
    original = getattr(cognito_client, method)

    def spy(**kwargs):
        seen.append(db.in_transaction())
        return original(**kwargs)

    setattr(cognito_client, method, spy)
    if existing:
        cognito_client.stubber.add_response(
            method,
            {
                "Username": EMAIL,
                "UserStatus": "CONFIRMED",
                "UserAttributes": [{"Name": "sub", "Value": SUB}],
            },
        )
    else:
        cognito_client.stubber.add_response(
            method,
            {
                "User": {
                    "Username": EMAIL,
                    "UserStatus": "FORCE_CHANGE_PASSWORD",
                    "Attributes": [{"Name": "sub", "Value": SUB}],
                }
            },
        )
    argv = ["--platform", "--email", EMAIL, "--yes"] + (["--existing-cognito-user"] if existing else [])
    code, _ = run(db, cognito_client, *argv)
    assert code == 0
    assert seen == [False]


def test_cognito_error_creates_no_row_and_prints_only_the_code(db, cognito_client):
    cognito_client.stubber.add_client_error(
        "admin_create_user",
        service_error_code="UsernameExistsException",
        service_message="secret detail that must not be printed",
    )
    code, out = run(db, cognito_client, "--platform", "--email", EMAIL, "--yes")
    assert code == 1
    assert "UsernameExistsException" in out and "--existing-cognito-user" in out
    assert "secret detail" not in out
    assert users(db) == []


def test_missing_sub_is_rejected(db, cognito_client):
    cognito_client.stubber.add_response(
        "admin_get_user",
        {"Username": EMAIL, "UserStatus": "CONFIRMED", "UserAttributes": []},
        {"UserPoolId": POOL, "Username": EMAIL},
    )
    code, _ = run(db, cognito_client, "--platform", "--existing-cognito-user", "--email", EMAIL, "--yes")
    assert code == 1
    assert users(db) == []


# --- 중앙 관리자 명시 지정 — ACTIVE 중앙 관리자가 0명일 때만 -------------------------------------


@pytest.mark.parametrize("existing", [None, models.AppUserStatus.DISABLED], ids=["first", "all-disabled"])
def test_create_central_admin_when_no_active_central_admin_exists(db, cognito_client, existing):
    if existing is not None:  # 전원 비활성 뒤 복구도 같은 경로다
        seed_platform(db, sub="old", email="old@example.com",
                      platform_role=PlatformRole.CENTRAL_ADMIN, status=existing)
    cognito_client.stubber.add_response("admin_create_user", create_response())
    code, out = run(db, cognito_client, "--platform", "--central-admin", "--email", EMAIL, "--yes")
    assert code == 0 and "platform_role=CENTRAL_ADMIN" in out
    assert (ISSUER, SUB, EMAIL, "ADMIN", "PLATFORM", None, "CENTRAL_ADMIN") in users(db)
    assert auth_repo.count_active_central_admins(db) == 1


@pytest.mark.parametrize("yes", [False, True], ids=["preview", "apply"])
def test_create_central_admin_is_refused_with_zero_sdk_calls_when_one_is_active(db, cognito_client, yes):
    seed_platform(db, sub="first", email="first@example.com", platform_role=PlatformRole.CENTRAL_ADMIN)
    before = users(db)
    argv = ["--platform", "--central-admin", "--email", EMAIL] + (["--yes"] if yes else [])
    code, out = run(db, cognito_client, *argv)
    assert code == 2 and "/api/v1/staff" in out
    assert users(db) == before  # 스텁 응답 없음 → SDK 호출이 있었다면 실패한다


def test_bootstrap_existing_manager_to_central_admin_clears_its_assignments(db, cognito_client):
    """중앙 관리자 0명(전원 비활성 등)에서 지정한 담당자만 올리고, 그 담당자의 배정만 비운다."""
    tenant_id = seed_tenant(db)
    target = seed_platform(db)  # 운영 기본값과 같은 TENANT_MANAGER
    other = seed_platform(db, sub="other-manager", email="o@example.com")
    for row in (target, other):
        auth_repo.replace_assigned_tenants(db, user_id=row.user_id, tenant_ids=(tenant_id,), now=NOW)
    db.commit()
    seed_session(db, target)
    code, out = run(db, cognito_client, "--bootstrap-central-admin-user-id", target.user_id, "--email", EMAIL)
    assert code == 0 and "미리보기" in out  # 스텁 응답 없음 — 미리보기는 SDK 를 부르지 않는다
    assert auth_repo.count_active_central_admins(db) == 0
    assert len(assignments(db)) == 2
    cognito_client.stubber.add_response(
        "admin_get_user", get_response(), {"UserPoolId": POOL, "Username": EMAIL}
    )
    code, out = run(db, cognito_client, "--bootstrap-central-admin-user-id", target.user_id,
                    "--email", EMAIL, "--yes")
    assert code == 0
    assert f"user_id={target.user_id} platform_role=CENTRAL_ADMIN cleared_assignments=1" in out
    assert {row[1]: row[3:] for row in users(db)} == {
        SUB: ("ADMIN", "PLATFORM", None, "CENTRAL_ADMIN"),
        "other-manager": ("ADMIN", "PLATFORM", None, "TENANT_MANAGER"),  # 다른 계정은 그대로
    }
    # 중앙 관리자에게 예전 배정을 남기지 않는다(뒤에 담당자로 내려도 되살아나지 않게) — 다른 담당자의 배정은 그대로
    assert assignments(db) == [(other.user_id, tenant_id)]
    # 세션은 그대로 — 판정이 매 요청 현재 등급을 읽는다
    alive = db.execute(text("SELECT count(*) FROM auth_sessions WHERE revoked_at IS NULL")).scalar()
    assert alive == 1
    assert CLIENT_SECRET not in out


@pytest.mark.parametrize("case", [
    "tenant", "unassigned", "disabled", "already-central", "other-active-central",
    "foreign-issuer", "missing", "bad-id", "no-email",
])
def test_bootstrap_refuses_ineligible_target_with_zero_sdk_calls_and_no_change(db, cognito_client, case):
    def other_active_central():
        seed_platform(db, sub="first", email="first@example.com", platform_role=PlatformRole.CENTRAL_ADMIN)
        return seed_platform(db)

    seeds = {
        # 고객사 · 미지정 계정은 계정 종류 · 소속을 바꿔 중앙 관리자로 만들지 않는다
        "tenant": lambda: seed_user(db, sub=SUB, email=EMAIL, role=UserRole.ADMIN,
                                    account_type=AccountType.TENANT, tenant_id=seed_tenant(db)),
        "unassigned": lambda: seed_user(db, sub=SUB, email=EMAIL, role=UserRole.ADMIN),
        "disabled": lambda: seed_platform(db, status=models.AppUserStatus.DISABLED),
        "already-central": lambda: seed_platform(db, platform_role=PlatformRole.CENTRAL_ADMIN),
        "other-active-central": other_active_central,
        "foreign-issuer": lambda: seed_platform(db, issuer=ISSUER + "-other"),
        "no-email": lambda: seed_platform(db),
    }
    target_id = {"missing": str(uuid.uuid4()), "bad-id": "not-a-uuid"}.get(case)
    if target_id is None:
        target_id = seeds[case]().user_id
    before = users(db)
    argv = ["--bootstrap-central-admin-user-id", target_id, "--yes"]
    if case != "no-email":
        argv += ["--email", EMAIL]
    code, out = run(db, cognito_client, *argv)
    assert code == 2 and "거절" in out
    assert users(db) == before  # 스텁 응답 없음 → SDK 호출이 있었다면 실패한다


def test_bootstrap_does_not_accept_creation_flags(db, cognito_client):
    target = seed_platform(db)
    for extra in (["--platform"], ["--existing-cognito-user"], ["--central-admin"]):
        code, _ = run(db, cognito_client, "--bootstrap-central-admin-user-id", target.user_id,
                      "--email", EMAIL, "--yes", *extra)
        assert code == 2
    assert users(db)[0][6] == "TENANT_MANAGER"


def test_bootstrap_refuses_when_cognito_sub_is_not_the_accounts_identity(db, cognito_client):
    target = seed_platform(db)
    cognito_client.stubber.add_response(
        "admin_get_user", get_response(sub="someone-else"), {"UserPoolId": POOL, "Username": EMAIL}
    )
    code, out = run(db, cognito_client, "--bootstrap-central-admin-user-id", target.user_id,
                    "--email", EMAIL, "--yes")
    assert code == 2 and "sub" in out
    assert users(db)[0][6] == "TENANT_MANAGER"


def test_bootstrap_cognito_error_changes_nothing_and_prints_only_the_code(db, cognito_client):
    target = seed_platform(db)
    cognito_client.stubber.add_client_error(
        "admin_get_user",
        service_error_code="UserNotFoundException",
        service_message="secret detail that must not be printed",
    )
    code, out = run(db, cognito_client, "--bootstrap-central-admin-user-id", target.user_id,
                    "--email", EMAIL, "--yes")
    assert code == 1 and "UserNotFoundException" in out
    assert "secret detail" not in out
    assert users(db)[0][6] == "TENANT_MANAGER"


@pytest.mark.parametrize("mode", ["create-central", "bootstrap"])
def test_no_db_transaction_is_open_during_central_admin_cognito_call(db, cognito_client, mode):
    if mode == "bootstrap":
        target = seed_platform(db)
        method, response = "admin_get_user", get_response()
        argv = ["--bootstrap-central-admin-user-id", target.user_id, "--email", EMAIL, "--yes"]
    else:
        method, response = "admin_create_user", create_response()
        argv = ["--platform", "--central-admin", "--email", EMAIL, "--yes"]
    seen = []
    original = getattr(cognito_client, method)

    def spy(**kwargs):
        seen.append(db.in_transaction())
        return original(**kwargs)

    setattr(cognito_client, method, spy)
    cognito_client.stubber.add_response(method, response)
    code, _ = run(db, cognito_client, *argv)
    assert code == 0
    assert seen == [False]


# --- 동시성: 실제 커밋 · 별도 연결. 테스트 소유 issuer 의 행만 지운다 ---------------------------

SENTINEL_ISSUER = "https://sentinel.invalid/unrelated-pool"


@pytest.fixture()
def committed(pg_engine):
    # 무관한 사용자 · 세션 하나를 먼저 심어 두고, 정리가 그것을 건드리지 않았는지 확인한다
    with Session(pg_engine) as s:
        sentinel = auth_repo.create_user(
            s, issuer=SENTINEL_ISSUER, cognito_sub="sentinel", email="s@example.com",
            role=UserRole.VIEWER, account_type=AccountType.UNASSIGNED, tenant_id=None, now=NOW,
        )
        seed_session(s, sentinel, token="s")
        sentinel_id = sentinel.user_id
    try:
        yield pg_engine
    finally:
        owned = "SELECT user_id FROM app_users WHERE issuer = :i"
        with Session(pg_engine) as s:
            s.execute(text(f"DELETE FROM auth_sessions WHERE user_id IN ({owned})"), {"i": ISSUER})
            s.execute(text("DELETE FROM app_users WHERE issuer = :i"), {"i": ISSUER})
            s.commit()
            preserved = s.execute(
                text("SELECT count(*) FROM auth_sessions WHERE user_id = :u"), {"u": sentinel_id}
            ).scalar()
            s.execute(text("DELETE FROM auth_sessions WHERE user_id = :u"), {"u": sentinel_id})
            s.execute(text("DELETE FROM app_users WHERE user_id = :u"), {"u": sentinel_id})
            s.commit()
        assert preserved == 1


def _run_until_lock(engine, client, monkeypatch, *argv):
    """CLI 를 스레드에서 돌리고, 그 스레드가 lock_users_table 에 닿는 순간(사전 점검이 끝난 뒤)까지 기다린다.

    호출자는 이미 잠금을 쥐고 있어야 한다 — 그래야 스레드가 잠금에서 막힌다.
    """
    reached = threading.Event()
    original = auth_repo.lock_users_table
    result = {}

    def target():
        with Session(engine) as s:
            result["code"], result["out"] = run(s, client, *argv)

    thread = threading.Thread(target=target)

    def wrapped(db):
        if threading.current_thread() is thread:
            reached.set()  # 사전 점검은 이 호출 이전에 끝났다
        return original(db)

    monkeypatch.setattr(auth_repo, "lock_users_table", wrapped)
    thread.start()
    assert reached.wait(10)
    return thread, result


def test_designation_rechecks_under_lock_and_refuses_if_disabled_meanwhile(
    committed, cognito_client, monkeypatch
):
    with Session(committed) as s:
        target_id = seed_user(s).user_id
    with Session(committed) as holder:
        auth_repo.lock_users_table(holder)
        thread, result = _run_until_lock(
            committed, cognito_client, monkeypatch,
            "--designate-unassigned-user-id", target_id, "--yes",
        )
        auth_repo.set_status(holder, user_id=target_id, status=models.AppUserStatus.DISABLED, now=NOW)
        holder.commit()
    thread.join(10)
    assert not thread.is_alive()
    assert result["code"] == 2 and "그 사이" in result["out"]
    with Session(committed) as s:
        row = auth_repo.get_user(s, target_id)
        assert (row.account_type, row.status) == (AccountType.UNASSIGNED, models.AppUserStatus.DISABLED)


def test_concurrent_create_of_same_identity_creates_one_account(committed, cognito_client, monkeypatch):
    cognito_client.stubber.add_response("admin_get_user", get_response())
    with Session(committed) as holder:
        auth_repo.lock_users_table(holder)
        thread, result = _run_until_lock(
            committed, cognito_client, monkeypatch,
            "--platform", "--existing-cognito-user", "--email", EMAIL, "--yes",
        )
        seed_user(holder, sub=SUB, email=EMAIL)  # 다른 실행이 먼저 같은 신원을 만들고 커밋
    thread.join(10)
    assert not thread.is_alive()
    assert result["code"] == 2
    with Session(committed) as s:
        owned = [row for row in users(s) if row[0] == ISSUER]
        assert owned == [(ISSUER, SUB, EMAIL, "VIEWER", "UNASSIGNED", None, None)]


def test_central_admin_bootstrap_rechecks_under_lock_and_refuses_if_one_appeared(
    committed, cognito_client, monkeypatch
):
    with Session(committed) as s:
        target_id = seed_platform(s).user_id
        rival_id = seed_platform(s, sub="rival", email="r@example.com").user_id
    cognito_client.stubber.add_response("admin_get_user", get_response())
    with Session(committed) as holder:
        auth_repo.lock_users_table(holder)
        thread, result = _run_until_lock(
            committed, cognito_client, monkeypatch,
            "--bootstrap-central-admin-user-id", target_id, "--email", EMAIL, "--yes",
        )
        # 사전 확인(0명) 뒤, 잠금을 기다리는 사이 다른 중앙 관리자가 생겼다
        assert auth_repo.set_platform_role(
            holder, user_id=rival_id, platform_role=PlatformRole.CENTRAL_ADMIN, now=NOW
        )
        holder.commit()
    thread.join(10)
    assert not thread.is_alive()
    assert result["code"] == 2 and "/api/v1/staff" in result["out"]
    with Session(committed) as s:
        assert auth_repo.get_user(s, target_id).platform_role is PlatformRole.TENANT_MANAGER
        assert auth_repo.get_user(s, rival_id).platform_role is PlatformRole.CENTRAL_ADMIN


def test_create_central_admin_rechecks_under_lock_and_refuses_if_one_appeared(
    committed, cognito_client, monkeypatch
):
    with Session(committed) as s:
        rival_id = seed_platform(s, sub="rival", email="r@example.com").user_id
    cognito_client.stubber.add_response("admin_create_user", create_response())
    with Session(committed) as holder:
        auth_repo.lock_users_table(holder)
        thread, result = _run_until_lock(
            committed, cognito_client, monkeypatch,
            "--platform", "--central-admin", "--email", EMAIL, "--yes",
        )
        assert auth_repo.set_platform_role(
            holder, user_id=rival_id, platform_role=PlatformRole.CENTRAL_ADMIN, now=NOW
        )
        holder.commit()
    thread.join(10)
    assert not thread.is_alive()
    assert result["code"] == 2 and "--existing-cognito-user" in result["out"]
    with Session(committed) as s:
        assert auth_repo.get_user_by_identity(s, issuer=ISSUER, cognito_sub=SUB) is None
