"""/api/v1/me/subscription — 고객 자기 구독 HTTP (Post-MVP T-16 · #528). 실제 PostgreSQL + 실세션.

지키는 것.
  ① 고객사(TENANT) 계정 전용 — 플랫폼 계정은 ADMIN 이어도 403. 조회는 모든 역할, 쓰기는 ADMIN 만(본문 검증 전 403).
  ② 대상은 세션의 소속뿐 — 본문에 테넌트 · 날짜 · 금액 · 구매자 · 결과를 실을 수 없다(422 · 쓰기 없음).
  ③ HTTP 로 구매(202 · PENDING) → 활성화 → 갱신 끄기 → 만료(앱 정책 Free)까지 이어진다. 처리 주기는 서버 함수다.
  ④ 정지 고객사는 조회 · 갱신 끄기를 하고, 구매 · 중단은 409. 연결 전 · 새 구매 닫힘도 409.
  ⑤ 쓰기는 잠금 뒤 세션을 다시 판정한다 — 그 사이 회수 · 강등되면 401 · 403 이고 쓰기가 없다.
Origin · 미인증 전수는 test_api_access 가 본다.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta

import pytest
import subscription_workflow
from auth import service as auth_service
from auth.http import get_clock
from config import SubscriptionSettings, get_subscription_settings
from db import models
from fastapi.routing import iter_route_contexts
from schemas.api.auth import UserRole
from schemas.api.tenants import TenantStatus
from sqlalchemy import select

BASE = "/api/v1/me/subscription"
NOW = datetime(2026, 10, 31, 3, 0, tzinfo=UTC)  # KST 10-31 12:00
ACTIVATED = NOW + timedelta(minutes=1)
TERM_END = datetime(2026, 11, 29, 15, 0, tzinfo=UTC)  # KST 11-30 00:00
WRITES = [("POST", "/purchase", {"plan": "Business"}), ("POST", "/cancel", None), ("POST", "/stop", {"reason": "중단"})]
RESPONSE_KEYS = {
    "tenant_name", "service_status", "subscription", "source", "entitlement_ends_at", "agreement",
    "next_renewal_date", "expiry_date", "mock_marketplace_enabled", "can_manage", "available_actions",
}
FREE = {"plan": "Free", "period_start": None, "period_end": None, "cancel_at_period_end": False,
        "billing_anchor_day": None, "amount_minor": None, "currency": None}


def _settings(enabled=True) -> SubscriptionSettings:
    return SubscriptionSettings(_env_file=None, SUBSCRIPTION_MOCK_MARKETPLACE_ENABLED=enabled)


@pytest.fixture()
def clock(client_pg, login_as):
    """요청 · 세션 판정 · 처리 주기를 같은 시계로 맞춘다. 새 구매는 열어 둔다(settings 로 바꾼다).

    login(role, **kwargs) 는 그 시각의 세션으로 로그인하고, travel(now) 는 시계를 옮긴 뒤 같은 계정 종류 ·
    역할 · 소속으로 그 시각에 다시 로그인한다(세션 유휴 · 최대 수명이 시계를 따른다).
    """
    state = {"now": NOW, "settings": _settings(), "who": None}
    client_pg.app.dependency_overrides[get_clock] = lambda: (lambda: state["now"])
    client_pg.app.dependency_overrides[get_subscription_settings] = lambda: state["settings"]

    def login(role, **kwargs):
        state["who"] = (role, kwargs)
        return login_as(role, now=state["now"], **kwargs)

    def travel(now):
        state["now"] = now
        if state["who"] is not None:
            role, kwargs = state["who"]
            login_as(role, now=now, **kwargs)

    state["login"], state["travel"] = login, travel
    return state


def _cycle(db, clock, now):
    clock["travel"](now)
    return subscription_workflow.process_subscription_cycle(
        db, settings=clock["settings"], sweep=subscription_workflow.EventSweep(), now=now)


def _agreements(db, tenant_id):
    return list(db.scalars(select(models.MockMarketplaceAgreement)
                           .where(models.MockMarketplaceAgreement.tenant_id == tenant_id)))


def _code(response):
    return response.json()["error"]["code"]


# ----- ① 계정 종류 · 역할 ------------------------------------------------------------

def test_routes_are_exactly_the_contract(client_pg):
    registered = {
        (method, route.path)
        for route in iter_route_contexts(client_pg.app.routes)
        if route.path.startswith(BASE)
        for method in route.methods or ()
    }
    assert registered == {("GET", BASE)} | {(method, BASE + path) for method, path, _ in WRITES}


def test_platform_accounts_have_no_own_subscription(client_pg, db, clock):
    clock["login"](UserRole.ADMIN)  # PLATFORM ADMIN
    for method, path, body in [("GET", "", None), *WRITES]:
        response = client_pg.request(method, BASE + path, json=body)
        assert (response.status_code, _code(response)) == (403, "AUTH_FORBIDDEN"), path
    assert db.scalars(select(models.MockMarketplaceAgreement)).all() == []


@pytest.mark.parametrize("role", [UserRole.OPERATOR, UserRole.VIEWER])
def test_non_admin_tenant_users_read_but_cannot_write(client_pg, db, login_as, make_tenant, clock, role):
    tenant_id = make_tenant()
    clock["login"](role, tenant_id=tenant_id)

    shown = client_pg.get(BASE)
    assert shown.status_code == 200
    assert (shown.json()["can_manage"], shown.json()["available_actions"]) == (False, [])
    for method, path, body in WRITES:
        # 역할 거절은 본문 검증보다 먼저다 — 깨진 본문도 403
        for payload in (body, {"unexpected": True}):
            response = client_pg.request(method, BASE + path, json=payload)
            assert (response.status_code, _code(response)) == (403, "AUTH_FORBIDDEN"), path
    assert _agreements(db, tenant_id) == []
    assert client_pg.get(BASE).json()["service_status"] == "ACTIVE"


# ----- ② 대상 · 본문 ----------------------------------------------------------------

@pytest.mark.parametrize("body", [
    {"plan": "Business", "tenant_id": "00000000-0000-0000-0000-000000000000"},
    {"plan": "Business", "term_start": "2026-10-01T00:00:00Z"},
    {"plan": "Business", "amount_minor": 0},
    {"plan": "Business", "buyer_ref": "mock-buyer-0"},
    {"plan": "Business", "outcome": "SUCCEED"},
    {"plan": "Free"},
    {},
])
def test_purchase_body_carries_only_the_plan(client_pg, db, login_as, make_tenant, clock, body):
    tenant_id = make_tenant()
    clock["login"](UserRole.ADMIN, tenant_id=tenant_id)
    response = client_pg.post(f"{BASE}/purchase", json=body)
    assert (response.status_code, _code(response)) == (422, "REQUEST_VALIDATION_FAILED")
    assert _agreements(db, tenant_id) == []


def test_target_tenant_comes_only_from_the_session(client_pg, db, login_as, make_tenant, clock):
    mine, other = make_tenant(), make_tenant()
    clock["login"](UserRole.ADMIN, tenant_id=mine)
    assert client_pg.post(f"{BASE}/purchase", json={"plan": "Enterprise"}).status_code == 202
    assert [a.plan for a in _agreements(db, mine)] == ["Enterprise"]
    assert _agreements(db, other) == []


# ----- ③ HTTP 생애주기 -----------------------------------------------------------------

def test_purchase_activation_renewal_disable_and_expiry_over_http(client_pg, db, login_as, make_tenant, clock):
    tenant_id = make_tenant()
    clock["login"](UserRole.ADMIN, tenant_id=tenant_id)

    start = client_pg.get(BASE).json()
    assert set(start) == RESPONSE_KEYS
    assert (start["subscription"], start["source"], start["agreement"], start["available_actions"]) == (
        FREE, "APP_FREE", None, ["PURCHASE", "STOP"])

    purchased = client_pg.post(f"{BASE}/purchase", json={"plan": "Business"})
    assert purchased.status_code == 202
    body = purchased.json()
    assert (body["subscription"], body["source"], body["agreement"]["status"], body["available_actions"]) == (
        FREE, "APP_FREE", "PENDING", ["STOP"])
    assert body["agreement"]["source"] == "MOCK_MARKETPLACE"
    again = client_pg.post(f"{BASE}/purchase", json={"plan": "Business"})
    assert (again.status_code, _code(again)) == (409, "TENANT_STATE_CONFLICT")

    _cycle(db, clock, ACTIVATED)
    active = client_pg.get(BASE).json()
    assert (active["subscription"]["plan"], active["subscription"]["period_end"], active["source"]) == (
        "Business", "2026-11-29", "MOCK_MARKETPLACE")
    assert (active["entitlement_ends_at"], active["next_renewal_date"], active["expiry_date"]) == (
        "2026-11-29T15:00:00Z", "2026-11-30", None)
    assert (active["agreement"]["status"], active["agreement"]["term_end"]) == ("ACTIVE", "2026-11-29T15:00:00Z")
    assert active["available_actions"] == ["CANCEL_RENEWAL", "STOP"]

    clock["travel"](NOW + timedelta(days=5))
    cancelled = client_pg.post(f"{BASE}/cancel")
    assert cancelled.status_code == 200
    assert (cancelled.json()["subscription"]["plan"], cancelled.json()["next_renewal_date"],
            cancelled.json()["expiry_date"], cancelled.json()["agreement"]["auto_renew"]) == (
        "Business", None, "2026-11-30", False)
    assert client_pg.get(BASE).json() == cancelled.json()  # 쓰기 뒤 가장 가까운 조회가 같은 값이다
    repeat = client_pg.post(f"{BASE}/cancel")
    assert (repeat.status_code, _code(repeat)) == (409, "TENANT_STATE_CONFLICT")

    _cycle(db, clock, TERM_END)
    expired = client_pg.get(BASE).json()
    assert (expired["subscription"], expired["source"], expired["entitlement_ends_at"]) == (FREE, "APP_FREE", None)
    assert (expired["agreement"]["status"], expired["agreement"]["ended_at"]) == ("EXPIRED", "2026-11-29T15:00:00Z")
    # 만료 = 서비스 정지(SSOT 2026-11-06) — 정지 중이라 구매 · 중단 동작이 없다
    assert (expired["service_status"], expired["available_actions"]) == ("SUSPENDED", [])


# ----- ④ 정지 · 연결 전 · 닫힘 ----------------------------------------------------------

def test_suspended_customer_reads_and_disables_renewal_but_cannot_buy_or_stop(
    client_pg, db, login_as, make_tenant, clock
):
    tenant_id = make_tenant()
    clock["login"](UserRole.ADMIN, tenant_id=tenant_id)
    assert client_pg.post(f"{BASE}/purchase", json={"plan": "Business"}).status_code == 202
    _cycle(db, clock, ACTIVATED)

    stopped = client_pg.post(f"{BASE}/stop", json={"reason": "운영 중단"})
    assert stopped.status_code == 200
    assert (stopped.json()["service_status"], stopped.json()["source"], stopped.json()["next_renewal_date"]) == (
        "SUSPENDED", "MOCK_MARKETPLACE", "2026-11-30")

    assert client_pg.get(BASE).status_code == 200
    for path, body in (("/purchase", {"plan": "Enterprise"}), ("/stop", {"reason": "다시"})):
        response = client_pg.post(BASE + path, json=body)
        assert (response.status_code, _code(response)) == (409, "TENANT_STATE_CONFLICT"), path
    cancelled = client_pg.post(f"{BASE}/cancel")
    assert (cancelled.status_code, cancelled.json()["expiry_date"]) == (200, "2026-11-30")


@pytest.mark.parametrize("status,enabled", [
    (TenantStatus.PENDING_CONNECTION, True),
    (TenantStatus.ACTIVE, False),
], ids=["not-connected", "new-purchases-closed"])
def test_purchase_fails_closed(client_pg, db, login_as, make_tenant, clock, status, enabled):
    clock["settings"] = _settings(enabled)
    tenant_id = make_tenant(status)
    clock["login"](UserRole.ADMIN, tenant_id=tenant_id)
    response = client_pg.post(f"{BASE}/purchase", json={"plan": "Business"})
    assert (response.status_code, _code(response)) == (409, "TENANT_STATE_CONFLICT")
    assert _agreements(db, tenant_id) == []
    shown = client_pg.get(BASE).json()
    assert (shown["mock_marketplace_enabled"], "PURCHASE" in shown["available_actions"]) == (enabled, False)


# ----- ⑤ 잠금 뒤 재판정 ----------------------------------------------------------------

@pytest.mark.parametrize("change,expected", [
    pytest.param("revoked", (401, "AUTH_REQUIRED"), id="session-revoked"),
    pytest.param("demoted", (403, "AUTH_FORBIDDEN"), id="demoted-to-viewer"),
])
def test_write_reauthorizes_after_taking_the_lock(client_pg, db, login_as, make_tenant, clock, monkeypatch,
                                                  change, expected):
    tenant_id = make_tenant()
    clock["login"](UserRole.ADMIN, tenant_id=tenant_id)
    real = auth_service.resolve_current_user
    calls = []

    def changing(*args, **kwargs):
        calls.append(1)
        user = real(*args, **kwargs)
        if len(calls) == 1:  # 요청 관문은 통과하고, 잠금 뒤 재판정에서 바뀐 상태를 본다
            return user
        if change == "revoked":
            raise auth_service.AuthRequiredError()
        return dataclasses.replace(user, role=UserRole.VIEWER)

    monkeypatch.setattr(auth_service, "resolve_current_user", changing)
    response = client_pg.post(f"{BASE}/purchase", json={"plan": "Business"})

    assert (response.status_code, _code(response)) == expected
    assert len(calls) == 2
    assert _agreements(db, tenant_id) == []
