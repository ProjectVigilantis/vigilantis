"""사용자 관리 저장소 · 담당 그룹 배정 · 고객사 그룹 · 고객사 검색(#572) · 감사 이력 append-only — 실제 PostgreSQL."""

from datetime import UTC, datetime, timedelta

import pytest
from db import models
from db.repositories import auth as auth_repo
from db.repositories import tenant_groups as groups_repo
from db.repositories import tenants as tenants_repo
from pydantic import ValidationError
from schemas.api.auth import AccountType, PlatformRole, UserRole
from schemas.api.tenants import TenantVerificationResult
from schemas.api.users import UserEventAction
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

ISSUER = "https://cognito-idp.ap-northeast-2.amazonaws.com/pool-a"
NOW = datetime(2026, 10, 20, 9, 0, tzinfo=UTC)
REASONS = models.AuthSessionRevokeReason


def _tenant(db, account):
    """실제 ACTIVE 테넌트 — 연결 확인 기록을 거쳐야 ACTIVE 가 된다."""
    tenant = tenants_repo.create_tenant(
        db, name=f"t-{account}", aws_account_id=account, regions=["ap-northeast-2"], now=NOW
    )
    assert tenants_repo.record_verification(
        db,
        tenant.tenant_id,
        result=TenantVerificationResult.OK,
        reason_code=None,
        now=NOW,
    )
    return tenant


def _user(
    db,
    sub,
    role=UserRole.VIEWER,
    status=models.AppUserStatus.ACTIVE,
    account_type=AccountType.UNASSIGNED,
    tenant_id=None,
    platform_role=None,
):
    return auth_repo.create_user(
        db,
        issuer=ISSUER,
        cognito_sub=sub,
        email=f"{sub}@example.com",
        role=role,
        account_type=account_type,
        tenant_id=tenant_id,
        status=status,
        now=NOW,
        platform_role=platform_role,
    )


def _platform(db, sub, platform_role=None, **kw):
    return _user(
        db, sub, role=UserRole.ADMIN, account_type=AccountType.PLATFORM,
        platform_role=platform_role, **kw,
    )


def _member(db, sub, tenant, **kw):
    return _user(
        db, sub, account_type=AccountType.TENANT, tenant_id=tenant.tenant_id, **kw
    )


def _session(db, user, token_hash, created_at, revoked=False):
    row = auth_repo.create_session(
        db,
        token_hash=token_hash,
        user_id=user.user_id,
        mfa_step=models.AuthSessionMfaStep.SOFTWARE_TOKEN_MFA,
        now=created_at,
        absolute_expires_at=created_at + timedelta(hours=8),
    )
    if revoked:
        auth_repo.revoke_session(
            db, token_hash=token_hash, reason=REASONS.LOGOUT, now=created_at
        )
    return row


def _event(db, actor, target, **overrides):
    values = {
        "actor_user_id": actor.user_id,
        "target_user_id": target.user_id,
        "action": UserEventAction.ROLE_CHANGED,
        "before_state": {"role": "VIEWER"},
        "after_state": {"role": "ADMIN"},
        "now": NOW,
    }
    values.update(overrides)
    return auth_repo.add_user_event(db, **values)


def test_enum_labels_include_new_revoke_reasons(db):
    labels = set(
        db.execute(
            text(
                "SELECT enumlabel FROM pg_enum e JOIN pg_type t ON t.oid = e.enumtypid "
                "WHERE t.typname = 'auth_session_revoke_reason'"
            )
        ).scalars()
    )
    assert labels == {
        "LOGOUT",
        "SUPERSEDED",
        "ADMIN_REVOKED",
        "ACCOUNT_DISABLED",
        "PASSWORD_RESET",
        "TOTP_RESET",  # #515
    }


def test_list_users_with_last_login_uses_newest_session_even_if_revoked(db):
    tenant = _tenant(db, "111111111111")
    never = _member(db, "never", tenant)
    user = _member(db, "someone", tenant)
    _session(db, user, "1" * 64, NOW + timedelta(hours=1))
    _session(db, user, "2" * 64, NOW + timedelta(hours=3), revoked=True)
    rows = dict(
        (u.user_id, login)
        for u, login in auth_repo.list_users_with_last_login(
            db, tenant_ids=(tenant.tenant_id,)
        )
    )
    assert rows[never.user_id] is None
    assert rows[user.user_id] == NOW + timedelta(hours=3)


