# ==============================================================================
# [파일 설명]  담당: 김승철 (API 계약 · BE)
# 테넌트 저장소 회귀 (Post-MVP T-4 · #510) — 실제 PostgreSQL · Alembic head.
#
# 지키는 것 넷.
#   ① 받는 상태 = 계약 상수 — 상태 3종 × 전이 4종 전부에서 「상수에 있으면 바뀌고, 없으면 0행」.
#   ② 전이는 조건부 UPDATE — 두 세션이 같은 테넌트를 동시에 정지하면 하나만 바뀐다.
#   ③ 결과 행이 DB CHECK(#506)를 지킨다 — 정지 기록 · 확인 결과 · ACTIVE 의 확인 기록.
#   ④ commit 하지 않는다 · 목록 집계는 소멸 자산 · 종료 인시던트 · 소속 없는 기록(NULL)을 세지 않는다.
# ==============================================================================

from __future__ import annotations

import threading
from datetime import UTC, date, datetime, timedelta

import pytest
from db import models
from db.repositories import tenants as tenants_repo
from schemas.api.assets import AssetType
from schemas.api.auth import AccountType, UserRole
from schemas.api.incidents import IncidentCategory, IncidentStatus
from schemas.api.tenants import (
    DELETABLE_STATUSES,
    RESUMABLE_STATUSES,
    SUSPENDABLE_STATUSES,
    VERIFIABLE_STATUSES,
    TenantStatus,
    TenantSubscription,
    TenantUpdateRequest,
    TenantVerificationResult,
)
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

NOW = datetime(2026, 10, 21, 6, 0, tzinfo=UTC)
LATER = NOW + timedelta(minutes=5)
ACCOUNT = "123456789012"
OK = TenantVerificationResult.OK
DENIED = TenantVerificationResult.ASSUME_DENIED


def _create(db, account: str = ACCOUNT, **over) -> models.Tenant:
    values = dict(name="학원", aws_account_id=account, regions=["ap-northeast-2"], now=NOW)
    values.update(over)
    return tenants_repo.create_tenant(db, **values)


def _in_status(db, status: TenantStatus, account: str = ACCOUNT) -> models.Tenant:
    """상태별 출발점 — 저장소 함수만으로 만든다(손으로 칸을 맞추지 않는다)."""
    tenant = _create(db, account)
    if status is not TenantStatus.PENDING_CONNECTION:
        assert tenants_repo.record_verification(
            db, tenant.tenant_id, result=OK, reason_code=None, now=NOW)
    if status is TenantStatus.SUSPENDED:
        assert tenants_repo.suspend_tenant(db, tenant.tenant_id, reason="계약 만료", now=NOW)
    return tenant


def _get(db, tenant_id: str) -> models.Tenant:
    return tenants_repo.get_tenant(db, tenant_id).tenant


# ----- 생성 · 조회 -------------------------------------------------------------

def test_new_tenant_is_pending_with_platform_made_values(db):
    tenant = _create(db)
    assert tenant.status is TenantStatus.PENDING_CONNECTION
    assert tenant.access_role_arn == f"arn:aws:iam::{ACCOUNT}:role/vigilantis-access"
    # 플랫폼이 정하는 값 — 추측할 수 없는 32자 hex
    assert len(tenant.external_id) == 32 and int(tenant.external_id, 16) >= 0
    assert tenant.collect_vpc_ids == [] and tenant.auto_action_enabled is True
    assert tenant.created_at == tenant.updated_at == NOW
    assert tenant.last_verified_at is None and tenant.suspended_at is None


def test_given_role_arn_is_kept(db):
    role = f"arn:aws:iam::{ACCOUNT}:role/custom/vigilantis"
    assert _create(db, access_role_arn=role).access_role_arn == role


def test_external_ids_differ_between_tenants(db):
    assert _create(db).external_id != _create(db, "111122223333").external_id


def test_same_account_cannot_become_two_tenants(db):
    _create(db)
    with pytest.raises(IntegrityError), db.begin_nested():
        _create(db, name="다른 이름")


def test_role_outside_the_tenant_account_is_rejected_by_the_database(db):
    with pytest.raises(IntegrityError), db.begin_nested():
        _create(db, access_role_arn="arn:aws:iam::111122223333:role/vigilantis-access")


def test_unknown_tenant_is_none(db):
    assert tenants_repo.get_tenant(db, "00000000-0000-0000-0000-000000000000") is None


