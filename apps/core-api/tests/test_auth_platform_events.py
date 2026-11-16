"""로그인 · 세션의 플랫폼 활동 로그(#589 B) — 실제 PostgreSQL 행으로 본다.

지키는 것
  - 로그인 성공은 세션 발급과 같은 트랜잭션의 AUTH_LOGIN 한 행이다 — 대상은 그 계정, 세션은 방금 생긴 세션.
    중간 단계의 성공은 행이 없고, 기록을 못 쓰면 세션도 생기지 않는다.
  - 실패 · 거절된 시도는 따로 남는다. TOTP 코드 단계는 AUTH_MFA, 그 밖의 단계와 세션 발급의 거절은 AUTH_LOGIN.
    대상은 흐름이 포착한 안정 신원으로만 잇는다 — 비밀번호 단계의 실패는 대상이 없다.
  - 입력 형식 문제 · 어긋난 단계 · 대상 모르는 만료는 남기지 않는다.
  - 로그아웃은 실제로 폐기했을 때만 AUTH_LOGOUT 한 행이다(행위자 = 그 세션의 사용자). 폐기를 먼저 commit 하고
    기록은 따로 남긴다 — 기록을 못 써도 세션은 폐기된 채고, 세션 행 잠금을 쥔 채 기록을 쓰지 않아 app_users 를
    잠그는 쪽(로그인 완료 · 비밀번호 재설정)과 교착하지 않는다.
  - 비밀번호 재설정은 확정만 남긴다 — 성공 · 거절 · 실패 모두 따로. 성공은 세션 회수를 commit 한 뒤에 쓰고,
    기록을 못 써도 회수는 유지된다(204).
  - 행에는 이메일 · 비밀번호 · 코드 · 토큰이 없다.
"""

from __future__ import annotations

import threading
import time
import uuid
from datetime import UTC, datetime

import pytest
from auth import cognito, login_flow, service
from auth import sessions as auth_sessions
from auth.http import SESSION_COOKIE, get_clock, get_login_flow
from config import get_auth_settings
from db import models
from db.repositories import auth as auth_repo
from db.repositories import platform_events as platform_events_repo
from fastapi.testclient import TestClient
from schemas.api.auth import AccountType, LoginStep, UserRole
from schemas.platform_events import (
    PlatformActorKind,
    PlatformEventAction,
    PlatformEventCategory,
    PlatformEventOutcome,
    PlatformEventTargetType,
)
from sqlalchemy import create_engine, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker
from tests import test_auth_login_flow as flow_harness
from tests.platform_event_cleanup import purge_platform_events
from tests import test_auth_password_reset as reset_harness
from tests.test_auth_api import (
    CODE,
    COMPLETED,
    ISSUER,
    NEW_PASSWORD,
    ORIGIN,
    PASSWORD,
    SUB,
    T0,
    Clock,
    FakeFlow,
    post,
    set_cookies,
    settings,
)

A = PlatformEventAction
S, D, F = PlatformEventOutcome.SUCCEEDED, PlatformEventOutcome.DENIED, PlatformEventOutcome.FAILED
EMAIL = "u@example.com"
LOGIN_BODY = {"email": EMAIL, "password": PASSWORD}
SECRETS = (EMAIL, PASSWORD, NEW_PASSWORD, CODE, "flowtok-abc123")


# ----- 픽스처 · 헬퍼 -----------------------------------------------------------------


@pytest.fixture()
def flow():
    fake = FakeFlow()
    fake.bound = (ISSUER, SUB)  # 비밀번호 단계를 지난 흐름 — 기본은 대상 신원을 안다
    return fake


@pytest.fixture()
def clock():
    return Clock()


@pytest.fixture()
def http(client_pg_anon, flow, clock):
    app = client_pg_anon.app
    app.dependency_overrides[get_auth_settings] = lambda: settings()
    app.dependency_overrides[get_login_flow] = lambda: flow
    app.dependency_overrides[get_clock] = lambda: (lambda: clock.now)
    with TestClient(app, base_url="https://testserver") as test_client:
        yield test_client


def _create_user(db, *, account_type=AccountType.TENANT, tenant_id=None, role=UserRole.OPERATOR, sub=SUB, **more):
    row = auth_repo.create_user(
        db, issuer=ISSUER, cognito_sub=sub, email=EMAIL, role=role,
        account_type=account_type, tenant_id=tenant_id, now=T0, **more,
    )
    db.commit()
    return row


@pytest.fixture()
def tenant_id(make_tenant):
    return make_tenant()


@pytest.fixture()
def user(db, tenant_id):
    """고객사(TENANT) OPERATOR."""
    return _create_user(db, tenant_id=tenant_id)


# 다른 테스트가 독립 세션으로 commit 해 둔 행(정리가 덜 된 것 포함)에 기대지 않는다 — 이 테스트가 시작할 때
# 이미 있던 행은 빼고 본다
_seen_before: set[str] = set()