def test_list_users_returns_only_tenant_accounts_scoped_by_tenant(db):
    first = _tenant(db, "111111111111")
    second = _tenant(db, "222222222222")
    a = _member(db, "a", first)
    b = _member(db, "b", second)
    _user(db, "unassigned")
    _user(
        db, "platform", role=UserRole.ADMIN, account_type=AccountType.PLATFORM
    )

    def ids(tenant_ids):
        return {
            u.user_id
            for u, _ in auth_repo.list_users_with_last_login(db, tenant_ids=tenant_ids)
        }

    assert ids((first.tenant_id,)) == {a.user_id}
    assert ids((second.tenant_id,)) == {b.user_id}
    assert ids((first.tenant_id, second.tenant_id)) == {a.user_id, b.user_id}
    # 빈 튜플 = 아무 고객사도 아니다(전체로 읽히지 않는다)
    assert ids(()) == set()
    # None = 모든 고객 테넌트 — PLATFORM·UNASSIGNED 는 포함하지 않는다
    assert ids(None) == {a.user_id, b.user_id}


def test_count_active_admins_counts_only_active_tenant_admins_in_that_tenant(db):
    tenant = _tenant(db, "111111111111")
    other = _tenant(db, "222222222222")
    assert auth_repo.count_active_admins(db, tenant_id=tenant.tenant_id) == 0
    _member(db, "a1", tenant, role=UserRole.ADMIN)
    _member(db, "a2", tenant, role=UserRole.ADMIN, status=models.AppUserStatus.DISABLED)
    _member(db, "o1", tenant, role=UserRole.OPERATOR)
    _member(db, "x1", other, role=UserRole.ADMIN)
    _user(db, "p1", role=UserRole.ADMIN, account_type=AccountType.PLATFORM)
    assert auth_repo.count_active_admins(db, tenant_id=tenant.tenant_id) == 1
    assert auth_repo.count_active_admins(db, tenant_id=other.tenant_id) == 1


def test_primitives_see_every_account_type(db):
    tenant = _tenant(db, "111111111111")
    before = auth_repo.count_users(db)
    p = _user(db, "p", role=UserRole.ADMIN, account_type=AccountType.PLATFORM)
    u = _user(db, "u")
    t = _member(db, "t", tenant)
    assert auth_repo.count_users(db) == before + 3
    for row in (p, u, t):
        assert auth_repo.get_user(db, row.user_id).user_id == row.user_id
        assert (
            auth_repo.get_user_by_identity(db, issuer=ISSUER, cognito_sub=row.cognito_sub).user_id
            == row.user_id
        )


def test_set_role_updates_and_reports_missing(db):
    user = _user(db, "r1")
    later = NOW + timedelta(minutes=1)
    assert auth_repo.set_role(db, user_id=user.user_id, role=UserRole.ADMIN, now=later)
    assert auth_repo.get_user(db, user.user_id).role is UserRole.ADMIN
    assert auth_repo.get_user(db, user.user_id).updated_at == later
    assert not auth_repo.set_role(
        db,
        user_id="00000000-0000-0000-0000-000000000000",
        role=UserRole.ADMIN,
        now=later,
    )


def test_create_user_defaults_platform_to_unassigned_manager_and_others_to_null(db):
    tenant = _tenant(db, "111111111111")
    defaulted = _platform(db, "p-default")
    central = _platform(db, "p-central", PlatformRole.CENTRAL_ADMIN)
    member = _member(db, "t", tenant, role=UserRole.ADMIN)
    unassigned = _user(db, "u", role=UserRole.ADMIN)
    stored = dict(
        db.execute(
            text("SELECT cognito_sub, platform_role::text FROM app_users WHERE issuer = :i"),
            {"i": ISSUER},
        ).all()
    )
    # 등급을 정하지 않은 플랫폼 계정은 TENANT_MANAGER — role=ADMIN 이 전역 권한을 만들지 않는다
    assert stored == {
        defaulted.cognito_sub: "TENANT_MANAGER",
        central.cognito_sub: "CENTRAL_ADMIN",
        member.cognito_sub: None,
        unassigned.cognito_sub: None,
    }
    # 만들 때 배정을 주지 않는다 — 소속(member) · 계정 종류도 배정이 아니다
    for row in (defaulted, central, member, unassigned):
        assert auth_repo.list_assigned_group_ids(db, user_id=row.user_id) == ()
        assert auth_repo.list_group_scoped_tenant_ids(db, user_id=row.user_id) == ()
    for account_type, tenant_id in (
        (AccountType.TENANT, tenant.tenant_id),
        (AccountType.UNASSIGNED, None),
    ):
        with pytest.raises(IntegrityError, match="platform_role_by_account_type"), db.begin_nested():
            _user(
                db, f"bad-{account_type.value}", role=UserRole.ADMIN, account_type=account_type,
                tenant_id=tenant_id, platform_role=PlatformRole.TENANT_MANAGER,
            )


