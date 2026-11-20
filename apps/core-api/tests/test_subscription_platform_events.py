"""구독 흐름이 서비스를 멈추는 두 자리의 플랫폼 활동 로그(#589 B) — 실제 PostgreSQL 행으로 본다.

지키는 것
  - 고객의 이용 중단(POST /me/subscription/stop)은 TENANT_SUSPEND 한 행을 남긴다. 행위자는 요청한 고객사
    중앙계정(계정 종류 · 세션 포함), 소속과 대상은 그 고객사, 내용은 상태의 전 · 후다. 사유 원문은 들어가지 않는다.
  - 권리 만료 정지(reconcile_tenant_subscription)는 같은 행동을 SYSTEM 으로 남긴다. 요청 밖(처리 주기)이라 요청
    문맥은 비어 있다.
  - 실제로 멈췄을 때만 남는다. 이미 정지(고객의 중단 · 운영자 정지)인 고객사의 만료 · 거절된 중단(409) · 다시 읽은
    투영은 행을 늘리지 않는다.
  - 정지와 같은 트랜잭션이다 — 행을 못 쓰면 정지도 투영도 되돌아가고, 다음 시도에서 그대로 멈추며 한 행이 남는다.
    처리 주기에서는 그 이벤트가 처리되지 않은 채 남아 다음 바퀴가 다시 집는다.
실제 Marketplace 권리 종료와 재개 직후의 만료 정지는 그 흐름의 테스트(test_marketplace · test_tenant_verification_api)가
같은 행을 본다.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
import subscription_workflow as workflow
from db import models
from schemas.api.auth import AccountType, UserRole
from schemas.api.subscriptions import SubscriptionStopRequest
from schemas.api.tenants import TenantStatus
from schemas.platform_events import PlatformActorKind, PlatformEventOutcome, PlatformEventTargetType
from schemas.subscriptions import SubscriptionSource
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from tests.platform_event_faults import failing_platform_event_write
from tests.test_subscription_workflow import buyer  # noqa: F401 — 픽스처는 이름으로 등록된다
from tests.test_subscription_workflow import (
    TERM_END,
    _active,
    _allow,
    _cancel,
    _consume,
    _cycle,
    _requester,
    _settings,
    _stop,
    _tenant,
    kst,
)

BASE = "/api/v1/me/subscription"
STOPPED_AT = kst(2026, 11, 10)
TRANSITION = {"before": "ACTIVE", "after": "SUSPENDED"}


def _rows(db, tenant_id) -> list[models.PlatformEvent]:
    db.expire_all()
    return list(db.scalars(
        select(models.PlatformEvent)
        .where(models.PlatformEvent.target_id == tenant_id)
        .order_by(models.PlatformEvent.occurred_at, models.PlatformEvent.event_id)))


def _total(db) -> int:
    db.expire_all()
    return db.scalar(select(func.count()).select_from(models.PlatformEvent))


def _is_suspension(row, tenant_id) -> None:
    assert (row.action, row.outcome, row.target_type, row.target_id, row.tenant_id, row.details) == (
        "TENANT_SUSPEND", PlatformEventOutcome.SUCCEEDED, PlatformEventTargetType.TENANT, tenant_id, tenant_id,
        TRANSITION)


def _is_system(row) -> None:
    assert row.actor_kind is PlatformActorKind.SYSTEM
    assert (row.actor_user_id, row.actor_account_type, row.actor_platform_role, row.session_id) == (
        None, None, None, None)


def _unprocessed(db) -> int:
    db.expire_all()
    return db.scalar(
        select(func.count())
        .select_from(models.MockMarketplaceEvent)
        .where(models.MockMarketplaceEvent.processed_at.is_(None)))


# ==============================================================================
# 고객의 이용 중단 — TENANT_SUSPEND (USER)
# ==============================================================================


def test_http_stop_records_the_requesting_tenant_admin_once(client_pg, db, login_as, make_tenant):
    tenant_id = make_tenant()
    seed = login_as(UserRole.ADMIN, tenant_id=tenant_id)

    stopped = client_pg.post(f"{BASE}/stop", json={"reason": "사유-원문-운영 중단"})
    before_retry = _total(db)
    again = client_pg.post(f"{BASE}/stop", json={"reason": "다시"})

    assert (stopped.status_code, stopped.json()["service_status"]) == (200, "SUSPENDED")
    assert (again.status_code, again.json()["error"]["code"]) == (409, "TENANT_STATE_CONFLICT")
    assert _total(db) == before_retry  # 거절된 중단은 행을 늘리지 않는다
    [row] = _rows(db, tenant_id)
    _is_suspension(row, tenant_id)
    assert row.actor_kind is PlatformActorKind.USER
    assert (row.actor_user_id, row.actor_account_type, row.actor_platform_role, row.session_id) == (
        seed.user_id, AccountType.TENANT, None, seed.session_id)
    assert row.request_id is not None  # 요청 문맥은 기록 함수가 채운다
    # 사유 원문은 고객사 행에만 있다 — 고칠 수 없는 로그에는 싣지 않는다
    assert _tenant(db, tenant_id).suspended_reason == "사유-원문-운영 중단"
    assert "사유-원문" not in str(row.details)


def test_a_failed_platform_event_write_leaves_the_service_running(db, buyer):  # noqa: F811
    tenant_id = _active(db, buyer())
    before = _total(db)

    with failing_platform_event_write() as failed, pytest.raises(IntegrityError):
        _stop(db, tenant_id, STOPPED_AT)
    db.rollback()

    assert failed.count == 1
    tenant = _tenant(db, tenant_id)
    assert (tenant.status, tenant.suspended_at, tenant.suspended_reason) == (TenantStatus.ACTIVE, None, None)
    assert _total(db) == before

    assert _stop(db, tenant_id, STOPPED_AT).service_status is TenantStatus.SUSPENDED  # 다시 요청하면 그대로 멈춘다
    [row] = _rows(db, tenant_id)
    _is_suspension(row, tenant_id)
    assert row.actor_user_id == _requester(db, tenant_id).user_id


def test_stop_requires_the_actor():
    """행위자를 빠뜨린 호출이 로그 없는 정지를 조용히 만들지 못한다 — 기본값이 없다."""
    with pytest.raises(TypeError, match="event_actor"):
        workflow.stop_service(
            None, "tenant", SubscriptionStopRequest(reason="x"), authorize=_allow, settings=_settings(), now=STOPPED_AT)


# ==============================================================================
# 권리 만료 정지 — TENANT_SUSPEND (SYSTEM)
# ==============================================================================


def test_expiry_in_the_cycle_records_one_system_suspension(db, buyer):  # noqa: F811
    tenant_id = _active(db, buyer())
    _cancel(db, tenant_id, kst(2026, 11, 5))
    assert _rows(db, tenant_id) == []  # 갱신 끄기 · 활성화는 행동 목록에 없다

    _cycle(db, TERM_END - timedelta(microseconds=1))  # 끝 시각 직전 — 아직 권리가 있다
    assert _rows(db, tenant_id) == []
    _cycle(db, TERM_END)
    before_repeat = _total(db)
    # 투영이 이미 APP_FREE 라 다시 읽어도, 다음 주기가 돌아도 정지도 행도 다시 쓰지 않는다
    assert workflow.reconcile_tenant_subscription(db, tenant_id, now=TERM_END + timedelta(hours=1)) is False
    db.commit()
    _cycle(db, TERM_END + timedelta(hours=2))

    assert _tenant(db, tenant_id).suspended_reason == workflow.EXPIRY_SUSPENSION_REASON
    assert _total(db) == before_repeat
    [row] = _rows(db, tenant_id)
    _is_suspension(row, tenant_id)
    _is_system(row)
    assert row.request_id is None  # 요청 밖(처리 주기)이다


def test_expiry_of_an_already_stopped_tenant_adds_no_row(db, buyer):  # noqa: F811
    tenant_id = _active(db, buyer())
    _stop(db, tenant_id, STOPPED_AT)
    _cancel(db, tenant_id, kst(2026, 11, 11))  # 정지 중에도 갱신 끄기를 받는다
    before = _total(db)

    _cycle(db, TERM_END)  # 정지 중에 만료한다 — 투영만 APP_FREE 로 돌고 그 정지 · 사유는 그대로다

    tenant = _tenant(db, tenant_id)
    assert (tenant.subscription_source, tenant.suspended_at, tenant.suspended_reason) == (
        SubscriptionSource.APP_FREE, STOPPED_AT, "운영 중단")
    assert _total(db) == before
    [row] = _rows(db, tenant_id)  # 고객의 중단 한 행뿐이다
    assert row.actor_kind is PlatformActorKind.USER


def test_a_failed_platform_event_write_leaves_the_expiry_for_the_next_sweep(db, buyer):  # noqa: F811
    tenant_id = _active(db, buyer())
    _cancel(db, tenant_id, kst(2026, 11, 5))
    before = _total(db)

    with failing_platform_event_write() as failed:
        report = _cycle(db, TERM_END)  # 공급자의 만료는 저장되고, 그 이벤트의 소비(정지 + 투영 + 행)만 실패한다

    assert (failed.count, report.events.processed, report.events.errored) == (1, 0, 1)
    tenant = _tenant(db, tenant_id)
    # 정지도 투영도 되돌아갔다 — 로그 없는 정지가 남지 않는다. 이벤트는 처리되지 않은 채 남는다
    assert (tenant.status, tenant.suspended_at, tenant.subscription_source) == (
        TenantStatus.ACTIVE, None, SubscriptionSource.MOCK_MARKETPLACE)
    assert (_total(db), _unprocessed(db)) == (before, 1)

    retried = _consume(db, TERM_END + timedelta(minutes=1))  # 다음 바퀴

    assert (retried.processed, retried.errored, _unprocessed(db)) == (1, 0, 0)
    tenant = _tenant(db, tenant_id)
    assert (tenant.status, tenant.subscription_source) == (TenantStatus.SUSPENDED, SubscriptionSource.APP_FREE)
    [row] = _rows(db, tenant_id)
    _is_suspension(row, tenant_id)
    _is_system(row)
