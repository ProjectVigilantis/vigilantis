"""POST /api/v1/auth/platform-enrollment (#492) — 실제 PostgreSQL + 가짜 Cognito 어댑터(실 AWS 호출 없음).

지키는 것: Origin → 기능 스위치 → 본문 → 링크 판정이 어댑터 조립 · Cognito 설정 · SDK 보다 먼저, 쓸 수 없는 링크는
사유와 관계없이 같은 404, 기존 세션 쿠키는 권한 근거가 아님, 성공은 쿠키 · 본문 없는 204 + PLATFORM ADMIN 1명,
예약 commit 실패면 SDK 0회, 예약 뒤 모든 실패에서 예약 유지 · 재시도 없음, 제공자 성공 뒤 commit 전 DB 실패 · 회수 ·
만료는 앱 계정 없이 끝남, 확정 COMMIT 반영 뒤 응답 유실은 500 + 결과 미확정 로그(계정 · REDEEMED 가 남을 수 있음),
같은 링크 동시 사용은 계정 1개 · SDK 1회, 로그에 토큰 · 해시 · 이메일 없음.
"""

from __future__ import annotations

import logging
import secrets
import threading
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from auth import enrollment as enrollment_service
from auth import service
from auth.cognito import (
    AuthEmailDeliveryFailedError,
    AuthFailedError,
    AuthProviderUnavailableError,
    AuthRateLimitedError,
    CognitoUser,
    UserStateConflictError,
)
from auth.http import get_clock
from auth.login_flow import MfaStep, VerifiedIdentity
from auth.sessions import hash_token
from config import AuthSettings, get_auth_settings
from db import models
from db.repositories import auth as auth_repo
from db.repositories import enrollment as enrollment_repo
from exceptions import ApiError
from routers.users import get_cognito_adapter_factory
from schemas.api.auth import AccountType, UserRole
from schemas.api.errors import ErrorCode
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker

URL = "/api/v1/auth/platform-enrollment"
ORIGIN = "https://app.example.com"
ISSUER = "https://cognito-idp.ap-northeast-2.amazonaws.com/ap-northeast-2_enroll"
EMAIL = "owner@example.com"
T0 = datetime(2026, 10, 23, 9, 0, tzinfo=UTC)
EXPIRES = T0 + timedelta(hours=72)
S = models.PlatformEnrollmentStatus


def settings(**over) -> AuthSettings:
    values = {"AUTH_ALLOWED_ORIGINS": ORIGIN, "AUTH_PLATFORM_ENROLLMENT_ENABLED": True, **over}
    return AuthSettings(_env_file=None, **values)


class FakeAdapter:
    """CognitoAdapter 대역 — AdminCreateUser 호출만 기록한다. during 은 호출 중에 끼워 넣을 동작이다."""

    issuer = ISSUER

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.error: Exception | None = None
        self.during = None
        self.sub = f"sub-{uuid.uuid4().hex}"
        self._lock = threading.Lock()

    def admin_create_user(self, email, *, resend=False):
        assert resend is False
        with self._lock:
            self.calls.append(email)
        if self.during is not None:
            self.during()
        if self.error is not None:
            raise self.error
        return CognitoUser(email, self.sub, email, "FORCE_CHANGE_PASSWORD")


class Clock:
    def __init__(self) -> None:
        self.now = T0


@pytest.fixture()
def adapter():
    return FakeAdapter()


@pytest.fixture()
def clock():
    return Clock()


@pytest.fixture()
def built():
    """어댑터 조립 횟수 — 0 이면 Cognito 설정 · SDK 어느 쪽에도 닿지 않았다."""
    return []


@pytest.fixture()
def http(client_pg_anon, adapter, clock, built):
    app = client_pg_anon.app
    app.dependency_overrides[get_auth_settings] = lambda: settings()
    app.dependency_overrides[get_clock] = lambda: (lambda: clock.now)

    def factory():
        def build():
            built.append(1)
            return adapter

        return build

    app.dependency_overrides[get_cognito_adapter_factory] = factory
    return client_pg_anon


