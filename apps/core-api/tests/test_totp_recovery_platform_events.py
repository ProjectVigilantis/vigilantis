"""TOTP 분실 복구의 플랫폼 활동 로그(#589 B · AUTH_TOTP_RECOVERY) — 실제 PostgreSQL 행으로 본다.

지키는 것
  - 완료(204)는 결과 확정과 같은 트랜잭션의 성공 한 행이다. 행위자는 복구를 실행한 관리자(그 세션)이고 대상은
    복구된 계정, 소속은 그 계정의 고객사다. 그 행을 못 쓰면 결과도 확정되지 않는다(결과 대기 · 울타리 그대로) —
    제공자 호출 전에 끝난 세션 폐기 · 세대 올림은 그대로다.
  - 대상이 범위 안으로 정해진 뒤의 거절(409)은 거절 한 행, 제공자 신원 불일치와 완료되지 못한 결과(503)는 실패
    한 행이다. 요청 트랜잭션과 따로 남고, 못 써도 응답은 그대로다.
  - 권한 없음 · 본인 대상(403) · 범위 밖 · 없는 대상(404) · 진술 없음(422) · 정지 고객사의 행위자(409)는 이 행동으로
    남지 않는다. 같은 operation_id 의 재요청도 새 행을 만들지 않는다.
  - 운영자 CLI 의 행위자는 SYSTEM 이다.
  - 계약에 맞지 않는 행위자면 아무것도 바꾸기 전에 멈춘다.

복구의 상태 · 순서는 test_auth_totp_recovery.py, 응답과 권한 표는 test_totp_recovery_api.py 가 지킨다.
"""

from __future__ import annotations

import io
import uuid

import pytest
from auth import event_log, recover_totp, totp_recovery
from auth.cognito import SoftwareTokenDeletion as D
from db import models
from db.repositories import auth as auth_repo
from db.repositories import platform_events as platform_events_repo
from schemas.api.auth import AccountType, PlatformRole, UserRole
from schemas.api.tenants import TenantStatus
from schemas.platform_events import (
    PlatformActorKind,
    PlatformEventAction,
    PlatformEventCategory,
    PlatformEventOutcome,
    PlatformEventTargetType,
)
from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session
from tests import test_auth_totp_recovery as core
from tests.test_auth_totp_recovery import world  # noqa: F401 — 픽스처
from tests.test_totp_recovery_api import code, env  # noqa: F401 — env 는 픽스처

A = PlatformEventAction
S, DENIED, FAILED = PlatformEventOutcome.SUCCEEDED, PlatformEventOutcome.DENIED, PlatformEventOutcome.FAILED


# ----- 도우미 ---------------------------------------------------------------------------


def rows(engine, action=A.AUTH_TOTP_RECOVERY, *, target=None, actor=None):
    """그 대상 · 행위자의 활동 로그 행(시각순)."""
    query = select(models.PlatformEvent).where(models.PlatformEvent.action == action.value)
    if target is not None:
        query = query.where(models.PlatformEvent.target_id == target)
    if actor is not None:
        query = query.where(models.PlatformEvent.actor_user_id == actor)
    with Session(engine) as s:
        return s.scalars(query.order_by(models.PlatformEvent.occurred_at)).all()


def outcomes(engine, target) -> list[tuple]:
    return [(row.outcome, row.details.get("reason_code")) for row in rows(engine, target=target)]


def is_recovery_row(row, outcome, *, actor, target, tenant, reason=None) -> None:
    """복구 행의 공통 모양 — 행위자는 요청한 관리자(그 세션), 대상은 복구되는 계정."""
    assert (row.action, row.outcome) == (A.AUTH_TOTP_RECOVERY.value, outcome)
    assert row.category is PlatformEventCategory.AUTH
    assert (row.actor_kind, row.actor_user_id) == (PlatformActorKind.USER, actor.user_id)
    assert (row.actor_account_type, row.actor_platform_role) == (actor.account_type, actor.platform_role)
    assert row.session_id == actor.session_id
    assert (row.target_type, row.target_id) == (PlatformEventTargetType.USER, target)
    assert row.tenant_id == tenant
    assert row.details == ({} if reason is None else {"reason_code": reason})


def pair(env, *, target_kwargs=None):  # noqa: F811
    """같은 고객사의 ADMIN 과 대상."""
    tenant = env.tenant()
    admin = env.user(UserRole.ADMIN, tenant_id=tenant)
    target = env.user(tenant_id=tenant, **(target_kwargs or {}))
    return tenant, admin, target