def test_set_platform_role_changes_only_platform_accounts(db):
    tenant = _tenant(db, "111111111111")
    manager = _platform(db, "p")
    member = _member(db, "t", tenant, role=UserRole.ADMIN)
    unassigned = _user(db, "u", role=UserRole.ADMIN)
    later = NOW + timedelta(minutes=1)
    assert auth_repo.set_platform_role(
        db, user_id=manager.user_id, platform_role=PlatformRole.CENTRAL_ADMIN, now=later
    )
    changed = auth_repo.get_user(db, manager.user_id)
    assert (changed.platform_role, changed.account_type, changed.role, changed.updated_at) == (
        PlatformRole.CENTRAL_ADMIN, AccountType.PLATFORM, UserRole.ADMIN, later,
    )
    # 계정 종류를 바꾸지 않는다 — 고객 · 미지정 계정과 없는 계정은 False 이고 그대로다
    for user_id in (member.user_id, unassigned.user_id, "00000000-0000-0000-0000-000000000000"):
        assert not auth_repo.set_platform_role(
            db, user_id=user_id, platform_role=PlatformRole.CENTRAL_ADMIN, now=later
        )
    for row in (member, unassigned):
        kept = auth_repo.get_user(db, row.user_id)
        assert (kept.account_type, kept.platform_role, kept.updated_at) == (
            row.account_type, None, NOW,
        )


def test_count_active_central_admins_counts_only_active_platform_central_admins(db):
    tenant = _tenant(db, "111111111111")
    before = auth_repo.count_active_central_admins(db)
    _platform(db, "c1", PlatformRole.CENTRAL_ADMIN)
    _platform(db, "c2", PlatformRole.CENTRAL_ADMIN)
    _platform(db, "c3", PlatformRole.CENTRAL_ADMIN, status=models.AppUserStatus.DISABLED)
    _platform(db, "m1", PlatformRole.TENANT_MANAGER)
    _platform(db, "s1")
    _member(db, "a1", tenant, role=UserRole.ADMIN)  # 고객사 ADMIN 은 중앙 관리자가 아니다
    assert auth_repo.count_active_central_admins(db) == before + 2


def _group(db, name, *tenant_ids):
    """고객사 그룹 1건 — 주어진 고객사를 넣는다(#572 2번)."""
    group = groups_repo.create_group(db, name=name, now=NOW)
    for tenant_id in tenant_ids:
        assert groups_repo.set_tenant_group(db, tenant_id, group.group_id, now=NOW)
    return group.group_id


def test_replace_assigned_groups_replaces_the_whole_set_in_canonical_order(db):
    tenants = [_tenant(db, f"{n}" * 12).tenant_id for n in (1, 2, 3)]
    groups = [_group(db, f"g{n}", tenant_id) for n, tenant_id in enumerate(tenants)]
    manager = _platform(db, "m")
    other = _platform(db, "o")
    later = NOW + timedelta(minutes=1)
    auth_repo.replace_assigned_groups(db, user_id=other.user_id, group_ids=(groups[0],), now=NOW)

    auth_repo.replace_assigned_groups(
        db, user_id=manager.user_id, group_ids=(groups[2], groups[0]), now=later
    )
    assert auth_repo.list_assigned_group_ids(db, user_id=manager.user_id) == tuple(
        sorted((groups[0], groups[2]))
    )
    # 범위는 그 그룹들에 든 고객사다(tenant_id 오름차순)
    assert auth_repo.list_group_scoped_tenant_ids(db, user_id=manager.user_id) == tuple(
        sorted((tenants[0], tenants[2]))
    )
    assert auth_repo.get_user(db, manager.user_id).updated_at == later

    # 통째로 교체 — 빠진 그룹은 회수되고, 다른 사용자의 배정은 그대로다
    auth_repo.replace_assigned_groups(db, user_id=manager.user_id, group_ids=(groups[1],), now=later)
    assert auth_repo.list_group_scoped_tenant_ids(db, user_id=manager.user_id) == (tenants[1],)
    auth_repo.replace_assigned_groups(db, user_id=manager.user_id, group_ids=(), now=later)
    assert auth_repo.list_assigned_group_ids(db, user_id=manager.user_id) == ()
    assert auth_repo.list_group_scoped_tenant_ids(db, user_id=manager.user_id) == ()
    assert auth_repo.list_assigned_group_ids(db, user_id=other.user_id) == (groups[0],)