@pytest.fixture(autouse=True)
def _baseline(request):
    _seen_before.clear()
    if "db" in request.fixturenames:
        session = request.getfixturevalue("db")
        _seen_before.update(session.scalars(select(models.PlatformEvent.event_id)).all())
    yield


def _events(db, *actions):
    db.expire_all()
    query = select(models.PlatformEvent).where(
        models.PlatformEvent.category == PlatformEventCategory.AUTH,
        models.PlatformEvent.event_id.notin_(_seen_before),
    )
    if actions:
        query = query.where(models.PlatformEvent.action.in_([a.value for a in actions]))
    return db.scalars(query.order_by(models.PlatformEvent.occurred_at)).all()


def _sessions(db) -> int:
    return db.execute(text("SELECT count(*) FROM auth_sessions")).scalar_one()


def _is_attempt(event, action, outcome, *, target=None, tenant=None, reason=None) -> None:
    """로그인 전 시도의 공통 모양 — 행위자 ANONYMOUS, 대상은 계정(모르면 없음)."""
    assert (event.action, event.outcome) == (action.value, outcome)
    assert event.category is PlatformEventCategory.AUTH
    assert event.actor_kind is PlatformActorKind.ANONYMOUS
    assert (event.actor_user_id, event.actor_account_type, event.actor_platform_role) == (None, None, None)
    assert (event.target_id, event.tenant_id) == (target, tenant)
    assert event.target_type is (None if target is None else PlatformEventTargetType.USER)
    assert event.details == ({} if reason is None else {"reason_code": reason})


def _no_secrets(db) -> None:
    for event in _events(db):
        dump = f"{event.details}{event.target_id}{event.user_agent}{event.request_id}{event.session_id}"
        assert not any(secret in dump for secret in SECRETS)


def _login(http, flow) -> str:
    """끝까지 로그인 → 세션 토큰."""
    assert post(http, "/login", LOGIN_BODY).status_code == 200
    flow.result = COMPLETED
    resp = post(http, "/login/totp", {"code": CODE})
    assert resp.status_code == 200
    return set_cookies(resp)[SESSION_COOKIE][0]


def _failing_after_write(monkeypatch, action):
    original = platform_events_repo.record_platform_event

    def fail(db, act, *args, **kwargs):
        row = original(db, act, *args, **kwargs)
        if act is action:
            raise OperationalError("event write", {}, Exception("not exposed"))
        return row

    monkeypatch.setattr(platform_events_repo, "record_platform_event", fail)


# ----- 로그인 성공 ---------------------------------------------------------------------


def test_login_success_is_one_row_with_the_new_session(http, db, flow, user, tenant_id):
    assert post(http, "/login", LOGIN_BODY).status_code == 200
    assert _events(db) == []  # 비밀번호 단계의 통과는 행이 아니다
    flow.result = COMPLETED
    resp = http.post(
        "/api/v1/auth/login/totp", json={"code": CODE},
        headers={"Origin": ORIGIN, "X-Request-ID": "req-login-1", "User-Agent": "pytest-agent/1.0"},
    )
    assert resp.status_code == 200

    [event] = _events(db)
    _is_attempt(event, A.AUTH_LOGIN, S, target=user.user_id, tenant=tenant_id)
    session = db.scalars(select(models.AuthSession)).one()
    assert event.session_id == session.session_id  # 그 요청에서 생긴 세션
    assert (event.request_id, event.user_agent) == ("req-login-1", "pytest-agent/1.0")
    _no_secrets(db)


def test_platform_account_login_has_no_tenant(http, db, flow):
    staff = _create_user(db, account_type=AccountType.PLATFORM, role=UserRole.ADMIN)
    _login(http, flow)
    [event] = _events(db)
    _is_attempt(event, A.AUTH_LOGIN, S, target=staff.user_id, tenant=None)


@pytest.mark.parametrize("step", [
    LoginStep.NEW_PASSWORD_REQUIRED, LoginStep.TOTP_SETUP_REQUIRED, LoginStep.TOTP_REQUIRED,
    LoginStep.EMAIL_VERIFICATION_REQUIRED,
])
def test_intermediate_steps_record_nothing(http, db, flow, user, step):
    flow.result = login_flow.StepResult(step)
    for path, body in [
        ("/login", LOGIN_BODY), ("/login/new-password", {"new_password": NEW_PASSWORD}),
        ("/login/totp-setup", None), ("/login/totp-setup/verify", {"code": CODE}),
        ("/login/totp", {"code": CODE}), ("/login/email-verification", {"code": CODE}),
    ]:
        assert post(http, path, body).status_code == 200
    assert _events(db) == []


