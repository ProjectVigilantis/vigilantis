"""보호 대상 에스컬레이션 사건 migration (#598 PR1) — 실제 PostgreSQL/Alembic.

지키는 것: 실행 없는 새 사건은 받고 모양이 어긋난 것은 막는다 · 기존 사건 종류는 그대로 유효하다 ·
ORM CHECK 와 migration CHECK 가 같다 · 새 사건 행이 있으면 downgrade 가 데이터를 지우지 않고 멈춘다.
테스트마다 고유 이름의 일회용 DB 를 만들고 지운다(공용 pg_engine DB 의 head 를 건드리지 않는다).
"""

from __future__ import annotations

import importlib.util
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from config import get_settings
from db import models
from sqlalchemy import CheckConstraint, create_engine, text
from sqlalchemy.exc import IntegrityError

REVISION = "e598a1b2c301"
KIND = "PROTECTED_TARGET_ESCALATION"
_MIGRATION = next(
    (Path(__file__).resolve().parents[1] / "migrations" / "versions").glob(f"*_{REVISION}_*.py")
)


@pytest.fixture()
def scratch_db(pg_engine, monkeypatch):
    """(Config, engine) — 같은 서버의 새 빈 DB. 끝나면 DROP."""
    name = f"vigilantis_escalation_{uuid.uuid4().hex[:8]}"
    admin = create_engine(pg_engine.url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    url = pg_engine.url.set(database=name)
    monkeypatch.setenv("DATABASE_URL", url.render_as_string(hide_password=False))
    get_settings.cache_clear()
    engine = create_engine(url)
    try:
        yield Config(str(Path(__file__).resolve().parents[2] / "alembic.ini")), engine
    finally:
        engine.dispose()
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()
        get_settings.cache_clear()


def _parent(config: Config) -> str:
    return ScriptDirectory.from_config(config).get_revision(REVISION).down_revision


def _insert(engine, **overrides) -> None:
    values = {
        "event_id": str(uuid.uuid4()),
        "incident_id": str(uuid.uuid4()),
        "execution_id": None,
        "related_execution_id": None,
        "kind": KIND,
        "phase": "CREATED",
        "outcome": None,
        "target_arn": "arn:aws:ec2:ap-northeast-2:123456789012:instance/i-0abc",
        "risk_level": None,
        "trigger_source": "PRE_MITIGATION_0_5S",
        "runbook_id": None,
        "execution_status": None,
        "reason_code": "PROTECTED_TAG",
        "occurred_at": datetime(2026, 11, 12, tzinfo=UTC),
        "created_at": datetime(2026, 11, 12, tzinfo=UTC),
    }
    values.update(overrides)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO notification_events (event_id, incident_id, execution_id,"
                " related_execution_id, kind, phase, outcome, target_arn, risk_level,"
                " trigger_source, runbook_id, execution_status, reason_code, occurred_at, created_at)"
                " VALUES (:event_id, :incident_id, :execution_id, :related_execution_id, :kind,"
                " :phase, :outcome, :target_arn, CAST(:risk_level AS risk_level),"
                " CAST(:trigger_source AS trigger_source), CAST(:runbook_id AS runbook_id),"
                " CAST(:execution_status AS execution_status), :reason_code, :occurred_at,"
                " :created_at)"
            ),
            values,
        )


def _count(engine, kind: str) -> int:
    with engine.connect() as conn:
        return conn.execute(
            text("SELECT count(*) FROM notification_events WHERE kind = :kind"), {"kind": kind}
        ).scalar_one()


def _old_high_incident(engine) -> None:
    _insert(engine, kind="HIGH_INCIDENT_CREATED", risk_level="HIGH", trigger_source=None, reason_code=None)


