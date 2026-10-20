"""서비스 전환 3종 + scale-in 보호 결과 칸 migration — 단일 체인·ORM 일치·일지 불변식."""

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
from schemas.api.incidents import IncidentCategory, IncidentStatus, RiskLevel
from schemas.runbooks import RunbookId, TriggerSource
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError

REVISION = "c8e2f5a1d704"
PARENT = "b6c1e4a9d853"
TABLES = {
    "service_transitions",
    "service_transition_operations",
    "service_transition_observations",
}
TARGET = "arn:aws:ec2:ap-northeast-2:123456789012:instance/i-0abc123456789def0"


@pytest.fixture()
def migration_config(pg_engine, monkeypatch):
    monkeypatch.setenv(
        "DATABASE_URL", pg_engine.url.render_as_string(hide_password=False)
    )
    get_settings.cache_clear()
    yield Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    get_settings.cache_clear()


def test_revision_follows_autoscaling_plan_on_a_single_chain(migration_config):
    script = ScriptDirectory.from_config(migration_config)
    assert script.get_revision(REVISION).down_revision == PARENT
    assert len(script.get_heads()) == 1


def test_roundtrip_matches_orm(pg_engine, migration_config):
    command.downgrade(migration_config, PARENT)
    try:
        with pg_engine.connect() as connection:
            inspector = inspect(connection)
            assert not TABLES & set(inspector.get_table_names())
            assert "scale_in_protection_result" not in {
                c["name"] for c in inspector.get_columns("action_executions")
            }
    finally:
        command.upgrade(migration_config, "head")
    with pg_engine.connect() as connection:
        inspector = inspect(connection)
        assert TABLES <= set(inspector.get_table_names())
        column = next(
            c
            for c in inspector.get_columns("action_executions")
            if c["name"] == "scale_in_protection_result"
        )
        assert (
            column["nullable"] is True and column["type"].__class__.__name__ == "JSONB"
        )
        assert (
            compare_metadata(
                MigrationContext.configure(connection), models.Base.metadata
            )
            == []
        )


def _execution(db):
    incident = models.Incident(
        title="t",
        subject_arn=TARGET,
        category=IncidentCategory.SECOPS,
        status=IncidentStatus.ACTION_IN_PROGRESS,
        initial_risk_level=RiskLevel.LOW,
        initial_risk_reason_codes=["TEST"],
    )
    db.add(incident)
    db.flush()
    execution = models.ActionExecution(
        incident_id=incident.incident_id,
        runbook_id=RunbookId.RUNBOOK_EC2_ENABLE_AUTOSCALING,
        target_arn=TARGET,
        trigger_source=TriggerSource.USER_APPROVAL,
    )
    db.add(execution)
    db.flush()
    return execution.execution_id


def _attempt(execution_id, supersedes=None, transition_id=None):
    return models.ServiceTransition(
        transition_id=transition_id or str(uuid.uuid4()),
        execution_id=execution_id,
        supersedes_transition_id=supersedes,
        plan={"k": "v"},
    )


def _added(db, row):
    db.add(row)
    db.flush()
    return row


def _rejected(db, row):
    with pytest.raises(IntegrityError), db.begin_nested():
        db.add(row)
        db.flush()


def _operation(execution_id, transition_id=None, **overrides):
    values = {
        "execution_id": execution_id,
        "transition_id": transition_id,
        "sequence": 1,
        "command": "prepare",
        "aws_operation": "elbv2.create_target_group",
        "resource_ref": "r",
        "request": {},
        "state": "PENDING",
        "started_at": datetime.now(UTC),
    }
    values.update(overrides)
    return models.ServiceTransitionOperation(**values)


@pytest.mark.parametrize(
    "overrides",
    [
        {"state": "APPLIED"},  # 끝난 결과에는 finished_at이 있어야 한다
        {"state": "PENDING", "finished_at": datetime.now(UTC)},
        {"state": "DONE", "finished_at": datetime.now(UTC)},
        {"reconciled_state": "EFFECT_OBSERVED"},  # 대조 시각 없이
        {
            "reconciled_state": "OBSERVED_ABSENT",
            "reconciled_at": datetime.now(UTC),
        },  # 옛 어휘
        {
            "state": "APPLIED",
            "finished_at": datetime.now(UTC),
            "reconciled_state": "EFFECT_OBSERVED",
            "reconciled_at": datetime.now(UTC),
        },
    ],
)
def test_journal_rows_keep_their_shape(db, overrides):
    execution_id = _execution(db)
    attempt = _added(db, _attempt(execution_id))
    _rejected(db, _operation(execution_id, attempt.transition_id, **overrides))


def test_attempts_form_one_linear_chain_inside_one_execution(db):
    execution_id, other_execution = _execution(db), _execution(db)
    root = _added(db, _attempt(execution_id))
    _rejected(db, _attempt(execution_id))  # root는 실행당 하나
    second = _added(db, _attempt(execution_id, supersedes=root.transition_id))
    _rejected(db, _attempt(execution_id, supersedes=root.transition_id))  # fork 없음
    _rejected(
        db, _attempt(other_execution, supersedes=second.transition_id)
    )  # 다른 실행의 시도는 대체 못 한다
    own = str(uuid.uuid4())
    _rejected(
        db, _attempt(execution_id, supersedes=own, transition_id=own)
    )  # 자기 대체 없음
    _added(db, _attempt(execution_id, supersedes=second.transition_id))
    _added(db, _attempt(other_execution))  # 다른 실행은 자기 root를 갖는다


def test_journal_is_per_execution_and_attribution_stays_in_that_execution(db):
    execution_id, other_execution = _execution(db), _execution(db)
    attempt = _added(db, _attempt(execution_id))
    _added(db, _operation(execution_id, attempt.transition_id, sequence=1))
    _added(
        db, _operation(execution_id, None, sequence=2)
    )  # #446 인프라 정리 — 시도 없음
    _rejected(db, _operation(execution_id, None, sequence=2))  # 순번은 실행 단위로 유일
    _rejected(
        db, _operation(other_execution, attempt.transition_id, sequence=1)
    )  # 남의 실행의 시도
    _rejected(
        db,
        models.ServiceTransitionObservation(
            execution_id=other_execution,
            transition_id=attempt.transition_id,
            kind="PLAN",
            verdict="PASSED",
            observed_at=datetime.now(UTC),
            evidence={},
        ),
    )
    _added(
        db,
        models.ServiceTransitionObservation(
            execution_id=execution_id,
            transition_id=None,
            kind="CLEANUP",
            verdict="UNKNOWN",
            observed_at=datetime.now(UTC),
            evidence={},
        ),
    )