def test_login_issues_no_session_when_the_event_cannot_be_written(http, db, flow, user, monkeypatch):
    _failing_after_write(monkeypatch, A.AUTH_LOGIN)
    assert post(http, "/login", LOGIN_BODY).status_code == 200
    flow.result = COMPLETED
    resp = post(http, "/login/totp", {"code": CODE})
    assert resp.status_code == 500 and SESSION_COOKIE not in set_cookies(resp)
    assert _sessions(db) == 0
    assert _events(db) == []


# ----- 실패 · 거절된 시도 ---------------------------------------------------------------


@pytest.mark.parametrize("error,outcome,reason", [
    (cognito.AuthFailedError(), D, "AUTH_FAILED"),                       # 틀린 비밀번호 · 없는 계정(구분하지 않는다)
    (cognito.AuthRateLimitedError(), D, "AUTH_RATE_LIMITED"),
    (cognito.AuthProviderUnavailableError(), F, "AUTH_PROVIDER_UNAVAILABLE"),
])
def test_password_step_failure_has_no_target(http, db, flow, user, error, outcome, reason):
    flow.error = error
    assert post(http, "/login", LOGIN_BODY).status_code in (401, 429, 503)
    [event] = _events(db)
    # 계정이 있어도 잇지 않는다 — 이메일로 앱 계정을 고르지 않는다
    _is_attempt(event, A.AUTH_LOGIN, outcome, reason=reason)
    assert event.session_id is None
    _no_secrets(db)


@pytest.mark.parametrize("path,body,action", [
    ("/login/totp", {"code": CODE}, A.AUTH_MFA),
    ("/login/totp-setup/verify", {"code": CODE}, A.AUTH_MFA),
    ("/login/totp-setup", None, A.AUTH_MFA),
    ("/login/email-verification", {"code": CODE}, A.AUTH_LOGIN),   # 이메일 증명은 MFA 가 아니다
    ("/login/new-password", {"new_password": NEW_PASSWORD}, A.AUTH_LOGIN),
])
def test_step_failure_is_recorded_against_the_bound_account(
    http, db, flow, user, tenant_id, path, body, action
):
    assert post(http, "/login", LOGIN_BODY).status_code == 200
    flow.error = cognito.AuthCodeInvalidError()
    assert post(http, path, body).status_code == 401
    [event] = _events(db)
    _is_attempt(event, action, D, target=user.user_id, tenant=tenant_id, reason="AUTH_CODE_INVALID")
    _no_secrets(db)


def test_step_failure_for_an_identity_without_an_app_account_has_no_target(http, db, flow):
    assert post(http, "/login", LOGIN_BODY).status_code == 200
    flow.error = cognito.AuthCodeInvalidError()
    assert post(http, "/login/totp", {"code": CODE}).status_code == 401
    [event] = _events(db)
    _is_attempt(event, A.AUTH_MFA, D, reason="AUTH_CODE_INVALID")


@pytest.mark.parametrize("error,status", [
    (cognito.AuthPasswordPolicyError(), 422),   # 입력 형식
    (cognito.AuthStepConflictError(), 409),     # 어긋난 단계
])
def test_input_and_protocol_refusals_are_not_attempts(http, db, flow, user, error, status):
    assert post(http, "/login", LOGIN_BODY).status_code == 200
    flow.error = error
    assert post(http, "/login/new-password", {"new_password": NEW_PASSWORD}).status_code == status
    assert post(http, "/login/totp", {"code": CODE}).status_code == status
    assert _events(db) == []


def test_expired_flow_is_recorded_only_when_the_account_is_known(http, db, flow, user, tenant_id):
    assert post(http, "/login", LOGIN_BODY).status_code == 200
    flow.error = cognito.AuthFlowExpiredError()  # 세 번째 틀린 코드에서 흐름이 끝난 경우 등
    assert post(http, "/login/totp", {"code": CODE}).status_code == 401
    [event] = _events(db)
    _is_attempt(event, A.AUTH_MFA, D, target=user.user_id, tenant=tenant_id, reason="AUTH_FLOW_EXPIRED")

    # 진행 쿠키는 남았는데 흐름이 이미 없다(만료 정리 · 재시작) — 누구의 계정에도 시도가 일어나지 않았다.
    # 앞의 만료 응답이 쿠키를 지웠으므로 다시 싣는다(쿠키가 없으면 흐름을 부르기 전에 끝나 이 경로를 지나지 않는다)
    flow.bound = None
    calls = len(flow.calls)
    resp = http.post(
        "/api/v1/auth/login/totp", json={"code": CODE},
        headers={"Origin": ORIGIN, "Cookie": "__Host-vg_login=flowtok-abc123"},
    )
    assert resp.status_code == 401 and len(flow.calls) == calls + 1  # 흐름까지 갔다
    assert len(_events(db)) == 1


def test_step_without_a_flow_cookie_records_nothing(http, db, flow, user):
    assert post(http, "/login/totp", {"code": CODE}).status_code == 401
    assert _events(db) == [] and flow.calls == []