def test_group_scope_follows_tenant_placement_and_shared_groups(db):
    """한 그룹을 여러 직원이 맡고, 고객사를 그룹에 넣고 빼면 그 그룹 담당자 모두의 범위가 따라간다."""
    a, b = _tenant(db, "111111111111").tenant_id, _tenant(db, "222222222222").tenant_id
    shared = _group(db, "광주 지사", a)
    first, second = _platform(db, "m1"), _platform(db, "m2")
    for row in (first, second):
        auth_repo.replace_assigned_groups(db, user_id=row.user_id, group_ids=(shared,), now=NOW)
    assert groups_repo.set_tenant_group(db, b, shared, now=NOW)
    for row in (first, second):
        assert auth_repo.list_group_scoped_tenant_ids(db, user_id=row.user_id) == tuple(sorted((a, b)))
    assert groups_repo.set_tenant_group(db, a, None, now=NOW)  # 그룹에서 뺀다
    for row in (first, second):
        assert auth_repo.list_group_scoped_tenant_ids(db, user_id=row.user_id) == (b,)
    [listed] = [row for row in groups_repo.list_groups(db) if row.group.group_id == shared]
    assert listed.tenant_count == 1


def test_assignment_rejects_unknown_targets_and_duplicates(db):
    group = _group(db, "g")
    manager = _platform(db, "m")
    missing = "00000000-0000-0000-0000-000000000000"
    for user_id, group_ids in (
        (manager.user_id, (missing,)),  # 없는 그룹
        (missing, (group,)),  # 없는 사용자
        (manager.user_id, (group, group)),  # 중복 — 복합 PK
    ):
        with pytest.raises(IntegrityError), db.begin_nested():
            auth_repo.replace_assigned_groups(db, user_id=user_id, group_ids=group_ids, now=NOW)
    assert auth_repo.list_assigned_group_ids(db, user_id=manager.user_id) == ()


def test_group_names_are_unique(db):
    _group(db, "광주 지사")
    with pytest.raises(IntegrityError, match=groups_repo.GROUP_NAME_UNIQUE), db.begin_nested():
        _group(db, "광주 지사")


def test_deleting_a_group_ungroups_its_tenants_and_leaves_no_stale_assignment(db):
    kept_tenant, grouped = _tenant(db, "111111111111").tenant_id, _tenant(db, "222222222222").tenant_id
    kept, doomed = _group(db, "kept", kept_tenant), _group(db, "doomed", grouped)
    manager = _platform(db, "m")
    auth_repo.replace_assigned_groups(db, user_id=manager.user_id, group_ids=(kept, doomed), now=NOW)
    assert groups_repo.delete_group(db, doomed)
    assert not groups_repo.delete_group(db, doomed)  # 이미 없다
    db.flush()
    assert auth_repo.list_assigned_group_ids(db, user_id=manager.user_id) == (kept,)
    assert auth_repo.list_group_scoped_tenant_ids(db, user_id=manager.user_id) == (kept_tenant,)
    # 고객사는 지워지지 않고 그룹 없음(담당자 없음)으로 돌아간다
    assert tenants_repo.get_tenant(db, grouped).tenant.group_id is None


def test_deleting_a_tenant_leaves_the_group_and_its_assignment(db):
    kept = _tenant(db, "111111111111").tenant_id
    pending = tenants_repo.create_tenant(
        db, name="pending", aws_account_id="222222222222", regions=["ap-northeast-2"], now=NOW
    ).tenant_id
    group = _group(db, "g", kept, pending)
    manager = _platform(db, "m")
    auth_repo.replace_assigned_groups(db, user_id=manager.user_id, group_ids=(group,), now=NOW)
    assert tenants_repo.delete_tenant(db, pending)
    db.flush()
    assert auth_repo.list_assigned_group_ids(db, user_id=manager.user_id) == (group,)
    assert auth_repo.list_group_scoped_tenant_ids(db, user_id=manager.user_id) == (kept,)


