"""TOTP 분실 복구 운영 CLI 어댑터 (#515) — 실제 PostgreSQL + Cognito 대역(실 AWS · 실 클라이언트 없음)."""

from __future__ import annotations

import io
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest
from auth import recover_totp
from auth.cognito import CognitoAdapter
from botocore.exceptions import ClientError, ReadTimeoutError
from db import models
from db.repositories import auth as auth_repo
from schemas.api.auth import AccountType, PlatformRole, UserRole
from sqlalchemy import delete, or_, select, text
from sqlalchemy.orm import Session

REGION = "ap-northeast-2"
POOL = "ap-northeast-2_CliRecovery"
ISSUER = f"https://cognito-idp.{REGION}.amazonaws.com/{POOL}"
NOW = datetime(2026, 10, 29, 9, 0, tzinfo=UTC)
SECRET = "client-secret-zzz-9"
PRIVATE = "echo PRIVATE-TEXT"


class FakeCognito:
    def __init__(self, hook=None) -> None:
        self.calls: list[dict] = []
        self.hook = hook

    def admin_delete_software_token(self, **params):
        self.calls.append(params)
        if self.hook is not None:
            self.hook()
        return {}


def client_error(code: str, status: int) -> ClientError:
    return ClientError(
        {"Error": {"Code": code, "Message": PRIVATE}, "ResponseMetadata": {"HTTPStatusCode": status}},
        "AdminDeleteSoftwareToken",
    )


def raising(exc):
    def hook():
        raise exc

    return hook


@dataclass
class Env:
    engine: object
    tenant_id: str
    fake: FakeCognito = field(default_factory=FakeCognito)
    builds: int = 0
    user_ids: list[str] = field(default_factory=list)

    def get_adapter(self) -> CognitoAdapter:
        self.builds += 1
        return CognitoAdapter(
            self.fake, user_pool_id=POOL, client_id="cid", client_secret=SECRET, region=REGION
        )

    def user(self, *, account_type=AccountType.TENANT, role=UserRole.VIEWER, platform_role=None,
             status=models.AppUserStatus.ACTIVE) -> str:
        sub = f"sub-{uuid.uuid4().hex}"
        with Session(self.engine) as s:
            row = auth_repo.create_user(
                s, issuer=ISSUER, cognito_sub=sub, email=f"{sub[:16]}@example.com", role=role,
                account_type=account_type,
                tenant_id=self.tenant_id if account_type is AccountType.TENANT else None,
                now=NOW, status=status, platform_role=platform_role, email_verified_at=NOW,
            )
            user_id = row.user_id
            s.commit()
        self.user_ids.append(user_id)
        return user_id

    def platform(self, role=PlatformRole.TENANT_MANAGER, **kw) -> str:
        return self.user(account_type=AccountType.PLATFORM, role=UserRole.ADMIN, platform_role=role, **kw)

    def tenant_admin(self, **kw) -> str:
        return self.user(role=UserRole.ADMIN, **kw)

    def cli(self, *argv):
        out = io.StringIO()
        code = recover_totp.run(
            [str(a) for a in argv], engine=self.engine, get_adapter=self.get_adapter,
            now=lambda: NOW, out=out,
        )
        return code, out.getvalue()

    def execute(self, user_id, op=None, *extra):
        return self.cli(
            "--user-id", user_id, "--operation-id", op or uuid.uuid4(),
            "--identity-verified", "true", "--yes", *extra,
        )

    def snapshot(self, user_id):
        with self.engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT auth_generation, mfa_recovery_state::text, email_verified_at, account_type::text,"
                    " role::text, platform_role::text, status::text, updated_at FROM app_users"
                    " WHERE user_id = :u"
                ),
                {"u": user_id},
            ).one()
            ops = conn.execute(
                text("SELECT count(*) FROM totp_recovery_operations WHERE target_user_id = :u"),
                {"u": user_id},
            ).scalar_one()
        return tuple(row), ops

    def op_status(self, op):
        with self.engine.connect() as conn:
            return conn.execute(
                text("SELECT status::text FROM totp_recovery_operations WHERE operation_id = :o"),
                {"o": str(op)},
            ).scalar_one_or_none()