# ----- 세션 발급의 거절 -----------------------------------------------------------------


def test_completion_for_an_unprovisioned_identity_is_denied_without_a_target(http, db, flow):
    post(http, "/login", LOGIN_BODY)
    flow.result = COMPLETED
    assert post(http, "/login/totp", {"code": CODE}).status_code == 403
    [event] = _events(db)
    _is_attempt(event, A.AUTH_LOGIN, D, reason="ACCOUNT_NOT_PROVISIONED")
    assert _sessions(db) == 0


def test_completion_for_a_disabled_account_is_denied_against_it(http, db, flow, user, tenant_id):
    auth_repo.set_status(db, user_id=user.user_id, status=models.AppUserStatus.DISABLED, now=T0)
    db.commit()
    post(http, "/login", LOGIN_BODY)
    flow.result = COMPLETED
    assert post(http, "/login/totp", {"code": CODE}).status_code == 403
    [event] = _events(db)
    _is_attempt(event, A.AUTH_LOGIN, D, target=user.user_id, tenant=tenant_id, reason="ACCOUNT_DISABLED")
    assert _sessions(db) == 0


def test_completion_with_a_stale_generation_is_denied_against_the_account(http, db, flow, user, tenant_id):
    user.auth_generation = 1  # TOTP 복구로 세대가 올랐다 — 그 전에 시작한 흐름은 세션을 받지 못한다
    db.commit()
    post(http, "/login", LOGIN_BODY)
    flow.result = COMPLETED
    assert post(http, "/login/totp", {"code": CODE}).status_code == 401
    [event] = _events(db)
    _is_attempt(event, A.AUTH_LOGIN, D, target=user.user_id, tenant=tenant_id, reason="AUTH_FAILED")
    assert _sessions(db) == 0


def test_denied_attempt_survives_when_the_detached_write_fails(http, db, flow, user):
    import db.session as db_session

    def unavailable():
        raise RuntimeError("detached session unavailable")

    http.app.dependency_overrides[db_session.get_session_factory] = lambda: unavailable
    flow.error = cognito.AuthFailedError()
    resp = post(http, "/login", LOGIN_BODY)
    assert (resp.status_code, resp.json()["error"]["code"]) == (401, "AUTH_FAILED")
    assert _events(db) == []


# ----- 로그아웃 ------------------------------------------------------------------------


def test_logout_records_the_session_owner_once(http, db, flow, user, tenant_id):
    token = _login(http, flow)
    session = db.scalars(select(models.AuthSession)).one()
    headers = {"Cookie": f"{SESSION_COOKIE}={token}", "Origin": ORIGIN}

    assert http.post("/api/v1/auth/logout", headers=headers).status_code == 204
    assert http.post("/api/v1/auth/logout", headers=headers).status_code == 204  # 멱등 — 새 행 없음

    [event] = _events(db, A.AUTH_LOGOUT)
    assert (event.outcome, event.actor_kind, event.actor_user_id) == (S, PlatformActorKind.USER, user.user_id)
    assert (event.actor_account_type, event.actor_platform_role) == (AccountType.TENANT, None)
    assert (event.session_id, event.tenant_id) == (session.session_id, tenant_id)
    assert (event.target_type, event.target_id, event.details) == (None, None, {})


def test_platform_logout_carries_the_platform_grade(http, db, flow):
    staff = _create_user(db, account_type=AccountType.PLATFORM, role=UserRole.ADMIN)
    token = _login(http, flow)
    http.post("/api/v1/auth/logout", headers={"Cookie": f"{SESSION_COOKIE}={token}", "Origin": ORIGIN})
    [event] = _events(db, A.AUTH_LOGOUT)
    assert (event.actor_user_id, event.actor_account_type, event.tenant_id) == (
        staff.user_id, AccountType.PLATFORM, None,
    )
    assert event.actor_platform_role is db.get(models.AppUser, staff.user_id).platform_role is not None


@pytest.mark.parametrize("cookie", [None, f"{SESSION_COOKIE}=not-a-session-token"])
def test_logout_without_a_live_session_records_nothing(http, db, user, cookie):
    headers = {"Origin": ORIGIN} | ({"Cookie": cookie} if cookie else {})
    assert http.post("/api/v1/auth/logout", headers=headers).status_code == 204
    assert _events(db) == []


def _detached_writes_unavailable(http) -> None:
    """따로 남기는 기록이 쓸 세션을 얻지 못하게 한다 — 요청 자신의 트랜잭션은 건드리지 않는다."""
    import db.session as db_session

    def unavailable():
        raise RuntimeError("detached session unavailable")

    http.app.dependency_overrides[db_session.get_session_factory] = lambda: unavailable


