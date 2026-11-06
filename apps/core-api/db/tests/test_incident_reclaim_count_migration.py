"""incidents.agent_reclaim_count 마이그레이션 (#434 §4-2) — 실제 PostgreSQL/Alembic.

기존 Incident는 남고 새 칸은 0(회수한 적 없음)이다 — 지어낸 횟수를 채우지 않는다. 왕복 뒤 ORM과 같다.
"""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import delete, inspect, select, text
from sqlalchemy.orm import Session

from config import get_settings
from db import models
from schemas.api.incidents import IncidentCategory, IncidentStatus, RiskLevel

REVISION = "a1b7c4d2e905"
PARENT = "b434c7e2a915"


def test_existing_incidents_survive_with_zero_reclaims(pg_engine, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", pg_engine.url.render_as_string(hide_password=False))
    get_settings.cache_clear()
    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    script = ScriptDirectory.from_config(config)
    assert len(script.get_heads()) == 1
    assert script.get_revision(REVISION).down_revision == PARENT

    command.downgrade(config, PARENT)
    try:
        assert "agent_reclaim_count" not in {
            c["name"] for c in inspect(pg_engine).get_columns("incidents")}
        # 옛 리비전에는 ORM이 아니라 그때의 칸으로 넣는다
        with pg_engine.begin() as conn:
            incident_id = conn.execute(text(
                "INSERT INTO incidents (incident_id, subject_arn, category, status, title,"
                " initial_risk_level, initial_risk_reason_codes, summary_lines,"
                " agent_invocation_status, created_at, updated_at)"
                " VALUES (gen_random_uuid(),"
                " 'arn:aws:ec2:ap-northeast-2:123456789012:instance/i-reclaim', 'SECOPS',"
                " 'ANALYZING', '위협', 'LOW', '[\"TEST\"]'::jsonb, '[]'::jsonb, 'IN_PROGRESS',"
                " now(), now()) RETURNING incident_id::text"
            )).scalar_one()
    finally:
        command.upgrade(config, "head")
        get_settings.cache_clear()
    try:
        with pg_engine.connect() as conn:
            stored = conn.execute(
                select(models.Incident.agent_invocation_status, models.Incident.agent_reclaim_count)
                .where(models.Incident.incident_id == incident_id)
            ).one()
            assert (stored.agent_invocation_status.value, stored.agent_reclaim_count) == (
                "IN_PROGRESS", 0
            )
            assert compare_metadata(MigrationContext.configure(conn), models.Base.metadata) == []
        # ORM으로 새로 만든 행도 0에서 시작한다
        with Session(pg_engine) as session:
            fresh = models.Incident(
                title="위협", subject_arn="arn:aws:ec2:ap-northeast-2:123456789012:instance/i-new",
                category=IncidentCategory.SECOPS, status=IncidentStatus.ANALYZING,
                initial_risk_level=RiskLevel.LOW, initial_risk_reason_codes=["TEST"],
            )
            session.add(fresh)
            session.commit()
            fresh_id = fresh.incident_id
            session.expire_all()
            assert session.get(models.Incident, fresh_id).agent_reclaim_count == 0
    finally:
        with Session(pg_engine) as session:
            session.execute(delete(models.Incident).where(
                models.Incident.incident_id.in_([incident_id, fresh_id])))
            session.commit()