def issue(session, *, expires_at=EXPIRES) -> tuple[str, str]:
    token = secrets.token_urlsafe(32)
    row = enrollment_repo.add(
        session, enrollment_id=str(uuid.uuid4()), token_hash=hash_token(token),
        now=expires_at - timedelta(days=30), expires_at=expires_at,
    )
    session.commit()
    return token, row.enrollment_id


def post(http, token, email=EMAIL, *, origin=ORIGIN, **headers):
    hdrs = {} if origin is None else {"Origin": origin}
    return http.post(URL, json={"token": token, "email": email}, headers={**hdrs, **headers})


def error(resp) -> tuple[int, str]:
    return resp.status_code, resp.json()["error"]["code"]


def link(db, enrollment_id) -> models.PlatformEnrollment:
    return enrollment_repo.get(db, enrollment_id)


def app_users(db) -> list:
    return db.execute(
        text("SELECT user_id::text, issuer, cognito_sub, email, role::text, account_type::text,"
             " tenant_id, status::text, email_verified_at FROM app_users")
    ).all()


# ----- 관문 순서 -----------------------------------------------------------------


def test_disabled_by_default_rejects_before_body_link_and_provider(http, db, adapter, built):
    assert AuthSettings(_env_file=None).AUTH_PLATFORM_ENROLLMENT_ENABLED is False
    http.app.dependency_overrides[get_auth_settings] = lambda: AuthSettings(
        _env_file=None, AUTH_ALLOWED_ORIGINS=ORIGIN
    )
    token, enrollment_id = issue(db)

    valid = post(http, token)
    malformed = http.post(URL, json={"role": "ADMIN"}, headers={"Origin": ORIGIN})
    assert error(valid) == error(malformed) == (404, "ENROLLMENT_UNAVAILABLE")
    assert valid.json()["error"]["message"] == malformed.json()["error"]["message"]
    assert (built, adapter.calls) == ([], [])
    assert link(db, enrollment_id).status is S.ISSUED


@pytest.mark.parametrize("origin", [None, "https://evil.example.com", "null"])
@pytest.mark.parametrize("enabled", [True, False])
def test_origin_is_checked_before_switch_body_and_link(http, db, adapter, built, origin, enabled):
    http.app.dependency_overrides[get_auth_settings] = lambda: settings(
        AUTH_PLATFORM_ENROLLMENT_ENABLED=enabled
    )
    token, enrollment_id = issue(db)
    assert error(post(http, token, origin=origin)) == (403, "AUTH_ORIGIN_REJECTED")
    assert (built, adapter.calls) == ([], [])
    assert link(db, enrollment_id).status is S.ISSUED


@pytest.mark.parametrize(
    "extra",
    [{"role": "ADMIN"}, {"account_type": "PLATFORM"}, {"tenant_id": None}, {"user_id": "x"}],
)
def test_authority_fields_are_rejected_without_touching_the_link(http, db, adapter, built, extra):
    token, enrollment_id = issue(db)
    resp = http.post(URL, json={"token": token, "email": EMAIL, **extra}, headers={"Origin": ORIGIN})
    assert error(resp) == (422, "REQUEST_VALIDATION_FAILED")
    assert token not in resp.text
    assert (built, adapter.calls) == ([], [])
    assert link(db, enrollment_id).status is S.ISSUED
    assert app_users(db) == []


@pytest.mark.parametrize(
    "body",
    [{}, {"email": EMAIL}, {"token": "t" * 20}, {"token": "t" * 20, "email": "nope"},
     {"token": "t" * 513, "email": EMAIL}, {"token": "", "email": EMAIL}],
)
def test_malformed_body_is_422_without_provider(http, adapter, built, body):
    resp = http.post(URL, json=body, headers={"Origin": ORIGIN})
    assert error(resp) == (422, "REQUEST_VALIDATION_FAILED")
    assert "t" * 20 not in resp.text
    assert (built, adapter.calls) == ([], [])