@pytest.fixture()
def env(pg_engine, seed_tenant):
    with Session(pg_engine) as s:
        tenant_id = seed_tenant(s, now=NOW)
    e = Env(engine=pg_engine, tenant_id=tenant_id)
    yield e
    with Session(pg_engine) as s:
        op = models.TotpRecoveryOperation
        mut = models.TotpProviderMutation
        s.execute(
            delete(mut).where(
                mut.operation_id.in_(
                    select(op.operation_id).where(
                        or_(op.target_user_id.in_(e.user_ids), op.actor_user_id.in_(e.user_ids))
                    )
                )
            )
        )
        s.execute(delete(op).where(or_(op.target_user_id.in_(e.user_ids), op.actor_user_id.in_(e.user_ids))))
        s.execute(delete(models.AuthSession).where(models.AuthSession.user_id.in_(e.user_ids)))
        s.execute(delete(models.AppUser).where(models.AppUser.user_id.in_(e.user_ids)))
        s.execute(delete(models.Tenant).where(models.Tenant.tenant_id == tenant_id))
        s.commit()


def test_preview_is_read_only_and_builds_no_client(env):
    target = env.platform()
    op = uuid.uuid4()
    before = env.snapshot(target)
    code, out = env.cli("--user-id", target, "--operation-id", op)
    assert code == 0
    assert "미리보기" in out and "PLATFORM" in out and "새 초기화" in out
    assert env.snapshot(target) == before
    assert env.builds == 0 and env.fake.calls == []
    code, out = env.cli("--user-id", target, "--identity-verified", "true")
    assert code == 0 and "이전 작업: 0건" in out
    assert "@" not in out and "SELECT" not in out


@pytest.mark.parametrize(
    "argv",
    [
        ["--user-id", "nope", "--operation-id", "{op}", "--identity-verified", "true", "--yes"],
        ["--user-id", "{t}", "--operation-id", "nope", "--identity-verified", "true", "--yes"],
        ["--user-id", "{t}", "--identity-verified", "true", "--yes"],
        ["--user-id", "{t}", "--operation-id", "{op}", "--yes"],
        ["--user-id", "{t}", "--operation-id", "{op}", "--identity-verified", "false", "--yes"],
        ["--user-id", "{t}", "--operation-id", "{op}", "--identity-verified", "true"],  # 미리보기
    ],
)
def test_invalid_or_missing_flags_do_nothing(env, argv):
    target = env.platform()
    op = uuid.uuid4()
    args = [a.format(t=target, op=op) for a in argv]
    before = env.snapshot(target)
    code, out = env.cli(*args)
    assert code == (0 if "--yes" not in args and "false" not in args and "nope" not in args else 2)
    assert env.snapshot(target) == before
    assert env.builds == 0 and env.fake.calls == []
    if code == 2:
        assert out.startswith("거절(호출)")


@pytest.mark.parametrize("role", list(PlatformRole))
def test_active_platform_allowed_without_app_session_or_attestation(env, role):
    target = env.platform(role)
    op = uuid.uuid4()
    before, _ = env.snapshot(target)
    code, out = env.execute(target, op)
    assert code == 0 and "완료" in out
    assert env.builds == 1 and len(env.fake.calls) == 1
    assert env.op_status(op) == "COMPLETED"
    after, ops = env.snapshot(target)
    assert ops == 1
    # 계정 종류 · 역할 · 등급 · 상태는 그대로다(세대 · 복구 상태만 바뀐다)
    assert after[3:7] == before[3:7]
    with env.engine.connect() as conn:
        actor = conn.execute(
            text("SELECT actor_user_id FROM totp_recovery_operations WHERE operation_id = :o"), {"o": str(op)}
        ).scalar_one()
    assert actor is None