def _asset(tenant_id, suffix: str, **over) -> models.Asset:
    return models.Asset(
        arn=f"arn:aws:ec2:ap-northeast-2:{ACCOUNT}:instance/i-{suffix}", tenant_id=tenant_id,
        asset_type=AssetType.EC2, resource_id=f"i-{suffix}", account_id=ACCOUNT,
        region="ap-northeast-2", **over)


def _incident(tenant_id, suffix: str, **over) -> models.Incident:
    return models.Incident(
        subject_arn=f"arn:aws:ec2:ap-northeast-2:{ACCOUNT}:instance/i-{suffix}",
        category=IncidentCategory.FINOPS, tenant_id=tenant_id, **over)


def test_list_is_in_creation_order_with_counts_of_live_assets_and_open_incidents(db):
    second = _create(db, "111122223333", name="둘째", now=LATER)
    first = _create(db, name="첫째")
    db.add_all([
        _asset(first.tenant_id, "a1"),
        _asset(first.tenant_id, "a2"),
        _asset(first.tenant_id, "gone", absent_since=NOW),  # 소멸 — 세지 않는다
        _asset(second.tenant_id, "b1"),
        _asset(None, "mvp"),  # MVP 단일 계정 기록 — 어느 테넌트에도 세지 않는다
        _incident(first.tenant_id, "a1"),
        _incident(first.tenant_id, "a2", status=IncidentStatus.FAILED),  # 열린 상태다
        _incident(first.tenant_id, "done", status=IncidentStatus.RESOLVED),  # 종료 — 세지 않는다
        _incident(None, "mvp"),
    ])
    db.flush()

    rows = tenants_repo.list_tenants(db)

    assert [(r.tenant.name, r.asset_count, r.open_incident_count) for r in rows] == [
        ("첫째", 2, 2), ("둘째", 1, 0)]
    detail = tenants_repo.get_tenant(db, first.tenant_id)
    assert (detail.asset_count, detail.open_incident_count) == (2, 2)


# ----- 수정 --------------------------------------------------------------------

def test_update_changes_only_sent_fields(db):
    tenant = _create(db, collect_vpc_ids=["vpc-0a"])
    changes = TenantUpdateRequest(name="학원 본점", auto_action_enabled=False)
    assert tenants_repo.update_tenant(db, tenant.tenant_id, changes, now=LATER)
    row = _get(db, tenant.tenant_id)
    assert (row.name, row.auto_action_enabled) == ("학원 본점", False)
    assert (row.regions, row.collect_vpc_ids) == (["ap-northeast-2"], ["vpc-0a"])  # 안 보낸 칸
    assert row.updated_at == LATER and row.created_at == NOW


def test_update_sets_and_clears_subscription_as_one(db):
    tenant = _create(db)
    sub = TenantSubscription(plan="pilot", period_start=date(2026, 10, 1), period_end=date(2026, 12, 31))
    assert tenants_repo.update_tenant(db, tenant.tenant_id, TenantUpdateRequest(subscription=sub), now=NOW)
    row = _get(db, tenant.tenant_id)
    assert (row.plan, row.period_start, row.period_end) == ("pilot", date(2026, 10, 1), date(2026, 12, 31))

    assert tenants_repo.update_tenant(db, tenant.tenant_id, TenantUpdateRequest(subscription=None), now=NOW)
    row = _get(db, tenant.tenant_id)
    assert (row.plan, row.period_start, row.period_end) == (None, None, None)


def test_update_works_in_every_status_but_not_for_unknown_tenant(db):
    suspended = _in_status(db, TenantStatus.SUSPENDED)
    assert tenants_repo.update_tenant(
        db, suspended.tenant_id, TenantUpdateRequest(regions=["us-east-1"]), now=LATER)
    assert _get(db, suspended.tenant_id).status is TenantStatus.SUSPENDED
    assert not tenants_repo.update_tenant(
        db, "00000000-0000-0000-0000-000000000000", TenantUpdateRequest(name="x"), now=LATER)


# ----- ① 받는 상태 = 계약 상수 ---------------------------------------------------

def _verify(db, tid):
    return tenants_repo.record_verification(db, tid, result=OK, reason_code=None, now=LATER)


def _suspend(db, tid):
    return tenants_repo.suspend_tenant(db, tid, reason="해지", now=LATER)


def _resume(db, tid):
    return tenants_repo.resume_tenant(db, tid, result=OK, reason_code=None, now=LATER)


TRANSITIONS = [
    pytest.param(_verify, VERIFIABLE_STATUSES, id="verify"),
    pytest.param(_suspend, SUSPENDABLE_STATUSES, id="suspend"),
    pytest.param(_resume, RESUMABLE_STATUSES, id="resume"),
    pytest.param(tenants_repo.delete_tenant, DELETABLE_STATUSES, id="delete"),
]