def _unusable(db, kind) -> str:
    """kind 상태의 링크 토큰. 사유는 응답으로 구분되지 않아야 한다."""
    if kind == "unknown":
        return secrets.token_urlsafe(32)
    if kind == "expired":
        return issue(db, expires_at=T0)[0]
    token, enrollment_id = issue(db)
    if kind in ("revoked", "reserved", "redeemed"):
        enrollment_repo.reserve(db, token_hash=hash_token(token), email="x@example.com", now=T0)
    if kind == "revoked":
        enrollment_repo.revoke(db, enrollment_id=enrollment_id, now=T0)
    if kind == "redeemed":
        user = auth_repo.create_user(
            db, issuer=ISSUER, cognito_sub=f"old-{uuid.uuid4().hex}", email="x@example.com",
            role=UserRole.ADMIN, account_type=AccountType.PLATFORM, tenant_id=None, now=T0,
        )
        row = enrollment_repo.lock(db, enrollment_id)
        row.status, row.redeemed_user_id, row.redeemed_at = S.REDEEMED, user.user_id, T0
    db.commit()
    return token


@pytest.mark.parametrize("kind", ["unknown", "expired", "revoked", "reserved", "redeemed"])
def test_unusable_links_are_one_404_before_cognito_config_and_sdk(
    http, db, make_auth_session, adapter, built, kind
):
    # Cognito 미설정 + 실제 지연 조립 — 링크 판정이 503 보다 먼저인지 본다
    del http.app.dependency_overrides[get_cognito_adapter_factory]
    adapter.error = UserStateConflictError()  # 이미 있는 이메일이어도 링크가 없으면 409 를 드러내지 않는다
    token = _unusable(db, kind)
    reference = post(http, secrets.token_urlsafe(32))
    before = db.execute(text("SELECT count(*) FROM app_users")).scalar_one()

    # 기존 PLATFORM 세션 쿠키는 권한 근거가 아니다
    seed = make_auth_session()
    resp = post(http, token, Cookie=seed.cookie)

    assert error(resp) == (404, "ENROLLMENT_UNAVAILABLE")
    assert resp.json()["error"]["message"] == reference.json()["error"]["message"]
    assert (built, adapter.calls) == ([], [])
    assert db.execute(text("SELECT count(*) FROM app_users")).scalar_one() == before + 1  # 세션 시드뿐
    assert "set-cookie" not in resp.headers


def test_valid_link_with_unconfigured_cognito_is_503_and_link_stays_issued(http, db, adapter):
    del http.app.dependency_overrides[get_cognito_adapter_factory]
    token, enrollment_id = issue(db)
    assert error(post(http, token)) == (503, "AUTH_PROVIDER_UNAVAILABLE")
    assert link(db, enrollment_id).status is S.ISSUED  # 예약 전 실패 — 링크를 쓰지 않았다
    assert adapter.calls == []


# ----- 성공 ---------------------------------------------------------------------


def test_success_creates_one_platform_admin_without_session_or_login(
    http, db, make_auth_session, adapter, built
):
    seed = make_auth_session()  # 요청에 실린 다른 사람의 세션은 건드리지도, 근거로 쓰지도 않는다
    token, enrollment_id = issue(db)
    sessions_before = db.execute(text("SELECT session_id::text, revoked_at FROM auth_sessions")).all()

    resp = post(http, token, Cookie=seed.cookie)

    assert resp.status_code == 204
    assert resp.content == b""
    assert "set-cookie" not in resp.headers
    assert resp.headers["cache-control"] == "no-store"
    assert (built, adapter.calls) == ([1], [EMAIL])
    created = [u for u in app_users(db) if u.user_id != seed.user_id]
    assert len(created) == 1
    user = created[0]
    assert (user.issuer, user.cognito_sub, user.email) == (ISSUER, adapter.sub, EMAIL)
    assert (user.role, user.account_type, user.tenant_id, user.status) == ("ADMIN", "PLATFORM", None, "ACTIVE")
    assert user.email_verified_at is None
    row = link(db, enrollment_id)
    assert (row.status, row.redeemed_user_id, row.redeemed_at) == (S.REDEEMED, user.user_id, T0)
    assert (row.reserved_at, row.email) == (T0, EMAIL)
    assert db.execute(text("SELECT session_id::text, revoked_at FROM auth_sessions")).all() == sessions_before

    again = post(http, token)
    assert error(again) == (404, "ENROLLMENT_UNAVAILABLE")
    assert adapter.calls == [EMAIL]


