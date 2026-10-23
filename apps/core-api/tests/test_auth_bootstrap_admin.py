"""플랫폼 계정 수명주기 CLI — botocore Stubber(실 AWS 호출 없음) + 실제 PostgreSQL."""

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
from schemas.api.auth import AccountType, UserRole
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
            "SELECT issuer, cognito_sub, email, role::text, account_type::text, tenant_id::text"
            " FROM app_users"
        )
    ).all()


def seed_user(db, *, sub="other", email="x@example.com", role=UserRole.VIEWER,
              account_type=AccountType.UNASSIGNED, tenant_id=None,
              status=models.AppUserStatus.ACTIVE):
    row = auth_repo.create_user(
        db, issuer=ISSUER, cognito_sub=sub, email=email, role=role,
        account_type=account_type, tenant_id=tenant_id, now=NOW, status=status,
    )
    db.commit()
    return row


def seed_tenant(db):
    tenant = models.Tenant(
        name="고객", aws_account_id="111111111111",
        access_role_arn="arn:aws:iam::111111111111:role/vigilantis-test",
        external_id=f"external-{uuid.uuid4()}", regions=["ap-northeast-2"],
    )
    db.add(tenant)
    db.commit()
    return tenant.tenant_id


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
    assert users(db) == [(ISSUER, SUB, EMAIL, "ADMIN", "PLATFORM", None)]
    row = auth_repo.get_user_by_identity(db, issuer=ISSUER, cognito_sub=SUB)
    assert f"user_id={row.user_id}" in out and "cognito_status=FORCE_CHANGE_PASSWORD" in out
    for secret in (CLIENT_SECRET, "assword", "oken"):
        assert secret not in out


def test_additional_platform_account_is_allowed_when_users_exist(db, cognito_client):
    tenant_id = seed_tenant(db)
    seed_user(db, sub="first", email="first@example.com", role=UserRole.ADMIN,
              account_type=AccountType.PLATFORM)
    seed_user(db, sub="member", email="m@example.com", role=UserRole.OPERATOR,
              account_type=AccountType.TENANT, tenant_id=tenant_id)
    cognito_client.stubber.add_response("admin_create_user", create_response())
    code, _ = run(db, cognito_client, "--platform", "--email", EMAIL, "--yes")
    assert code == 0
    assert (ISSUER, SUB, EMAIL, "ADMIN", "PLATFORM", None) in users(db)
    assert len(users(db)) == 3


def test_existing_identity_is_never_transferred_to_platform(db, cognito_client):
    tenant_id = seed_tenant(db)
    seed_user(db, sub=SUB, email=EMAIL, role=UserRole.OPERATOR,
              account_type=AccountType.TENANT, tenant_id=tenant_id)
    cognito_client.stubber.add_response("admin_get_user", get_response())
    code, out = run(db, cognito_client, "--platform", "--existing-cognito-user", "--email", EMAIL, "--yes")
    assert code == 2 and "이미 app_users 에 있다" in out
    assert users(db) == [(ISSUER, SUB, EMAIL, "OPERATOR", "TENANT", tenant_id)]


def test_designate_preview_changes_nothing_and_calls_nothing(db, cognito_client):
    target = seed_user(db)
    code, out = run(db, cognito_client, "--designate-unassigned-user-id", target.user_id)
    assert code == 0 and "미리보기" in out
    assert users(db)[0][3:] == ("VIEWER", "UNASSIGNED", None)


def test_designate_unassigned_promotes_revokes_sessions_with_zero_sdk_calls(db, cognito_client):
    target = seed_user(db)
    seed_session(db, target)
    code, out = run(db, cognito_client, "--designate-unassigned-user-id", target.user_id, "--yes")
    assert code == 0 and "revoked_sessions=1" in out
    assert users(db)[0][3:] == ("ADMIN", "PLATFORM", None)
    reasons = db.execute(text("SELECT revoke_reason::text, revoked_at FROM auth_sessions")).all()
    assert reasons == [("ADMIN_REVOKED", NOW)]
    assert CLIENT_SECRET not in out


@pytest.mark.parametrize("case", ["tenant", "disabled", "platform", "missing", "bad-id"])
def test_designate_rejects_ineligible_with_zero_sdk_calls_and_no_change(db, cognito_client, case):
    seed = {
        "tenant": lambda: seed_user(db, role=UserRole.OPERATOR, account_type=AccountType.TENANT,
                                    tenant_id=seed_tenant(db)),
        "disabled": lambda: seed_user(db, status=models.AppUserStatus.DISABLED),
        "platform": lambda: seed_user(db, role=UserRole.ADMIN, account_type=AccountType.PLATFORM),
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


def test_designate_does_not_accept_email_or_platform_flags(db, cognito_client):
    target = seed_user(db)
    for extra in (["--email", EMAIL], ["--platform"], ["--existing-cognito-user"]):
        code, _ = run(db, cognito_client, "--designate-unassigned-user-id", target.user_id, "--yes", *extra)
        assert code == 2
    assert users(db)[0][4] == "UNASSIGNED"


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
    assert users(db) == [(ISSUER, SUB, EMAIL, "ADMIN", "PLATFORM", None)]
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
        assert owned == [(ISSUER, SUB, EMAIL, "VIEWER", "UNASSIGNED", None)]
