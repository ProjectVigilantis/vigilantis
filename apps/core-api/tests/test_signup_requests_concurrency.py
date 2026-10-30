"""도입 신청 동시 접수 · 처리 (#552) — 재시도 키 유일 제약과 행 잠금이 경쟁의 관문이다. 실제 commit 하는 독립 세션을 쓴다."""

from __future__ import annotations

import threading
import uuid
from datetime import UTC, datetime

import pytest
import signup_requests_service as service
from auth import sessions as auth_sessions
from db import models
from db.repositories import auth as auth_repo
from exceptions import ApiError
from schemas.api.auth import AccountType, PlatformRole, UserRole
from schemas.api.errors import ErrorCode
from schemas.api.signup_requests import SignupRequestCreate, SignupRequestStatusRequest
from sqlalchemy import delete, func, select
from sqlalchemy.orm import sessionmaker
from support_access import Actor

WORKERS = 8
ISSUER = "https://cognito-idp.ap-northeast-2.amazonaws.com/ap-northeast-2_test"  # conftest TEST_AUTH_ISSUER


@pytest.fixture()
def sessions(pg_engine):
    factory = sessionmaker(bind=pg_engine, autoflush=False, expire_on_commit=False)
    keys: list[str] = []
    yield factory, keys
    with factory() as session:
        ids = session.scalars(
            select(models.SignupRequest.request_id).where(models.SignupRequest.idempotency_key.in_(keys))
        ).all()
        session.execute(
            delete(models.OperatorNotification).where(models.OperatorNotification.target_id.in_(ids))
        )
        session.execute(
            delete(models.SignupRequest).where(models.SignupRequest.idempotency_key.in_(keys))
        )
        session.commit()


def body(**overrides) -> SignupRequestCreate:
    data = {"company_name": "Acme", "contact_name": "Kim", "email": "k@example.com"}
    return SignupRequestCreate.model_validate({**data, **overrides})


def race(factory, key, bodies):
    barrier = threading.Barrier(len(bodies))
    results: list = [None] * len(bodies)

    def run(i):
        with factory() as session:
            barrier.wait()
            try:
                results[i] = service.create(session, key, bodies[i]).request_id
            except ApiError as exc:
                results[i] = exc.code

    threads = [threading.Thread(target=run, args=(i,)) for i in range(len(bodies))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert all(r is not None for r in results)
    return results


def stored(factory, key):
    with factory() as session:
        rows = session.scalars(
            select(models.SignupRequest).where(models.SignupRequest.idempotency_key == str(key))
        ).all()
        notes = session.scalar(
            select(func.count())
            .select_from(models.OperatorNotification)
            .where(models.OperatorNotification.target_id.in_([r.request_id for r in rows]))
        )
    return rows, notes


def test_same_key_same_payload_race_creates_one_request_and_one_notification(sessions):
    factory, keys = sessions
    key = uuid.uuid4()
    keys.append(str(key))
    results = race(factory, key, [body()] * WORKERS)
    rows, notes = stored(factory, key)
    assert len(rows) == 1 and notes == 1
    assert set(results) == {rows[0].request_id}


def test_same_key_different_payload_race_has_one_winner(sessions):
    factory, keys = sessions
    key = uuid.uuid4()
    keys.append(str(key))
    bodies = [body(company_name=f"Company {i}") for i in range(WORKERS)]
    results = race(factory, key, bodies)
    rows, notes = stored(factory, key)
    assert len(rows) == 1 and notes == 1
    winners = [i for i, r in enumerate(results) if r == rows[0].request_id]
    assert len(winners) == 1 and bodies[winners[0]].company_name == rows[0].company_name
    assert [r for i, r in enumerate(results) if i not in winners] == [
        ErrorCode.IDEMPOTENCY_KEY_CONFLICT
    ] * (WORKERS - 1)


@pytest.fixture()
def admin_db(pg_engine):
    """관리자 세션 · 사용자 행은 지울 수 없는 이력과 얽혀 있어 이 테스트 전용 DB 를 만들고 그 DB 만 지운다."""
    import os
    from pathlib import Path

    from alembic import command
    from alembic.config import Config
    from config import get_settings
    from sqlalchemy import create_engine, text

    core_api = Path(__file__).resolve().parents[1]
    admin_url = pg_engine.url.set(database="postgres").render_as_string(hide_password=False)
    name = f"vigilantis_test_{uuid.uuid4().hex}"
    admin = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    previous = os.environ.get("DATABASE_URL")
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    engine = None
    try:
        url = admin_url.rsplit("/", 1)[0] + "/" + name
        os.environ["DATABASE_URL"] = url
        get_settings.cache_clear()
        command.upgrade(Config(str(core_api / "alembic.ini")), "head")
        engine = create_engine(url)
        yield sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    finally:
        if engine is not None:
            engine.dispose()
        if previous is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous
        get_settings.cache_clear()
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


def seed_central_admin(db, n):
    now = datetime.now(UTC)
    user = auth_repo.create_user(
        db,
        issuer=ISSUER,
        cognito_sub=f"signup-race-{n}",
        email=f"signup-race-{n}@example.com",
        role=UserRole.ADMIN,
        account_type=AccountType.PLATFORM,
        platform_role=PlatformRole.CENTRAL_ADMIN,
        tenant_id=None,
        now=now,
        email_verified_at=now,
    )
    issued = auth_sessions.issue_session(
        db,
        user_id=user.user_id,
        mfa_step=models.AuthSessionMfaStep.SOFTWARE_TOKEN_MFA,
        now=now,
        max_seconds=3600,
        idle_seconds=1800,
    )
    return issued.token


def test_concurrent_approve_and_reject_have_one_winner_and_a_coherent_row(admin_db):
    with admin_db() as db:
        tokens = [seed_central_admin(db, n) for n in (1, 2)]
        db.commit()
    with admin_db() as db:
        request_id = service.create(db, uuid.uuid4(), body()).request_id
    bodies = [
        SignupRequestStatusRequest(status="APPROVED"),
        SignupRequestStatusRequest(status="REJECTED", reason="중복 신청"),
    ]
    barrier = threading.Barrier(2)
    results: list = [None, None]

    def run(i):
        actor = Actor(token=tokens[i], idle_seconds=1800, clock=lambda: datetime.now(UTC))
        with admin_db() as db:
            barrier.wait(10)
            try:
                results[i] = service.change_status(db, actor, request_id, bodies[i])
            except ApiError as exc:
                results[i] = exc

    threads = [threading.Thread(target=run, args=(i,)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
        assert not t.is_alive()

    winners = [i for i, r in enumerate(results) if not isinstance(r, ApiError)]
    losers = [r for r in results if isinstance(r, ApiError)]
    assert len(winners) == 1 and len(losers) == 1
    assert losers[0].code == ErrorCode.SIGNUP_REQUEST_STATE_CONFLICT
    with admin_db() as db:
        row = db.get(models.SignupRequest, request_id)
    assert row.status.value == bodies[winners[0]].status.value
    assert row.rejected_reason == bodies[winners[0]].reason
    assert results[winners[0]].status == row.status