def test_enrolled_account_still_goes_through_email_proof_at_login(http, db, adapter):
    token, _ = issue(db)
    assert post(http, token).status_code == 204
    identity = VerifiedIdentity(issuer=ISSUER, sub=adapter.sub, email=EMAIL, mfa_step=MfaStep.MFA_SETUP)
    with pytest.raises(AuthFailedError):  # 링크 사용은 이메일 소유 증명이 아니다
        service.complete_login(
            db, identity, previous_token=None, now=T0, max_seconds=28800, idle_seconds=1800
        )
    issued = service.complete_login(
        db, VerifiedIdentity(issuer=ISSUER, sub=adapter.sub, email=EMAIL,
                             mfa_step=MfaStep.MFA_SETUP, email_proven=True),
        previous_token=None, now=T0, max_seconds=28800, idle_seconds=1800,
    )
    current = service.resolve_current_user(db, issued.token, now=T0, idle_seconds=1800, touch=False)
    assert (current.account_type, current.role, current.tenant_id) == (AccountType.PLATFORM, UserRole.ADMIN, None)


# ----- 예약 · 제공자 · 확정 실패 -------------------------------------------------


def test_reservation_commit_failure_makes_zero_provider_calls(http, db, adapter, monkeypatch):
    token, enrollment_id = issue(db)

    def failing_commit():
        raise OperationalError("COMMIT", {}, Exception("db down"))

    monkeypatch.setattr(db, "commit", failing_commit)
    assert error(post(http, token)) == (500, "INTERNAL_ERROR")
    monkeypatch.undo()
    assert adapter.calls == []
    assert link(db, enrollment_id).status is S.ISSUED
    assert app_users(db) == []


def test_admin_create_user_runs_with_no_open_transaction(http, db, adapter, monkeypatch):
    """AdminCreateUser 순간 요청 세션에 열린 트랜잭션이 없다 — 예약 commit 뒤 다시 열지 않았다는 직접 관측."""
    token, _ = issue(db)
    served: list = []
    real_redeem = enrollment_service.redeem

    def spy_redeem(session, *args, **kwargs):
        served.append(session)
        return real_redeem(session, *args, **kwargs)

    monkeypatch.setattr(enrollment_service, "redeem", spy_redeem)
    open_at_call: list[bool] = []
    adapter.during = lambda: open_at_call.append(db.in_transaction())

    assert post(http, token).status_code == 204
    assert served == [db]  # 서비스가 실제로 받은 세션이 관측한 세션이다
    assert open_at_call == [False]
    assert adapter.calls == [EMAIL]


@pytest.mark.parametrize(
    "exc, expected",
    [
        pytest.param(UserStateConflictError(), (409, "USER_STATE_CONFLICT"), id="email-exists"),
        pytest.param(AuthRateLimitedError(), (429, "AUTH_RATE_LIMITED"), id="provider-throttle"),
        pytest.param(AuthEmailDeliveryFailedError(), (503, "AUTH_EMAIL_DELIVERY_FAILED"), id="mail-failed"),
        pytest.param(AuthProviderUnavailableError(), (503, "AUTH_PROVIDER_UNAVAILABLE"), id="timeout-or-5xx"),
        pytest.param(RuntimeError("unclassified"), (500, "INTERNAL_ERROR"), id="unclassified"),
    ],
)
def test_every_failure_after_reservation_keeps_it_and_never_retries(http, db, adapter, exc, expected):
    token, enrollment_id = issue(db)
    adapter.error = exc
    assert error(post(http, token)) == expected
    row = link(db, enrollment_id)
    assert (row.status, row.email, row.reserved_at) == (S.RESERVED, EMAIL, T0)
    assert app_users(db) == []

    adapter.error = None  # 제공자가 회복돼도 같은 링크는 다시 쓰지 못한다(자동 · 수동 재시도 모두)
    assert error(post(http, token)) == (404, "ENROLLMENT_UNAVAILABLE")
    assert adapter.calls == [EMAIL]
    assert link(db, enrollment_id).status is S.RESERVED


