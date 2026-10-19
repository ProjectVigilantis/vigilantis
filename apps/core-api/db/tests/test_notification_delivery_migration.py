"""#493 PR2 migration: 빈 DB·기존 head(d493a1b2c301) 업그레이드, ORM 일치, 행 모양 제약.

pg_engine은 빈 일회용 DB에 Alembic head를 적용한다. create_all을 쓰지 않는다.
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
from sqlalchemy import func, inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

REVISION = "d493a1b2c302"
PARENT = "d493a1b2c301"
TABLES = {"notification_deliveries", "notification_routes", "notification_lanes"}


@pytest.fixture()
def migration_config(pg_engine, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", pg_engine.url.render_as_string(hide_password=False))
    get_settings.cache_clear()
    yield Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    get_settings.cache_clear()


def _event(db: Session):
    return events_repo.add(
        db,
        incident_id=str(uuid.uuid4()), execution_id=None, related_execution_id=None,
        kind="HIGH_INCIDENT_CREATED", phase="CREATED", outcome=None,
        target_arn="arn:aws:ec2:ap-northeast-2:123456789012:instance/i-migration",
        aws_account_id="123456789012", region="ap-northeast-2", risk_level=RiskLevel.HIGH,
        trigger_source=None, runbook_id=None, execution_status=None, reason_code=None,
        occurred_at=datetime.now(UTC),
    )


def test_revision_follows_outbox_on_a_single_chain(migration_config):
    script = ScriptDirectory.from_config(migration_config)
    assert script.get_heads() == [REVISION]
    assert script.get_revision(REVISION).down_revision == PARENT


def test_empty_database_upgrade_matches_orm(pg_engine):
    with pg_engine.connect() as connection:
        inspector = inspect(connection)
        assert TABLES <= set(inspector.get_table_names())
        lane = {c["name"]: c for c in inspector.get_columns("notification_lanes")}
        # 간격 관문과 공급자 대기는 별도 칸이다 — 보유자 해제가 대기를 덮어쓰지 않게
        assert {"available_at", "cooldown_until", "holder_token"} <= set(lane)
        assert lane["cooldown_until"]["nullable"] is False
        assert lane["cooldown_until"]["type"].timezone is True
        assert compare_metadata(MigrationContext.configure(connection), models.Base.metadata) == []


def test_lane_requires_a_cooldown_deadline(db):
    with pytest.raises(IntegrityError) as failure, db.begin_nested():
        db.add(models.NotificationLane(channel="SNS", available_at=datetime.now(UTC)))
        db.flush()
    assert failure.value.orig.sqlstate == "23502"  # NOT NULL 위반


def test_upgrade_from_outbox_head_preserves_events_without_backfill(pg_engine, migration_config):
    command.downgrade(migration_config, PARENT)
    try:
        assert not TABLES & set(inspect(pg_engine).get_table_names())
        with Session(pg_engine) as db:
            event = _event(db)
            event_id, created_at = event.event_id, event.created_at
            db.commit()
    finally:
        command.upgrade(migration_config, "head")
    with Session(pg_engine) as db:
        kept = events_repo.get(db, event_id)
        assert (kept.kind, kept.aws_account_id, kept.created_at) == (
            "HIGH_INCIDENT_CREATED", "123456789012", created_at,
        )
        # 업그레이드가 과거 사건의 발송 행·바인딩·관문을 만들지 않는다
        for model in (models.NotificationDelivery, models.NotificationRoute, models.NotificationLane):
            assert db.scalar(select(func.count()).select_from(model)) == 0
        db.delete(kept)
        db.commit()
    with pg_engine.connect() as connection:
        assert compare_metadata(MigrationContext.configure(connection), models.Base.metadata) == []


def _pending(event_id: str, **overrides) -> models.NotificationDelivery:
    values = {
        "event_id": event_id, "channel": "SLACK", "state": "PENDING",
        "tenant_id": "customer-a", "aws_account_id": "123456789012",
        "route_fingerprint": "f" * 64, "route_epoch": str(uuid.uuid4()),
        "next_attempt_at": datetime.now(UTC),
    }
    return models.NotificationDelivery(**(values | overrides))


def test_valid_pending_row_and_one_row_per_event_channel(db):
    event_id = _event(db).event_id
    db.add(_pending(event_id))
    db.flush()
    with pytest.raises(IntegrityError), db.begin_nested():
        db.add(_pending(event_id))
        db.flush()
    db.add(_pending(event_id, channel="SNS"))
    db.flush()


NOW = datetime.now(UTC)


@pytest.mark.parametrize("overrides", [
    {"channel": "EMAIL"},
    {"state": "DONE"},
    {"route_epoch": None},  # 보낼 수 있는 행은 바인딩 epoch에 묶인다
    {"next_attempt_at": None},
    {"attempts": -1},
    {"last_category": "RETRYABLE"},
    {"blocked_reason": "ROUTE_CHANGED"},  # PENDING에 차단 사유 없음
    {"state": "IN_FLIGHT", "next_attempt_at": None, "claim_token": str(uuid.uuid4()),
     "lease_expires_at": NOW},  # claim은 시도를 센다(attempts >= 1)
    {"state": "SUCCEEDED", "next_attempt_at": None, "attempts": 1, "finished_at": NOW},
    {"state": "SUCCEEDED", "next_attempt_at": None, "attempts": 1, "finished_at": NOW,
     "receipt_kind": "EMAIL_SENT"},
    {"state": "FAILED", "next_attempt_at": None, "attempts": 1, "finished_at": NOW,
     "last_category": "TRANSIENT", "final_reason": "GAVE_UP"},
    {"state": "FAILED", "next_attempt_at": None, "attempts": 1, "finished_at": NOW,
     "final_reason": "EXHAUSTED"},  # 마지막 실패 범주 필수
    {"state": "BLOCKED", "next_attempt_at": None, "finished_at": NOW},  # 사유 필수
    {"state": "BLOCKED", "blocked_reason": "NOT_ACTIVATED", "finished_at": NOW},
])
def test_database_rejects_invalid_delivery_shapes(db, overrides):
    event_id = _event(db).event_id
    with pytest.raises(IntegrityError), db.begin_nested():
        db.add(_pending(event_id, **overrides))
        db.flush()


def test_blocked_row_may_have_no_binding(db):
    event_id = _event(db).event_id
    db.add(_pending(
        event_id, state="BLOCKED", blocked_reason="ACCOUNT_MISSING", tenant_id=None,
        aws_account_id=None, route_fingerprint=None, route_epoch=None,
        next_attempt_at=None, finished_at=NOW,
    ))
    db.flush()
