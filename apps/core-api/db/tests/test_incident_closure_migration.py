"""추가 조치 없는 종료의 PostgreSQL migration·ORM 일치와 손실 방지."""

from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import delete, text
from sqlalchemy.orm import Session

from config import get_settings
from db import models
from schemas.api.incidents import IncidentCategory, IncidentStatus, ResolutionJudgement, RiskLevel


@pytest.fixture()
def migration_config(pg_engine, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", pg_engine.url.render_as_string(hide_password=False))
    get_settings.cache_clear()
    yield Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    get_settings.cache_clear()


def test_closure_migration_roundtrip_matches_orm(pg_engine, migration_config):
    command.downgrade(migration_config, "c7a9e1d83f24")
    command.upgrade(migration_config, "head")
    with pg_engine.connect() as connection:
        assert compare_metadata(MigrationContext.configure(connection), models.Base.metadata) == []
        assert connection.execute(text(
            "SELECT unnest(enum_range(NULL::resolution_judgement))::text"
        )).scalars().all() == [item.value for item in ResolutionJudgement]


@pytest.mark.parametrize("judgement, note", [
    (ResolutionJudgement.NO_FURTHER_ACTION, None),
    (ResolutionJudgement.JUSTIFIED, "종료 사유 보존"),
])
def test_downgrade_refuses_to_discard_closure_data(pg_engine, migration_config, judgement, note):
    with Session(pg_engine) as session:
        incident = models.Incident(
            title="위협", subject_arn="arn:aws:ec2:ap-northeast-2:123456789012:instance/i-test",
            category=IncidentCategory.SECOPS, status=IncidentStatus.RESOLVED,
            initial_risk_level=RiskLevel.LOW, initial_risk_reason_codes=["TEST"],
            resolution=judgement, resolution_note=note, resolved_at=models._utcnow(),
        )
        session.add(incident)
        session.commit()
        incident_id = incident.incident_id
    try:
        with pytest.raises(RuntimeError, match="손실 없는 downgrade"):
            command.downgrade(migration_config, "c7a9e1d83f24")
        with Session(pg_engine) as session:
            stored = session.get(models.Incident, incident_id)
            assert (stored.resolution, stored.resolution_note) == (judgement, note)
            # 거절된 downgrade 는 통째로 되돌아가 head 에 머문다. 리비전 문자열을 적어 두면
            # 뒤에 마이그레이션이 붙을 때마다 이 줄을 고쳐야 한다(test_repositories 와 같은 이유).
            head = ScriptDirectory.from_config(migration_config).get_current_head()
            assert session.scalar(text("SELECT version_num FROM alembic_version")) == head
    finally:
        with Session(pg_engine) as session:
            session.execute(delete(models.Incident).where(models.Incident.incident_id == incident_id))
            session.commit()
