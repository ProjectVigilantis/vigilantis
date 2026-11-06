"""플랫폼 활동 로그(platform_events · #589 A) — 기록 계약 · 테이블 · 기록 함수 · 요청 문맥.

지키는 것: 행동마다 정한 결과 · 행위자 · 대상 · details 만 받는다 · 거절 · 실패에만 사유가 있다 ·
정한 칸 밖(비밀번호 · 이메일 등)은 details 에 들어가지 않는다 · 같은 트랜잭션 기록은 변경과 함께 되돌아간다 ·
따로 남긴 기록은 요청이 되돌아가도 남고, 쓰기가 실패해도 예외를 올리지 않는다 · UPDATE · DELETE 는 트리거가,
계약을 건너뛴 쓰기는 DB CHECK 가 막는다 · 요청 문맥(요청 식별자 · 접속지 · 브라우저 정보)은 미들웨어가 채운다.
"""

from __future__ import annotations

import importlib.util
import logging
import uuid
from datetime import UTC, datetime
from pathlib import Path

import platform_event_log
import pytest
from db import models
from db.repositories import platform_events as repo
from fastapi.testclient import TestClient
from pydantic import ValidationError
from request_origin import (
    RequestOrigin,
    current_origin,
    origin_from_request,
    request_origin_var,
)
from schemas.api.auth import AccountType, PlatformRole
from schemas.api.tenants import TenantVerificationResult
from schemas.platform_events import (
    ACTION_MAX_LENGTH,
    ACTION_SPECS,
    USER_AGENT_MAX_LENGTH,
    PlatformActorKind,
    PlatformEventAction,
    PlatformEventCategory,
    PlatformEventOutcome,
    PlatformEventRecord,
    PlatformEventTargetType,
)
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import sessionmaker
from starlette.requests import Request

A = PlatformEventAction
OUT = PlatformEventOutcome
MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "db/migrations/versions/20261106_c589e7a1b2d3_platform_events.py"
)


def _record(**overrides) -> PlatformEventRecord:
    values = {
        "action": A.TENANT_SUSPEND,
        "actor_kind": PlatformActorKind.SYSTEM,
        "target_id": str(uuid.uuid4()),
        "details": {"before": "ACTIVE", "after": "SUSPENDED"},
    }
    values.update(overrides)
    return PlatformEventRecord(**values)


def _person(user_id: str) -> repo.EventActor:
    return repo.EventActor(
        PlatformActorKind.USER,
        user_id=user_id,
        account_type=AccountType.PLATFORM,
        platform_role=PlatformRole.CENTRAL_ADMIN,
    )


# --- 행동 목록 ---------------------------------------------------------------------


def test_every_action_has_one_spec_and_fits_the_column():
    assert set(ACTION_SPECS) == set(PlatformEventAction)
    assert all(len(action.value) <= ACTION_MAX_LENGTH for action in PlatformEventAction)
    # 거절만 있는 행동(권한 거절 · 실행 전 차단)을 빼면 성공이 있다 — 결과 없는 행동은 없다
    assert all(spec.outcomes and spec.actors for spec in ACTION_SPECS.values())