def test_tenant_admin_requires_no_admin_available_attestation(env):
    target = env.tenant_admin()  # 테넌트의 마지막(유일한) ADMIN
    before = env.snapshot(target)
    code, out = env.execute(target)
    assert code == 2 and "거절(정책)" in out and "--no-admin-available" in out
    assert env.snapshot(target) == before
    assert env.builds == 0 and env.fake.calls == []

    op = uuid.uuid4()
    code, out = env.execute(target, op, "--no-admin-available")
    assert code == 0 and "완료" in out
    assert env.op_status(op) == "COMPLETED" and len(env.fake.calls) == 1
    assert env.snapshot(target)[0][3:7] == before[0][3:7]


def test_denied_target_categories_make_zero_calls(env):
    targets = {
        "viewer": env.user(role=UserRole.VIEWER),
        "operator": env.user(role=UserRole.OPERATOR),
        "disabled_admin": env.tenant_admin(status=models.AppUserStatus.DISABLED),
        "disabled_platform": env.platform(status=models.AppUserStatus.DISABLED),
        "unassigned": env.user(account_type=AccountType.UNASSIGNED, role=UserRole.VIEWER),
        "missing": str(uuid.uuid4()),
    }
    for name, target in targets.items():
        before = None if name == "missing" else env.snapshot(target)
        for extra in ((), ("--no-admin-available",)):
            code, out = env.execute(target, uuid.uuid4(), *extra)
            assert code == 2, (name, extra, out)
            assert out.count("거절(정책)") == 1
            if before is not None:
                assert env.snapshot(target) == before
        code, out = env.cli("--user-id", target, "--no-admin-available")  # 미리보기도 같은 정책
        assert code == 2
    assert env.builds == 0 and env.fake.calls == []


def test_identity_confirmation_is_required_by_core_and_cli(env):
    target = env.platform()
    code, out = env.cli("--user-id", target, "--operation-id", uuid.uuid4(), "--yes")
    assert code == 2 and "--identity-verified" in out
    assert env.builds == 0 and env.fake.calls == []


def test_replay_same_operation_never_calls_provider_again(env):
    target = env.platform()
    op = uuid.uuid4()
    assert env.execute(target, op)[0] == 0
    code, out = env.execute(target, op)
    assert code == 0 and "재생" in out and "COMPLETED" in out
    assert env.builds == 1 and len(env.fake.calls) == 1
    # 미리보기는 이전 요청을 보여 준다
    code, out = env.cli("--user-id", target, "--operation-id", op)
    assert code == 0 and "status=COMPLETED" in out and "revoked_sessions=0" in out
    # 다른 operation-id 는 명시적인 새 초기화다
    assert env.execute(target, uuid.uuid4())[0] == 0
    assert len(env.fake.calls) == 2


def test_operation_id_of_another_target_is_refused(env):
    first, second = env.platform(), env.platform()
    op = uuid.uuid4()
    assert env.execute(first, op)[0] == 0
    code, out = env.execute(second, op)
    assert code == 2 and "다른 대상" in out
    assert len(env.fake.calls) == 1
    code, out = env.cli("--user-id", second, "--operation-id", op)
    assert code == 2 and "operation_id=" not in out


@pytest.mark.parametrize(
    ("hook", "status", "wording"),
    [
        (raising(client_error("ResourceNotFoundException", 400)), "PROVIDER_NOT_FOUND", "등록이 없다"),
        (raising(client_error("NotAuthorizedException", 400)), "PROVIDER_REJECTED", "거절했다"),
        (raising(client_error("InternalErrorException", 500)), "PROVIDER_UNKNOWN", "알 수 없다"),
        (raising(ReadTimeoutError(endpoint_url="https://x.invalid")), "PROVIDER_UNKNOWN", "알 수 없다"),
    ],
)
def test_noncompleted_provider_outcomes_exit_nonzero_without_raw_text(env, hook, status, wording):
    env.fake.hook = hook
    target = env.platform()
    op = uuid.uuid4()
    code, out = env.execute(target, op)
    assert code == 1 and wording in out and f"status={status}" in out
    assert "\n완료:" not in out
    assert PRIVATE not in out and SECRET not in out and "Traceback" not in out
    assert env.op_status(op) == status
    expected_state = "ENROLLMENT_REQUIRED" if status == "PROVIDER_NOT_FOUND" else "RESET_PENDING"
    assert env.snapshot(target)[0][1] == expected_state
    # 같은 operation-id 재요청은 제공자를 다시 부르지 않고 같은 비성공을 보고한다
    code, out = env.execute(target, op)
    assert code == 1 and "재생" in out and len(env.fake.calls) == 1


