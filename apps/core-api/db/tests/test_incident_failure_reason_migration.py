"""incidents.agent_failure_reason 마이그레이션 (#434) — 실제 PostgreSQL/Alembic.

단일 head 위에 붙는다. 기존 인시던트는 남고 새 칸은 NULL(사유 기록 이전)이다 — 지어낸 사유를 채우지
않는다. 사유는 FAILED 호출에만 둘 수 있다. 왕복 뒤 ORM 과 같다.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from config import get_settings
from db import models
from sqlalchemy import inspect, select, text
from sqlalchemy.exc import IntegrityError

REVISION = "b434c7e2a915"
PARENT = "f528f565e001"
CONFIG_PATH = Path(__file__).resolve().parents[2] / "alembic.ini"


def test_revision_follows_the_previous_head_and_leads_to_the_single_head():
    script = ScriptDirectory.from_config(Config(str(CONFIG_PATH)))
    assert script.get_revision(REVISION).down_revision == PARENT
    [head] = script.get_heads()
    assert REVISION in {rev.revision for rev in script.walk_revisions(base="base", head=head)}


def test_existing_incidents_survive_with_null_reason(pg_engine, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", pg_engine.url.render_as_string(hide_password=False))
    get_settings.cache_clear()
    config = Config(str(CONFIG_PATH))
    incident_id = str(uuid.uuid4())
    command.downgrade(config, PARENT)
    try:
        assert "agent_failure_reason" not in {
            c["name"] for c in inspect(pg_engine).get_columns("incidents")}
        # 옛 리비전에는 ORM 이 아니라 그때의 칸으로 넣는다
        with pg_engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO incidents (incident_id, subject_arn, category, status, "
                    "initial_risk_reason_codes, summary_lines, agent_invocation_status, "
                    "created_at, updated_at) VALUES (:incident, "
                    "'arn:aws:ec2:ap-northeast-2:123456789012:instance/i-reason', "
                    "CAST('FINOPS' AS incident_category), CAST('FAILED' AS incident_status), "
                    "'[]'::jsonb, '[]'::jsonb, CAST('FAILED' AS agent_invocation_status), now(), now())"
                ),
                {"incident": incident_id},
            )
    finally:
        command.upgrade(config, "head")
        get_settings.cache_clear()
    try:
        with pg_engine.connect() as conn:
            assert conn.execute(
                select(models.Incident.agent_failure_reason)
                .where(models.Incident.incident_id == incident_id)
            ).scalar_one() is None
            assert compare_metadata(MigrationContext.configure(conn), models.Base.metadata) == []
        with pg_engine.begin() as conn:
            conn.execute(
                text("UPDATE incidents SET agent_failure_reason = 'NO_ACTION_AVAILABLE' WHERE incident_id = :i"),
                {"i": incident_id},
            )
        # FAILED가 아닌 호출 상태에 사유가 남으면 조회가 성공한 분석에 실패 사유를 붙인다
        with pytest.raises(IntegrityError, match="failure_reason_when_failed"), pg_engine.begin() as conn:
            conn.execute(
                text("UPDATE incidents SET agent_invocation_status = 'NO_PROPOSAL' WHERE incident_id = :i"),
                {"i": incident_id},
            )
    finally:
        with pg_engine.begin() as conn:
            conn.execute(text("DELETE FROM incidents WHERE incident_id = :i"), {"i": incident_id})