def fence(engine, op):
    """그 작업의 울타리 풀림(풀리지 않았으면 None)."""
    with engine.connect() as conn:
        return conn.execute(
            text("SELECT resolution::text FROM totp_provider_mutations WHERE operation_id = :o"),
            {"o": str(op)},
        ).one()[0]


# ----- 완료 ----------------------------------------------------------------------------


@pytest.mark.parametrize("actor_kind", ["tenant-admin", "central-admin"])
def test_completed_recovery_is_one_row_by_the_admin_about_the_target(env, actor_kind):  # noqa: F811
    tenant = env.tenant()
    target = env.user(tenant_id=tenant)
    if actor_kind == "tenant-admin":
        admin = env.user(UserRole.ADMIN, tenant_id=tenant)
    else:
        admin = env.user(UserRole.ADMIN, platform_role=PlatformRole.CENTRAL_ADMIN)

    assert env.post(admin, target.user_id).status_code == 204

    [row] = rows(env.engine, target=target.user_id)
    # 소속은 행위자가 아니라 대상 계정의 고객사다 — 중앙 관리자가 해도 그 고객사의 행이다
    is_recovery_row(row, S, actor=admin, target=target.user_id, tenant=tenant)
    assert len(rows(env.engine, actor=admin.user_id)) == 1


def test_replaying_the_same_operation_adds_no_row(env):  # noqa: F811
    _, admin, target = pair(env)
    op = uuid.uuid4()
    assert env.post(admin, target.user_id, op=op).status_code == 204
    assert env.post(admin, target.user_id, op=op).status_code == 204
    assert outcomes(env.engine, target.user_id) == [(S, None)]


# ----- 이 행동으로 남지 않는 거절 ----------------------------------------------------------


@pytest.mark.parametrize("case", ["forbidden", "self", "foreign", "missing", "unverified", "suspended"])
def test_refusals_before_the_target_is_settled_are_not_recovery_rows(env, case):  # noqa: F811
    tenant = env.tenant(TenantStatus.SUSPENDED if case == "suspended" else None)
    actor = env.user(UserRole.OPERATOR if case == "forbidden" else UserRole.ADMIN, tenant_id=tenant)
    target = env.user(tenant_id=env.tenant() if case == "foreign" else tenant).user_id
    body = None
    if case == "self":
        target = actor.user_id
    elif case == "missing":
        target = str(uuid.uuid4())
    elif case == "unverified":
        body = {"operation_id": str(uuid.uuid4()), "identity_verified": False}

    resp = env.post(actor, target, body)

    expected = {
        "forbidden": (403, "AUTH_FORBIDDEN"),
        "self": (403, "AUTH_FORBIDDEN"),
        "foreign": (404, "USER_NOT_FOUND"),
        "missing": (404, "USER_NOT_FOUND"),
        "unverified": (422, "REQUEST_VALIDATION_FAILED"),
        "suspended": (409, "TENANT_SUSPENDED"),
    }[case]
    assert (resp.status_code, code(resp)) == expected
    assert rows(env.engine, actor=actor.user_id) == []
    assert rows(env.engine, target=target) == []
    # 403 은 공통 오류 처리기의 권한 거절 한 행이 맡는다 — 복구 행과 겹쳐 남지 않는다
    forbidden = rows(env.engine, A.ACCESS_FORBIDDEN, actor=actor.user_id)
    assert len(forbidden) == (1 if expected[0] == 403 else 0)


# ----- 대상이 정해진 뒤의 거절 · 실패 -------------------------------------------------------


def test_ineligible_target_is_a_denied_row(env):  # noqa: F811
    tenant, admin, target = pair(env, target_kwargs={"status": models.AppUserStatus.DISABLED})
    resp = env.post(admin, target.user_id)
    assert (resp.status_code, code(resp)) == (409, "USER_STATE_CONFLICT")
    [row] = rows(env.engine, target=target.user_id)
    is_recovery_row(
        row, DENIED, actor=admin, target=target.user_id, tenant=tenant, reason="TARGET_INELIGIBLE"
    )


def test_operation_id_of_another_target_is_a_denied_row_about_the_requested_target(env):  # noqa: F811
    tenant = env.tenant()
    admin = env.user(UserRole.ADMIN, tenant_id=tenant)
    first, second = env.user(tenant_id=tenant), env.user(tenant_id=tenant)
    op = uuid.uuid4()
    assert env.post(admin, first.user_id, op=op).status_code == 204
    assert env.post(admin, second.user_id, op=op).status_code == 409

    assert outcomes(env.engine, first.user_id) == [(S, None)]
    [row] = rows(env.engine, target=second.user_id)
    is_recovery_row(
        row, DENIED, actor=admin, target=second.user_id, tenant=tenant, reason="OPERATION_CONFLICT"
    )


