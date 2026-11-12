"""도입 신청의 플랫폼 활동 로그(#589 B1) — 실제 PostgreSQL 행으로 본다.

지키는 것: 새로 만든 신청만 SIGNUP_REQUEST_CREATE(ANONYMOUS) · 승인 / 반려만 SIGNUP_REQUEST_DECIDE(실제 인가 사용자) ·
재시도 · 409 · 검증 실패 · IN_REVIEW 는 행을 만들지 않는다 · 이벤트를 못 쓰면 업무 쓰기도 되돌아간다.
"""

from __future__ import annotations

import uuid

import pytest
import signup_requests_service
from config import TenantSettings, get_tenant_settings
from db import models
from exceptions import ApiError
from schemas.api.auth import AccountType, PlatformRole
from schemas.api.signup_requests import SignupRequestCreate
from schemas.api.support import SignupRequestStatus
from schemas.platform_events import (
    PlatformActorKind,
    PlatformEventAction,
    PlatformEventOutcome,
    PlatformEventTargetType,
)
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError

BASE = "/api/v1/signup-requests"
APPROVE = {"aws_account_id": "123456789012", "regions": ["ap-northeast-2"]}
BODY = {
    "company_name": "Acme",
    "contact_name": "Kim",
    "email": "kim-secret@example.com",
    "phone": "010-1234-5678",
    "note": "private-note-text",
}
CREATE = PlatformEventAction.SIGNUP_REQUEST_CREATE
DECIDE = PlatformEventAction.SIGNUP_REQUEST_DECIDE


@pytest.fixture()
def post(client_pg_anon, allowed_origin):
    def _post(key=None):
        return client_pg_anon.post(
            BASE,
            json=BODY,
            headers={"Origin": allowed_origin, "Idempotency-Key": key or str(uuid.uuid4())},
        )

    return _post


@pytest.fixture()
def onboarding(client_pg):
    client_pg.app.dependency_overrides[get_tenant_settings] = lambda: TenantSettings(
        TENANT_ONBOARDING_TEMPLATE_URL="https://templates.example.com/vigilantis-access.yaml",
        PLATFORM_PRINCIPAL_ARN="arn:aws:iam::999988887777:role/vigilantis-platform",
    )


def events(db, action, target_id=None):
    db.expire_all()
    query = select(models.PlatformEvent).where(models.PlatformEvent.action == action.value)
    if target_id is not None:
        query = query.where(models.PlatformEvent.target_id == target_id)
    return db.scalars(query).all()


def event_count(db, action) -> int:
    return len(events(db, action))


def seed(post) -> str:
    return post().json()["request_id"]


def put(client, rid, status, reason=None):
    body = {"status": status, **({"reason": reason} if reason else {})}
    return client.put(f"{BASE}/{rid}/status", json=body)


def approve(client, rid, body=APPROVE):
    return client.post(f"{BASE}/{rid}/approve", json=body)


def failing_after_write(monkeypatch, action):
    """이벤트 행을 쓴 직후 DB 오류 — 업무 쓰기와 이벤트가 함께 되돌아가는지 본다."""
    original = signup_requests_service.record_platform_event

    def fail(db, act, *args, **kwargs):
        original(db, act, *args, **kwargs)
        if act is action:
            raise OperationalError("event write", {}, Exception("not exposed"))

    monkeypatch.setattr(signup_requests_service, "record_platform_event", fail)


def test_create_records_one_anonymous_event_for_the_new_request(post, db):
    resp = post()
    rid = resp.json()["request_id"]
    [event] = events(db, CREATE, rid)
    assert (event.category.value, event.outcome) == ("SUPPORT", PlatformEventOutcome.SUCCEEDED)
    assert event.actor_kind is PlatformActorKind.ANONYMOUS
    assert (event.actor_user_id, event.actor_account_type, event.actor_platform_role) == (None, None, None)
    assert (event.session_id, event.tenant_id) == (None, None)
    assert event.target_type is PlatformEventTargetType.SIGNUP_REQUEST
    assert event.details == {}
    dump = f"{event.details}{event.target_id}{event.user_agent}"
    assert not any(text in dump for text in (BODY["email"], BODY["note"], BODY["contact_name"]))


def test_replay_of_the_same_key_adds_no_event(post, db):
    key = str(uuid.uuid4())
    rid = post(key).json()["request_id"]
    assert post(key).json()["request_id"] == rid
    assert len(events(db, CREATE, rid)) == 1


def test_conflicting_key_adds_no_event(post, db, client_pg_anon, allowed_origin):
    key = str(uuid.uuid4())
    rid = post(key).json()["request_id"]
    resp = client_pg_anon.post(
        BASE,
        json={**BODY, "company_name": "Other"},
        headers={"Origin": allowed_origin, "Idempotency-Key": key},
    )
    assert resp.status_code == 409
    assert len(events(db, CREATE, rid)) == 1 and event_count(db, CREATE) == 1


def test_event_failure_rolls_back_signup_notification_and_event(db, monkeypatch):
    failing_after_write(monkeypatch, CREATE)
    key = uuid.uuid4()
    before = db.scalar(select(func.count()).select_from(models.OperatorNotification))
    with pytest.raises(ApiError):
        signup_requests_service.create(db, key, SignupRequestCreate.model_validate(BODY))
    assert db.scalar(
        select(func.count()).select_from(models.SignupRequest).where(
            models.SignupRequest.idempotency_key == str(key)
        )
    ) == 0
    assert db.scalar(select(func.count()).select_from(models.OperatorNotification)) == before
    assert event_count(db, CREATE) == 0