def test_adapter_build_failure_is_system_failure_without_secret(env):
    target = env.platform()
    before = env.snapshot(target)

    def boom():
        raise RuntimeError(f"cannot build {SECRET}")

    out = io.StringIO()
    code = recover_totp.run(
        ["--user-id", target, "--operation-id", str(uuid.uuid4()), "--identity-verified", "true", "--yes"],
        engine=env.engine, get_adapter=boom, now=lambda: NOW, out=out,
    )
    assert code == 1 and "실패(시스템)" in out.getvalue() and "RuntimeError" in out.getvalue()
    assert SECRET not in out.getvalue()
    assert env.snapshot(target) == before


class _Settings:
    def __init__(self, missing=()):
        self._missing = list(missing)

    def missing_cognito_keys(self):
        return self._missing


def _wire_main(monkeypatch, env, *, settings=None):
    from auth import provider
    from db import session

    built = []
    monkeypatch.setattr(recover_totp, "get_auth_settings", lambda: settings or _Settings())
    monkeypatch.setattr(session, "get_engine", lambda: env.engine)
    monkeypatch.setattr(
        provider, "build_recovery_adapter", lambda s: built.append(s) or env.get_adapter()
    )
    return built


def _validation_error():
    from pydantic import BaseModel, ValidationError

    class M(BaseModel):
        n: int

    try:
        M(n=f"FAKEKEY-{SECRET}")
    except ValidationError as exc:
        return exc
    raise AssertionError


def _assert_safe(text_):
    for leaked in ("FAKEKEY", SECRET, "Traceback", "ValidationError", "postgresql", "SELECT"):
        assert leaked not in text_


@pytest.mark.parametrize(
    "exc",
    [
        _validation_error(),
        RuntimeError(f"postgresql+psycopg://u:{SECRET}@h/db FAKEKEY"),
    ],
)
@pytest.mark.parametrize("failing", ["settings", "engine"])
def test_main_initialization_failure_is_safe_and_nonzero(env, monkeypatch, capsys, exc, failing):
    _wire_main(monkeypatch, env)
    from db import session

    def boom():
        raise exc

    if failing == "settings":
        monkeypatch.setattr(recover_totp, "get_auth_settings", boom)
    else:
        monkeypatch.setattr(session, "get_engine", boom)
    target = env.platform()
    code = recover_totp.main(["--user-id", target])
    captured = capsys.readouterr()
    assert code == 1 and "실패(시스템)" in captured.err
    _assert_safe(captured.out + captured.err)
    assert env.builds == 0 and env.fake.calls == []


def test_main_keeps_argparse_usage_exit(env, monkeypatch, capsys):
    _wire_main(monkeypatch, env)
    with pytest.raises(SystemExit) as info:
        recover_totp.main(["--bogus"])
    assert info.value.code == 2
    assert "usage" in capsys.readouterr().err


def test_main_preview_builds_no_client_and_writes_nothing(env, monkeypatch, capsys):
    built = _wire_main(monkeypatch, env)
    target = env.platform()
    before = env.snapshot(target)
    assert recover_totp.main(["--user-id", target]) == 0
    assert "미리보기" in capsys.readouterr().out
    assert built == [] and env.builds == 0 and env.snapshot(target) == before