def test_logout_revokes_the_session_even_when_the_event_cannot_be_written(http, db, flow, user, monkeypatch):
    # 기록 한 행을 못 써서 로그아웃한 세션이 살아 있으면 안 된다(2026-11-13 결정) — 폐기는 이미 commit 됐다.
    # 어느 길로 쓰든 실패하게 둔다: 요청 트랜잭션 안의 쓰기도, 따로 남기는 쓰기도
    token = _login(http, flow)
    _failing_after_write(monkeypatch, A.AUTH_LOGOUT)
    _detached_writes_unavailable(http)
    headers = {"Cookie": f"{SESSION_COOKIE}={token}", "Origin": ORIGIN}
    assert http.post("/api/v1/auth/logout", headers=headers).status_code == 204
    db.expire_all()
    session = db.scalars(select(models.AuthSession)).one()
    assert session.revoked_at is not None
    assert session.revoke_reason is models.AuthSessionRevokeReason.LOGOUT
    assert _events(db, A.AUTH_LOGOUT) == []


def _contract_refuses_every_row(monkeypatch) -> None:
    """기록 계약이 행을 받지 않는 경우 — record_detached 는 세션을 열기 전에 ValueError 를 올린다."""
    import platform_event_log

    def refuse(*args, **kwargs):
        raise ValueError("outside the event contract")

    monkeypatch.setattr(platform_event_log, "record_detached", refuse)


def test_logout_is_not_undone_by_a_row_outside_the_contract(http, db, flow, user, monkeypatch):
    # 폐기는 이미 commit 됐다 — 그 뒤의 기록이 계약에 걸려도 로그아웃 응답은 그대로다
    token = _login(http, flow)
    _contract_refuses_every_row(monkeypatch)
    headers = {"Cookie": f"{SESSION_COOKIE}={token}", "Origin": ORIGIN}
    assert http.post("/api/v1/auth/logout", headers=headers).status_code == 204
    db.expire_all()
    assert db.scalars(select(models.AuthSession)).one().revoked_at is not None


def test_logout_of_an_account_outside_the_contract_revokes_without_a_row(http, db, flow, user):
    token = _login(http, flow)
    # 로그인 뒤 소속이 풀린 계정 — 기록 계약의 사람(PLATFORM · TENANT)이 아니다. 로그아웃은 그래도 된다
    db.execute(text(
        "UPDATE app_users SET account_type = 'UNASSIGNED', tenant_id = NULL, role = 'VIEWER' WHERE user_id = :id"
    ), {"id": user.user_id})
    db.commit()
    headers = {"Cookie": f"{SESSION_COOKIE}={token}", "Origin": ORIGIN}
    assert http.post("/api/v1/auth/logout", headers=headers).status_code == 204
    db.expire_all()
    assert db.scalars(select(models.AuthSession)).one().revoked_at is not None
    assert _events(db, A.AUTH_LOGOUT) == []


# ----- 비밀번호 재설정 -------------------------------------------------------------------


@pytest.fixture()
def cog():
    c = reset_harness.Cog()
    yield c
    c.stubber.assert_no_pending_responses()


@pytest.fixture()
def reset_http(client_pg_anon, cog):
    from auth.http import get_cognito_adapter
    from config import AuthSettings

    app = client_pg_anon.app
    app.dependency_overrides[get_auth_settings] = lambda: AuthSettings(
        _env_file=None,
        COGNITO_REGION="ap-northeast-2",
        COGNITO_USER_POOL_ID=reset_harness.POOL,
        COGNITO_CLIENT_ID=reset_harness.CLIENT_ID,
        COGNITO_CLIENT_SECRET=reset_harness.CLIENT_SECRET,
        AUTH_ALLOWED_ORIGINS=ORIGIN,
    )
    app.dependency_overrides[get_cognito_adapter] = lambda: cog.adapter
    app.dependency_overrides[get_clock] = lambda: (lambda: reset_harness.NOW)
    with TestClient(app, base_url="https://testserver") as c:
        yield c


def _reset_user(db, tenant_id) -> str:
    """재설정 대역이 돌려주는 신원(reset_harness.SUB)의 고객사 계정 + 살아 있는 세션."""
    headers = reset_harness.seed_session(db, tenant_id=tenant_id)
    assert headers
    return db.scalars(
        select(models.AppUser.user_id).where(models.AppUser.cognito_sub == reset_harness.SUB)
    ).one()


def test_password_reset_success_is_recorded_with_the_session_revoke(reset_http, db, cog, tenant_id):
    user_id = _reset_user(db, tenant_id)
    cog.get_user()
    cog.confirm()
    assert reset_harness.confirm(reset_http).status_code == 204

    [event] = _events(db)
    _is_attempt(event, A.AUTH_PASSWORD_RESET, S, target=user_id, tenant=tenant_id)
    db.expire_all()
    assert db.scalars(select(models.AuthSession)).one().revoked_at is not None
    dump = f"{event.details}{event.target_id}"
    assert not any(s in dump for s in (reset_harness.EMAIL, reset_harness.CODE, reset_harness.NEW_PASSWORD))