def test_approve_records_decision_with_created_tenant_and_real_actor(
    client_pg, post, db, platform_session, onboarding
):
    rid = seed(post)
    resp = approve(client_pg, rid)
    assert resp.status_code == 200
    tenant_id = resp.json()["tenant"]["tenant"]["tenant_id"]
    [event] = events(db, DECIDE, rid)
    assert event.outcome is PlatformEventOutcome.SUCCEEDED
    assert event.actor_kind is PlatformActorKind.USER
    assert event.actor_user_id == platform_session.user_id
    assert event.actor_account_type is AccountType.PLATFORM
    assert event.actor_platform_role is PlatformRole.CENTRAL_ADMIN
    assert event.session_id == platform_session.session_id
    assert event.tenant_id == tenant_id
    assert event.target_type is PlatformEventTargetType.SIGNUP_REQUEST
    assert event.details == {"decision": "APPROVED"}
    assert db.get(models.SignupRequestDecision, rid).outcome is SignupRequestStatus.APPROVED


@pytest.mark.parametrize("state", ["NEW", "IN_REVIEW"])
def test_reject_records_decision_without_tenant_or_reason_text(
    client_pg, post, db, platform_session, state
):
    rid = seed(post)
    if state == "IN_REVIEW":
        assert put(client_pg, rid, "IN_REVIEW").status_code == 200
        assert events(db, DECIDE, rid) == []  # 검토 중으로의 이동은 판정이 아니다
    assert put(client_pg, rid, "REJECTED", "secret-reject-reason").status_code == 200
    [event] = events(db, DECIDE, rid)
    assert event.outcome is PlatformEventOutcome.SUCCEEDED
    assert (event.actor_kind, event.actor_user_id) == (PlatformActorKind.USER, platform_session.user_id)
    assert (event.actor_account_type, event.actor_platform_role) == (
        AccountType.PLATFORM, PlatformRole.CENTRAL_ADMIN,
    )
    assert event.session_id == platform_session.session_id
    assert event.tenant_id is None
    assert event.target_type is PlatformEventTargetType.SIGNUP_REQUEST
    assert event.details == {"decision": "REJECTED"}
    assert db.get(models.SignupRequestDecision, rid).outcome is SignupRequestStatus.REJECTED


def test_refused_or_repeated_decisions_add_no_event_and_keep_the_first(
    client_pg, post, db, onboarding
):
    approved = seed(post)
    assert approve(client_pg, approved).status_code == 200
    rejected = seed(post)
    assert put(client_pg, rejected, "REJECTED", "사유").status_code == 200
    first = {(e.target_id, e.event_id) for e in events(db, DECIDE)}
    decision = db.get(models.SignupRequestDecision, approved)
    stored = (decision.outcome, decision.actor_user_id, decision.decided_at)

    assert approve(client_pg, approved).status_code == 409  # 이미 승인
    assert approve(client_pg, rejected).status_code == 409  # 이미 반려
    assert put(client_pg, approved, "REJECTED", "번복").status_code == 409
    assert put(client_pg, rejected, "REJECTED", "다시").status_code == 409
    assert put(client_pg, rejected, "APPROVED").status_code == 409  # PUT 은 승인하지 않는다
    assert approve(client_pg, approved, {"regions": []}).status_code == 422
    assert approve(client_pg, str(uuid.uuid4())).status_code == 404

    assert {(e.target_id, e.event_id) for e in events(db, DECIDE)} == first
    db.expire_all()
    decision = db.get(models.SignupRequestDecision, approved)
    assert (decision.outcome, decision.actor_user_id, decision.decided_at) == stored


def test_approval_that_conflicts_on_the_account_adds_no_event(client_pg, post, db, onboarding):
    first, second = seed(post), seed(post)
    assert approve(client_pg, first).status_code == 200
    assert approve(client_pg, second).status_code == 409  # 같은 AWS 계정
    assert [e.target_id for e in events(db, DECIDE)] == [first]
    assert db.get(models.SignupRequestDecision, second) is None


def test_approve_event_failure_rolls_back_tenant_request_decision_and_event(
    client_pg, post, db, onboarding, monkeypatch
):
    prior = seed(post)
    assert put(client_pg, prior, "REJECTED", "이전 결정").status_code == 200
    rid = seed(post)
    tenants = db.scalar(select(func.count()).select_from(models.Tenant))
    failing_after_write(monkeypatch, DECIDE)
    resp = approve(client_pg, rid, {"aws_account_id": "210987654321", "regions": ["us-east-1"]})
    assert resp.status_code == 500
    db.expire_all()
    row = db.get(models.SignupRequest, rid)
    assert (row.status, row.tenant_id) == (SignupRequestStatus.NEW, None)
    assert db.get(models.SignupRequestDecision, rid) is None
    assert db.scalar(select(func.count()).select_from(models.Tenant)) == tenants
    assert events(db, DECIDE, rid) == []
    # 앞선 결정과 그 감사는 그대로다
    assert db.get(models.SignupRequestDecision, prior).outcome is SignupRequestStatus.REJECTED
    assert len(events(db, DECIDE, prior)) == 1


def test_reject_event_failure_rolls_back_status_decision_and_event(
    client_pg, post, db, monkeypatch
):
    rid = seed(post)
    failing_after_write(monkeypatch, DECIDE)
    resp = put(client_pg, rid, "REJECTED", "연락 불가")
    assert resp.status_code == 500
    db.expire_all()
    row = db.get(models.SignupRequest, rid)
    assert (row.status, row.rejected_reason) == (SignupRequestStatus.NEW, None)
    assert db.get(models.SignupRequestDecision, rid) is None
    assert events(db, DECIDE, rid) == []