def test_main_execution_wiring_uses_recovery_adapter(env, monkeypatch, capsys):
    built = _wire_main(monkeypatch, env)
    target, op = env.platform(), uuid.uuid4()
    argv = ["--user-id", target, "--operation-id", str(op), "--identity-verified", "true", "--yes"]
    assert recover_totp.main(argv) == 0
    assert "완료" in capsys.readouterr().out
    assert len(built) == 1 and len(env.fake.calls) == 1 and env.op_status(op) == "COMPLETED"


def test_main_missing_cognito_keys_blocks_execution_not_preview(env, monkeypatch, capsys):
    built = _wire_main(monkeypatch, env, settings=_Settings(["COGNITO_CLIENT_ID"]))
    target = env.platform()
    argv = ["--user-id", target, "--operation-id", str(uuid.uuid4()), "--identity-verified", "true"]
    assert recover_totp.main(argv) == 0
    capsys.readouterr()
    assert recover_totp.main([*argv, "--yes"]) == 1
    assert "COGNITO_CLIENT_ID" in capsys.readouterr().err
    assert built == [] and env.fake.calls == []


def test_provider_issuer_mismatch_is_policy_refusal_without_call(env):
    target = env.platform()
    out = io.StringIO()
    other = CognitoAdapter(
        env.fake, user_pool_id="ap-northeast-2_Other", client_id="c", client_secret=SECRET, region=REGION
    )
    code = recover_totp.run(
        ["--user-id", target, "--operation-id", str(uuid.uuid4()), "--identity-verified", "true", "--yes"],
        engine=env.engine, get_adapter=lambda: other, now=lambda: NOW, out=out,
    )
    assert code == 2 and "issuer" in out.getvalue() and env.fake.calls == []


# ---- 정리(reconcile) 모드 ----


def _unresolved(env, user_id):
    with env.engine.connect() as conn:
        return conn.execute(
            text(
                "SELECT m.mutation_id::text, m.resolution::text, m.reconciled_by FROM totp_provider_mutations m"
                " JOIN app_users u ON u.issuer = m.issuer AND u.cognito_sub = m.cognito_sub"
                " WHERE u.user_id = :u ORDER BY m.started_at DESC"
            ),
            {"u": user_id},
        ).all()


def _fenced(env, role=PlatformRole.TENANT_MANAGER):
    """제공자 결과를 알 수 없는 초기화로 풀리지 않은 울타리를 가진 대상."""
    target = env.platform(role)
    op = uuid.uuid4()
    env.fake.hook = raising(ReadTimeoutError(endpoint_url="https://x.invalid"))
    code, _ = env.execute(target, op)
    assert code == 1 and env.op_status(op) == "PROVIDER_UNKNOWN"
    env.fake.hook = None
    rows = _unresolved(env, target)
    assert len(rows) == 1 and rows[0][1] is None
    return target, op, rows[0][0]


def _reconcile_argv(target, mutation, *extra):
    return ["--user-id", target, "--reconcile-mutation", mutation, *extra]


def _ack(target, mutation, *extra):
    return _reconcile_argv(
        target, mutation, "--identity-verified", "true", "--external-state-verified", "true", "--yes", *extra
    )


def test_reconcile_preview_has_no_effects_and_builds_no_client(env):
    target, _op, mutation = _fenced(env)
    before, calls, builds = env.snapshot(target), len(env.fake.calls), env.builds
    code, out = env.cli(*_reconcile_argv(target, mutation))
    assert code == 0 and "미리보기" in out and mutation in out and "RESET" in out
    assert "앱이 AWS 상태를 검증했다는 증거가 아니다" in out and "완료되지 않으며" in out
    assert env.snapshot(target) == before and _unresolved(env, target)[0][1] is None
    assert len(env.fake.calls) == calls and env.builds == builds
    # 초기화 미리보기도 울타리를 보여 준다
    code, out = env.cli("--user-id", target)
    assert code == 0 and mutation in out