def test_provider_success_then_db_failure_leaves_no_account_and_keeps_reservation(http, db, adapter, monkeypatch):
    token, enrollment_id = issue(db)
    real_commit = db.commit
    commits = []

    def second_commit_fails():
        commits.append(1)
        if len(commits) == 2:  # 1: 예약, 2: 확정 — PG 에 닿기 전 실패
            raise OperationalError("COMMIT", {}, Exception("db down"))
        real_commit()

    monkeypatch.setattr(db, "commit", second_commit_fails)
    assert error(post(http, token)) == (500, "INTERNAL_ERROR")
    monkeypatch.undo()
    assert adapter.calls == [EMAIL]  # 제공자 쪽은 성공했다 — 되돌리지 않는다
    assert app_users(db) == []
    assert link(db, enrollment_id).status is S.RESERVED
    assert error(post(http, token)) == (404, "ENROLLMENT_UNAVAILABLE")
    assert adapter.calls == [EMAIL]


def test_expiry_during_provider_call_fails_closed(http, db, adapter, clock):
    token, enrollment_id = issue(db)

    def expire():
        clock.now = EXPIRES

    adapter.during = expire
    assert error(post(http, token)) == (404, "ENROLLMENT_UNAVAILABLE")
    assert adapter.calls == [EMAIL]
    assert app_users(db) == []
    assert link(db, enrollment_id).status is S.RESERVED


# ----- 독립 commit 세션: 동시 사용 · 제공자 호출 중 회수 ---------------------------


@pytest.fixture()
def committed(pg_engine):
    factory = sessionmaker(bind=pg_engine, autoflush=False, expire_on_commit=False)
    made: list[str] = []
    yield factory, made
    with factory() as session:
        session.execute(
            text("DELETE FROM platform_enrollments WHERE enrollment_id::text = ANY(:ids)"), {"ids": made}
        )
        session.execute(text("DELETE FROM app_users WHERE issuer = :i"), {"i": ISSUER})
        session.commit()


def _redeem(factory, adapter, token, get_adapter=None):
    with factory() as session:
        return enrollment_service.redeem(
            session, get_adapter or (lambda: adapter), token=token, email=EMAIL,
            clock=lambda: datetime.now(UTC),
        )


def test_parallel_redemptions_of_one_link_make_one_account_and_one_provider_call(committed, adapter):
    factory, made = committed
    with factory() as session:
        token, enrollment_id = issue(session, expires_at=datetime.now(UTC) + timedelta(hours=1))
    made.append(enrollment_id)
    workers = 4
    barrier = threading.Barrier(workers)
    outcomes: list = [None] * workers

    def get_adapter():
        barrier.wait(timeout=10)  # 모두 사전 확인을 통과한 뒤에 함께 예약하러 간다
        return adapter

    def run(i):
        try:
            outcomes[i] = ("ok", _redeem(factory, adapter, token, get_adapter))
        except ApiError as exc:
            outcomes[i] = ("err", exc.code)

    threads = [threading.Thread(target=run, args=(i,)) for i in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)
    assert sorted(kind for kind, _ in outcomes) == ["err"] * (workers - 1) + ["ok"]
    assert {value for kind, value in outcomes if kind == "err"} == {ErrorCode.ENROLLMENT_UNAVAILABLE}
    assert adapter.calls == [EMAIL]
    with factory() as session:
        row = enrollment_repo.get(session, enrollment_id)
        users = session.execute(text("SELECT user_id::text FROM app_users WHERE issuer = :i"), {"i": ISSUER}).scalars().all()
    assert row.status is S.REDEEMED
    assert users == [row.redeemed_user_id]


def test_revoke_committed_during_provider_call_fails_closed(committed, adapter):
    factory, made = committed
    with factory() as session:
        token, enrollment_id = issue(session, expires_at=datetime.now(UTC) + timedelta(hours=1))
    made.append(enrollment_id)

    def operator_revokes():
        with factory() as other:
            assert enrollment_repo.revoke(other, enrollment_id=enrollment_id, now=datetime.now(UTC))
            other.commit()

    adapter.during = operator_revokes
    with pytest.raises(ApiError) as exc:
        _redeem(factory, adapter, token)
    assert exc.value.code is ErrorCode.ENROLLMENT_UNAVAILABLE
    assert adapter.calls == [EMAIL]  # Cognito 사용자는 생겼을 수 있다 — 앱 계정은 만들지 않는다
    with factory() as session:
        row = enrollment_repo.get(session, enrollment_id)
        count = session.execute(text("SELECT count(*) FROM app_users WHERE issuer = :i"), {"i": ISSUER}).scalar_one()
    assert (row.status, row.email, row.redeemed_user_id) == (S.REVOKED, EMAIL, None)
    assert count == 0


