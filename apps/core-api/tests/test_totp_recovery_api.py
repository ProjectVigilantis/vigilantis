"""POST /api/v1/users/{user_id}/totp/reset (#515) — 실제 HTTP + PostgreSQL, Cognito 는 전부 대역.

지키는 것: 행위자 · 대상 범위(승인된 표) · 거절 요청의 Cognito 클라이언트 조립 0회 · 204 는 COMPLETED 기록뿐 ·
같은 operation_id 재요청은 제공자를 다시 부르지 않음. 코어 내부 경합은 test_auth_totp_recovery.py 가 지킨다.
복구 코어가 전용 연결을 열므로 시드는 commit 한다(committed_auth) — 요청별 세션도 실제 엔진에서 연다.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from auth import sessions
from auth.cognito import SoftwareTokenDeletion as D
from db import models
from db.session import get_db
from fastapi.testclient import TestClient
from routers import users as users_router
from schemas.api.auth import AccountType, PlatformRole, UserRole
from schemas.api.tenants import TenantStatus
from sqlalchemy import delete, or_, select, text
from sqlalchemy.orm import Session

ISSUER = "https://cognito-idp.ap-northeast-2.amazonaws.com/ap-northeast-2_test"  # conftest TEST_AUTH_ISSUER


#: 복구 경로 표 — 일반 TARGET_OPS(test_user_admin_api.py)와 행위자 정책이 달라 따로 둔다.
#: Env.post 가 이 표로 요청하고, test_user_admin_api 의 경로 전수 검사가 두 표를 합쳐 등록 경로와 비교한다.
RECOVERY_OPS = [("POST", "/{id}/totp/reset")]
BASE = "/api/v1/users"


class FakeRecoveryAdapter:
    issuer = ISSUER

    def __init__(self) -> None:
        self.deleted: list[str] = []
        self.outcome = D.DELETED

    def admin_delete_software_token(self, username):
        self.deleted.append(username)
        return self.outcome


class Env:
    def __init__(self, engine, auth, client, adapter) -> None:
        self.engine, self.auth, self.client, self.adapter = engine, auth, client, adapter
        self.built = 0

    def tenant(self, status=None) -> str:
        return self.auth.make_tenant(status)

    def user(self, role=UserRole.VIEWER, **kwargs):
        return self.auth.make(role, **kwargs)

    def extra_sessions(self, user_id: str, count: int = 2) -> list[str]:
        with Session(self.engine) as s:
            tokens = [
                sessions.issue_session(
                    s,
                    user_id=user_id,
                    mfa_step=models.AuthSessionMfaStep.SOFTWARE_TOKEN_MFA,
                    now=datetime.now(UTC),
                    max_seconds=28800,
                    idle_seconds=1800,
                ).token
                for _ in range(count)
            ]
            s.commit()
        return tokens

    def post(self, actor, user_id, body=None, *, headers=None, op=None):
        payload = {"operation_id": str(op or uuid.uuid4()), "identity_verified": True} if body is None else body
        [(method, path)] = RECOVERY_OPS
        return self.client.request(
            method,
            BASE + path.replace("{id}", user_id),
            json=payload,
            headers=actor.headers if headers is None else headers,
        )

    def row(self, user_id):
        with self.engine.connect() as conn:
            return tuple(
                conn.execute(
                    text(
                        "SELECT auth_generation, mfa_recovery_state::text, email_verified_at "
                        "FROM app_users WHERE user_id = :u"
                    ),
                    {"u": user_id},
                ).one()
            )

    def revoke_reasons(self, user_id) -> list:
        with self.engine.connect() as conn:
            return list(
                conn.execute(
                    text("SELECT revoke_reason::text FROM auth_sessions WHERE user_id = :u ORDER BY created_at"),
                    {"u": user_id},
                ).scalars()
            )

    def operations(self, target_id) -> list:
        with self.engine.connect() as conn:
            return list(
                conn.execute(
                    text(
                        "SELECT operation_id::text, status::text, actor_user_id::text FROM totp_recovery_operations "
                        "WHERE target_user_id = :u"
                    ),
                    {"u": target_id},
                ).all()
            )


@pytest.fixture()
def env(pg_engine, committed_auth, allowed_origin):
    from config import get_settings
    from main import create_app

    adapter = FakeRecoveryAdapter()
    get_settings.cache_clear()
    app = create_app()

    def _get_db():
        with Session(pg_engine, autoflush=False, expire_on_commit=False) as s:
            yield s

    holder: dict = {}

    def _factory():
        def build():
            holder["env"].built += 1
            return adapter

        return build

    app.dependency_overrides[get_db] = _get_db
    app.dependency_overrides[users_router.get_recovery_engine] = lambda: pg_engine
    app.dependency_overrides[users_router.get_recovery_adapter_factory] = _factory
    with TestClient(app) as client:
        holder["env"] = Env(pg_engine, committed_auth, client, adapter)
        yield holder["env"]
    with Session(pg_engine) as s:
        ids = [*committed_auth._user_ids]
        op = models.TotpRecoveryOperation
        mut = models.TotpProviderMutation
        owned = select(op.operation_id).where(or_(op.target_user_id.in_(ids), op.actor_user_id.in_(ids)))
        # 울타리 행이 작업 행을 RESTRICT 로 잡는다 — 먼저 지운다
        s.execute(delete(mut).where(or_(mut.operation_id.in_(owned), mut.reconciled_by.in_(ids))))
        s.execute(delete(op).where(or_(op.target_user_id.in_(ids), op.actor_user_id.in_(ids))))
        s.commit()


def code(resp) -> str:
    return resp.json()["error"]["code"]


def assert_denied_untouched(env, resp, status, error, *user_ids):
    assert (resp.status_code, code(resp)) == (status, error)
    assert env.built == 0 and env.adapter.deleted == []
    assert not any(env.operations(u) for u in user_ids)
    assert all(env.row(u)[0] == 0 for u in user_ids)


# --- 행위자 ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "actor_kwargs",
    [
        {"platform_role": PlatformRole.TENANT_MANAGER},
        # 대상 고객사를 배정받은 담당자도 복구 행위자가 아니다(고객 사용자 관리 자격 · 범위와 별개)
        {"platform_role": PlatformRole.TENANT_MANAGER, "assigned": True},
        {"tenant": True, "role": UserRole.OPERATOR},
        {"tenant": True, "role": UserRole.VIEWER},
    ],
    ids=["unassigned_manager", "assigned_manager", "tenant_operator", "tenant_viewer"],
)
@pytest.mark.parametrize("body", ["valid", "invalid"])
def test_non_recovery_actors_are_denied_before_body_and_client(env, actor_kwargs, body):
    tenant = env.tenant()
    target = env.user(tenant_id=tenant)
    kwargs = dict(actor_kwargs)
    if kwargs.pop("tenant", False):
        actor = env.user(kwargs.pop("role"), tenant_id=tenant)
    else:
        if kwargs.pop("assigned", False):
            kwargs["assigned_tenant_ids"] = (tenant,)
        actor = env.user(UserRole.ADMIN, **kwargs)
    payload = None if body == "valid" else {"operation_id": "nope", "identity_verified": "yes", "extra": 1}
    resp = env.post(actor, target.user_id, payload)
    assert_denied_untouched(env, resp, 403, "AUTH_FORBIDDEN", target.user_id)


def test_tenant_admin_recovers_own_tenant_target_and_revokes_all_target_sessions(env):
    tenant = env.tenant()
    admin = env.user(UserRole.ADMIN, tenant_id=tenant)
    target = env.user(tenant_id=tenant)
    other = env.user(tenant_id=tenant)
    env.extra_sessions(target.user_id)
    op = uuid.uuid4()
    resp = env.post(admin, target.user_id, op=op)
    assert resp.status_code == 204 and resp.content == b""
    assert env.built == 1 and env.adapter.deleted == [_sub(env, target.user_id)]
    assert env.operations(target.user_id) == [(str(op), "COMPLETED", admin.user_id)]
    generation, state, proof = env.row(target.user_id)
    assert (generation, state, proof) == (1, "ENROLLMENT_REQUIRED", None)
    reasons = env.revoke_reasons(target.user_id)
    assert len(reasons) == 3 and set(reasons) == {"TOTP_RESET"}
    assert env.revoke_reasons(other.user_id) == [None]
    assert env.row(other.user_id)[0] == 0


def _sub(env, user_id) -> str:
    with env.engine.connect() as conn:
        return conn.execute(text("SELECT cognito_sub FROM app_users WHERE user_id = :u"), {"u": user_id}).scalar_one()


def test_tenant_admin_is_scoped_to_own_tenant_and_hidden_targets_look_missing(env):
    own, foreign = env.tenant(), env.tenant()
    admin = env.user(UserRole.ADMIN, tenant_id=own)
    targets = {
        "foreign": env.user(tenant_id=foreign).user_id,
        "platform": env.user(UserRole.ADMIN).user_id,
        "unassigned": env.user(UserRole.ADMIN, account_type=AccountType.UNASSIGNED).user_id,
        "missing": str(uuid.uuid4()),
        "malformed": "not-a-uuid",
    }
    for user_id in targets.values():
        resp = env.post(admin, user_id)
        assert (resp.status_code, code(resp)) == (404, "USER_NOT_FOUND")
    assert env.built == 0 and env.adapter.deleted == []
    assert not any(env.operations(u) for u in targets.values() if u != "not-a-uuid")


def test_central_admin_recovers_any_tenant_including_last_tenant_admin(env):
    central = env.user(UserRole.ADMIN, platform_role=PlatformRole.CENTRAL_ADMIN)
    tenant = env.tenant()
    last_admin = env.user(UserRole.ADMIN, tenant_id=tenant)
    resp = env.post(central, last_admin.user_id)
    assert resp.status_code == 204
    assert env.adapter.deleted == [_sub(env, last_admin.user_id)]
    assert env.operations(last_admin.user_id)[0][1:] == ("COMPLETED", central.user_id)


def test_central_admin_cannot_see_platform_or_unassigned_targets(env):
    central = env.user(UserRole.ADMIN, platform_role=PlatformRole.CENTRAL_ADMIN)
    for target in (
        # PLATFORM 은 등급 · 배정과 무관하게 API 대상이 아니다(운영 CLI 전용)
        env.user(UserRole.ADMIN, platform_role=PlatformRole.TENANT_MANAGER, assigned_tenant_ids=(env.tenant(),)),
        env.user(UserRole.ADMIN, platform_role=PlatformRole.CENTRAL_ADMIN),
        env.user(UserRole.ADMIN, account_type=AccountType.UNASSIGNED),
    ):
        resp = env.post(central, target.user_id)
        assert_denied_untouched(env, resp, 404, "USER_NOT_FOUND", target.user_id)


def test_self_target_is_refused(env):
    actor = env.user(UserRole.ADMIN, tenant_id=env.tenant())
    resp = env.post(actor, actor.user_id)
    assert_denied_untouched(env, resp, 403, "AUTH_FORBIDDEN", actor.user_id)


def test_disabled_target_conflicts(env):
    tenant = env.tenant()
    admin = env.user(UserRole.ADMIN, tenant_id=tenant)
    target = env.user(tenant_id=tenant, status=models.AppUserStatus.DISABLED)
    assert_denied_untouched(env, env.post(admin, target.user_id), 409, "USER_STATE_CONFLICT", target.user_id)


def test_suspended_tenant_actor_cannot_write(env):
    tenant = env.tenant(TenantStatus.SUSPENDED)
    admin = env.user(UserRole.ADMIN, tenant_id=tenant)
    target = env.user(tenant_id=tenant)
    assert_denied_untouched(env, env.post(admin, target.user_id), 409, "TENANT_SUSPENDED", target.user_id)


# --- 요청 형식 ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        {"identity_verified": True},
        {"operation_id": "not-a-uuid", "identity_verified": True},
        {"operation_id": str(uuid.uuid4())},
        {"operation_id": str(uuid.uuid4()), "identity_verified": False},
        {"operation_id": str(uuid.uuid4()), "identity_verified": "true"},
        {"operation_id": str(uuid.uuid4()), "identity_verified": 1},
        {"operation_id": str(uuid.uuid4()), "identity_verified": True, "extra": 1},
    ],
)
def test_invalid_body_is_422_without_client_or_state_change(env, payload):
    tenant = env.tenant()
    admin = env.user(UserRole.ADMIN, tenant_id=tenant)
    target = env.user(tenant_id=tenant)
    resp = env.post(admin, target.user_id, payload)
    assert_denied_untouched(env, resp, 422, "REQUEST_VALIDATION_FAILED", target.user_id)


# --- 세션 · Origin ------------------------------------------------------------------------


def test_unauthenticated_and_bad_origin(env):
    tenant = env.tenant()
    admin = env.user(UserRole.ADMIN, tenant_id=tenant)
    target = env.user(tenant_id=tenant)
    anon = env.post(admin, target.user_id, headers={"Origin": admin.headers["Origin"]})
    assert_denied_untouched(env, anon, 401, "AUTH_REQUIRED", target.user_id)
    bad = env.post(admin, target.user_id, headers={"Cookie": admin.cookie, "Origin": "https://evil.example"})
    assert_denied_untouched(env, bad, 403, "AUTH_ORIGIN_REJECTED", target.user_id)


@pytest.mark.parametrize("how", ["logged_out", "disabled"])
def test_stale_actor_is_refused_before_any_write(env, how):
    tenant = env.tenant()
    admin = env.user(UserRole.ADMIN, tenant_id=tenant)
    target = env.user(tenant_id=tenant)
    with Session(env.engine) as s:
        if how == "logged_out":
            sessions.revoke_session(s, admin.token, now=datetime.now(UTC))
        else:
            s.execute(
                text("UPDATE app_users SET status = 'DISABLED' WHERE user_id = :u"), {"u": admin.user_id}
            )
        s.commit()
    assert_denied_untouched(env, env.post(admin, target.user_id), 401, "AUTH_REQUIRED", target.user_id)


@pytest.mark.parametrize("actor_kind", ["tenant_admin", "central_admin"])
def test_actor_demoted_between_checks_is_refused_under_the_lock(env, monkeypatch, actor_kind):
    """코어의 읽기 단계는 통과하고 잠금 안 재판정에서 행위자가 회수되면 Cognito 를 부르지 않는다.

    중앙 관리자가 대상 고객사를 배정받은 TENANT_MANAGER 로 바뀐 경우도 같다 — 고객 사용자 관리 자격(require_admin) ·
    배정 범위는 통과하지만 복구 행위자가 아니다."""
    from auth import totp_recovery
    from db.repositories import auth as auth_repo

    tenant = env.tenant()
    if actor_kind == "tenant_admin":
        admin = env.user(UserRole.ADMIN, tenant_id=tenant)
    else:
        admin = env.user(UserRole.ADMIN, platform_role=PlatformRole.CENTRAL_ADMIN)
    target = env.user(tenant_id=tenant)
    real = totp_recovery._fence

    def demote_then_fence(*args, **kwargs):
        with Session(env.engine) as s:
            if actor_kind == "tenant_admin":
                s.execute(text("UPDATE app_users SET role = 'VIEWER' WHERE user_id = :u"), {"u": admin.user_id})
            else:
                now = datetime.now(UTC)
                assert auth_repo.set_platform_role(
                    s, user_id=admin.user_id, platform_role=PlatformRole.TENANT_MANAGER, now=now
                )
                auth_repo.replace_assigned_tenants(s, user_id=admin.user_id, tenant_ids=(tenant,), now=now)
            s.commit()
        return real(*args, **kwargs)

    monkeypatch.setattr(totp_recovery, "_fence", demote_then_fence)
    resp = env.post(admin, target.user_id)
    assert (resp.status_code, code(resp)) == (403, "AUTH_FORBIDDEN")
    assert env.adapter.deleted == [] and env.operations(target.user_id) == []
    assert env.row(target.user_id)[0] == 0


# --- 멱등 · 결과 ----------------------------------------------------------------------------


def test_completed_duplicate_returns_204_without_second_provider_call_even_after_reenrollment(env):
    tenant = env.tenant()
    admin = env.user(UserRole.ADMIN, tenant_id=tenant)
    target = env.user(tenant_id=tenant)
    op = uuid.uuid4()
    assert env.post(admin, target.user_id, op=op).status_code == 204
    with Session(env.engine) as s:  # 새 TOTP 등록 완료
        s.execute(
            text("UPDATE app_users SET mfa_recovery_state = 'NONE' WHERE user_id = :u"), {"u": target.user_id}
        )
        s.commit()
    assert env.post(admin, target.user_id, op=op).status_code == 204
    assert len(env.adapter.deleted) == 1 and env.built == 1
    assert env.row(target.user_id)[:2] == (1, "NONE")


def test_operation_id_of_another_target_conflicts(env):
    tenant = env.tenant()
    admin = env.user(UserRole.ADMIN, tenant_id=tenant)
    first, second = env.user(tenant_id=tenant), env.user(tenant_id=tenant)
    op = uuid.uuid4()
    assert env.post(admin, first.user_id, op=op).status_code == 204
    resp = env.post(admin, second.user_id, op=op)
    assert (resp.status_code, code(resp)) == (409, "USER_STATE_CONFLICT")
    assert len(env.adapter.deleted) == 1 and env.operations(second.user_id) == []


@pytest.mark.parametrize("outcome", [D.NOT_FOUND, D.REJECTED, D.UNKNOWN])
def test_noncompleted_provider_outcome_is_not_success_and_not_retried(env, outcome):
    tenant = env.tenant()
    admin = env.user(UserRole.ADMIN, tenant_id=tenant)
    target = env.user(tenant_id=tenant)
    env.adapter.outcome = outcome
    op = uuid.uuid4()
    resp = env.post(admin, target.user_id, op=op)
    assert (resp.status_code, code(resp)) == (503, "AUTH_PROVIDER_UNAVAILABLE")
    [(_, status, _)] = env.operations(target.user_id)
    assert status != "COMPLETED"
    # 같은 operation_id 의 재요청은 제공자를 다시 부르지 않고 같은 비성공이다
    again = env.post(admin, target.user_id, op=op)
    assert (again.status_code, code(again)) == (503, "AUTH_PROVIDER_UNAVAILABLE")
    assert len(env.adapter.deleted) == 1
    assert env.row(target.user_id)[2] is None  # 제공자 결과와 무관하게 이메일 증명은 이미 제거됐다


def test_unrecorded_result_is_not_success(env, monkeypatch):
    from auth import totp_recovery

    tenant = env.tenant()
    admin = env.user(UserRole.ADMIN, tenant_id=tenant)
    target = env.user(tenant_id=tenant)
    monkeypatch.setattr(totp_recovery.recovery_repo, "finalize_operation", lambda *a, **k: False)
    resp = env.post(admin, target.user_id)
    assert (resp.status_code, code(resp)) == (503, "AUTH_PROVIDER_UNAVAILABLE")
    assert env.operations(target.user_id)[0][1] == "PROVIDER_PENDING"
    assert len(env.adapter.deleted) == 1


# --- 어댑터 조립 --------------------------------------------------------------------------


def test_adapter_factory_is_lazy_and_uses_the_no_retry_recovery_adapter(monkeypatch):
    sentinel = object()
    seen = []
    monkeypatch.setattr(users_router, "build_recovery_adapter", lambda s: seen.append(s) or sentinel)
    missing = SimpleNamespace(missing_cognito_keys=lambda: ["COGNITO_USER_POOL_ID"])
    build = users_router.get_recovery_adapter_factory(missing)
    assert seen == []
    with pytest.raises(Exception) as exc:
        build()
    assert exc.value.code.value == "AUTH_PROVIDER_UNAVAILABLE" and seen == []
    ok = SimpleNamespace(missing_cognito_keys=list)
    assert users_router.get_recovery_adapter_factory(ok)() is sentinel and seen == [ok]


def test_reset_route_is_the_only_recovery_surface():
    from fastapi.routing import iter_route_contexts

    app = users_router.router
    paths = {(m, r.path) for r in iter_route_contexts(app.routes) for m in r.methods or ()}
    assert {p for _, p in paths if "totp" in p or "recover" in p} == {"/api/v1/users/{user_id}/totp/reset"}