@pytest.mark.parametrize(
    "extra",
    [
        ["--identity-verified", "true", "--yes"],  # 외부 확인 없음
        ["--external-state-verified", "true", "--yes"],  # 본인 확인 없음
        ["--identity-verified", "true", "--external-state-verified", "false", "--yes"],
        ["--identity-verified", "true", "--external-state-verified", "true", "--yes", "--operation-id", "{op}"],
        ["--identity-verified", "true", "--external-state-verified", "true", "--operation-id", "{op}"],
    ],
)
def test_reconcile_requires_attestations_and_rejects_mixed_modes(env, extra):
    target, _op, mutation = _fenced(env)
    before, calls = env.snapshot(target), len(env.fake.calls)
    code, out = env.cli(*_reconcile_argv(target, mutation, *[a.format(op=uuid.uuid4()) for a in extra]))
    assert code == 2 and out.startswith("거절(호출)")
    assert env.snapshot(target) == before and _unresolved(env, target)[0][1] is None
    assert len(env.fake.calls) == calls


def test_reconcile_flags_are_not_silently_reset_flags(env):
    target = env.platform()
    code, out = env.cli("--user-id", target, "--external-state-verified", "true")
    assert code == 2 and out.startswith("거절(호출)")
    code, out = env.cli(*_reconcile_argv(target, "nope"))
    assert code == 2 and out.startswith("거절(호출)")
    assert env.builds == 0 and env.fake.calls == []


def test_reconcile_exact_target_and_mutation_guard(env):
    target, _, mutation = _fenced(env)
    other, _, other_mutation = _fenced(env)
    before = env.snapshot(target)
    # 다른 대상의 울타리 · 임의 UUID
    for wrong in (other_mutation, str(uuid.uuid4())):
        code, out = env.cli(*_ack(target, wrong))
        assert code == 2 and "거절(정책)" in out and "mutation-id" in out
    assert env.snapshot(target) == before
    assert _unresolved(env, target)[0][1] is None and _unresolved(env, other)[0][1] is None
    # 울타리가 없는 대상
    clean = env.platform()
    code, out = env.cli(*_ack(clean, mutation))
    assert code == 2 and "거절(정책)" in out
    # 비활성 · 없는 사용자 거절. 활성 TENANT 는 역할과 무관하게 정리 대상이다.
    for denied in (env.platform(status=models.AppUserStatus.DISABLED), str(uuid.uuid4())):
        code, out = env.cli(*_ack(denied, mutation))
        assert code == 2 and "거절(정책)" in out
    assert _unresolved(env, target)[0][1] is None


def _fenced_tenant(env, role):
    """관리자 경로에서 시작한 고객사 초기화의 결과 불명 상태를 코어로 만든다."""
    target = env.user(role=role)
    op = uuid.uuid4()
    env.fake.hook = raising(client_error("InternalErrorException", 500))
    result = recover_totp.totp_recovery.reset_totp(
        env.engine,
        operation_id=op,
        target_user_id=target,
        identity_verified=True,
        authorize=lambda db, user: None,
        get_adapter=env.get_adapter,
        clock=lambda: NOW,
    )
    assert result.operation.status is models.TotpRecoveryStatus.PROVIDER_UNKNOWN
    env.fake.hook = None
    return target, op, _unresolved(env, target)[0][0]


@pytest.mark.parametrize("role", list(UserRole))
def test_reconcile_all_active_tenant_roles_without_no_admin_available(env, role):
    target, op, mutation = _fenced_tenant(env, role)
    before, calls, builds = env.snapshot(target), len(env.fake.calls), env.builds
    code, out = env.cli(*_reconcile_argv(target, mutation))
    assert code == 0 and "미리보기" in out and mutation in out
    assert _unresolved(env, target)[0][1] is None
    assert env.snapshot(target) == before
    code, out = env.cli(*_ack(target, mutation))
    assert code == 0 and "정리됨" in out
    assert _unresolved(env, target)[0][1] == "OPERATOR_RECONCILED"
    assert env.snapshot(target) == before and env.op_status(op) == "PROVIDER_UNKNOWN"
    assert len(env.fake.calls) == calls and env.builds == builds
    with Session(env.engine) as db:
        assert db.scalars(
            select(models.AuthSession).where(models.AuthSession.user_id == target)
        ).all() == []
    # 정리 허용이 초기화 허용으로 이어지지는 않는다(ADMIN 은 여전히 별도 진술 필요).
    assert env.execute(target)[0] == 2
    if role is not UserRole.ADMIN:
        assert env.execute(target, uuid.uuid4(), "--no-admin-available")[0] == 2
    assert env.snapshot(target) == before
    assert len(env.fake.calls) == calls and env.builds == builds