def test_orm_check_matches_the_migration():
    migration_spec = importlib.util.spec_from_file_location("escalation_migration", _MIGRATION)
    migration = importlib.util.module_from_spec(migration_spec)
    migration_spec.loader.exec_module(migration)
    [check] = [
        c
        for c in models.NotificationEvent.__table__.constraints
        if isinstance(c, CheckConstraint) and c.name == "ck_notification_events_event_shape"
    ]
    assert str(check.sqltext) == migration._NEW_SHAPE
    assert migration.down_revision == "f588d1a2b3c4"


def test_new_event_is_accepted_and_old_kinds_remain_valid(scratch_db):
    config, engine = scratch_db
    command.upgrade(config, _parent(config))
    _old_high_incident(engine)

    command.upgrade(config, REVISION)

    assert _count(engine, "HIGH_INCIDENT_CREATED") == 1
    _old_high_incident(engine)
    execution = str(uuid.uuid4())
    _insert(
        engine, kind="AUTO_ISOLATION", phase="REQUESTED", execution_id=execution,
        runbook_id="RUNBOOK_EC2_ISOLATE", execution_status="IN_PROGRESS", reason_code=None,
    )
    for source in ("PRE_MITIGATION_0_5S", "TIMEOUT_ISOLATION_1M"):
        _insert(engine, trigger_source=source)
    assert _count(engine, KIND) == 2


def test_the_unique_transition_still_blocks_a_duplicate_escalation(scratch_db):
    config, engine = scratch_db
    command.upgrade(config, REVISION)
    incident = str(uuid.uuid4())
    _insert(engine, incident_id=incident)
    with pytest.raises(IntegrityError, match="uq_notification_events_transition"):
        _insert(engine, incident_id=incident)


@pytest.mark.parametrize(
    "overrides",
    [
        {"execution_id": str(uuid.uuid4())},
        {"related_execution_id": str(uuid.uuid4())},
        {"runbook_id": "RUNBOOK_EC2_ISOLATE"},
        {"execution_status": "IN_PROGRESS"},
        {"trigger_source": None},
        {"trigger_source": "USER_APPROVAL"},
        {"trigger_source": "AUTO_ON_FAILURE"},
        {"reason_code": None},
        {"reason_code": "OTHER"},
        {"phase": "REQUESTED"},
        {"phase": "RESULT", "outcome": "SUCCESS"},
        {"outcome": "SUCCESS"},
        {"kind": "PROTECTED_TARGET_ESCALATIONS"},
    ],
    ids=lambda o: "-".join(f"{k}={v}"[:40] for k, v in o.items()),
)
def test_malformed_event_is_rejected(scratch_db, overrides):
    config, engine = scratch_db
    command.upgrade(config, REVISION)
    with pytest.raises(IntegrityError, match="ck_notification_events_"):
        _insert(engine, **overrides)
    assert _count(engine, KIND) == 0


def test_the_new_event_is_rejected_before_the_revision(scratch_db):
    config, engine = scratch_db
    command.upgrade(config, _parent(config))
    with pytest.raises(IntegrityError, match="ck_notification_events_event_shape"):
        _insert(engine)


def test_downgrade_stops_without_deleting_new_events_then_restores_the_old_check(scratch_db):
    config, engine = scratch_db
    command.upgrade(config, REVISION)
    _old_high_incident(engine)
    _insert(engine)

    with pytest.raises(RuntimeError, match=KIND):
        command.downgrade(config, _parent(config))
    assert _count(engine, KIND) == 1
    assert _count(engine, "HIGH_INCIDENT_CREATED") == 1

    # 사람이 행을 따로 처리한 뒤에는 내려가고, 옛 CHECK 가 새 사건을 다시 막는다
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM notification_events WHERE kind = :kind"), {"kind": KIND})
    command.downgrade(config, _parent(config))
    assert _count(engine, "HIGH_INCIDENT_CREATED") == 1
    with pytest.raises(IntegrityError, match="ck_notification_events_event_shape"):
        _insert(engine)

    command.upgrade(config, REVISION)
    _insert(engine)