def test_migration_enum_values_follow_the_contract():
    spec = importlib.util.spec_from_file_location(
        "platform_events_migration", MIGRATION
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module._CATEGORIES == tuple(c.value for c in PlatformEventCategory)
    assert module._OUTCOMES == tuple(o.value for o in PlatformEventOutcome)
    assert module._ACTOR_KINDS == tuple(k.value for k in PlatformActorKind)
    assert module._TARGET_TYPES == tuple(t.value for t in PlatformEventTargetType)


# --- 기록 계약 ---------------------------------------------------------------------


def test_category_and_target_type_come_from_the_action():
    record = _record()
    assert record.category is PlatformEventCategory.TENANT_ADMIN
    assert record.target_type is PlatformEventTargetType.TENANT
    assert record.details == {"before": "ACTIVE", "after": "SUSPENDED"}


def test_unknown_action_is_rejected():
    with pytest.raises(ValidationError):
        _record(action="TENANT_RENAME")


@pytest.mark.parametrize(
    "overrides",
    [
        # 이 행동에 없는 결과
        {
            "action": A.TENANT_CREATE,
            "outcome": OUT.FAILED,
            "details": {"reason_code": "X"},
        },
        # 이 행동의 행위자가 될 수 없음 — 수집은 시스템만, 로그인 시도는 로그인 전만
        {
            "action": A.COLLECTION_RUN,
            "actor_kind": PlatformActorKind.ANONYMOUS,
            "details": {},
        },
        {
            "action": A.AUTH_LOGIN,
            "actor_kind": PlatformActorKind.SYSTEM,
            "target_id": None,
            "details": {},
        },
        # 대상이 없는 행동에 대상
        {
            "action": A.AUTH_LOGOUT,
            "actor_kind": PlatformActorKind.USER,
            "actor_user_id": str(uuid.uuid4()),
            "actor_account_type": AccountType.TENANT,
            "details": {},
        },
    ],
)
def test_outcome_actor_and_target_must_fit_the_action(overrides):
    with pytest.raises(ValidationError):
        _record(**overrides)


def test_person_carries_account_type_and_only_platform_has_a_role():
    user_id = str(uuid.uuid4())
    base = {"actor_kind": PlatformActorKind.USER, "actor_user_id": user_id}
    with pytest.raises(ValidationError):  # 사람인데 계정 종류 없음
        _record(**base)
    with pytest.raises(ValidationError):  # PLATFORM 인데 등급 없음
        _record(**base, actor_account_type=AccountType.PLATFORM)
    with pytest.raises(ValidationError):  # TENANT 인데 등급 있음
        _record(
            **base,
            actor_account_type=AccountType.TENANT,
            actor_platform_role=PlatformRole.CENTRAL_ADMIN,
        )
    with pytest.raises(ValidationError):  # 미소속은 행위자가 될 수 없음
        _record(**base, actor_account_type=AccountType.UNASSIGNED)
    with pytest.raises(ValidationError):  # 사람이 아닌데 사용자 ID
        _record(actor_user_id=user_id)
    assert (
        _record(**base, actor_account_type=AccountType.TENANT).actor_platform_role
        is None
    )


def test_reason_code_exists_only_for_denied_and_failed():
    with pytest.raises(ValidationError):
        _record(
            action=A.TENANT_VERIFY,
            actor_kind=PlatformActorKind.USER,
            actor_user_id=str(uuid.uuid4()),
            actor_account_type=AccountType.TENANT,
            outcome=OUT.DENIED,
            details={"result": "ASSUME_DENIED"},
        )
    with pytest.raises(ValidationError):
        _record(details={"before": "ACTIVE", "after": "SUSPENDED", "reason_code": "X"})
    denied = _record(
        action=A.ACCESS_FORBIDDEN,
        actor_kind=PlatformActorKind.USER,
        actor_user_id=str(uuid.uuid4()),
        actor_account_type=AccountType.TENANT,
        outcome=OUT.DENIED,
        target_id=None,
        details={
            "method": "POST",
            "path": "/api/v1/tenants",
            "reason_code": "FORBIDDEN",
        },
    )
    assert denied.details["reason_code"] == "FORBIDDEN"


@pytest.mark.parametrize(
    "forbidden", ["password", "token", "email", "external_id", "secret"]
)
def test_details_take_only_the_declared_fields(forbidden):
    with pytest.raises(ValidationError):
        _record(details={"before": "ACTIVE", "after": "SUSPENDED", forbidden: "x"})


def test_access_path_carries_no_query_string():
    with pytest.raises(ValidationError):
        _record(
            action=A.ACCESS_OUT_OF_SCOPE,
            actor_kind=PlatformActorKind.USER,
            actor_user_id=str(uuid.uuid4()),
            actor_account_type=AccountType.TENANT,
            outcome=OUT.DENIED,
            target_id=None,
            details={
                "method": "GET",
                "path": "/api/v1/x?token=abc",
                "reason_code": "NOT_FOUND",
            },
        )


def test_details_are_normalized_to_json():
    before, after = uuid.uuid4(), uuid.uuid4()
    record = _record(
        action=A.USER_TENANT_ASSIGNMENTS_CHANGE,
        actor_kind=PlatformActorKind.USER,
        actor_user_id=str(uuid.uuid4()),
        actor_account_type=AccountType.PLATFORM,
        actor_platform_role=PlatformRole.CENTRAL_ADMIN,
        details={"before": [str(before)], "after": [after]},
    )
    assert record.details == {"before": [str(before)], "after": [str(after)]}
    verify = _record(
        action=A.TENANT_VERIFY,
        actor_kind=PlatformActorKind.USER,
        actor_user_id=str(uuid.uuid4()),
        actor_account_type=AccountType.TENANT,
        details={"result": TenantVerificationResult.OK},
    )
    assert verify.details == {"result": "OK"}  # None 칸(reason_code)은 빠진다


# --- 요청 문맥 ---------------------------------------------------------------------


def _request(headers: dict[str, str], client=("203.0.113.7", 50000)) -> Request:
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
        "client": client,
    }
    return Request(scope)