def test_password_reset_for_an_identity_without_an_app_account_has_no_target(reset_http, db, cog):
    cog.get_user()
    cog.confirm()
    assert reset_harness.confirm(reset_http).status_code == 204
    [event] = _events(db)
    _is_attempt(event, A.AUTH_PASSWORD_RESET, S)


def test_wrong_reset_code_is_denied_against_the_account(reset_http, db, cog, tenant_id):
    user_id = _reset_user(db, tenant_id)
    cog.get_user()
    cog.error("confirm_forgot_password", "CodeMismatchException")
    assert reset_harness.confirm(reset_http).status_code == 401

    [event] = _events(db)
    _is_attempt(event, A.AUTH_PASSWORD_RESET, D, target=user_id, tenant=tenant_id, reason="AUTH_CODE_INVALID")
    db.expire_all()
    assert db.scalars(select(models.AuthSession)).one().revoked_at is None  # 바뀌지 않았다 — 세션 그대로


def test_reset_failing_before_the_identity_is_known_has_no_target(reset_http, db, cog, tenant_id):
    _reset_user(db, tenant_id)
    cog.error("admin_get_user", "InternalErrorException", status=500)
    assert reset_harness.confirm(reset_http).status_code == 503
    [event] = _events(db)
    _is_attempt(event, A.AUTH_PASSWORD_RESET, F, reason="AUTH_PROVIDER_UNAVAILABLE")


def test_reset_revokes_the_sessions_even_when_the_event_cannot_be_written(
    reset_http, db, cog, tenant_id, monkeypatch
):
    # 비밀번호는 이미 바뀌었다(Cognito) — 기록 한 행을 못 써서 이전 세션 회수가 되돌려지면 안 된다(2026-11-13 결정).
    # 어느 길로 쓰든 실패하게 둔다: 요청 트랜잭션 안의 쓰기도, 따로 남기는 쓰기도
    _reset_user(db, tenant_id)
    _failing_after_write(monkeypatch, A.AUTH_PASSWORD_RESET)
    _detached_writes_unavailable(reset_http)
    cog.get_user()
    cog.confirm()
    assert reset_harness.confirm(reset_http).status_code == 204

    db.expire_all()
    session = db.scalars(select(models.AuthSession)).one()
    assert session.revoked_at is not None
    assert session.revoke_reason is models.AuthSessionRevokeReason.PASSWORD_RESET
    assert _events(db) == []


def test_reset_is_not_undone_by_a_row_outside_the_contract(reset_http, db, cog, tenant_id, monkeypatch):
    # 회수는 이미 commit 됐다 — 그 뒤의 기록이 계약에 걸려도 재설정 응답은 그대로다
    _reset_user(db, tenant_id)
    _contract_refuses_every_row(monkeypatch)
    cog.get_user()
    cog.confirm()
    assert reset_harness.confirm(reset_http).status_code == 204
    db.expire_all()
    assert db.scalars(select(models.AuthSession)).one().revoked_at is not None


def test_reset_is_a_failure_when_the_session_revoke_fails(reset_http, db, cog, tenant_id, monkeypatch):
    # 회수 자체가 실패하면 그대로 500 이고, 그 실패가 따로 남는다 — 성공 행은 없다
    user_id = _reset_user(db, tenant_id)

    def fail(*args, **kwargs):
        raise OperationalError("session revoke", {}, Exception("not exposed"))

    monkeypatch.setattr(auth_sessions, "revoke_user_sessions", fail)
    cog.get_user()
    cog.confirm()
    assert reset_harness.confirm(reset_http).status_code == 500

    [event] = _events(db)
    _is_attempt(event, A.AUTH_PASSWORD_RESET, F, target=user_id, tenant=tenant_id, reason="INTERNAL_ERROR")
    db.expire_all()
    assert db.scalars(select(models.AuthSession)).one().revoked_at is None


def test_reset_request_and_policy_refusal_record_nothing(reset_http, db, cog, tenant_id):
    _reset_user(db, tenant_id)
    cog.forgot()
    assert reset_harness.request(reset_http).status_code == 204  # 요청은 존재를 구분하지 않는다 — 남기지 않는다
    assert reset_harness.confirm(reset_http, new_password="short").status_code == 422
    assert _events(db) == []


# ----- 따로 남기기 — 독립 세션(운영과 같은 갈래) ----------------------------------------------


def _committing_app(sessions_, overrides=None):
    """요청 · 따로 남기는 기록이 각자 자기 연결로 commit 하는 앱 — 운영과 같은 갈래다."""
    import api_usage
    import db.session as db_session
    from config import get_settings
    from main import create_app

    get_settings.cache_clear()
    app = create_app()

    def _get_db():
        session = sessions_()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[db_session.get_db] = _get_db
    app.dependency_overrides[db_session.get_session_factory] = lambda: sessions_
    app.dependency_overrides[api_usage.get_usage_session_factory] = lambda: sessions_
    app.dependency_overrides[get_auth_settings] = lambda: settings()
    app.dependency_overrides.update(overrides or {})
    return app