@pytest.mark.parametrize("kind", ["disabled_tenant", "disabled_platform", "unassigned"])
def test_reconcile_ineligible_target_with_its_own_fence_is_refused(env, kind):
    if kind == "disabled_platform":
        target, _op, mutation = _fenced(env)
    else:
        target, _op, mutation = _fenced_tenant(env, UserRole.VIEWER)
    with Session(env.engine) as db:
        user = db.get(models.AppUser, target)
        if kind == "unassigned":
            user.account_type = AccountType.UNASSIGNED
            user.tenant_id = None
        else:
            user.status = models.AppUserStatus.DISABLED
        db.commit()
    before, calls, builds = env.snapshot(target), len(env.fake.calls), env.builds
    for argv in (_reconcile_argv(target, mutation), _ack(target, mutation)):
        code, out = env.cli(*argv)
        assert code == 2 and "거절(정책)" in out
        assert env.snapshot(target) == before and _unresolved(env, target)[0][1] is None
    assert len(env.fake.calls) == calls and env.builds == builds


def test_unknown_reset_busy_then_reconcile_then_new_reset(env):
    target, op, mutation = _fenced(env)
    calls = len(env.fake.calls)
    # 새 operation-id 는 풀리지 않은 울타리를 덮지 못한다
    new_op = uuid.uuid4()
    code, out = env.execute(target, new_op)
    assert code == 1 and "풀리지 않은" in out and "--reconcile-mutation" in out
    assert "나중에 다시" not in out
    assert len(env.fake.calls) == calls and env.op_status(new_op) is None

    before, builds = env.snapshot(target), env.builds
    code, out = env.cli(*_ack(target, mutation))
    assert code == 0 and "정리됨" in out and "완료되지 않았다" in out
    assert "앱이 AWS 상태를 검증했다는 증거가 아니다" in out
    rows = _unresolved(env, target)
    assert rows[0][1] == "OPERATOR_RECONCILED" and rows[0][2] is None
    # 사용자 · 세션 · 작업 상태 불변, Cognito 호출 · 클라이언트 생성 없음
    assert env.snapshot(target) == before and env.op_status(op) == "PROVIDER_UNKNOWN"
    assert len(env.fake.calls) == calls and env.builds == builds

    # 이미 풀린 울타리를 다시 정리할 수 없다
    code, out = env.cli(*_ack(target, mutation))
    assert code == 2 and "mutation-id" in out and env.snapshot(target) == before

    # 외부 검토 뒤 새 operation-id 의 별도 초기화
    code, out = env.execute(target, new_op)
    assert code == 0 and "완료" in out and env.op_status(new_op) == "COMPLETED"
    assert env.op_status(op) == "PROVIDER_UNKNOWN"


def test_pending_status_text_never_suggests_new_operation_clears(env):
    target, op, _ = _fenced(env)
    _, out = env.cli("--user-id", target, "--operation-id", op)
    assert "새 operation-id 로 해소되지 않는다" in out or "해소하지 않는다" in out