def test_origin_trims_user_agent_and_drops_unreadable_addresses():
    long_agent = "Mozilla/5.0 " + "x" * 1000 + "\r\n"
    origin = origin_from_request(_request({"User-Agent": long_agent}), "req-1")
    assert origin.request_id == "req-1"
    assert origin.source_ip == "203.0.113.7"
    assert len(origin.user_agent) == USER_AGENT_MAX_LENGTH
    assert "\r" not in origin.user_agent and "\n" not in origin.user_agent
    assert origin_from_request(
        _request({}, client=("testclient", 1)), None
    ) == RequestOrigin(None, None, None)
    assert origin_from_request(_request({}, client=None), "r").source_ip is None


def test_middleware_sets_the_origin_for_the_request_only(client: TestClient):
    seen: list = []

    @client.app.get("/__origin_probe")
    def probe():
        seen.append(current_origin())
        return {}

    response = client.get(
        "/__origin_probe",
        headers={"X-Request-ID": "probe-123", "User-Agent": "probe-agent"},
    )
    assert response.status_code == 200
    assert seen[0].request_id == "probe-123"
    assert seen[0].user_agent == "probe-agent"
    assert current_origin() is None  # 요청이 끝나면 되돌린다


# --- 테이블 · 기록 함수 (실제 PostgreSQL) -----------------------------------------------


def _rows(db, event_id):
    db.expire_all()
    return db.scalars(
        select(models.PlatformEvent).where(models.PlatformEvent.event_id == event_id)
    ).all()


def test_record_writes_one_row_with_the_request_origin(db, make_auth_session):
    user_id = make_auth_session().user_id
    token = request_origin_var.set(RequestOrigin("req-abc", "198.51.100.4", "agent/1"))
    try:
        row = repo.record_platform_event(
            db,
            A.TENANT_SUSPEND,
            _person(user_id),
            tenant_id=None,
            target_id=str(uuid.uuid4()),
            details={"before": "ACTIVE", "after": "SUSPENDED"},
        )
    finally:
        request_origin_var.reset(token)
    [stored] = _rows(db, row.event_id)
    assert stored.category is PlatformEventCategory.TENANT_ADMIN
    assert stored.action == "TENANT_SUSPEND"
    assert stored.outcome is OUT.SUCCEEDED
    assert stored.actor_user_id == user_id
    assert stored.actor_account_type is AccountType.PLATFORM
    assert stored.actor_platform_role is PlatformRole.CENTRAL_ADMIN
    assert stored.target_type is PlatformEventTargetType.TENANT
    assert (stored.request_id, str(stored.source_ip), stored.user_agent) == (
        "req-abc",
        "198.51.100.4",
        "agent/1",
    )
    assert stored.details == {"before": "ACTIVE", "after": "SUSPENDED"}


def test_outside_a_request_the_origin_columns_are_empty(db):
    row = repo.record_platform_event(
        db,
        A.COLLECTION_RUN,
        repo.SYSTEM,
        target_id=str(uuid.uuid4()),
    )
    [stored] = _rows(db, row.event_id)
    assert (stored.request_id, stored.source_ip, stored.user_agent) == (
        None,
        None,
        None,
    )
    assert stored.details == {}


def test_contract_violation_writes_nothing(db):
    before = db.scalar(text("SELECT count(*) FROM platform_events"))
    with pytest.raises(ValidationError):
        repo.record_platform_event(db, A.COLLECTION_RUN, repo.ANONYMOUS)
    assert db.scalar(text("SELECT count(*) FROM platform_events")) == before


def test_same_transaction_record_rolls_back_with_the_change(db):
    savepoint = db.begin_nested()
    row = repo.record_platform_event(
        db, A.COLLECTION_RUN, repo.SYSTEM, target_id="run-1"
    )
    event_id = row.event_id
    savepoint.rollback()
    assert _rows(db, event_id) == []


def test_update_and_delete_are_rejected(db):
    row = repo.record_platform_event(
        db, A.COLLECTION_RUN, repo.SYSTEM, target_id="run-2"
    )
    for statement in (
        "UPDATE platform_events SET action = 'X' WHERE event_id = :id",
        "DELETE FROM platform_events WHERE event_id = :id",
    ):
        savepoint = db.begin_nested()
        with pytest.raises(DBAPIError, match="append-only"):
            db.execute(text(statement), {"id": row.event_id})
        savepoint.rollback()
    assert len(_rows(db, row.event_id)) == 1


@pytest.mark.parametrize(
    "values",
    [
        # 사유 없는 거절
        {"outcome": OUT.DENIED, "details": {}},
        # 사유 있는 성공
        {"details": {"reason_code": "X"}},
        # 사람이 아닌데 계정 종류
        {"actor_account_type": AccountType.TENANT},
        # 대상 종류만 있고 ID 없음
        {"target_id": None},
        # details 가 객체가 아님
        {"details": ["x"]},
    ],
)
def test_database_checks_stop_writes_that_skip_the_contract(db, values):
    row = {
        "category": PlatformEventCategory.COLLECTION,
        "action": "COLLECTION_RUN",
        "outcome": OUT.SUCCEEDED,
        "actor_kind": PlatformActorKind.SYSTEM,
        "target_type": PlatformEventTargetType.COLLECTION_RUN,
        "target_id": "run-3",
        "details": {},
    }
    row.update(values)
    savepoint = db.begin_nested()
    db.add(models.PlatformEvent(**row))
    with pytest.raises(IntegrityError):
        db.flush()
    savepoint.rollback()