def test_denied_completion_commits_apart_from_the_rolled_back_request(pg_engine, committed_auth):
    """세션 발급이 거절되면 요청 트랜잭션은 되돌려진다 — 거절 행은 다른 트랜잭션으로 남는다."""
    engine = create_engine(pg_engine.url, connect_args={"options": "-c lock_timeout=5000"})
    sessions_ = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    sub = f"sub-{uuid.uuid4().hex}"
    tenant = committed_auth.make_tenant()
    with sessions_() as setup:
        row = auth_repo.create_user(
            setup, issuer=ISSUER, cognito_sub=sub, email=EMAIL, role=UserRole.OPERATOR,
            account_type=AccountType.TENANT, tenant_id=tenant, now=T0,
        )
        auth_repo.set_status(setup, user_id=row.user_id, status=models.AppUserStatus.DISABLED, now=T0)
        setup.commit()
        user_id = row.user_id
    fake = FakeFlow()
    fake.bound = (ISSUER, sub)
    fake.result = login_flow.StepResult(
        LoginStep.COMPLETED,
        login_flow.VerifiedIdentity(
            issuer=ISSUER, sub=sub, email=EMAIL, mfa_step=login_flow.MfaStep.SOFTWARE_TOKEN_MFA,
            email_proven=True, generation=0,
        ),
    )
    app = _committing_app(sessions_, {get_login_flow: lambda: fake})
    try:
        with TestClient(app, base_url="https://testserver") as client:
            client.cookies.set("__Host-vg_login", "flowtok-abc123")
            resp = post(client, "/login/totp", {"code": CODE})
        assert resp.status_code == 403
        with sessions_() as check:
            [event] = check.scalars(
                select(models.PlatformEvent).where(models.PlatformEvent.target_id == user_id)
            ).all()
            _is_attempt(event, A.AUTH_LOGIN, D, target=user_id, tenant=tenant, reason="ACCOUNT_DISABLED")
    finally:
        with sessions_() as cleanup:
            # 이 사용자는 committed_auth 가 만든 것이 아니다 — 거절 행(대상 = 이 사용자)과 함께 여기서 지운다
            purge_platform_events(cleanup, target_ids=[user_id])
            cleanup.execute(text("DELETE FROM app_users WHERE user_id = :id"), {"id": user_id})
            cleanup.commit()
        engine.dispose()


def test_logout_does_not_deadlock_with_a_reset_revoking_the_same_session(pg_engine, committed_auth):
    """로그아웃이 세션 행을 폐기한 채 기록을 쓰면, app_users 를 잠그고 같은 세션을 회수하는 쪽과 교착한다(PR 리뷰).

    두 연결로 순서를 고정한다.
      ① 한 연결이 비밀번호 재설정 · 로그인 완료처럼 app_users 를 EXCLUSIVE 로 잠근다.
      ② 다른 연결이 같은 사용자의 세션으로 로그아웃한다 — 기록 INSERT 가 행위자 FK 검사(app_users ROW SHARE)로
         ①을 기다리는 데까지 간다.
      ③ ①의 연결이 그 사용자의 세션을 회수하고 commit 한다.
    로그아웃이 폐기를 먼저 commit 하면 ②가 기다리는 동안 쥔 잠금이 없어 ③이 바로 끝나고, 기록은 그 뒤에 남는다.
    기록을 폐기와 같은 트랜잭션에 쓰면 ②는 세션 행 잠금을 쥔 채 ①을 기다리고 ③은 그 행 잠금을 기다려 서로 물린다 —
    PostgreSQL 이 한쪽을 교착으로 끝낸다."""
    engine = create_engine(pg_engine.url, connect_args={"options": "-c lock_timeout=15000"})
    sessions_ = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    seed = committed_auth.make()
    headers = {"Cookie": f"{SESSION_COOKIE}={seed.token}", "Origin": ORIGIN}
    app = _committing_app(sessions_)
    ready, locked = threading.Event(), threading.Event()
    result: dict[str, object] = {}

    def logout():
        try:
            with TestClient(app, base_url="https://testserver") as client:
                ready.set()  # 앱 기동은 잠금 전에 끝낸다
                locked.wait(timeout=20)
                result["logout"] = client.post("/api/v1/auth/logout", headers=headers).status_code
        except Exception as exc:  # noqa: BLE001 — 스레드의 실패를 본 스레드가 단언한다
            result["logout"] = exc

    request = threading.Thread(target=logout)
    try:
        request.start()
        assert ready.wait(timeout=20), "앱이 뜨지 않았다"
        with sessions_() as other:
            auth_repo.lock_users_table(other)  # ①
            locked.set()  # ②
            # pg_stat_activity 는 트랜잭션 안에서 굳는다 — AUTOCOMMIT 으로 매번 새로 읽는다
            with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as watch:
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline:
                    if watch.execute(text(
                        "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database()"
                        " AND wait_event_type = 'Lock' AND query ILIKE 'INSERT INTO platform_events%'"
                    )).scalar_one():
                        break
                    time.sleep(0.05)
                else:
                    pytest.fail("로그아웃의 기록 INSERT 가 app_users 잠금을 기다리는 데까지 가지 않았다")
            auth_sessions.revoke_user_sessions(  # ③ — 교착이면 여기서 한쪽이 DeadlockDetected 로 끝난다
                other,
                user_id=seed.user_id,
                now=datetime.now(UTC),
                reason=models.AuthSessionRevokeReason.PASSWORD_RESET,
            )
            other.commit()
        request.join(timeout=40)
        assert not request.is_alive()

        assert result["logout"] == 204
        with sessions_() as check:
            session = check.get(models.AuthSession, seed.session_id)
            # 먼저 폐기한 쪽은 로그아웃이다 — ③의 회수는 이미 폐기된 세션을 건드리지 않는다
            assert session.revoke_reason is models.AuthSessionRevokeReason.LOGOUT
            [event] = check.scalars(
                select(models.PlatformEvent).where(models.PlatformEvent.actor_user_id == seed.user_id)
            ).all()
            assert (event.action, event.outcome) == (A.AUTH_LOGOUT.value, S)
            assert event.session_id == seed.session_id
    finally:
        locked.set()
        request.join(timeout=40)
        engine.dispose()  # 남은 행은 committed_auth 가 지운다(행위자 = 시드 사용자)