def test_search_matches_names_case_insensitively_with_literal_wildcards(db):
    names = ("zq9 Acme 학원", "zq9 ACME 2", "zq9 100% 학원", "zq9_lab", "zq9xlab")
    ids = {
        name: tenants_repo.create_tenant(
            db, name=name, aws_account_id=f"{n}" * 12, regions=["ap-northeast-2"], now=NOW
        ).tenant_id
        for n, name in enumerate(names, start=1)
    }

    def found(query, limit=20):
        return [name for _, name in tenants_repo.search_tenants(db, query, limit=limit)]

    assert set(found("ZQ9 acme")) == {"zq9 Acme 학원", "zq9 ACME 2"}
    # LIKE 와일드카드(% · _)는 글자 그대로 찾는다 — 와일드카드였다면 zq9_lab · zq9xlab 이 걸린다
    assert found("zq9%lab") == []
    assert found("zq9_lab") == ["zq9_lab"]
    assert found("100% 학") == ["zq9 100% 학원"]
    every = tenants_repo.search_tenants(db, "zq9", limit=20)
    assert {name for _, name in every} == set(names) and {tid for tid, _ in every} == set(ids.values())
    assert tenants_repo.search_tenants(db, "zq9", limit=2) == every[:2]  # 상한은 같은 정렬의 앞부분


def test_list_platform_users_returns_every_platform_account_with_last_login(db):
    tenant = _tenant(db, "111111111111")
    central = _platform(db, "pc", PlatformRole.CENTRAL_ADMIN)
    disabled = _platform(db, "pd", status=models.AppUserStatus.DISABLED)
    _member(db, "t", tenant, role=UserRole.ADMIN)
    _user(db, "u", role=UserRole.ADMIN)
    _session(db, central, "1" * 64, NOW + timedelta(hours=1))
    _session(db, central, "2" * 64, NOW + timedelta(hours=2), revoked=True)
    rows = {
        u.user_id: (u.platform_role, login)
        for u, login in auth_repo.list_platform_users_with_last_login(db)
        if u.issuer == ISSUER
    }
    assert rows == {
        central.user_id: (PlatformRole.CENTRAL_ADMIN, NOW + timedelta(hours=2)),
        disabled.user_id: (PlatformRole.TENANT_MANAGER, None),
    }


def test_get_user_reads_fresh_values_after_external_change(db):
    user = _user(db, "fresh")
    db.execute(
        text("UPDATE app_users SET status = 'DISABLED' WHERE user_id = :u"),
        {"u": user.user_id},
    )
    assert auth_repo.get_user(db, user.user_id).status is models.AppUserStatus.DISABLED


def test_revoke_user_sessions_revokes_every_unrevoked_including_expired(db):
    user = _user(db, "rv")
    other = _user(db, "other")
    _session(db, user, "a" * 64, NOW)  # 만료 시각이 지난 세션도 대상
    _session(db, user, "b" * 64, NOW + timedelta(hours=1))
    _session(db, user, "c" * 64, NOW + timedelta(hours=2), revoked=True)
    _session(db, other, "d" * 64, NOW)
    later = NOW + timedelta(days=2)
    assert (
        auth_repo.revoke_user_sessions(
            db, user_id=user.user_id, reason=REASONS.ADMIN_REVOKED, now=later
        )
        == 2
    )
    assert (
        auth_repo.revoke_user_sessions(
            db, user_id=user.user_id, reason=REASONS.ADMIN_REVOKED, now=later
        )
        == 0
    )
    rows = db.execute(
        text(
            "SELECT user_id, revoke_reason::text FROM auth_sessions "
            "ORDER BY token_hash"
        )
    ).all()
    assert [r[1] for r in rows if str(r[0]) == user.user_id] == [
        "ADMIN_REVOKED",
        "ADMIN_REVOKED",
        "LOGOUT",
    ]
    assert [r[1] for r in rows if str(r[0]) == other.user_id] == [None]


