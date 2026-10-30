"""TOTP 분실 복구 핵심 (#515) — 실제 PostgreSQL · 독립 세션 · Cognito 대역.

지키는 것: 제공자 호출 전 차단(세대 · 이메일 증명 · 대상 세션 전부) · 제공자 결과의 지속 분류(성공은 삭제 확인뿐) ·
같은 작업 재요청은 제공자를 다시 부르지 않음 · 서로 다른 요청 · 신원 변경 울타리(연결 유실 · 작업자 중단 · 결과 불명에도
남고, 확정 응답 기록이나 운영자 정리로만 풀린다) · 로그인 완료 경합 · 결과 기록 실패 · 새 등록(AuthenticationResult 뒤
기록)과 이메일 증명 · 잠금 순서(교착 없음). 경합은 barrier(Event) · 잠금 대기 관측으로 순서를 고정한다. 연결 유실은
작업자 엔진의 DB 연결을 서버에서 끊어 만든다(운영 엔진처럼 pre-ping 으로 다시 맺는다).
Cognito 는 항상 대역이다 — 실제 AWS 를 부르지 않는다(인증 클라이언트는 실제 Cognito 주소에 못 박혀 있다). SDK 재시도
회귀는 운영 조립(provider.build_login_flow · build_adapter)의 실제 botocore 클라이언트를 HTTP 전송 층에서만 대역한다.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import botocore.httpsession
import pytest
from auth import provider, service, sessions, totp_recovery
from auth.cognito import (
    AuthCodeInvalidError,
    AuthFailedError,
    AuthFlowExpiredError,
    AuthProviderUnavailableError,
    CognitoAdapter,
)
from auth.cognito import SoftwareTokenDeletion as D
from auth.login_flow import LoginFlow, MfaStep, VerifiedIdentity
from auth.totp_recovery import RecoveryRejection, TotpRecoveryRejected
from botocore.awsrequest import AWSResponse
from botocore.exceptions import ClientError, ReadTimeoutError
from config import AuthSettings
from db import models
from db.models import MfaRecoveryState as S
from db.models import TotpProviderMutationKind as K
from db.models import TotpProviderMutationResolution as Res
from db.models import TotpRecoveryStatus as R
from db.repositories import auth as auth_repo
from db.repositories import totp_recovery as recovery_repo
from schemas.api.auth import AccountType, LoginStep, UserRole
from sqlalchemy import create_engine, delete, or_, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

REGION = "ap-northeast-2"
POOL = "ap-northeast-2_Recovery"
ISSUER = f"https://cognito-idp.{REGION}.amazonaws.com/{POOL}"
T0 = datetime(2026, 10, 29, 9, 0, tzinfo=UTC)
T1 = T0 + timedelta(minutes=5)
MAX, IDLE = 28800, 1800
#: Cognito Username — sub 와 다른 값이다(Username == sub 로 추정하지 않는다)
USERNAME = "user-name-not-the-sub"
EMAIL = "target@example.com"
PASSWORD = "Pa55-word-initial"
CODE = "123456"
#: 작업자 엔진의 연결 이름 — 연결 유실은 이 이름의 서버 연결만 끊는다(다른 테스트의 엔진은 건드리지 않는다)
WORKER = "totp-recovery-worker"


# --- Cognito 대역 ------------------------------------------------------------------


class FakeCognito:
    """boto3 cognito-idp 대역 — CognitoAdapter 가 부르는 연산만. (연산, 파라미터)를 기록하고 연산별 훅을 건다.

    훅 (params) → None 이면 기본 응답, dict 면 그 응답. 예외를 던지면 SDK 실패처럼 어댑터로 전파된다.
    """

    def __init__(self, sub: str, *, challenge: str = "SOFTWARE_TOKEN_MFA") -> None:
        self.sub = sub
        self.challenge = challenge
        self.calls: list[tuple[str, dict]] = []
        self.hooks: dict = {}

    def ops(self, name: str) -> list[dict]:
        return [params for op, params in self.calls if op == name]

    def _op(self, name, params, default):
        self.calls.append((name, params))
        hook = self.hooks.get(name)
        resp = hook(params) if hook is not None else None
        return default if resp is None else resp

    def admin_initiate_auth(self, **p):
        return self._op(
            "admin_initiate_auth",
            p,
            {"ChallengeName": self.challenge, "Session": "S0", "ChallengeParameters": {"USER_ID_FOR_SRP": USERNAME}},
        )

    def admin_get_user(self, **p):
        attrs = [{"Name": "sub", "Value": self.sub}, {"Name": "email", "Value": EMAIL}]
        return self._op(
            "admin_get_user", p, {"Username": USERNAME, "UserAttributes": attrs, "UserStatus": "CONFIRMED"}
        )

    def admin_respond_to_auth_challenge(self, **p):
        tokens = {"AccessToken": "AT", "RefreshToken": "RT", "ExpiresIn": 300}
        return self._op("admin_respond_to_auth_challenge", p, {"AuthenticationResult": tokens})

    def associate_software_token(self, **p):
        return self._op("associate_software_token", p, {"SecretCode": "JBSWY3DPEHPK3PXP", "Session": "S1"})

    def verify_software_token(self, **p):
        return self._op("verify_software_token", p, {"Status": "SUCCESS", "Session": "S2"})

    def set_user_mfa_preference(self, **p):
        return self._op("set_user_mfa_preference", p, {})

    def get_user(self, **p):
        attrs = [
            {"Name": "sub", "Value": self.sub},
            {"Name": "email", "Value": EMAIL},
            {"Name": "email_verified", "Value": "true"},  # Cognito 쪽 표시 — 앱 증명이 아니다
        ]
        return self._op("get_user", p, {"Username": USERNAME, "UserAttributes": attrs})

    def get_user_attribute_verification_code(self, **p):
        return self._op("get_user_attribute_verification_code", p, {})

    def verify_user_attribute(self, **p):
        return self._op("verify_user_attribute", p, {})

    def revoke_token(self, **p):
        return self._op("revoke_token", p, {})

    def admin_delete_software_token(self, **p):
        return self._op("admin_delete_software_token", p, {})


def adapter_for(fake: FakeCognito, pool: str = POOL) -> CognitoAdapter:
    return CognitoAdapter(fake, user_pool_id=pool, client_id="cid", client_secret="csecret", region=REGION)


class Pause:
    """훅 — 들어오면 entered 를 세우고 release 까지 기다린다(그 연산이 진행 중인 구간을 고정한다)."""

    def __init__(self, then=None) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self.then = then

    def __call__(self, params):
        self.entered.set()
        assert self.release.wait(10), "pause never released"
        return self.then(params) if self.then is not None else None


def client_error(code: str, status: int) -> ClientError:
    return ClientError(
        {"Error": {"Code": code, "Message": "echo PRIVATE-TEXT"}, "ResponseMetadata": {"HTTPStatusCode": status}},
        "AdminDeleteSoftwareToken",
    )


def raising(exc):
    def hook(params):
        raise exc

    return hook


def on_challenge(name, hook):
    """AdminRespondToAuthChallenge 훅을 한 챌린지에만 건다."""

    def challenge_hook(params):
        return hook(params) if params["ChallengeName"] == name else None

    return challenge_hook


class Crash(BaseException):
    """작업자 중단 — 제공자 호출을 보낸 뒤 결과를 기록하기 전에 프로세스가 사라졌다."""


# --- commit 된 시드 ------------------------------------------------------------------


@dataclass
class World:
    #: 작업자 엔진(운영 get_engine 처럼 pool_pre_ping) — 복구 · 관문 · 조회가 쓴다
    engine: object
    #: 시드 · 연결 끊기용 엔진(작업자 연결이 아니다)
    admin_engine: object
    tenant_id: str
    target: str
    sub: str
    admin: str
    other: str
    target_tokens: tuple[str, ...]
    logged_out: str
    other_token: str
    user_ids: list[str] = field(default_factory=list)

    def make_user(self, *, role=UserRole.VIEWER, account_type=AccountType.TENANT, status=None) -> str:
        with Session(self.engine) as s:
            row = _create_user(s, self.tenant_id, role=role, account_type=account_type, status=status)
            user_id = row.user_id
            s.commit()
        self.user_ids.append(user_id)
        return user_id


def _create_user(s, tenant_id, *, role=UserRole.VIEWER, account_type=AccountType.TENANT, status=None):
    sub = f"sub-{uuid.uuid4().hex}"
    return auth_repo.create_user(
        s,
        issuer=ISSUER,
        cognito_sub=sub,
        email=f"{sub[:16]}@example.com",
        role=role,
        account_type=account_type,
        tenant_id=tenant_id if account_type is AccountType.TENANT else None,
        now=T0,
        status=status or models.AppUserStatus.ACTIVE,
        email_verified_at=T0,
    )


def _issue(s, user_id) -> str:
    return sessions.issue_session(
        s,
        user_id=user_id,
        mfa_step=models.AuthSessionMfaStep.SOFTWARE_TOKEN_MFA,
        now=T0,
        max_seconds=MAX,
        idle_seconds=IDLE,
    ).token


@pytest.fixture()
def world(pg_engine, seed_tenant):
    """대상(TENANT VIEWER · 이메일 증명 있음 · 살아 있는 세션 2 + 로그아웃 1) · 같은 테넌트 ADMIN · 다른 사용자(세션 1)."""
    with Session(pg_engine) as s:
        tenant_id = seed_tenant(s, now=T0)
    with Session(pg_engine) as s:
        target = _create_user(s, tenant_id)
        admin = _create_user(s, tenant_id, role=UserRole.ADMIN)
        other = _create_user(s, tenant_id)
        ids = (target.user_id, target.cognito_sub, admin.user_id, other.user_id)
        live = tuple(_issue(s, target.user_id) for _ in range(2))
        logged_out = _issue(s, target.user_id)
        sessions.revoke_session(s, logged_out, now=T0)
        other_token = _issue(s, other.user_id)
        s.commit()
    worker = create_engine(
        pg_engine.url, pool_pre_ping=True, connect_args={"application_name": WORKER}
    )
    w = World(
        engine=worker,
        admin_engine=pg_engine,
        tenant_id=tenant_id,
        target=ids[0],
        sub=ids[1],
        admin=ids[2],
        other=ids[3],
        target_tokens=live,
        logged_out=logged_out,
        other_token=other_token,
        user_ids=[ids[0], ids[2], ids[3]],
    )
    yield w
    worker.dispose()
    with Session(pg_engine) as s:
        s.execute(delete(models.TotpProviderMutation).where(models.TotpProviderMutation.issuer == ISSUER))
        op = models.TotpRecoveryOperation
        s.execute(delete(op).where(or_(op.target_user_id.in_(w.user_ids), op.actor_user_id.in_(w.user_ids))))
        s.execute(delete(models.AuthSession).where(models.AuthSession.user_id.in_(w.user_ids)))
        s.execute(delete(models.AppUser).where(models.AppUser.user_id.in_(w.user_ids)))
        s.execute(delete(models.Tenant).where(models.Tenant.tenant_id == tenant_id))
        s.commit()


# --- 도우미 ---------------------------------------------------------------------------


def allow(actor_id):
    def authorize(db, target):
        if target is None:
            raise LookupError("no target")
        return actor_id

    return authorize


def reset(world, adapter, op=None, **overrides):
    params = {
        "operation_id": op or uuid.uuid4(),
        "target_user_id": world.target,
        "identity_verified": True,
        "authorize": allow(world.admin),
        "get_adapter": lambda: adapter,
        "clock": lambda: T1,
    }
    params.update(overrides)
    return totp_recovery.reset_totp(world.engine, **params)


def user_state(engine, user_id):
    with engine.connect() as conn:
        return tuple(
            conn.execute(
                text(
                    "SELECT auth_generation, mfa_recovery_state::text, email_verified_at "
                    "FROM app_users WHERE user_id = :u"
                ),
                {"u": user_id},
            ).one()
        )


def reason(engine, token):
    with engine.connect() as conn:
        return conn.execute(
            text("SELECT revoke_reason::text FROM auth_sessions WHERE token_hash = :h"),
            {"h": sessions.hash_token(token)},
        ).scalar_one()


def alive(engine, token) -> bool:
    with Session(engine) as s:
        view = sessions.resolve_session(s, token, now=T1 + timedelta(seconds=1), idle_seconds=IDLE, touch=False)
        return view is not None


def operations(engine, user_id):
    with Session(engine) as s:
        return totp_recovery.list_recovery_operations(s, user_id)


def unresolved(world):
    with Session(world.engine) as s:
        return totp_recovery.get_unresolved_provider_mutation(s, world.target)


def fences(world) -> list[tuple[str, str]]:
    """대상 신원의 울타리 (종류, 풀림) — 풀리지 않았으면 UNRESOLVED."""
    with world.admin_engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT kind::text, coalesce(resolution::text, 'UNRESOLVED') FROM totp_provider_mutations "
                "WHERE issuer = :i AND cognito_sub = :s"
            ),
            {"i": ISSUER, "s": world.sub},
        )
        return sorted(tuple(r) for r in rows)


def drop_worker_connections(world) -> int:
    """연결 유실 — 작업자 엔진의 서버 연결을 모두 끊는다. 다음 사용은 pre-ping 으로 새 연결을 맺는다."""
    with world.admin_engine.connect() as conn:
        dropped = conn.execute(
            text(
                "SELECT count(*) FILTER (WHERE pg_terminate_backend(pid, 5000)) FROM pg_stat_activity "
                "WHERE datname = current_database() AND application_name = :a"
            ),
            {"a": WORKER},
        ).scalar_one()
        conn.commit()
    return dropped


def reconcile(world, mutation_id, **overrides):
    params = {
        "target_user_id": world.target,
        "mutation_id": mutation_id,
        "external_state_verified": True,
        "authorize": allow(world.admin),
        "clock": lambda: T1,
    }
    params.update(overrides)
    return totp_recovery.reconcile_provider_mutation(world.engine, **params)


def open_transactions(conn) -> int:
    return conn.execute(
        text(
            "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
            "AND state LIKE 'idle in transaction%'"
        )
    ).scalar_one()


def login_flow(world, fake) -> LoginFlow:
    gate = totp_recovery.DbLoginGate(lambda: world.engine, clock=lambda: T1)
    return LoginFlow(adapter_for(fake), gate=gate)


def complete(world, identity):
    with Session(world.engine) as s:
        return service.complete_login(
            s, identity, previous_token=None, now=T1, max_seconds=MAX, idle_seconds=IDLE
        )


def old_identity(world, *, generation=0, mfa_step=MfaStep.SOFTWARE_TOKEN_MFA):
    """TOTP · 이메일 증명까지 마친 진행 중 흐름의 결과(세대는 그 흐름이 포착한 값)."""
    return VerifiedIdentity(
        issuer=ISSUER, sub=world.sub, email=EMAIL, mfa_step=mfa_step, email_proven=True, generation=generation
    )


def wait_until_blocked_on_users_lock(engine, timeout=10.0, query="LOCK TABLE app_users%"):
    """그 문장(기본: users 잠금)으로 잠금을 기다리는 연결이 관측될 때까지. query="%" 는 어느 잠금 대기든."""
    deadline = time.monotonic() + timeout
    with engine.connect() as probe:
        while time.monotonic() < deadline:
            waiting = probe.execute(
                text(
                    "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                    "AND wait_event_type = 'Lock' AND query LIKE :q"
                ),
                {"q": query},
            ).scalar_one()
            probe.rollback()
            if waiting:
                return
            time.sleep(0.02)  # 관측 간격일 뿐 — 순서는 pg_stat_activity 관측이 보장한다
    raise AssertionError("never observed a connection waiting on a lock")


def pause_after(monkeypatch, name) -> Pause:
    """recovery_repo 함수의 첫 호출을 실행한 뒤(그 트랜잭션의 커밋 전) 멈춘다 — 그 트랜잭션이 쥔 잠금을 고정한다."""
    original = getattr(recovery_repo, name)
    pause = Pause()

    def paused(*args, **kwargs):
        result = original(*args, **kwargs)
        if not pause.entered.is_set():
            pause(None)
        return result

    monkeypatch.setattr(recovery_repo, name, paused)
    return pause


def unchanged(world, fake):
    """차단 전 상태 그대로 — 세대 0 · NONE · 이메일 증명 · 세션 · 작업 · 복구 울타리 없음 · 제공자 삭제 0회."""
    assert user_state(world.engine, world.target) == (0, "NONE", T0)
    assert all(alive(world.engine, t) for t in world.target_tokens)
    assert alive(world.engine, world.other_token)
    assert operations(world.engine, world.target) == []
    assert [kind for kind, _ in fences(world) if kind == "RESET"] == []
    assert fake.ops("admin_delete_software_token") == []


# --- 정상 경로 · 차단 ---------------------------------------------------------------------


def test_reset_fences_before_provider_then_requires_fresh_enrollment(world):
    fake = FakeCognito(world.sub)
    seen = {}

    def during_delete(params):
        seen["checked_out"] = world.engine.pool.checkedout()  # 작업자가 쥔 DB 연결 — 조회 전에 센다
        with world.admin_engine.connect() as probe:
            seen["open_transactions"] = open_transactions(probe)
        seen["user"] = user_state(world.engine, world.target)
        seen["alive"] = [alive(world.engine, t) for t in world.target_tokens]
        fence = unresolved(world)
        seen["fence"] = (fence.kind, fence.operation_id, fence.resolution)

    fake.hooks["admin_delete_software_token"] = during_delete
    op = uuid.uuid4()
    result = reset(world, adapter_for(fake), op)

    assert result.completed and not result.replayed and result.provider_outcome is D.DELETED
    assert result.operation == totp_recovery.RecoveryOperation(
        operation_id=str(op),
        target_user_id=world.target,
        actor_user_id=world.admin,
        status=R.COMPLETED,
        generation=1,
        revoked_session_count=2,
        created_at=T1,
        finalized_at=T1,
    )
    # 제공자 호출은 1회, 앱 계정의 sub 로 — 이메일 · 사용자 이름으로 고르지 않는다
    assert fake.ops("admin_delete_software_token") == [{"UserPoolId": POOL, "Username": world.sub}]
    # 호출 시점: 차단 · 울타리는 이미 commit 됐고, 작업자는 DB 연결 · 열린 트랜잭션 없이 제공자를 기다린다
    assert seen == {
        "checked_out": 0,
        "open_transactions": 0,
        "user": (1, "RESET_PENDING", None),
        "alive": [False, False],
        "fence": (K.RESET, str(op), None),
    }
    assert user_state(world.engine, world.target) == (1, "ENROLLMENT_REQUIRED", None)
    assert fences(world) == [("RESET", "PROVIDER_RETURNED")]  # 확정 응답과 같은 commit 에 풀렸다
    assert [reason(world.engine, t) for t in world.target_tokens] == ["TOTP_RESET", "TOTP_RESET"]
    assert reason(world.engine, world.logged_out) == "LOGOUT"  # 이미 끝난 세션은 다시 쓰지 않는다
    assert alive(world.engine, world.other_token)  # 다른 사용자는 그대로
    assert user_state(world.engine, world.other) == (0, "NONE", T0)
    with Session(world.engine) as s:
        assert totp_recovery.get_recovery_operation(s, op) == result.operation


def test_new_login_is_blocked_while_pending_and_flows_from_before_never_get_a_session(world):
    fake = FakeCognito(world.sub)
    flow = login_flow(world, fake)
    before, step = flow.start(EMAIL, PASSWORD)  # 차단 전에 시작 — 세대 0 포착
    assert step is LoginStep.TOTP_REQUIRED
    pause = Pause()
    fake.hooks["admin_delete_software_token"] = pause
    with ThreadPoolExecutor(1) as pool:
        running = pool.submit(reset, world, adapter_for(fake))
        try:
            assert pause.entered.wait(10)
            # 차단 commit 뒤 · 삭제 확인 전: 새 흐름은 TOTP 단계에 가지 않는다(AdminGetUser 까지만)
            initiated = len(fake.ops("admin_initiate_auth"))
            with pytest.raises(AuthFailedError):
                flow.start(EMAIL, PASSWORD)
            assert len(fake.ops("admin_initiate_auth")) == initiated + 1
            assert fake.ops("admin_respond_to_auth_challenge") == []
            # 차단 전 흐름은 옛 TOTP · 이메일 코드까지 성공해도 세션을 받지 못한다
            assert flow.submit_totp(before, CODE).step is LoginStep.EMAIL_VERIFICATION_REQUIRED
            done = flow.submit_email_code(before, CODE)
            assert done.step is LoginStep.COMPLETED and done.identity.generation == 0
            with pytest.raises(AuthFailedError):
                complete(world, done.identity)
        finally:
            pause.release.set()
        assert running.result(timeout=10).completed
    with pytest.raises(AuthFailedError):  # 완료 뒤에도 같다
        complete(world, done.identity)
    # 삭제 확인 뒤 남은 등록(기존 TOTP 챌린지)은 새 등록 대기 중이라 진행하지 않는다
    with pytest.raises(AuthFailedError):
        flow.start(EMAIL, PASSWORD)
    assert not any(alive(world.engine, t) for t in world.target_tokens)
    assert user_state(world.engine, world.target) == (1, "ENROLLMENT_REQUIRED", None)
    with Session(world.engine) as s:
        assert s.scalar(
            text("SELECT count(*) FROM auth_sessions WHERE user_id = :u AND revoked_at IS NULL"),
            {"u": world.target},
        ) == 0


def test_email_proven_completion_waiting_on_the_users_lock_loses_to_the_fence(world):
    fake = FakeCognito(world.sub)
    in_fence, release = threading.Event(), threading.Event()
    calls = []

    def authorize(db, target):
        calls.append(target.user_id)
        if len(calls) == 2:  # 차단 트랜잭션 — users 잠금을 쥔 채 멈춘다
            in_fence.set()
            assert release.wait(10)
        return world.admin

    with ThreadPoolExecutor(2) as pool:
        running = pool.submit(reset, world, adapter_for(fake), authorize=authorize)
        try:
            assert in_fence.wait(10)
            login = pool.submit(complete, world, old_identity(world))
            wait_until_blocked_on_users_lock(world.engine)
            assert not login.done()
        finally:
            release.set()
        assert running.result(timeout=10).completed
        with pytest.raises(AuthFailedError):
            login.result(timeout=10)
    assert user_state(world.engine, world.target) == (1, "ENROLLMENT_REQUIRED", None)  # 이메일 증명도 쓰지 않았다
    with Session(world.engine) as s:
        assert s.scalar(
            text("SELECT count(*) FROM auth_sessions WHERE user_id = :u AND revoked_at IS NULL"),
            {"u": world.target},
        ) == 0


def test_login_committed_before_the_fence_is_revoked_by_it(world):
    fake = FakeCognito(world.sub)
    issued = complete(world, old_identity(world))  # 차단 전 완료 — 기존 세션 2개는 SUPERSEDED
    assert alive(world.engine, issued.token)
    result = reset(world, adapter_for(fake))
    assert result.completed and result.operation.revoked_session_count == 1
    assert not alive(world.engine, issued.token)
    assert reason(world.engine, issued.token) == "TOTP_RESET"


@pytest.mark.parametrize("state", [S.RESET_PENDING, S.ENROLLMENT_REQUIRED])
def test_completion_with_current_generation_is_refused_while_recovery_blocks(world, state):
    with Session(world.engine) as s:
        s.execute(
            text(
                "UPDATE app_users SET auth_generation = 1, mfa_recovery_state = CAST(:s AS app_user_mfa_recovery_state) "
                "WHERE user_id = :u"
            ),
            {"s": state.value, "u": world.target},
        )
        s.commit()
    for mfa_step in (MfaStep.SOFTWARE_TOKEN_MFA, MfaStep.MFA_SETUP):
        with pytest.raises(AuthFailedError):
            complete(world, old_identity(world, generation=1, mfa_step=mfa_step))
    assert user_state(world.engine, world.target)[2] == T0  # 이메일 증명 기록을 건드리지 않았다


# --- 등록 직렬화 -------------------------------------------------------------------------------


def _started_setup(flow, *, begin=True):
    token, step = flow.start(EMAIL, PASSWORD)
    assert step is LoginStep.TOTP_SETUP_REQUIRED
    if begin:
        flow.begin_totp_setup(token)
    return token


def test_enrollment_fence_outlives_connection_loss_and_keeps_resets_out_until_the_call_returns(world):
    # 리뷰 일정: 세대 0 등록이 VerifySoftwareToken 에서 멈춘 채 작업자 연결이 끊긴다 — 복구가 끼어들어 삭제한 뒤
    # 옛 검증이 등록되면 안 된다
    fake = FakeCognito(world.sub, challenge="MFA_SETUP")
    flow = login_flow(world, fake)
    token = _started_setup(flow)
    pause = Pause()
    fake.hooks["verify_software_token"] = pause
    with ThreadPoolExecutor(1) as pool:
        verifying = pool.submit(flow.verify_totp_setup, token, CODE)
        try:
            assert pause.entered.wait(10)
            assert world.engine.pool.checkedout() == 0  # 제공자 호출 중 쥔 연결 · 트랜잭션이 없다
            assert drop_worker_connections(world) >= 1
            assert unresolved(world).kind is K.ENROLLMENT  # 울타리는 DB 행이라 연결과 함께 사라지지 않는다
            with pytest.raises(TotpRecoveryRejected) as rejected:
                reset(world, adapter_for(fake))
            assert rejected.value.reason is RecoveryRejection.BUSY
            unchanged(world, fake)
        finally:
            pause.release.set()
        assert verifying.result(timeout=10).step is LoginStep.EMAIL_VERIFICATION_REQUIRED
    assert fences(world) == [("ENROLLMENT", "PROVIDER_RETURNED")] * 2  # 연결 · 검증 구간 모두 확정 응답으로 풀렸다
    # 등록이 끝난 뒤의 새 요청은 정상 진행한다(그 등록을 지우는 새 초기화) — 등록 변경은 모두 삭제보다 먼저다
    assert reset(world, adapter_for(fake)).completed
    names = [name for name, _ in fake.calls]
    last_enrollment = max(
        i for i, name in enumerate(names) if name in ("verify_software_token", "set_user_mfa_preference")
    )
    assert names.index("admin_delete_software_token") > last_enrollment
    with pytest.raises(AuthFailedError):  # 옛 흐름은 이메일 코드까지 마쳐도 세션을 받지 못한다
        complete(world, flow.submit_email_code(token, CODE).identity)


def test_reset_fence_outlives_connection_loss_and_blocks_old_enrollment_and_other_resets(world):
    # 리뷰 반대 일정: 삭제가 멈춘 채 작업자 연결이 끊긴다 — 다른 작업 · 새 등록이 원래 삭제보다 먼저 진행하면 안 된다
    fake = FakeCognito(world.sub, challenge="MFA_SETUP")
    flow = login_flow(world, fake)
    during = _started_setup(flow)
    after = _started_setup(flow)
    not_begun = _started_setup(flow, begin=False)
    assert len(fake.ops("associate_software_token")) == 2
    pause = Pause()
    fake.hooks["admin_delete_software_token"] = pause
    op = uuid.uuid4()
    with ThreadPoolExecutor(1) as pool:
        running = pool.submit(reset, world, adapter_for(fake), op)
        try:
            assert pause.entered.wait(10)
            assert world.engine.pool.checkedout() == 0
            assert drop_worker_connections(world) >= 1
            with pytest.raises(TotpRecoveryRejected) as busy:  # 다른 operation_id
                reset(world, adapter_for(fake))
            assert busy.value.reason is RecoveryRejection.BUSY
            with pytest.raises(AuthFailedError):  # 포착한 세대가 지났다 — 제공자 변경 없음
                flow.verify_totp_setup(during, CODE)
            with pytest.raises(AuthFailedError):  # 차단 중에는 새 흐름도 시작하지 않는다
                flow.start(EMAIL, PASSWORD)
            assert [o.operation_id for o in operations(world.engine, world.target)] == [str(op)]
        finally:
            pause.release.set()
        assert running.result(timeout=10).completed
    with pytest.raises(AuthFailedError):  # 완료 뒤: 포착한 세대가 지났다
        flow.verify_totp_setup(after, CODE)
    with pytest.raises(AuthFailedError):
        flow.begin_totp_setup(not_begun)
    assert fake.ops("verify_software_token") == []
    assert len(fake.ops("associate_software_token")) == 2
    assert fake.ops("set_user_mfa_preference") == []
    assert user_state(world.engine, world.target) == (1, "ENROLLMENT_REQUIRED", None)
    assert len(fake.ops("admin_delete_software_token")) == 1
    # 원래 삭제가 확정 · 기록된 뒤에야 새 등록이 열린다(차단 전 연결 2건 · 복구 1건 모두 확정 응답으로 풀렸다)
    assert fences(world) == [("ENROLLMENT", "PROVIDER_RETURNED")] * 2 + [("RESET", "PROVIDER_RETURNED")]
    _enroll_fresh(flow)
    assert user_state(world.engine, world.target) == (1, "NONE", None)


# --- 재요청 · 새 등록 ------------------------------------------------------------------------------


def _enroll_fresh(flow) -> str:
    token = _started_setup(flow)
    assert flow.verify_totp_setup(token, CODE).step is LoginStep.EMAIL_VERIFICATION_REQUIRED
    return token


def test_same_request_after_fresh_enrollment_returns_original_outcome_without_deleting_again(world):
    fake = FakeCognito(world.sub)
    op = uuid.uuid4()
    first = reset(world, adapter_for(fake), op)
    assert first.completed
    fake.challenge = "MFA_SETUP"  # 삭제 뒤 Cognito 는 새 등록을 요구한다
    flow = login_flow(world, fake)
    token = _enroll_fresh(flow)
    assert user_state(world.engine, world.target) == (1, "NONE", None)  # 새 등록 기록 · 이메일 증명은 아직
    done = flow.submit_email_code(token, CODE)
    assert (done.identity.generation, done.identity.mfa_step, done.identity.email_proven) == (
        1, MfaStep.MFA_SETUP, True,
    )
    issued = complete(world, done.identity)
    assert user_state(world.engine, world.target) == (1, "NONE", T1)

    again = reset(world, adapter_for(fake), op)
    assert again.replayed and again.completed and again.provider_outcome is None
    assert again.operation == first.operation  # 처음 결과 · 폐기 수 그대로
    assert len(fake.ops("admin_delete_software_token")) == 1  # 새 등록을 지우지 않았다
    assert alive(world.engine, issued.token)
    assert user_state(world.engine, world.target) == (1, "NONE", T1)


def test_abandoned_email_step_restarts_with_the_new_totp_and_a_fresh_email_code(world):
    fake = FakeCognito(world.sub)
    assert reset(world, adapter_for(fake)).completed
    fake.challenge = "MFA_SETUP"
    flow = login_flow(world, fake)
    _enroll_fresh(flow)  # 이메일 단계에서 떠난다
    fake.challenge = "SOFTWARE_TOKEN_MFA"  # 새 등록이 Cognito 에 있다
    token, step = flow.start(EMAIL, PASSWORD)
    assert step is LoginStep.TOTP_REQUIRED
    sent = len(fake.ops("get_user_attribute_verification_code"))
    # Cognito 의 email_verified=true 는 앱 증명을 대신하지 않는다 — 이 세대의 이메일 코드를 다시 요구한다
    assert flow.submit_totp(token, CODE).step is LoginStep.EMAIL_VERIFICATION_REQUIRED
    assert len(fake.ops("get_user_attribute_verification_code")) == sent + 1
    done = flow.submit_email_code(token, CODE)
    assert (done.identity.generation, done.identity.mfa_step) == (1, MfaStep.SOFTWARE_TOKEN_MFA)
    assert alive(world.engine, complete(world, done.identity).token)
    assert len(fake.ops("admin_delete_software_token")) == 1
    assert len(operations(world.engine, world.target)) == 1


def test_second_setup_flow_of_the_same_reset_generation_cannot_add_a_factor_after_the_first_enrollment(world):
    # 보안 재현: 같은 복구 세대의 새 등록 흐름 둘 — A 가 새 등록을 기록(NONE)한 뒤 B 가 늦게 둘째 TOTP 를 붙이면,
    # A 의 이메일 증명 뒤 B 의 TOTP 로 이메일 단계 없이 세션을 받는다
    fake = FakeCognito(world.sub)
    assert reset(world, adapter_for(fake)).completed
    fake.challenge = "MFA_SETUP"
    flow = login_flow(world, fake)
    winner = _started_setup(flow)
    late_verify = _started_setup(flow)  # 제공자 연결(Associate)까지 마쳤다
    late_begin = _started_setup(flow, begin=False)
    contender = _started_setup(flow, begin=False)
    assert len(fake.ops("associate_software_token")) == 2
    pause = Pause()
    fake.hooks["verify_software_token"] = pause
    with ThreadPoolExecutor(1) as pool:
        verifying = pool.submit(flow.verify_totp_setup, winner, CODE)
        try:
            assert pause.entered.wait(10)
            # 진행 중인 등록 울타리 — 같은 신원의 다른 등록 변경은 제공자 호출 · 울타리 행 없이 503
            with pytest.raises(AuthProviderUnavailableError):
                flow.begin_totp_setup(contender)
            assert len(fake.ops("associate_software_token")) == 2
            assert fences(world) == sorted([("ENROLLMENT", "PROVIDER_RETURNED")] * 2 + [
                ("ENROLLMENT", "UNRESOLVED"), ("RESET", "PROVIDER_RETURNED"),
            ])
        finally:
            pause.release.set()
        won = verifying.result(timeout=10)
    fake.hooks.clear()
    assert (won.step, won.identity) == (LoginStep.EMAIL_VERIFICATION_REQUIRED, None)  # 이메일 전에는 세션이 없다
    assert user_state(world.engine, world.target) == (1, "NONE", None)
    settled = fences(world)
    # 늦은 흐름: 포착 때 새 등록 대기였다 — 기록된 새 등록 뒤에는 제공자 변경 전에 거절한다(울타리 행도 없다)
    with pytest.raises(AuthFailedError):
        flow.verify_totp_setup(late_verify, CODE)
    with pytest.raises(AuthFailedError):
        flow.begin_totp_setup(late_begin)
    assert len(fake.ops("verify_software_token")) == 1  # A 의 등록 하나뿐
    assert len(fake.ops("associate_software_token")) == 2
    assert [p["ChallengeName"] for p in fake.ops("admin_respond_to_auth_challenge")] == ["MFA_SETUP"]
    assert len(fake.ops("set_user_mfa_preference")) == 1
    assert fences(world) == settled
    assert user_state(world.engine, world.target) == (1, "NONE", None)
    # 이긴 흐름은 그대로 이메일 코드를 증명하고 세션을 받는다
    done = flow.submit_email_code(winner, CODE)
    assert (done.step, done.identity.generation, done.identity.mfa_step, done.identity.email_proven) == (
        LoginStep.COMPLETED, 1, MfaStep.MFA_SETUP, True,
    )
    assert alive(world.engine, complete(world, done.identity).token)
    assert user_state(world.engine, world.target) == (1, "NONE", T1)


def test_setup_failing_after_verify_success_is_not_recorded_and_restart_needs_a_full_fresh_setup(world):
    fake = FakeCognito(world.sub)
    assert reset(world, adapter_for(fake)).completed
    fake.challenge = "MFA_SETUP"
    fake.hooks["admin_respond_to_auth_challenge"] = on_challenge(
        "MFA_SETUP", raising(client_error("NotAuthorizedException", 400))
    )
    flow = login_flow(world, fake)
    token = _started_setup(flow)
    with pytest.raises(AuthFlowExpiredError):
        flow.verify_totp_setup(token, CODE)
    assert len(fake.ops("verify_software_token")) == 1  # VerifySoftwareToken 은 성공했다
    assert user_state(world.engine, world.target) == (1, "ENROLLMENT_REQUIRED", None)  # 기록하지 않았다
    assert unresolved(world) is None  # 확정 응답 — 울타리는 풀렸다
    fake.hooks.clear()
    # 다시 시작: VerifySoftwareToken 이 남긴 등록(기존 TOTP 챌린지)으로는 진행하지 않는다
    fake.challenge = "SOFTWARE_TOKEN_MFA"
    with pytest.raises(AuthFailedError):
        flow.start(EMAIL, PASSWORD)
    assert [p["ChallengeName"] for p in fake.ops("admin_respond_to_auth_challenge")] == ["MFA_SETUP"]
    # 새 MFA_SETUP 이 AuthenticationResult 까지 성공해야 기록되고, 이메일 코드는 여전히 필요하다
    fake.challenge = "MFA_SETUP"
    token = _enroll_fresh(flow)
    assert user_state(world.engine, world.target) == (1, "NONE", None)
    done = flow.submit_email_code(token, CODE)
    assert alive(world.engine, complete(world, done.identity).token)


def test_enrollment_whose_fence_was_reconciled_mid_flight_records_nothing_and_revokes_its_tokens(world):
    # 운영자 진술이 틀린 경우(호출이 아직 진행 중) — 늦게 끝난 등록은 새 세대의 대기를 끝내지 못하고 토큰을 버린다
    fake = FakeCognito(world.sub, challenge="MFA_SETUP")
    flow = login_flow(world, fake)
    token = _started_setup(flow)
    pause = Pause()
    fake.hooks["admin_respond_to_auth_challenge"] = on_challenge("MFA_SETUP", pause)
    with ThreadPoolExecutor(1) as pool:
        verifying = pool.submit(flow.verify_totp_setup, token, CODE)
        try:
            assert pause.entered.wait(10)
            fence = unresolved(world)
            assert reconcile(world, fence.mutation_id).resolution is Res.OPERATOR_RECONCILED
            assert reset(world, adapter_for(fake)).completed
        finally:
            pause.release.set()
        with pytest.raises(AuthFailedError):
            verifying.result(timeout=10)
    assert fake.ops("revoke_token") == [{"Token": "RT", "ClientId": "cid", "ClientSecret": "csecret"}]
    assert fake.ops("get_user_attribute_verification_code") == []
    assert user_state(world.engine, world.target) == (1, "ENROLLMENT_REQUIRED", None)
    assert fences(world) == [
        ("ENROLLMENT", "OPERATOR_RECONCILED"),
        ("ENROLLMENT", "PROVIDER_RETURNED"),
        ("RESET", "PROVIDER_RETURNED"),
    ]


@pytest.mark.parametrize(
    "hook,error,kept",
    [
        pytest.param(raising(client_error("CodeMismatchException", 400)), AuthCodeInvalidError, False,
                     id="code-mismatch"),
        pytest.param(raising(client_error("InternalErrorException", 500)), AuthProviderUnavailableError, True,
                     id="5xx"),
        pytest.param(raising(ReadTimeoutError(endpoint_url="https://x.invalid")), AuthProviderUnavailableError,
                     True, id="timeout"),
    ],
)
def test_enrollment_outcome_decides_whether_the_fence_survives(world, hook, error, kept):
    fake = FakeCognito(world.sub, challenge="MFA_SETUP")
    flow = login_flow(world, fake)
    token = _started_setup(flow)
    fake.hooks["verify_software_token"] = hook
    with pytest.raises(error):
        flow.verify_totp_setup(token, CODE)
    fake.hooks.clear()
    if not kept:  # 확정 응답 — 울타리가 풀리고 복구가 바로 진행한다
        assert fences(world) == [("ENROLLMENT", "PROVIDER_RETURNED")] * 2
        assert reset(world, adapter_for(fake)).completed
        return
    fence = unresolved(world)
    assert fence.kind is K.ENROLLMENT
    # 결과 불명 — 새 operation_id 의 복구도, 같은 신원의 새 등록 변경도 시작하지 않는다
    with pytest.raises(TotpRecoveryRejected) as busy:
        reset(world, adapter_for(fake))
    assert busy.value.reason is RecoveryRejection.BUSY
    again, step = flow.start(EMAIL, PASSWORD)
    assert step is LoginStep.TOTP_SETUP_REQUIRED
    with pytest.raises(AuthProviderUnavailableError):
        flow.begin_totp_setup(again)
    assert len(fake.ops("associate_software_token")) == 1
    unchanged(world, fake)
    # 운영자 정리는 울타리만 푼다 — 사용자 상태를 바꾸지 않고 제공자를 부르지 않는다
    done = reconcile(world, fence.mutation_id)
    assert (done.mutation_id, done.kind, done.resolution, done.resolved_at, done.reconciled_by) == (
        fence.mutation_id, K.ENROLLMENT, Res.OPERATOR_RECONCILED, T1, world.admin,
    )
    unchanged(world, fake)
    assert unresolved(world) is None
    assert reset(world, adapter_for(fake)).completed


def test_concurrent_distinct_requests_never_overlap_and_in_flight_duplicate_is_not_success(world):
    fake = FakeCognito(world.sub)
    pause = Pause()
    fake.hooks["admin_delete_software_token"] = pause
    op = uuid.uuid4()
    with ThreadPoolExecutor(1) as pool:
        running = pool.submit(reset, world, adapter_for(fake), op)
        try:
            assert pause.entered.wait(10)
            with pytest.raises(TotpRecoveryRejected) as busy:
                reset(world, adapter_for(fake))
            assert busy.value.reason is RecoveryRejection.BUSY
            duplicate = reset(world, adapter_for(fake), op)
            assert duplicate.replayed and not duplicate.completed
            assert duplicate.operation.status is R.PROVIDER_PENDING
            assert user_state(world.engine, world.target) == (1, "RESET_PENDING", None)
            assert [o.operation_id for o in operations(world.engine, world.target)] == [str(op)]
        finally:
            pause.release.set()
        assert running.result(timeout=10).completed
    assert len(fake.ops("admin_delete_software_token")) == 1


# --- 제공자 결과 · 기록 실패 -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "hook,outcome,status,state,fence",
    [
        pytest.param(raising(client_error("ResourceNotFoundException", 400)), D.NOT_FOUND,
                     R.PROVIDER_NOT_FOUND, "ENROLLMENT_REQUIRED", "PROVIDER_RETURNED", id="not-found"),
        pytest.param(raising(client_error("NotAuthorizedException", 400)), D.REJECTED,
                     R.PROVIDER_REJECTED, "RESET_PENDING", "PROVIDER_RETURNED", id="iam-denied"),
        pytest.param(raising(client_error("TooManyRequestsException", 400)), D.REJECTED,
                     R.PROVIDER_REJECTED, "RESET_PENDING", "PROVIDER_RETURNED", id="throttled"),
        pytest.param(raising(client_error("InternalErrorException", 500)), D.UNKNOWN,
                     R.PROVIDER_UNKNOWN, "RESET_PENDING", "UNRESOLVED", id="5xx"),
        pytest.param(raising(ReadTimeoutError(endpoint_url="https://x.invalid")), D.UNKNOWN,
                     R.PROVIDER_UNKNOWN, "RESET_PENDING", "UNRESOLVED", id="timeout"),
        pytest.param(raising(RuntimeError("unclassified")), D.UNKNOWN,
                     R.PROVIDER_UNKNOWN, "RESET_PENDING", "UNRESOLVED", id="unclassified"),
    ],
)
def test_non_deleted_outcomes_are_durable_non_success_and_never_retried(
    world, hook, outcome, status, state, fence
):
    fake = FakeCognito(world.sub)
    fake.hooks["admin_delete_software_token"] = hook
    op = uuid.uuid4()
    result = reset(world, adapter_for(fake), op)
    assert not result.completed and result.provider_outcome is outcome
    assert (result.operation.status, result.operation.finalized_at) == (status, T1)
    assert user_state(world.engine, world.target) == (1, state, None)
    assert fences(world) == [("RESET", fence)]  # 결과 불명만 울타리를 남긴다
    assert not any(alive(world.engine, t) for t in world.target_tokens)
    again = reset(world, adapter_for(fake), op)
    assert again.replayed and again.operation == result.operation
    assert len(fake.ops("admin_delete_software_token")) == 1
    # 차단 중이면 로그인 시작부터, 새 등록 대기면 기존 TOTP 챌린지에서 멈춘다
    with pytest.raises(AuthFailedError):
        login_flow(world, fake).start(EMAIL, PASSWORD)
    assert fake.ops("admin_respond_to_auth_challenge") == []


def _lost_finalization(*args, **kwargs):
    raise OperationalError("UPDATE totp_recovery_operations", {}, Exception("connection lost"))


@pytest.mark.parametrize("case", ["worker-death", "finalization-failure", "timeout", "connection-loss"])
def test_unresolved_delete_blocks_every_new_mutation_until_an_operator_reconciles(world, monkeypatch, case):
    # 원래 삭제가 늦게 적용될 수 있는 동안: 새 operation_id 의 복구 · 새 등록이 먼저 진행하면 새 등록이 지워진다
    fake = FakeCognito(world.sub)
    op = uuid.uuid4()
    if case == "worker-death":  # 삭제를 보낸 뒤 결과 기록 전에 작업자가 사라진다
        fake.hooks["admin_delete_software_token"] = raising(Crash())
        with pytest.raises(Crash):
            reset(world, adapter_for(fake), op)
        expected = (R.PROVIDER_PENDING, None)
    elif case == "finalization-failure":  # 삭제 확인을 받았지만 기록하지 못했다
        monkeypatch.setattr(recovery_repo, "finalize_operation", _lost_finalization)
        result = reset(world, adapter_for(fake), op)
        assert not result.completed and result.provider_outcome is D.DELETED
        monkeypatch.undo()
        expected = (R.PROVIDER_PENDING, None)
    elif case == "timeout":
        fake.hooks["admin_delete_software_token"] = raising(ReadTimeoutError(endpoint_url="https://x.invalid"))
        assert reset(world, adapter_for(fake), op).provider_outcome is D.UNKNOWN
        expected = (R.PROVIDER_UNKNOWN, T1)
    else:  # 삭제 중 연결이 끊기고 응답도 시간 초과
        def dropped(params):
            assert drop_worker_connections(world) >= 1
            raise ReadTimeoutError(endpoint_url="https://x.invalid")

        fake.hooks["admin_delete_software_token"] = dropped
        assert reset(world, adapter_for(fake), op).provider_outcome is D.UNKNOWN
        expected = (R.PROVIDER_UNKNOWN, T1)
    fake.hooks.clear()
    drop_worker_connections(world)  # 연결 · 작업자가 사라져도 울타리는 남는다
    (pending,) = operations(world.engine, world.target)
    assert (pending.status, pending.finalized_at) == expected
    fence = unresolved(world)
    assert (fence.kind, fence.operation_id) == (K.RESET, str(op))
    assert user_state(world.engine, world.target) == (1, "RESET_PENDING", None)
    # 같은 요청은 이어받아 다시 지우지 않는다
    again = reset(world, adapter_for(fake), op)
    assert again.replayed and not again.completed and again.operation == pending
    # 새 operation_id 도 울타리를 풀지 않는다
    with pytest.raises(TotpRecoveryRejected) as busy:
        reset(world, adapter_for(fake))
    assert busy.value.reason is RecoveryRejection.BUSY
    with pytest.raises(AuthFailedError):
        login_flow(world, fake).start(EMAIL, PASSWORD)
    assert len(fake.ops("admin_delete_software_token")) == 1
    assert operations(world.engine, world.target) == [pending]
    # 운영자 정리: 진술한 그 울타리만 푼다 — 작업 결과 · 사용자 차단은 그대로(성공이 아니다)
    done = reconcile(world, fence.mutation_id)
    assert (done.kind, done.operation_id, done.resolution) == (K.RESET, str(op), Res.OPERATOR_RECONCILED)
    assert operations(world.engine, world.target) == [pending]
    assert user_state(world.engine, world.target) == (1, "RESET_PENDING", None)
    with pytest.raises(AuthFailedError):
        login_flow(world, fake).start(EMAIL, PASSWORD)
    # 그 뒤의 새 초기화는 행위자의 명시적 새 요청이다
    fresh = reset(world, adapter_for(fake))
    assert fresh.completed and fresh.operation.generation == 2
    assert user_state(world.engine, world.target) == (2, "ENROLLMENT_REQUIRED", None)
    assert [(o.generation, o.status) for o in operations(world.engine, world.target)] == [
        (1, expected[0]),
        (2, R.COMPLETED),
    ]
    assert fences(world) == [("RESET", "OPERATOR_RECONCILED"), ("RESET", "PROVIDER_RETURNED")]


@pytest.mark.parametrize("case", ["not-attested", "denied", "self", "other-mutation", "already-resolved"])
def test_reconciliation_needs_attestation_authority_and_the_exact_unresolved_fence(world, case):
    fake = FakeCognito(world.sub)
    fake.hooks["admin_delete_software_token"] = raising(ReadTimeoutError(endpoint_url="https://x.invalid"))
    assert reset(world, adapter_for(fake)).provider_outcome is D.UNKNOWN
    fence = unresolved(world)
    overrides: dict = {}
    expected: type[Exception] | RecoveryRejection
    if case == "not-attested":
        overrides.update(external_state_verified=False, authorize=_deny)  # 판정보다 먼저 거절한다
        expected = RecoveryRejection.EXTERNAL_STATE_NOT_CONFIRMED
    elif case == "denied":
        overrides["authorize"], expected = _deny, Denied
    elif case == "self":
        overrides["authorize"], expected = allow(world.target), RecoveryRejection.SELF_TARGET
    elif case == "other-mutation":
        overrides["mutation_id"], expected = uuid.uuid4(), RecoveryRejection.MUTATION_NOT_FOUND
    else:
        reconcile(world, fence.mutation_id)
        expected = RecoveryRejection.MUTATION_NOT_FOUND
    params = {"mutation_id": fence.mutation_id, **overrides}
    if isinstance(expected, RecoveryRejection):
        with pytest.raises(TotpRecoveryRejected) as rejected:
            reconcile(world, **params)
        assert rejected.value.reason is expected
    else:
        with pytest.raises(expected):
            reconcile(world, **params)
    want = "OPERATOR_RECONCILED" if case == "already-resolved" else "UNRESOLVED"
    assert fences(world) == [("RESET", want)]
    with Session(world.engine) as s:
        reconciled_by = s.scalar(
            text("SELECT reconciled_by FROM totp_provider_mutations WHERE mutation_id = :m"),
            {"m": fence.mutation_id},
        )
    assert reconciled_by == (uuid.UUID(world.admin) if case == "already-resolved" else None)
    assert user_state(world.engine, world.target) == (1, "RESET_PENDING", None)
    assert len(fake.ops("admin_delete_software_token")) == 1


# --- 판정 순서 · 거절 ------------------------------------------------------------------------------


def test_authorization_runs_before_adapter_assembly_and_again_under_the_write_lock(world):
    fake = FakeCognito(world.sub)
    events = []

    def authorize(db, target):
        held = db.execute(
            text(
                "SELECT count(*) FROM pg_locks l JOIN pg_class c ON c.oid = l.relation "
                "WHERE c.relname = 'app_users' AND l.mode = 'ExclusiveLock' AND l.granted "
                "AND l.pid = pg_backend_pid()"
            )
        ).scalar_one()
        events.append(("authorize", bool(held)))
        return world.admin

    def get_adapter():
        events.append("adapter")
        return adapter_for(fake)

    assert reset(world, None, authorize=authorize, get_adapter=get_adapter).completed
    assert events == [("authorize", False), "adapter", ("authorize", True)]


class Denied(Exception):
    pass


def _deny(db, target):
    raise Denied()


@pytest.mark.parametrize(
    "case",
    ["not-confirmed", "denied", "unassigned", "disabled", "self", "missing", "malformed", "foreign-pool"],
)
def test_rejections_change_nothing(world, case):
    fake = FakeCognito(world.sub)
    built = []

    def get_adapter():
        built.append(1)
        return adapter_for(fake, pool="ap-northeast-2_Other" if case == "foreign-pool" else POOL)

    overrides = {"get_adapter": get_adapter}
    expected: type[Exception] | RecoveryRejection = TotpRecoveryRejected
    if case == "not-confirmed":
        overrides.update(identity_verified=False, authorize=_deny)  # 판정보다 먼저 거절한다
        expected = RecoveryRejection.IDENTITY_NOT_CONFIRMED
    elif case == "denied":
        overrides["authorize"], expected = _deny, Denied
    elif case == "unassigned":
        overrides["target_user_id"] = world.make_user(account_type=AccountType.UNASSIGNED)
        expected = RecoveryRejection.TARGET_INELIGIBLE
    elif case == "disabled":
        overrides["target_user_id"] = world.make_user(status=models.AppUserStatus.DISABLED)
        expected = RecoveryRejection.TARGET_INELIGIBLE
    elif case == "self":
        overrides["authorize"], expected = allow(world.target), RecoveryRejection.SELF_TARGET
    elif case in ("missing", "malformed"):
        overrides["target_user_id"] = str(uuid.uuid4()) if case == "missing" else "not-a-uuid"
        overrides["authorize"] = lambda db, target: world.admin  # 대상 없음도 통과시키는 판정이어도 거절된다
        expected = RecoveryRejection.TARGET_NOT_FOUND
    else:
        expected = RecoveryRejection.PROVIDER_IDENTITY_MISMATCH
    if isinstance(expected, RecoveryRejection):
        with pytest.raises(TotpRecoveryRejected) as rejected:
            reset(world, None, **overrides)
        assert rejected.value.reason is expected
        assert str(rejected.value) == expected.value
    else:
        with pytest.raises(expected):
            reset(world, None, **overrides)
    assert built == ([1] if case == "foreign-pool" else [])  # 판정 실패는 Cognito 클라이언트를 만들지 않는다
    unchanged(world, fake)


def test_denial_under_the_write_lock_changes_nothing_and_leaves_no_fence(world):
    fake = FakeCognito(world.sub)
    calls = []

    def authorize(db, target):
        calls.append(1)
        if len(calls) == 2:  # 읽기 판정 뒤 강등 · 회수된 행위자
            raise Denied()
        return world.admin

    with pytest.raises(Denied):
        reset(world, adapter_for(fake), authorize=authorize)
    unchanged(world, fake)
    assert fences(world) == []


def test_operation_id_belongs_to_one_target(world):
    fake = FakeCognito(world.sub)
    op = uuid.uuid4()
    assert reset(world, adapter_for(fake), op).completed
    with pytest.raises(TotpRecoveryRejected) as rejected:
        reset(world, adapter_for(fake), op, target_user_id=world.other)
    assert rejected.value.reason is RecoveryRejection.OPERATION_CONFLICT
    assert user_state(world.engine, world.other) == (0, "NONE", T0)
    assert alive(world.engine, world.other_token)
    assert len(fake.ops("admin_delete_software_token")) == 1


# --- 잠금 순서: 울타리 행보다 users 잠금이 먼저 ---------------------------------------------------------


def test_enrollment_record_and_a_concurrent_enrollment_claim_do_not_deadlock(monkeypatch, world):
    # 리뷰 일정: A 가 울타리를 풀고(커밋 전) app_users 를 바꾸기 전에 B 가 users 잠금 → 같은 신원 울타리 생성
    fake = FakeCognito(world.sub, challenge="MFA_SETUP")
    flow = login_flow(world, fake)
    first = _started_setup(flow)
    second = _started_setup(flow, begin=False)
    pause = pause_after(monkeypatch, "resolve_mutation")
    with ThreadPoolExecutor(2) as pool:
        recording = pool.submit(flow.verify_totp_setup, first, CODE)
        try:
            assert pause.entered.wait(10)
            claiming = pool.submit(flow.begin_totp_setup, second)
            wait_until_blocked_on_users_lock(world.admin_engine, query="%")
        finally:
            pause.release.set()
        assert recording.result(timeout=10).step is LoginStep.EMAIL_VERIFICATION_REQUIRED
        claiming.result(timeout=10)
    assert len(fake.ops("associate_software_token")) == 2
    assert len(fake.ops("verify_software_token")) == 1
    assert fences(world) == [("ENROLLMENT", "PROVIDER_RETURNED")] * 3
    assert user_state(world.engine, world.target) == (0, "NONE", T0)


def test_reset_finalization_and_a_concurrent_reconcile_do_not_deadlock(monkeypatch, world):
    # A 가 삭제 확인을 기록하며 울타리를 풀었고(커밋 전) 대상 상태를 바꾸기 전 — 운영자 정리 B 가 끼어든다
    fake = FakeCognito(world.sub)
    pause = pause_after(monkeypatch, "resolve_mutation")
    with ThreadPoolExecutor(2) as pool:
        resetting = pool.submit(reset, world, adapter_for(fake))
        try:
            assert pause.entered.wait(10)
            fence = unresolved(world)  # 결과 기록이 아직 커밋되지 않았다
            reconciling = pool.submit(reconcile, world, fence.mutation_id)
            wait_until_blocked_on_users_lock(world.admin_engine, query="%")
        finally:
            pause.release.set()
        assert resetting.result(timeout=10).completed
        with pytest.raises(TotpRecoveryRejected) as late:
            reconciling.result(timeout=10)
    assert late.value.reason is RecoveryRejection.MUTATION_NOT_FOUND  # 이미 확정 응답으로 풀렸다
    assert fences(world) == [("RESET", "PROVIDER_RETURNED")]
    assert user_state(world.engine, world.target) == (1, "ENROLLMENT_REQUIRED", None)


# --- 실제 SDK 재시도 기계(HTTP 층 대역 — 네트워크 없음) ---------------------------------------------------


class _Body:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def stream(self, *a, **k):
        yield self._data


class CognitoHttp:
    """botocore HTTP 전송(URLLib3Session.send) 대역 — 운영 조립 클라이언트의 서명 · 재시도 기계를 그대로 타고
    네트워크에는 나가지 않는다. 연산별 script 에서 시도마다 결과를 꺼낸다("timeout" · (상태, 본문)) — 비면 기본 응답."""

    def __init__(self, sub: str, challenge: str) -> None:
        attrs = [{"Name": "sub", "Value": sub}, {"Name": "email", "Value": EMAIL}]
        session = "session-{}-padded-to-the-sdk-minimum".format  # SDK 파라미터 검증: Session 20자 이상
        self.defaults = {
            "AdminInitiateAuth": {
                "ChallengeName": challenge,
                "Session": session(0),
                "ChallengeParameters": {"USER_ID_FOR_SRP": USERNAME},
            },
            "AdminGetUser": {"Username": USERNAME, "UserAttributes": attrs, "UserStatus": "CONFIRMED"},
            "AssociateSoftwareToken": {"SecretCode": "JBSWY3DPEHPK3PXP", "Session": session(1)},
            "VerifySoftwareToken": {"Status": "SUCCESS", "Session": session(2)},
            "AdminRespondToAuthChallenge": {
                "AuthenticationResult": {"AccessToken": "AT", "RefreshToken": "RT", "ExpiresIn": 300}
            },
            "SetUserMFAPreference": {},
            "GetUser": {
                "Username": USERNAME,
                "UserAttributes": [*attrs, {"Name": "email_verified", "Value": "true"}],
            },
            "GetUserAttributeVerificationCode": {},
            "RevokeToken": {},
            "AdminDeleteSoftwareToken": {},
        }
        self.script: dict[str, list] = {}
        self.sent: list[str] = []

    def count(self, op: str) -> int:
        return self.sent.count(op)

    def send(self, request):
        assert request.url.startswith(f"https://cognito-idp.{REGION}.amazonaws.com"), request.url
        target = request.headers["X-Amz-Target"]
        op = (target.decode() if isinstance(target, bytes) else target).rsplit(".", 1)[1]
        self.sent.append(op)
        planned = self.script.get(op)
        action = planned.pop(0) if planned else (200, self.defaults[op])
        if action == "timeout":
            raise ReadTimeoutError(endpoint_url=request.url)
        status, body = action
        return AWSResponse(request.url, status, {}, _Body(json.dumps(body).encode()))


def cognito_http(monkeypatch, sub="sub-1", challenge="MFA_SETUP") -> CognitoHttp:
    for name in ("AWS_PROFILE", "AWS_SESSION_TOKEN", "COGNITO_AWS_PROFILE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    monkeypatch.setenv("AWS_ENDPOINT_URL", "http://localhost:4566")  # 인증 클라이언트는 무시한다
    fake = CognitoHttp(sub, challenge)
    monkeypatch.setattr(botocore.httpsession.URLLib3Session, "send", lambda self, request: fake.send(request))
    return fake


def auth_settings() -> AuthSettings:
    return AuthSettings(
        _env_file=None,
        COGNITO_REGION=REGION,
        COGNITO_USER_POOL_ID=POOL,
        COGNITO_CLIENT_ID="cid",
        COGNITO_CLIENT_SECRET="csecret",
    )


def production_flow(monkeypatch, world) -> LoginFlow:
    """운영 조립(provider.build_login_flow) 그대로 — DB 만 테스트 엔진이다."""
    monkeypatch.setattr(provider, "get_engine", lambda: world.engine)
    return provider.build_login_flow(auth_settings())


@pytest.mark.parametrize(
    "build,reads",
    [(provider.build_recovery_adapter, 1), (provider.build_adapter, 3)],
    ids=["recovery-adapter", "default-adapter"],
)
def test_every_built_adapter_sends_totp_delete_once_while_ordinary_calls_keep_their_policy(monkeypatch, build, reads):
    http = cognito_http(monkeypatch)
    # 첫 시도 시간 초과 뒤 재시도가 있었다면 4xx(등록 없음)를 받아 첫 시도를 가렸을 것이다
    http.script = {
        "AdminDeleteSoftwareToken": ["timeout", (400, {"__type": "ResourceNotFoundException", "message": "x"})],
        "AdminGetUser": ["timeout"] * 3,
    }
    adapter = build(auth_settings())
    assert adapter.admin_delete_software_token("sub-1") is D.UNKNOWN
    assert http.count("AdminDeleteSoftwareToken") == 1
    with pytest.raises(AuthProviderUnavailableError):
        adapter.admin_get_user(EMAIL)
    assert http.count("AdminGetUser") == reads  # 일반 어댑터의 조회는 재시도(최대 2회)를 그대로 둔다


@pytest.mark.parametrize(
    "op",
    ["AssociateSoftwareToken", "VerifySoftwareToken", "AdminRespondToAuthChallenge", "SetUserMFAPreference"],
)
def test_production_login_flow_sends_each_enrollment_mutation_once_and_keeps_the_fence_on_timeout(
    monkeypatch, world, op
):
    # 리뷰 재현: 첫 시도 시간 초과 · 재시도가 4xx 를 받으면 마지막 응답이 첫 시도를 가려 울타리가 풀렸다
    http = cognito_http(monkeypatch, world.sub)
    flow = production_flow(monkeypatch, world)
    http.script = {
        "AdminInitiateAuth": ["timeout"],  # 일반 호출은 재시도한다
        "AdminGetUser": ["timeout"],
        op: ["timeout", (400, {"__type": "CodeMismatchException", "message": "PRIVATE"})],
    }
    token, step = flow.start(EMAIL, PASSWORD)
    assert step is LoginStep.TOTP_SETUP_REQUIRED
    assert (http.count("AdminInitiateAuth"), http.count("AdminGetUser")) == (2, 2)
    if op == "AssociateSoftwareToken":
        with pytest.raises(AuthProviderUnavailableError):
            flow.begin_totp_setup(token)
    else:
        flow.begin_totp_setup(token)
        with pytest.raises(AuthProviderUnavailableError):
            flow.verify_totp_setup(token, CODE)
    assert http.count(op) == 1  # 재시도 없음 — 첫 시도의 적용 여부를 모른다
    fence = unresolved(world)
    assert (fence.kind, fence.resolution) == (K.ENROLLMENT, None)
    with pytest.raises(TotpRecoveryRejected) as busy:
        reset(world, adapter_for(FakeCognito(world.sub)))
    assert busy.value.reason is RecoveryRejection.BUSY
    assert user_state(world.engine, world.target) == (0, "NONE", T0)


def test_production_login_flow_keeps_sdk_retries_for_ordinary_calls(monkeypatch, world):
    http = cognito_http(monkeypatch, world.sub, challenge="SOFTWARE_TOKEN_MFA")
    flow = production_flow(monkeypatch, world)
    ordinary = ("AdminInitiateAuth", "AdminGetUser", "AdminRespondToAuthChallenge", "GetUser")
    http.script = {op: ["timeout"] for op in ordinary}
    token, step = flow.start(EMAIL, PASSWORD)
    assert step is LoginStep.TOTP_REQUIRED
    assert flow.submit_totp(token, CODE).step is LoginStep.COMPLETED
    assert [http.count(op) for op in ordinary] == [2, 2, 2, 2]  # SOFTWARE_TOKEN_MFA 응답은 일반 재시도
    assert fences(world) == []  # TOTP 증명은 등록 변경이 아니다
