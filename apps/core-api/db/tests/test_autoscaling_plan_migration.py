"""action_executions.autoscaling_plan migration — ORM 일치·기존 행 보존·손실 방지. (Issue #446)"""

from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import delete, inspect, text
from sqlalchemy.orm import Session

from config import get_settings
from db import models
from schemas.api.incidents import IncidentCategory, IncidentStatus, RiskLevel
from schemas.runbooks import RunbookId, TriggerSource

REVISION = "b6c1e4a9d853"
PARENT = "a3d8f1c6e742"
TARGET = "arn:aws:ec2:ap-northeast-2:123456789012:instance/i-0abc123456789def0"
PLAN = {"target_arn": TARGET, "execution_id": "x", "min_size": 1, "max_size": 3}


@pytest.fixture()
def migration_config(pg_engine, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", pg_engine.url.render_as_string(hide_password=False))
    get_settings.cache_clear()
    yield Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    get_settings.cache_clear()


def _incident(session):
    incident = models.Incident(
        title="위협", subject_arn=TARGET, category=IncidentCategory.SECOPS,
        status=IncidentStatus.ACTION_IN_PROGRESS, initial_risk_level=RiskLevel.LOW,
        initial_risk_reason_codes=["TEST"],
    )
    session.add(incident)
    session.flush()
    return incident.incident_id


def _seed_execution(pg_engine, plan):
    with Session(pg_engine) as session:
        incident_id = _incident(session)
        execution = models.ActionExecution(
            incident_id=incident_id,
            runbook_id=RunbookId.RUNBOOK_EC2_ENABLE_AUTOSCALING,
            target_arn=TARGET,
            trigger_source=TriggerSource.USER_APPROVAL,
            autoscaling_plan=plan,
        )
        session.add(execution)
        session.commit()
        return incident_id, execution.execution_id


def _cleanup(pg_engine, incident_id, execution_id):
    with Session(pg_engine) as session:
        session.execute(delete(models.ActionExecution).where(
            models.ActionExecution.execution_id == execution_id))
        session.execute(delete(models.Incident).where(models.Incident.incident_id == incident_id))
        session.commit()


def _restore_and_cleanup(pg_engine, migration_config, incident_id, execution_id):
    try:
        command.upgrade(migration_config, "head")
    finally:
        _cleanup(pg_engine, incident_id, execution_id)


def test_failed_schema_restore_does_not_leave_committed_rows(
    pg_engine, migration_config, monkeypatch,
):
    incident_id, execution_id = _seed_execution(pg_engine, PLAN)

    def fail_restore(*args, **kwargs):
        raise RuntimeError("schema restore failed")

    monkeypatch.setattr(command, "upgrade", fail_restore)
    with pytest.raises(RuntimeError, match="schema restore failed"):
        _restore_and_cleanup(pg_engine, migration_config, incident_id, execution_id)

    with Session(pg_engine) as session:
        assert session.get(models.ActionExecution, execution_id) is None
        assert session.get(models.Incident, incident_id) is None


def test_revision_follows_a3d8_on_a_single_chain(migration_config):
    script = ScriptDirectory.from_config(migration_config)
    assert script.get_revision(REVISION).down_revision == PARENT
    assert len(script.get_heads()) == 1


def test_roundtrip_matches_orm_and_column_is_nullable_jsonb(pg_engine, migration_config):
    command.downgrade(migration_config, PARENT)
    with pg_engine.connect() as connection:
        columns = {c["name"] for c in inspect(connection).get_columns("action_executions")}
    assert "autoscaling_plan" not in columns

    command.upgrade(migration_config, "head")
    with pg_engine.connect() as connection:
        column = next(
            c for c in inspect(connection).get_columns("action_executions")
            if c["name"] == "autoscaling_plan"
        )
        assert column["nullable"] is True and column["type"].__class__.__name__ == "JSONB"
        assert compare_metadata(MigrationContext.configure(connection), models.Base.metadata) == []


def test_existing_rows_stay_without_a_plan_after_upgrade(pg_engine, migration_config):
    # 인시던트는 head 에서 ORM 으로 먼저 넣는다 — 뒤 리비전이 incidents 에 칸을 더하면
    # (tenant_id · #485) 옛 리비전에서의 ORM INSERT 가 그 칸을 찾다 깨진다
    with Session(pg_engine) as session:
        incident_id = _incident(session)
        session.commit()
    command.downgrade(migration_config, PARENT)
    with Session(pg_engine) as session:
        execution_id = str(session.execute(text(
            "INSERT INTO action_executions (execution_id, incident_id, runbook_id, target_arn,"
            " status, trigger_source, verification_attempts, created_at, updated_at)"
            " VALUES (gen_random_uuid(), :i, 'RUNBOOK_EC2_ENABLE_AUTOSCALING', :t,"
            " 'IN_PROGRESS', 'USER_APPROVAL', 0, now(), now()) RETURNING execution_id"
        ), {"i": incident_id, "t": TARGET}).scalar_one())
        session.commit()
    try:
        command.upgrade(migration_config, "head")
        with Session(pg_engine) as session:
            assert session.get(models.ActionExecution, execution_id).autoscaling_plan is None
    finally:
        _restore_and_cleanup(pg_engine, migration_config, incident_id, execution_id)


def test_downgrade_drops_only_the_column_and_upgrade_restores_it_empty(pg_engine, migration_config):
    incident_id, execution_id = _seed_execution(pg_engine, PLAN)
    try:
        command.downgrade(migration_config, PARENT)
        with pg_engine.connect() as connection:
            columns = {c["name"] for c in inspect(connection).get_columns("action_executions")}
            assert "autoscaling_plan" not in columns
            # 실행 행 자체는 남는다
            assert connection.execute(
                text("SELECT count(*) FROM action_executions WHERE execution_id = :e"),
                {"e": execution_id},
            ).scalar_one() == 1
        command.upgrade(migration_config, "head")
        with Session(pg_engine) as session:
            assert session.get(models.ActionExecution, execution_id).autoscaling_plan is None
    finally:
        _restore_and_cleanup(pg_engine, migration_config, incident_id, execution_id)