def test_events_list_newest_first_for_target_only(db):
    actor = _user(db, "actor")
    target = _user(db, "target")
    elsewhere = _user(db, "elsewhere")
    first = _event(db, actor, target, now=NOW)
    second = _event(
        db,
        actor,
        target,
        action=UserEventAction.SESSIONS_REVOKED,
        before_state={},
        after_state={"revoked_session_count": 2},
        now=NOW + timedelta(minutes=1),
    )
    _event(db, actor, elsewhere)
    events = auth_repo.list_user_events(db, user_id=target.user_id, limit=10)
    assert [e.event_id for e in events] == [second.event_id, first.event_id]
    assert events[0].after_state == {"revoked_session_count": 2}
    assert len(auth_repo.list_user_events(db, user_id=target.user_id, limit=1)) == 1


def test_platform_role_change_event_is_stored_with_typed_states(db):
    actor = _platform(db, "pr-actor", PlatformRole.CENTRAL_ADMIN)
    target = _platform(db, "pr-target")
    event = _event(
        db,
        actor,
        target,
        action=UserEventAction.PLATFORM_ROLE_CHANGED,
        before_state={"platform_role": "TENANT_MANAGER"},
        after_state={"platform_role": "CENTRAL_ADMIN"},
    )
    stored = db.execute(
        text(
            "SELECT action::text, before_state, after_state FROM user_admin_events "
            "WHERE event_id = :e"
        ),
        {"e": event.event_id},
    ).one()
    assert tuple(stored) == (
        "PLATFORM_ROLE_CHANGED",
        {"platform_role": "TENANT_MANAGER"},
        {"platform_role": "CENTRAL_ADMIN"},
    )


def test_tenant_assignment_change_event_is_stored_with_typed_tenant_ids(db):
    first = _tenant(db, "111111111111")
    second = _tenant(db, "222222222222")
    actor = _platform(db, "ta-actor", PlatformRole.CENTRAL_ADMIN)
    target = _platform(db, "ta-target")
    event = _event(
        db,
        actor,
        target,
        action=UserEventAction.TENANT_ASSIGNMENTS_CHANGED,
        before_state={"tenant_ids": []},
        after_state={"tenant_ids": [first.tenant_id, second.tenant_id.upper()]},
    )
    stored = db.execute(
        text(
            "SELECT action::text, before_state, after_state FROM user_admin_events "
            "WHERE event_id = :e"
        ),
        {"e": event.event_id},
    ).one()
    # 빈 목록(담당 없음)도 남는다 — None 만 빠진다. ID 는 정규 UUID 문자열로 저장된다
    assert tuple(stored) == (
        "TENANT_ASSIGNMENTS_CHANGED",
        {"tenant_ids": []},
        {"tenant_ids": [first.tenant_id, second.tenant_id]},
    )


@pytest.mark.parametrize(
    "state",
    [
        {"password": "hunter2"},
        {"role": "ADMIN", "refresh_token": "abc"},
        {"role": "SUPERUSER"},
        {"platform_role": "ADMIN"},
        {"platform_role": "STAFF"},
        {"revoked_session_count": -1},
        {"tenant_ids": ["not-a-uuid"]},
        {"tenant_ids": "1f0c2c1e-0000-4000-8000-000000000002"},
    ],
)
def test_event_payload_outside_allowlist_is_rejected_and_not_stored(db, state):
    actor = _user(db, "p-actor")
    target = _user(db, "p-target")
    for key in ("before_state", "after_state"):
        with pytest.raises(ValidationError):
            _event(db, actor, target, **{key: state})
    assert auth_repo.list_user_events(db, user_id=target.user_id, limit=10) == []


def test_audit_rows_reject_update_and_delete_but_insert_is_allowed(db):
    actor = _user(db, "t-actor")
    target = _user(db, "t-target")
    event = _event(db, actor, target)
    with pytest.raises(DBAPIError, match="append-only"), db.begin_nested():
        db.execute(
            text("UPDATE user_admin_events SET action = 'USER_INVITED'")
        )
    with pytest.raises(DBAPIError, match="append-only"), db.begin_nested():
        db.execute(text("DELETE FROM user_admin_events"))
    _event(db, actor, target)  # INSERT는 계속 허용
    count = db.execute(text("SELECT count(*) FROM user_admin_events")).scalar_one()
    assert count == 2
    assert event.before_state == {"role": "VIEWER"}


def test_users_referenced_by_audit_cannot_be_deleted(db):
    actor = _user(db, "d-actor")
    target = _user(db, "d-target")
    _event(db, actor, target)
    for user in (actor, target):
        with pytest.raises(DBAPIError), db.begin_nested():
            db.execute(
                text("DELETE FROM app_users WHERE user_id = :u"), {"u": user.user_id}
            )