# ----- LoginFlow.subject — 실제 흐름 저장소 -------------------------------------------------


@pytest.fixture()
def env():
    e = flow_harness.Env()
    yield e
    e.stubber.deactivate()


def test_subject_reads_the_bound_identity_without_consuming_the_flow(env):
    env.initiate("SOFTWARE_TOKEN_MFA")
    env.respond_error("SOFTWARE_TOKEN_MFA", "SESS-0-0123456789abcdef", flow_harness.totp_extra("000000"))
    token, _ = env.flow.start(flow_harness.EMAIL, flow_harness.PASSWORD)

    assert env.flow.subject(token) == (flow_harness.ISSUER, flow_harness.SUB)
    assert env.flow.subject(token) == (flow_harness.ISSUER, flow_harness.SUB)  # 읽어도 흐름은 그대로다
    assert len(env.store) == 1
    with pytest.raises(cognito.AuthCodeInvalidError):  # 꺼내지 않았으므로 단계가 그대로 진행된다
        env.flow.submit_totp(token, "000000")
    assert env.flow.subject(token) == (flow_harness.ISSUER, flow_harness.SUB)
    env.done()


def test_subject_of_an_unknown_or_finished_flow_is_none(env):
    assert env.flow.subject("no-such-flow-token") is None
    env.initiate("SOFTWARE_TOKEN_MFA")
    for _ in range(3):
        env.respond_error("SOFTWARE_TOKEN_MFA", "SESS-0-0123456789abcdef", flow_harness.totp_extra("000000"))
    token, _ = env.flow.start(flow_harness.EMAIL, flow_harness.PASSWORD)
    for expected in (cognito.AuthCodeInvalidError, cognito.AuthCodeInvalidError, cognito.AuthFlowExpiredError):
        with pytest.raises(expected):
            env.flow.submit_totp(token, "000000")
    assert env.flow.subject(token) is None  # 세 번째 실패로 흐름이 끝났다


def test_subject_does_not_reach_cognito_or_the_gate(env):
    env.initiate("SOFTWARE_TOKEN_MFA")
    token, _ = env.flow.start(flow_harness.EMAIL, flow_harness.PASSWORD)
    binds = list(env.gate.binds)
    env.flow.subject(token)
    env.done()  # 등록한 응답만 썼다 — 읽기는 SDK 를 부르지 않는다
    assert env.gate.binds == binds


def test_complete_login_itself_does_not_record_denials(db, tenant_id):
    """거절의 기록은 호출부(라우터) 몫이다 — 서비스는 되돌리고 올리기만 한다."""
    identity = login_flow.VerifiedIdentity(
        issuer=ISSUER, sub="nobody", email=EMAIL, mfa_step=login_flow.MfaStep.SOFTWARE_TOKEN_MFA,
        email_proven=True, generation=0,
    )
    with pytest.raises(service.AccountNotProvisionedError):
        service.complete_login(db, identity, previous_token=None, now=T0, max_seconds=60, idle_seconds=60)
    assert _events(db) == []