def test_platform_role_check_needs_a_platform_account(db, make_auth_session):
    user_id = make_auth_session().user_id
    for account_type, role in (
        (AccountType.PLATFORM, None),
        (AccountType.TENANT, PlatformRole.CENTRAL_ADMIN),
    ):
        savepoint = db.begin_nested()
        db.add(
            models.PlatformEvent(
                category=PlatformEventCategory.AUTH,
                action="AUTH_LOGOUT",
                outcome=OUT.SUCCEEDED,
                actor_kind=PlatformActorKind.USER,
                actor_user_id=user_id,
                actor_account_type=account_type,
                actor_platform_role=role,
                details={},
            )
        )
        with pytest.raises(IntegrityError):
            db.flush()
        savepoint.rollback()


# --- 따로 남기기 -------------------------------------------------------------------


@pytest.fixture()
def committed_events(pg_engine):
    """따로 남긴(commit 된) 행을 테스트 뒤에 지운다 — 트리거를 정리 트랜잭션 안에서만 끈다."""
    event_ids: list[str] = []
    yield event_ids
    if not event_ids:
        return
    with pg_engine.begin() as conn:
        conn.execute(
            text(
                "ALTER TABLE platform_events DISABLE TRIGGER platform_events_append_only"
            )
        )
        conn.execute(
            text("DELETE FROM platform_events WHERE event_id::text = ANY(:ids)"),
            {"ids": event_ids},
        )
        conn.execute(
            text(
                "ALTER TABLE platform_events ENABLE TRIGGER platform_events_append_only"
            )
        )


def test_detached_record_survives_the_request_rollback(pg_engine, committed_events):
    factory = sessionmaker(bind=pg_engine, expire_on_commit=False)
    target = f"denied-{uuid.uuid4()}"
    with factory() as request_session:
        request_session.execute(text("SELECT 1"))  # 요청 트랜잭션이 열려 있다
        assert platform_event_log.record_detached(
            factory,
            A.ACTION_BLOCK,
            repo.SYSTEM,
            outcome=OUT.DENIED,
            target_id=target,
            details={"reason_code": "TENANT_SUSPENDED"},
        )
        request_session.rollback()
    with factory() as check:
        rows = check.scalars(
            select(models.PlatformEvent).where(models.PlatformEvent.target_id == target)
        ).all()
        committed_events.extend(row.event_id for row in rows)
    assert len(rows) == 1
    assert rows[0].outcome is OUT.DENIED
    assert rows[0].target_type is PlatformEventTargetType.INCIDENT


def test_detached_write_failure_is_logged_not_raised(caplog):
    def broken_factory():
        raise RuntimeError("db down")

    with caplog.at_level(logging.ERROR, logger="vigilantis.platform_events"):
        assert not platform_event_log.record_detached(
            broken_factory,
            A.ACTION_BLOCK,
            repo.SYSTEM,
            outcome=OUT.DENIED,
            target_id="x",
            details={"reason_code": "X"},
        )
    [entry] = [
        r for r in caplog.records if r.getMessage() == "platform_event_write_failed"
    ]
    assert entry.event_action == "ACTION_BLOCK"
    assert entry.error_type == "RuntimeError"


def test_detached_contract_violation_is_swallowed_and_writes_nothing(pg_engine, caplog):
    factory = sessionmaker(bind=pg_engine)
    target = f"bad-{uuid.uuid4()}"
    with caplog.at_level(logging.ERROR, logger="vigilantis.platform_events"):
        # 거절인데 사유 없음 — 계약 위반
        assert not platform_event_log.record_detached(
            factory, A.ACTION_BLOCK, repo.SYSTEM, outcome=OUT.DENIED, target_id=target
        )
    with factory() as check:
        assert (
            check.scalar(
                select(models.PlatformEvent.event_id).where(
                    models.PlatformEvent.target_id == target
                )
            )
            is None
        )


def test_occurred_at_can_follow_the_source_row(db):
    at = datetime(2026, 11, 6, 5, 0, tzinfo=UTC)
    row = repo.record_platform_event(
        db, A.COLLECTION_RUN, repo.SYSTEM, target_id="run-4", occurred_at=at
    )
    [stored] = _rows(db, row.event_id)
    assert stored.occurred_at == at