@pytest.mark.parametrize("status", list(TenantStatus), ids=lambda s: s.value)
@pytest.mark.parametrize("transition,accepted", TRANSITIONS)
def test_transition_is_accepted_exactly_in_contract_statuses(db, transition, accepted, status):
    tenant = _in_status(db, status)
    before = (tenant.status, tenant.last_verified_at, tenant.updated_at)

    changed = transition(db, tenant.tenant_id)

    assert changed is (status in accepted)
    if not changed:
        # 거절이면 아무것도 바뀌지 않는다 — 확인 결과도 덧씌우지 않는다
        row = _get(db, tenant.tenant_id)
        assert (row.status, row.last_verified_at, row.updated_at) == before


# ----- 연결 확인 기록 -------------------------------------------------------------

def test_first_ok_verification_activates(db):
    tenant = _create(db)
    assert _verify(db, tenant.tenant_id)
    row = _get(db, tenant.tenant_id)
    assert row.status is TenantStatus.ACTIVE
    assert (row.last_verification_result, row.last_verified_at, row.last_verification_reason_code) == (
        OK, LATER, None)


def test_failed_verification_is_recorded_without_changing_status(db):
    pending = _create(db)
    active = _in_status(db, TenantStatus.ACTIVE, "111122223333")
    for tenant in (pending, active):
        assert tenants_repo.record_verification(
            db, tenant.tenant_id, result=DENIED, reason_code="AccessDenied", now=LATER)
    assert _get(db, pending.tenant_id).status is TenantStatus.PENDING_CONNECTION
    # ACTIVE 의 확인 실패로 자동 정지하지 않는다 — 정지는 사람의 판단이다
    row = _get(db, active.tenant_id)
    assert row.status is TenantStatus.ACTIVE
    assert (row.last_verification_result, row.last_verification_reason_code) == (DENIED, "AccessDenied")


def test_created_object_sees_later_transitions_without_rereading(db):
    # INSERT 에 값을 주지 않은 칸도 세션 동기화에 걸려야 한다 — 호출부가 만든 객체를 들고 있다가
    # 전이 뒤 그대로 응답을 만들면 낡은 None 이 나간다
    tenant = _create(db)
    assert _verify(db, tenant.tenant_id)
    assert (tenant.status, tenant.last_verified_at) == (TenantStatus.ACTIVE, LATER)


def test_ok_verification_never_lifts_a_suspension_even_if_more_statuses_are_accepted(db, monkeypatch):
    # 정지를 푸는 길은 resume 하나다 — 받는 상태 상수가 넓어져도 확인 OK 는 PENDING 만 올린다
    monkeypatch.setattr(tenants_repo, "VERIFIABLE_STATUSES", frozenset(TenantStatus))
    tenant = _in_status(db, TenantStatus.SUSPENDED)
    assert _verify(db, tenant.tenant_id)
    assert _get(db, tenant.tenant_id).status is TenantStatus.SUSPENDED


def test_ok_without_reason_and_failure_with_reason_are_enforced_by_the_database(db):
    tenant = _create(db)
    with pytest.raises(IntegrityError), db.begin_nested():
        tenants_repo.record_verification(db, tenant.tenant_id, result=DENIED, reason_code=None, now=LATER)


# ----- 정지 · 재개 ----------------------------------------------------------------

def test_suspend_keeps_the_last_verification(db):
    tenant = _in_status(db, TenantStatus.ACTIVE)
    assert _suspend(db, tenant.tenant_id)
    row = _get(db, tenant.tenant_id)
    assert (row.status, row.suspended_at, row.suspended_reason) == (TenantStatus.SUSPENDED, LATER, "해지")
    assert (row.last_verification_result, row.last_verified_at) == (OK, NOW)


def test_resume_with_ok_clears_suspension(db):
    tenant = _in_status(db, TenantStatus.SUSPENDED)
    assert _resume(db, tenant.tenant_id)
    row = _get(db, tenant.tenant_id)
    assert (row.status, row.suspended_at, row.suspended_reason) == (TenantStatus.ACTIVE, None, None)
    assert row.last_verified_at == LATER


def test_resume_with_failure_stays_suspended_and_records_the_result(db):
    tenant = _in_status(db, TenantStatus.SUSPENDED)
    # 정지 사이에 고객이 역할을 지웠다
    assert tenants_repo.resume_tenant(
        db, tenant.tenant_id, result=DENIED, reason_code="AccessDenied", now=LATER)
    row = _get(db, tenant.tenant_id)
    assert (row.status, row.suspended_reason) == (TenantStatus.SUSPENDED, "계약 만료")
    assert (row.last_verification_result, row.last_verified_at) == (DENIED, LATER)


