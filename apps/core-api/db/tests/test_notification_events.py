"""실제 PostgreSQL/Alembic: 사건 제약, caller rollback, 기존 head 업그레이드.

pg_engine은 빈 일회용 DB를 Alembic head로 만든다. create_all을 사용하지 않는다.
"""

import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from config import get_settings
from db import models
from db.repositories import notification_events as events_repo
from schemas.api.incidents import RiskLevel
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

REVISION = "d493a1b2c301"
PARENT = "c8e2f5a1d704"


def _values(**overrides):
    values = {
        "incident_id": str(uuid.uuid4()), "execution_id": None, "related_execution_id": None,
        "kind": "HIGH_INCIDENT_CREATED", "phase": "CREATED", "outcome": None,
        "target_arn": "arn:aws:ec2:ap-northeast-2:123456789012:instance/i-test",
        "aws_account_id": "123456789012", "region": "ap-northeast-2",
        "risk_level": RiskLevel.HIGH, "trigger_source": None, "runbook_id": None,
        "execution_status": None, "reason_code": None, "occurred_at": datetime.now(UTC),
    }
    return values | overrides


def test_new_database_has_outbox_schema(pg_engine):
    with pg_engine.connect() as connection:
        assert "notification_events" in inspect(connection).get_table_names()
        assert compare_metadata(MigrationContext.configure(connection), models.Base.metadata) == []


def test_upgrade_from_previous_head_preserves_business_rows(pg_engine, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", pg_engine.url.render_as_string(hide_password=False))
    get_settings.cache_clear()
    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    script = ScriptDirectory.from_config(config)
    assert script.get_heads() == [REVISION]
    assert script.get_revision(REVISION).down_revision == PARENT
    command.downgrade(config, PARENT)
    incident_id = str(uuid.uuid4())
    try:
        with Session(pg_engine) as db:
            assert "notification_events" not in inspect(pg_engine).get_table_names()
            db.add(models.Incident(
                incident_id=incident_id,
                subject_arn="arn:aws:ec2:ap-northeast-2:123456789012:instance/i-migration",
                category="FINOPS",
            ))
            db.commit()
    finally:
        command.upgrade(config, "head")
        get_settings.cache_clear()
    with Session(pg_engine) as db:
        incident = db.get(models.Incident, incident_id)
        assert incident.status.value == "ANALYZING"
        assert events_repo.list_by_incident(db, incident_id) == []  # 과거 사건을 꾸며내지 않는다
        db.delete(incident)
        db.commit()
    with pg_engine.connect() as connection:
        assert compare_metadata(MigrationContext.configure(connection), models.Base.metadata) == []


def test_repository_never_commits_and_rollback_removes_intent(pg_engine):
    with Session(pg_engine) as db:
        record = events_repo.add(db, **_values())
        event_id = record.event_id
        with Session(pg_engine) as observer:
            assert events_repo.get(observer, event_id) is None
        db.rollback()
    with Session(pg_engine) as observer:
        assert events_repo.get(observer, event_id) is None


def test_database_rejects_duplicate_created_transition_without_overwriting(db):
    values = _values()
    original = events_repo.add(db, **values)
    with pytest.raises(IntegrityError), db.begin_nested():
        events_repo.add(db, **(values | {"target_arn": "different"}))
    assert events_repo.get(db, original.event_id).target_arn == values["target_arn"]
    assert len(events_repo.list_by_incident(db, values["incident_id"])) == 1


@pytest.mark.parametrize("overrides", [
    {"kind": "OTHER"},
    {"phase": "RESULT", "outcome": "SUCCESS"},
    {"outcome": "SUCCESS"},
    {"risk_level": None},
    {"risk_level": RiskLevel.LOW},
    {"aws_account_id": "unknown"},
])
def test_database_rejects_invalid_facts(db, overrides):
    with pytest.raises(IntegrityError), db.begin_nested():
        events_repo.add(db, **_values(**overrides))


def test_committed_fact_survives_new_session(pg_engine):
    with Session(pg_engine) as db:
        row = events_repo.add(db, **_values())
        event_id, incident_id = row.event_id, row.incident_id
        db.commit()
    with Session(pg_engine) as reopened:
        [row] = events_repo.list_by_incident(reopened, incident_id)
        assert row.event_id == event_id
        assert row.aws_account_id == "123456789012"
        assert row.occurred_at.tzinfo is not None
        reopened.delete(row)
        reopened.commit()
