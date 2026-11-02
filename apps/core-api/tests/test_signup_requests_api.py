"""도입 신청 API (#552) — 익명 멱등 접수, 알림과의 원자성, 중앙 관리자 처리."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
import signup_requests_service
from db import models
from exceptions import ApiError
from schemas.api.auth import PlatformRole, UserRole
from schemas.api.errors import ErrorCode
from schemas.api.signup_requests import SignupRequestCreate
from schemas.api.support import NotificationKind, SignupRequestStatus
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError

BASE = "/api/v1/signup-requests"
BODY = {
    "company_name": "Acme",
    "contact_name": "Kim",
    "email": "Kim@Example.com",
    "phone": "010-1234-5678",
    "note": "도입 문의",
}


def new_key() -> str:
    return str(uuid.uuid4())


@pytest.fixture()
def post(client_pg_anon, allowed_origin):
    def _post(body=BODY, key=None, *, origin=allowed_origin, **headers):
        sent = dict(headers)
        if origin is not None:
            sent["Origin"] = origin
        if key is not False:
            sent["Idempotency-Key"] = new_key() if key is None else key
        return client_pg_anon.post(BASE, json=body, headers=sent)

    return _post


def count(db, model) -> int:
    return db.scalar(select(func.count()).select_from(model))


def error_code(resp) -> str:
    return resp.json()["error"]["code"]


def test_create_returns_202_and_stores_signup_with_notification(post, db):
    key = new_key()
    resp = post(key=key)
    assert resp.status_code == 202
    assert set(resp.json()) == {"request_id"}
    row = db.get(models.SignupRequest, resp.json()["request_id"])
    assert (row.status, row.idempotency_key, row.rejected_reason) == (SignupRequestStatus.NEW, key, None)
    assert (row.company_name, row.contact_name, row.email) == ("Acme", "Kim", "Kim@Example.com")
    assert (row.phone, row.note) == (BODY["phone"], BODY["note"])
    note = db.scalars(
        select(models.OperatorNotification).where(models.OperatorNotification.target_id == row.request_id)
    ).one()
    assert (note.kind, note.tenant_id, note.subject, note.summary, note.inquiry_category) == (
        NotificationKind.SIGNUP_REQUEST, None, "Acme", "Kim", None,
    )
    assert note.created_at == row.created_at


def test_replay_returns_same_id_without_new_row_or_notification(post, db):
    key = new_key()
    first = post(key=key)
    row = db.get(models.SignupRequest, first.json()["request_id"])
    created_at = row.created_at
    notifications = count(db, models.OperatorNotification)
    again = post(key=key.upper())  # 정본 문자열로 같은 키다
    assert again.status_code == 202 and again.json() == first.json()
    db.refresh(row)
    assert row.created_at == created_at
    assert count(db, models.OperatorNotification) == notifications
    assert db.scalar(select(func.count()).select_from(models.SignupRequest)
                     .where(models.SignupRequest.idempotency_key == key)) == 1


def test_replay_compares_validated_values_not_raw_json(post):
    key = new_key()
    first = post(key=key)
    padded = {**BODY, "company_name": "  Acme ", "contact_name": "Kim\n"}
    assert post(padded, key=key).json() == first.json()
    # 선택 칸은 비어 있어도 null 과 같다
    key2 = new_key()
    sparse = {**BODY, "phone": "  ", "note": None}
    first2 = post(sparse, key=key2)
    assert post({k: v for k, v in BODY.items() if k not in ("phone", "note")}, key=key2).json() == first2.json()


@pytest.mark.parametrize(
    "change",
    [
        {"company_name": "Other"},
        {"contact_name": "Lee"},
        {"email": "kim@example.com"},  # 이메일은 소문자화로 같다고 보지 않는다
        {"phone": "02-000-0000"},
        {"phone": None},
        {"note": "다른 메모"},
        {"note": None},
    ],
)
def test_same_key_different_payload_is_409_and_changes_nothing(post, db, change):
    key = new_key()
    first = post(key=key)
    notifications = count(db, models.OperatorNotification)
    resp = post({**BODY, **change}, key=key)
    assert resp.status_code == 409 and error_code(resp) == ErrorCode.IDEMPOTENCY_KEY_CONFLICT.value
    row = db.get(models.SignupRequest, first.json()["request_id"])
    assert (row.company_name, row.email, row.phone, row.note) == ("Acme", "Kim@Example.com", BODY["phone"], BODY["note"])
    assert count(db, models.OperatorNotification) == notifications


def test_different_keys_create_separate_requests_even_for_same_email(post, db):
    ids = {post().json()["request_id"] for _ in range(2)}
    assert len(ids) == 2


@pytest.mark.parametrize("key", [False, "", "not-a-uuid", str(uuid.uuid1()), "12345"])
def test_invalid_or_missing_idempotency_key_is_422(post, db, key):
    before = count(db, models.SignupRequest)
    resp = post(key=key)
    assert resp.status_code == 422
    assert count(db, models.SignupRequest) == before


@pytest.mark.parametrize("origin", [None, "https://evil.example"])
def test_origin_is_required_and_checked(post, db, origin):
    before = count(db, models.SignupRequest)
    resp = post({"bad": 1}, origin=origin)  # 본문 검증보다 먼저 거절한다
    assert resp.status_code == 403 and error_code(resp) == ErrorCode.AUTH_ORIGIN_REJECTED.value
    assert count(db, models.SignupRequest) == before


@pytest.mark.parametrize(
    "body",
    [
        {**BODY, "status": "APPROVED"},
        {**BODY, "company_name": "  "},
        {**BODY, "contact_name": ""},
        {**BODY, "email": "not-email"},
        {**BODY, "company_name": "x" * 101},
        {**BODY, "note": "x" * 1001},
        {**BODY, "note": "a\x00b"},
        {k: v for k, v in BODY.items() if k != "email"},
    ],
)
def test_invalid_body_is_422_and_stores_nothing(post, db, body):
    before = count(db, models.SignupRequest)
    resp = post(body)
    assert resp.status_code == 422
    assert "Kim@Example.com" not in resp.text
    assert count(db, models.SignupRequest) == before


def test_surrogate_in_json_is_422(client_pg_anon, allowed_origin):
    raw = b'{"company_name":"A\\ud800","contact_name":"K","email":"k@e.co"}'
    resp = client_pg_anon.post(
        BASE,
        content=raw,
        headers={"Origin": allowed_origin, "Idempotency-Key": new_key(), "Content-Type": "application/json"},
    )
    assert resp.status_code == 422


def create_body(**overrides) -> SignupRequestCreate:
    return SignupRequestCreate.model_validate({**BODY, **overrides})


def test_notification_failure_rolls_back_the_signup(db, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("notification failed")

    monkeypatch.setattr(signup_requests_service.operator_notifications, "add_notification", boom)
    key = uuid.uuid4()
    with pytest.raises(RuntimeError):
        signup_requests_service.create(db, key, create_body())
    assert db.scalar(select(func.count()).select_from(models.SignupRequest)
                     .where(models.SignupRequest.idempotency_key == str(key))) == 0


def test_database_error_is_not_classified_or_revealed(db, monkeypatch):
    def boom(*args, **kwargs):
        raise OperationalError("INSERT secret-statement", {"email": "Kim@Example.com"}, Exception("pg detail"))

    monkeypatch.setattr(signup_requests_service.operator_notifications, "add_notification", boom)
    key = uuid.uuid4()
    with pytest.raises(ApiError) as err:
        signup_requests_service.create(db, key, create_body())
    assert err.value.code is ErrorCode.INTERNAL_ERROR
    assert err.value.__cause__ is None and err.value.__suppress_context__
    assert db.scalar(select(func.count()).select_from(models.SignupRequest)
                     .where(models.SignupRequest.idempotency_key == str(key))) == 0


# --- 중앙 관리자 처리 ----------------------------------------------------------------


def seed(post, **overrides) -> str:
    return post({**BODY, **overrides}).json()["request_id"]


def test_list_is_newest_first_with_full_item(client_pg, post, db):
    first = seed(post, company_name="First")
    second = seed(post, company_name="Second")
    # 같은 초 안의 저장 시각도 최신순이어야 한다. 응답은 초 단위로 직렬화된다.
    created_at = datetime(2026, 10, 30, tzinfo=UTC)
    db.get(models.SignupRequest, first).created_at = created_at
    db.get(models.SignupRequest, second).created_at = created_at + timedelta(microseconds=1)
    db.flush()
    items = client_pg.get(BASE).json()["items"]
    ids = [i["request_id"] for i in items]
    assert ids.index(second) < ids.index(first)
    item = next(i for i in items if i["request_id"] == first)
    assert set(item) == {
        "request_id", "company_name", "contact_name", "email", "phone", "note",
        "status", "rejected_reason", "created_at", "updated_at",
    }
    assert item["status"] == "NEW" and item["rejected_reason"] is None
    assert item["created_at"].endswith("Z")
    stamps = [(db.get(models.SignupRequest, rid).created_at, rid) for rid in ids]
    assert stamps == sorted(stamps, reverse=True)


def put(client, request_id, status, reason=None, **kwargs):
    body = {"status": status}
    if reason is not None:
        body["reason"] = reason
    return client.put(f"{BASE}/{request_id}/status", json=body, **kwargs)


@pytest.mark.parametrize("target", ["IN_REVIEW", "APPROVED"])
def test_new_moves_forward_without_reason(client_pg, post, db, target):
    rid = seed(post)
    resp = put(client_pg, rid, target)
    assert resp.status_code == 200
    assert resp.json()["status"] == target and resp.json()["rejected_reason"] is None
    assert resp.json()["request_id"] == rid
    assert db.get(models.SignupRequest, rid).status.value == target


def test_approval_only_records_decision(client_pg, post, db):
    rid = seed(post)
    tenants, users = count(db, models.Tenant), count(db, models.AppUser)
    assert put(client_pg, rid, "APPROVED").status_code == 200
    assert (count(db, models.Tenant), count(db, models.AppUser)) == (tenants, users)


def test_reject_requires_trimmed_reason_and_stores_it(client_pg, post, db):
    rid = seed(post)
    for bad in (None, "", "   ", "x" * 501):
        assert put(client_pg, rid, "REJECTED", bad).status_code == 422
    assert db.get(models.SignupRequest, rid).status is SignupRequestStatus.NEW
    resp = put(client_pg, rid, "REJECTED", "  연락 불가  ")
    assert resp.status_code == 200 and resp.json()["rejected_reason"] == "연락 불가"


def test_reason_outside_rejection_is_422(client_pg, post):
    rid = seed(post)
    assert put(client_pg, rid, "IN_REVIEW", "x").status_code == 422
    assert put(client_pg, rid, "APPROVED", "x").status_code == 422
    assert client_pg.put(f"{BASE}/{rid}/status", json={"status": "APPROVED", "reason": None}).status_code == 200
    # 응답 칸 이름은 요청에서 추가 필드다
    extra = {"status": "REJECTED", "rejected_reason": "x"}
    assert client_pg.put(f"{BASE}/{rid}/status", json=extra).status_code == 422


def test_in_review_moves_to_decision_then_terminal(client_pg, post):
    rid = seed(post)
    assert put(client_pg, rid, "IN_REVIEW").status_code == 200
    assert put(client_pg, rid, "IN_REVIEW").status_code == 409  # 같은 상태
    assert put(client_pg, rid, "NEW").status_code == 409
    assert put(client_pg, rid, "REJECTED", "사유").status_code == 200
    for target, reason in (("APPROVED", None), ("REJECTED", "다시"), ("IN_REVIEW", None), ("NEW", None)):
        resp = put(client_pg, rid, target, reason)
        assert resp.status_code == 409 and error_code(resp) == ErrorCode.SIGNUP_REQUEST_STATE_CONFLICT.value


def test_new_to_new_and_approved_terminal_are_409(client_pg, post, db):
    rid = seed(post)
    assert put(client_pg, rid, "NEW").status_code == 409
    assert put(client_pg, rid, "APPROVED").status_code == 200
    updated = db.get(models.SignupRequest, rid).updated_at
    assert put(client_pg, rid, "REJECTED", "번복").status_code == 409
    assert db.get(models.SignupRequest, rid).status is SignupRequestStatus.APPROVED
    assert db.get(models.SignupRequest, rid).updated_at == updated


@pytest.mark.parametrize("rid", [str(uuid.uuid4()), "not-a-uuid", "1234"])
def test_missing_or_malformed_id_is_404(client_pg, rid):
    resp = put(client_pg, rid, "APPROVED")
    assert resp.status_code == 404 and error_code(resp) == ErrorCode.SIGNUP_REQUEST_NOT_FOUND.value


def test_uppercase_uuid_finds_the_request(client_pg, post):
    rid = seed(post)
    assert put(client_pg, rid.upper(), "IN_REVIEW").status_code == 200


def test_put_requires_origin(client_pg, post, db):
    rid = seed(post)
    resp = client_pg.put(f"{BASE}/{rid}/status", json={"status": "APPROVED"}, headers={"Origin": "https://evil.example"})
    assert resp.status_code == 403
    assert db.get(models.SignupRequest, rid).status is SignupRequestStatus.NEW


def test_management_requires_central_admin(client_pg, client_pg_anon, post, db, login_as, make_tenant):
    rid = seed(post)
    assert client_pg_anon.get(BASE).status_code == 401
    for seed_user in (
        lambda: login_as(UserRole.ADMIN, platform_role=PlatformRole.TENANT_MANAGER),
        lambda: login_as(UserRole.ADMIN, tenant_id=make_tenant()),
    ):
        seed_user()
        assert client_pg.get(BASE).status_code == 403
        assert put(client_pg, rid, "APPROVED").status_code == 403
        assert put(client_pg, "not-a-uuid", "APPROVED").status_code == 403
    assert db.get(models.SignupRequest, rid).status is SignupRequestStatus.NEW