# ----- 삭제 --------------------------------------------------------------------

def test_delete_removes_a_pending_tenant(db):
    tenant = _create(db)
    tenant_id = tenant.tenant_id
    assert tenants_repo.delete_tenant(db, tenant_id)
    assert db.scalar(select(models.Tenant).where(models.Tenant.tenant_id == tenant_id)) is None


def test_delete_with_a_member_is_refused_by_the_foreign_key(db):
    tenant = _create(db)
    db.add(models.AppUser(issuer="https://cognito-idp.ap-northeast-2.amazonaws.com/pool",
                          cognito_sub="sub-1", email="a@example.com", role=UserRole.VIEWER,
                          account_type=AccountType.TENANT, tenant_id=tenant.tenant_id))
    db.flush()
    with pytest.raises(IntegrityError), db.begin_nested():
        tenants_repo.delete_tenant(db, tenant.tenant_id)
    assert tenants_repo.get_tenant(db, tenant.tenant_id) is not None


# ----- ② 경쟁 · ④ commit 하지 않음 ------------------------------------------------

def test_concurrent_suspends_change_the_row_once(pg_engine):
    with Session(pg_engine) as setup:
        tenant = _in_status(setup, TenantStatus.ACTIVE, "999988887777")
        tenant_id = tenant.tenant_id
        setup.commit()
    try:
        first, second = Session(pg_engine), Session(pg_engine)
        try:
            # 첫 세션이 행을 잠근 채 커밋 전이다 — 둘째 세션의 UPDATE 는 그 커밋을 기다린 뒤
            # 바뀐 행에 조건(SUSPENDABLE_STATUSES)을 다시 걸어 0행이 된다
            assert tenants_repo.suspend_tenant(first, tenant_id, reason="첫째", now=NOW)
            outcome: dict = {}
            racer = threading.Thread(target=lambda: outcome.setdefault(
                "second", tenants_repo.suspend_tenant(second, tenant_id, reason="둘째", now=LATER)))
            racer.start()
            racer.join(timeout=0.5)
            assert racer.is_alive(), "둘째 UPDATE 가 행 잠금을 기다려야 한다"
            first.commit()
            racer.join(timeout=10)
            second.commit()
            assert outcome == {"second": False}
        finally:
            first.close()
            second.close()
        with Session(pg_engine) as check:
            row = _get(check, tenant_id)
            assert (row.suspended_reason, row.suspended_at) == ("첫째", NOW)
    finally:
        with Session(pg_engine) as cleanup:
            cleanup.delete(cleanup.get(models.Tenant, tenant_id))
            cleanup.commit()


def test_reread_shows_a_change_committed_by_another_admin(pg_engine):
    # 라우터의 거절 경로: 확인하는 사이 다른 관리자가 정지했다 → 전이가 0행 → 다시 읽어 지금 상태로
    # 답한다. 이미 읽어 둔 객체의 낡은 ACTIVE 를 돌려주면 409 응답이 거짓 상태를 싣는다
    with Session(pg_engine) as setup:
        tenant_id = _in_status(setup, TenantStatus.ACTIVE, "444455556666").tenant_id
        setup.commit()
    try:
        with Session(pg_engine) as router:
            held = tenants_repo.get_tenant(router, tenant_id).tenant
            assert held.status is TenantStatus.ACTIVE
            with Session(pg_engine) as other:
                assert _suspend(other, tenant_id)
                other.commit()
            assert not tenants_repo.record_verification(
                router, tenant_id, result=DENIED, reason_code="AccessDenied", now=LATER)
            # 거절된 전이는 들고 있던 객체에도 흔적을 남기지 않는다
            assert (held.last_verified_at, held.last_verification_result) == (NOW, OK)
            reread = tenants_repo.get_tenant(router, tenant_id).tenant
            assert (reread.status, reread.last_verified_at) == (TenantStatus.SUSPENDED, NOW)
            router.rollback()
    finally:
        with Session(pg_engine) as cleanup:
            cleanup.delete(cleanup.get(models.Tenant, tenant_id))
            cleanup.commit()


def test_repository_never_commits(pg_engine):
    with Session(pg_engine) as writer:
        tenant = _create(writer, "555566667777")
        tenant_id = tenant.tenant_id
        with Session(pg_engine) as observer:
            assert tenants_repo.get_tenant(observer, tenant_id) is None
        writer.rollback()
    with Session(pg_engine) as observer:
        assert tenants_repo.get_tenant(observer, tenant_id) is None
