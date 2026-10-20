"""최초 관리자 부트스트랩 CLI — botocore Stubber(실 AWS 호출 없음) + 실제 PostgreSQL."""

from __future__ import annotations

import io
from datetime import UTC, datetime

import boto3
import pytest
from auth import bootstrap_admin
from botocore.stub import Stubber
from config import AuthSettings
from db.repositories import auth as auth_repo
from schemas.api.auth import UserRole
from sqlalchemy import text

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
    return db.execute(text("SELECT issuer, cognito_sub, email, role::text FROM app_users")).all()


def test_preview_changes_nothing_and_calls_nothing(db, cognito_client):
    code, out = run(db, cognito_client, "--email", EMAIL)
    assert code == 0
    assert "미리보기" in out
    assert users(db) == []  # Stubber 에 응답을 넣지 않았으므로 Cognito 호출이 있었다면 실패한다


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
    code, out = run(db, cognito_client, "--email", EMAIL, "--yes")
    assert code == 0
    assert users(db) == [(ISSUER, SUB, EMAIL, "ADMIN")]
    row = auth_repo.get_user_by_identity(db, issuer=ISSUER, cognito_sub=SUB)
    assert f"user_id={row.user_id}" in out and "cognito_status=FORCE_CHANGE_PASSWORD" in out
    for secret in (CLIENT_SECRET, "assword", "oken"):
        assert secret not in out


def test_refuses_when_any_user_exists(db, cognito_client):
    auth_repo.create_user(
        db, issuer=ISSUER, cognito_sub="other", email="x@example.com", role=UserRole.VIEWER, now=NOW
    )
    db.commit()
    for argv in (["--email", EMAIL], ["--email", EMAIL, "--yes"]):
        code, out = run(db, cognito_client, *argv)  # Cognito 호출 없음(스텁 응답 없음)
        assert code == 2
        assert "이미 사용자가 있다" in out
    assert len(users(db)) == 1


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
    code, out = run(db, cognito_client, "--existing-cognito-user", "--email", EMAIL, "--yes")
    assert code == 0
    assert users(db) == [(ISSUER, SUB, EMAIL, "ADMIN")]
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
    argv = ["--email", EMAIL, "--yes"] + (["--existing-cognito-user"] if existing else [])
    code, _ = run(db, cognito_client, *argv)
    assert code == 0
    assert seen == [False]


def test_cognito_error_creates_no_row_and_prints_only_the_code(db, cognito_client):
    cognito_client.stubber.add_client_error(
        "admin_create_user",
        service_error_code="UsernameExistsException",
        service_message="secret detail that must not be printed",
    )
    code, out = run(db, cognito_client, "--email", EMAIL, "--yes")
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
    code, _ = run(db, cognito_client, "--existing-cognito-user", "--email", EMAIL, "--yes")
    assert code == 1
    assert users(db) == []