def test_finalize_commit_applied_then_error_is_500_with_unknown_outcome(committed, adapter, monkeypatch, caplog):
    factory, made = committed
    with factory() as session:
        token, enrollment_id = issue(session, expires_at=datetime.now(UTC) + timedelta(hours=1))
    made.append(enrollment_id)

    with factory() as session:
        real_commit = session.commit
        commits = []

        def ack_lost_on_finalize():
            commits.append(1)
            real_commit()
            if len(commits) == 2:  # 1: 예약, 2: 확정 — PG 에는 반영됐고 응답만 잃었다
                raise OperationalError("COMMIT", {}, Exception("connection lost"))

        monkeypatch.setattr(session, "commit", ack_lost_on_finalize)
        with caplog.at_level(logging.INFO, logger="vigilantis.auth"), pytest.raises(ApiError) as exc:
            enrollment_service.redeem(
                session, lambda: adapter, token=token, email=EMAIL, clock=lambda: datetime.now(UTC)
            )
    assert exc.value.code is ErrorCode.INTERNAL_ERROR
    assert adapter.calls == [EMAIL]

    with factory() as other:  # 독립 연결로 본 실제 결과
        row = enrollment_repo.get(other, enrollment_id)
        users = other.execute(
            text("SELECT user_id::text, cognito_sub, role::text, account_type::text, tenant_id"
                 " FROM app_users WHERE issuer = :i"),
            {"i": ISSUER},
        ).all()
    assert row.status is S.REDEEMED
    assert [tuple(u) for u in users] == [(row.redeemed_user_id, adapter.sub, "ADMIN", "PLATFORM", None)]

    with pytest.raises(ApiError) as again:
        _redeem(factory, adapter, token)
    assert again.value.code is ErrorCode.ENROLLMENT_UNAVAILABLE
    assert adapter.calls == [EMAIL]

    log = caplog.text
    assert "platform_enrollment_finalize_failed" in log
    assert "db_outcome=unknown" in log and "app_user=may_exist" in log
    assert "reservation=kept" not in log and "platform_enrollment_redeemed" not in log
    assert token not in log and hash_token(token) not in log and EMAIL not in log


# ----- 로그 · 기동 진단 -----------------------------------------------------------


def test_logs_never_carry_token_hash_or_email(http, db, adapter, caplog, monkeypatch):
    tokens = [issue(db)[0] for _ in range(3)]
    with caplog.at_level(logging.DEBUG):
        assert post(http, tokens[0]).status_code == 204
        adapter.error = AuthProviderUnavailableError()
        assert post(http, tokens[1]).status_code == 503
        adapter.error = None
        adapter.sub = f"sub-{uuid.uuid4().hex}"
        monkeypatch.setattr(enrollment_service.auth_repo, "create_user", _raise_db_error)
        assert post(http, tokens[2]).status_code == 500
        assert post(http, secrets.token_urlsafe(32)).status_code == 404
    text_ = caplog.text
    assert "platform_enrollment_redeemed" in text_
    assert "platform_enrollment_provider_failed" in text_
    assert "platform_enrollment_finalize_failed" in text_
    for token in tokens:
        assert token not in text_
        assert hash_token(token) not in text_
    assert EMAIL not in text_


def _raise_db_error(*args, **kwargs):
    raise OperationalError("INSERT INTO app_users", {"email": EMAIL}, Exception("db down"))


def test_startup_log_reports_only_an_enabled_switch(caplog):
    with caplog.at_level(logging.WARNING, logger="vigilantis.auth"):
        enrollment_service.log_enrollment_status(settings(AUTH_PLATFORM_ENROLLMENT_ENABLED=False))
        assert "platform_enrollment_enabled" not in caplog.text
        enrollment_service.log_enrollment_status(settings())
    assert "platform_enrollment_enabled" in caplog.text