def test_main_reconcile_wiring_and_secret_mask(env, monkeypatch, capsys):
    target, _op, mutation = _fenced(env)
    built = _wire_main(monkeypatch, env)
    assert recover_totp.main(_reconcile_argv(target, mutation)) == 0
    assert _unresolved(env, target)[0][1] is None
    assert recover_totp.main(_ack(target, mutation)) == 0
    out = capsys.readouterr().out
    assert "정리됨" in out and SECRET not in out and PRIVATE not in out and "@" not in out
    assert _unresolved(env, target)[0][1] == "OPERATOR_RECONCILED"
    assert built == [] and env.builds == 1  # 기존 초기화 1회의 클라이언트뿐 — 정리는 만들지 않는다


def test_reconcile_db_failure_is_system_failure_without_raw_text(env, monkeypatch):
    from sqlalchemy.exc import OperationalError

    target, _, mutation = _fenced(env)
    out = io.StringIO()

    def bad_engine_call(*a, **k):
        raise OperationalError("SELECT secret", {}, Exception(f"password={SECRET}"))

    monkeypatch.setattr(recover_totp.totp_recovery, "reconcile_provider_mutation", bad_engine_call)
    code = recover_totp.run(
        _ack(target, mutation), engine=env.engine, get_adapter=env.get_adapter, now=lambda: NOW, out=out
    )
    assert code == 1 and "실패(시스템)" in out.getvalue()
    assert SECRET not in out.getvalue() and "SELECT" not in out.getvalue()
    assert _unresolved(env, target)[0][1] is None


def test_main_reconcile_executes_without_cognito_settings(env, monkeypatch, capsys):
    target, _op, mutation = _fenced(env)
    built = _wire_main(monkeypatch, env, settings=_Settings(["COGNITO_CLIENT_ID"]))
    builds = env.builds
    assert recover_totp.main(_ack(target, mutation)) == 0
    assert "COGNITO_CLIENT_ID" not in capsys.readouterr().err
    assert _unresolved(env, target)[0][1] == "OPERATOR_RECONCILED"
    assert built == [] and env.builds == builds
    # 초기화 실행은 여전히 Cognito 설정을 요구한다
    code = recover_totp.main(
        ["--user-id", target, "--operation-id", str(uuid.uuid4()), "--identity-verified", "true", "--yes"]
    )
    assert code == 1 and "COGNITO_CLIENT_ID" in capsys.readouterr().err


@pytest.mark.parametrize("case", ["wrong", "none", "resolved"])
@pytest.mark.parametrize("execute", [False, True])
def test_reconcile_preview_and_execute_refuse_absent_resolved_or_mismatched_id(env, case, execute):
    target, _op, mutation = _fenced(env)
    if case == "resolved":
        assert env.cli(*_ack(target, mutation))[0] == 0
        requested = mutation
    elif case == "wrong":
        requested = str(uuid.uuid4())
    else:
        requested = str(uuid.uuid4())
        target = env.platform()
    before, calls, builds = env.snapshot(target), len(env.fake.calls), env.builds
    resolution = _unresolved(env, target)
    argv = _ack(target, requested) if execute else _reconcile_argv(target, requested)
    code, out = env.cli(*argv)
    assert code == 2 and "거절(정책)" in out and "미리보기 — 변경 없음" not in out
    assert env.snapshot(target) == before and _unresolved(env, target) == resolution
    assert len(env.fake.calls) == calls and env.builds == builds


def test_reconcile_db_error_guidance_points_to_mutation_not_operation(env, monkeypatch):
    from sqlalchemy.exc import OperationalError

    target, _op, mutation = _fenced(env)
    out = io.StringIO()

    def bad(*a, **k):
        raise OperationalError("SELECT secret", {}, Exception(f"password={SECRET}"))

    monkeypatch.setattr(recover_totp.totp_recovery, "reconcile_provider_mutation", bad)
    code = recover_totp.run(
        _ack(target, mutation), engine=env.engine, get_adapter=env.get_adapter, now=lambda: NOW, out=out
    )
    text = out.getvalue()
    assert code == 1 and "--reconcile-mutation" in text and "알 수 없다" in text
    assert "operation-id" not in text.split("실패(시스템)")[1]
    assert SECRET not in text and "SELECT" not in text