def test_unresolved_fence_is_a_denied_row_after_the_failed_one(env):  # noqa: F811
    tenant, admin, target = pair(env)
    env.adapter.outcome = D.UNKNOWN
    assert env.post(admin, target.user_id).status_code == 503  # 결과 불명 — 울타리가 남는다
    env.adapter.outcome = D.DELETED
    resp = env.post(admin, target.user_id)  # 새 작업은 그 울타리에 막힌다(users 잠금 안의 거절)
    assert (resp.status_code, code(resp)) == (409, "USER_STATE_CONFLICT")

    assert outcomes(env.engine, target.user_id) == [(FAILED, "PROVIDER_UNKNOWN"), (DENIED, "BUSY")]
    is_recovery_row(
        rows(env.engine, target=target.user_id)[1],
        DENIED, actor=admin, target=target.user_id, tenant=tenant, reason="BUSY",
    )


@pytest.mark.parametrize(
    "outcome,reason",
    [(D.NOT_FOUND, "PROVIDER_NOT_FOUND"), (D.REJECTED, "PROVIDER_REJECTED"), (D.UNKNOWN, "PROVIDER_UNKNOWN")],
)
def test_recovery_that_did_not_complete_is_one_failed_row_even_when_replayed(env, outcome, reason):  # noqa: F811
    tenant, admin, target = pair(env)
    env.adapter.outcome = outcome
    op = uuid.uuid4()
    assert env.post(admin, target.user_id, op=op).status_code == 503
    assert env.post(admin, target.user_id, op=op).status_code == 503  # 재요청 — 제공자도 기록도 다시 하지 않는다

    [row] = rows(env.engine, target=target.user_id)
    is_recovery_row(row, FAILED, actor=admin, target=target.user_id, tenant=tenant, reason=reason)
    assert len(env.adapter.deleted) == 1


def test_provider_identity_mismatch_is_a_failed_row_without_any_change(env):  # noqa: F811
    tenant, admin, target = pair(env)
    env.adapter.issuer = "https://cognito-idp.ap-northeast-2.amazonaws.com/ap-northeast-2_other"
    resp = env.post(admin, target.user_id)
    assert (resp.status_code, code(resp)) == (503, "AUTH_PROVIDER_UNAVAILABLE")

    [row] = rows(env.engine, target=target.user_id)
    is_recovery_row(
        row, FAILED, actor=admin, target=target.user_id, tenant=tenant, reason="PROVIDER_IDENTITY_MISMATCH"
    )
    assert env.operations(target.user_id) == [] and env.adapter.deleted == []


# ----- 완료 행은 결과 확정과 같은 트랜잭션 ---------------------------------------------------


@pytest.mark.parametrize("error", [OperationalError("event write", {}, Exception("not exposed")), ValueError("x")])
def test_recovery_is_not_finalized_when_the_completed_row_cannot_be_written(env, monkeypatch, error):  # noqa: F811
    _, admin, target = pair(env)
    env.extra_sessions(target.user_id)
    original = platform_events_repo.record_platform_event

    def fail(db, action, *args, **kwargs):
        original(db, action, *args, **kwargs)  # 같은 트랜잭션에 쓴 뒤 실패한다 — 되돌려져야 한다
        raise error

    monkeypatch.setattr(platform_events_repo, "record_platform_event", fail)
    op = uuid.uuid4()
    resp = env.post(admin, target.user_id, op=op)

    # 제공자는 지웠지만 결과는 확정되지 않았다 — 204 가 아니고, 작업은 결과 대기 · 울타리는 그대로다
    assert (resp.status_code, code(resp)) == (503, "AUTH_PROVIDER_UNAVAILABLE")
    assert env.operations(target.user_id) == [(str(op), "PROVIDER_PENDING", admin.user_id)]
    assert fence(env.engine, op) is None and len(env.adapter.deleted) == 1
    # 제공자 호출 전에 끝난 차단은 그대로다 — 되살아난 세션이 없다
    assert env.row(target.user_id)[:2] == (1, "RESET_PENDING")
    assert set(env.revoke_reasons(target.user_id)) == {"TOTP_RESET"}
    # 성공 행은 없고, 완료되지 못했다는 한 행이 따로 남는다
    assert outcomes(env.engine, target.user_id) == [(FAILED, "PROVIDER_PENDING")]


def test_no_completed_row_when_the_result_was_not_recorded(env, monkeypatch):  # noqa: F811
    _, admin, target = pair(env)
    monkeypatch.setattr(totp_recovery.recovery_repo, "finalize_operation", lambda *a, **k: False)
    assert env.post(admin, target.user_id).status_code == 503
    assert outcomes(env.engine, target.user_id) == [(FAILED, "PROVIDER_PENDING")]


# ----- 따로 남기는 행은 응답을 바꾸지 않는다 ---------------------------------------------------


def test_refusal_and_failure_keep_their_responses_when_the_row_cannot_be_written(env, monkeypatch):  # noqa: F811
    import platform_event_log

    def refuse(*args, **kwargs):
        raise ValueError("outside the event contract")

    monkeypatch.setattr(platform_event_log, "record_detached", refuse)
    tenant = env.tenant()
    admin = env.user(UserRole.ADMIN, tenant_id=tenant)
    disabled = env.user(tenant_id=tenant, status=models.AppUserStatus.DISABLED)
    target = env.user(tenant_id=tenant)

    assert env.post(admin, disabled.user_id).status_code == 409
    env.adapter.outcome = D.REJECTED
    assert env.post(admin, target.user_id).status_code == 503
    assert env.operations(target.user_id)[0][1] == "PROVIDER_REJECTED"  # 결과 기록은 그대로다
    assert rows(env.engine, target=disabled.user_id) == rows(env.engine, target=target.user_id) == []


def test_row_is_still_written_without_a_tenant_when_the_target_lookup_fails(world):  # noqa: F811
    calls = {"n": 0}

    def sessions():
        calls["n"] += 1
        if calls["n"] == 1:  # 대상 조회용 연결만 실패한다
            raise OperationalError("lookup", {}, Exception("not exposed"))
        return Session(world.engine)

    event_log.totp_recovery_not_completed(
        sessions, platform_events_repo.SYSTEM, DENIED, "BUSY", target_user_id=world.target
    )
    [row] = rows(world.engine, target=world.target)
    assert (row.outcome, row.tenant_id, row.details) == (DENIED, None, {"reason_code": "BUSY"})


# ----- 운영자 CLI · 코어 --------------------------------------------------------------------


def test_operator_cli_recovery_is_a_system_row(world):  # noqa: F811
    with Session(world.engine) as s:
        staff = auth_repo.create_user(
            s, issuer=core.ISSUER, cognito_sub=f"sub-{uuid.uuid4().hex}", email="staff@example.com",
            role=UserRole.ADMIN, account_type=AccountType.PLATFORM, tenant_id=None, now=core.T0,
            platform_role=PlatformRole.TENANT_MANAGER, email_verified_at=core.T0,
        )
        staff_id, staff_sub = staff.user_id, staff.cognito_sub
        s.commit()
    world.user_ids.append(staff_id)
    fake = core.FakeCognito(staff_sub)

    exit_code = recover_totp.run(
        ["--user-id", staff_id, "--operation-id", str(uuid.uuid4()), "--identity-verified", "true", "--yes"],
        engine=world.engine, get_adapter=lambda: core.adapter_for(fake), now=lambda: core.T1, out=io.StringIO(),
    )

    assert exit_code == 0
    [row] = rows(world.engine, target=staff_id)
    assert (row.outcome, row.actor_kind) == (S, PlatformActorKind.SYSTEM)
    assert (row.actor_user_id, row.actor_account_type, row.actor_platform_role, row.session_id) == (
        None, None, None, None,
    )
    # 플랫폼 계정은 고객사 소속이 없다
    assert (row.target_type, row.tenant_id, row.details) == (PlatformEventTargetType.USER, None, {})


def test_system_recovery_of_a_tenant_account_belongs_to_its_tenant(world):  # noqa: F811
    fake = core.FakeCognito(world.sub)
    assert core.reset(world, core.adapter_for(fake)).completed
    [row] = rows(world.engine, target=world.target)
    assert (row.outcome, row.actor_kind, row.tenant_id) == (S, PlatformActorKind.SYSTEM, world.tenant_id)


def test_actor_outside_the_contract_stops_before_anything_changes(world):  # noqa: F811
    fake = core.FakeCognito(world.sub)
    outside = platform_events_repo.EventActor(
        PlatformActorKind.USER, user_id=world.admin, account_type=AccountType.UNASSIGNED
    )
    with pytest.raises(ValueError):
        core.reset(world, core.adapter_for(fake), event_actor=outside)
    core.unchanged(world, fake)
    assert rows(world.engine, target=world.target) == []
