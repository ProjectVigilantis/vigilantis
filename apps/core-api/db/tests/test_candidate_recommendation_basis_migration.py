"""후보 추천 근거 칸 migration (#598) — 실제 PostgreSQL/Alembic.

지키는 것: 옛 후보는 upgrade 뒤 NULL 근거로 남는다 · 저장한 근거는 mapper 왕복에서 그대로다 ·
없음은 SQL NULL 이다 · 근거가 있으면 downgrade 가 데이터를 지우지 않고 멈춘다.
테스트마다 고유 이름의 일회용 DB 를 만들고 지운다(공용 pg_engine DB 의 head 를 건드리지 않는다).
"""

from __future__ import annotations

import threading
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from config import get_settings
from db import mappers
from schemas.candidates import CandidateStatus, RunbookCandidateData
from schemas.recommendations import (
    IsolationRecommendationBasis,
    IsolationRecommendationClassification,
    IsolationRecommendationReason,
)
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

REVISION = "f598b2c3d402"
PARENT = "e598a1b2c301"
ARN = "arn:aws:ec2:ap-northeast-2:123456789012:instance/i-0abc"
LEGACY, WITH_BASIS, NO_BASIS = (str(uuid.uuid4()) for _ in range(3))
EVALUATED_AT = datetime(2026, 11, 13, 1, 2, 3, 123456, tzinfo=UTC)


@pytest.fixture()
def scratch_db(pg_engine, monkeypatch):
    """(Config, engine) — 같은 서버의 새 빈 DB. 끝나면 DROP."""
    name = f"vigilantis_cand_basis_{uuid.uuid4().hex[:8]}"
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


def _incident(candidate_id: str) -> str:
    # (incident_id, runbook_id)는 활성 후보 하나만 허용하므로 후보마다 사건을 따로 둔다
    return str(uuid.uuid5(uuid.NAMESPACE_URL, candidate_id))


def _basis() -> IsolationRecommendationBasis:
    return IsolationRecommendationBasis(
        classification=IsolationRecommendationClassification.CONDITIONALLY_RECOMMENDED,
        reason_code=IsolationRecommendationReason.GUARDDUTY_SSH_BRUTE_FORCE_ACTOR,
        evaluated_at=EVALUATED_AT,
    )


def _contract(candidate_id: str, basis=None) -> RunbookCandidateData:
    return RunbookCandidateData(
        candidate_id=candidate_id, incident_id=_incident(candidate_id), runbook_id="RUNBOOK_EC2_ISOLATE",
        target_arn=ARN, parameters={}, evidence_ids=["ev-1"],
        status=CandidateStatus.PENDING_VALIDATION, recommendation_basis=basis,
    )


def _persist(engine, contract: RunbookCandidateData) -> None:
    with Session(engine) as session:
        # 이 시험은 후보 칸만 본다 — incidents 행을 만들지 않으려고 FK 검사를 이 연결에서 끈다
        session.execute(text("SET session_replication_role = replica"))
        session.add(mappers.new_candidate(contract))
        session.commit()


def _insert_legacy(engine, candidate_id: str) -> None:
    """revision 이전 스키마(칸 없음)에 직접 넣은 옛 후보."""
    with engine.begin() as conn:
        conn.execute(text("SET session_replication_role = replica"))
        conn.execute(
            text(
                "INSERT INTO runbook_candidates (candidate_id, incident_id, runbook_id, target_arn,"
                " parameters, display_parameters, evidence_ids, status,"
                " created_at, updated_at)"
                " VALUES (:id, :incident, 'RUNBOOK_EC2_ISOLATE', :arn, '{}', '{}', '[\"ev-1\"]',"
                " 'PENDING_VALIDATION', now(), now())"
            ),
            {"id": candidate_id, "incident": _incident(candidate_id), "arn": ARN},
        )


def _raw_basis(engine, candidate_id: str):
    with engine.connect() as conn:
        return conn.execute(
            text(
                "SELECT recommendation_basis IS NULL, recommendation_basis"
                " FROM runbook_candidates WHERE candidate_id = :id"
            ),
            {"id": candidate_id},
        ).one()


def _has_column(engine) -> bool:
    with engine.connect() as conn:
        return bool(
            conn.execute(
                text(
                    "SELECT count(*) FROM information_schema.columns WHERE"
                    " table_name = 'runbook_candidates' AND column_name = 'recommendation_basis'"
                )
            ).scalar_one()
        )


def test_revision_follows_the_exact_parent_and_history_has_one_head(scratch_db):
    config, _ = scratch_db
    script = ScriptDirectory.from_config(config)
    assert script.get_revision(REVISION).down_revision == PARENT
    assert len(script.get_heads()) == 1


def test_old_candidates_survive_upgrade_with_null_basis(scratch_db):
    config, engine = scratch_db
    command.upgrade(config, PARENT)
    _insert_legacy(engine, LEGACY)

    command.upgrade(config, REVISION)

    assert _raw_basis(engine, LEGACY)[0] is True
    with Session(engine) as session:
        from db import models

        row = session.get(models.RunbookCandidate, LEGACY)
        assert mappers.to_candidate_data(row).recommendation_basis is None


def test_persisted_basis_survives_the_mapper_round_trip(scratch_db):
    config, engine = scratch_db
    command.upgrade(config, REVISION)
    contract = _contract(WITH_BASIS, _basis())
    _persist(engine, contract)

    is_null, stored = _raw_basis(engine, WITH_BASIS)
    assert is_null is False
    assert stored == {
        "origin": "SERVER_RULE", "rule_id": "PROTECTED_EC2_ISOLATION", "rule_version": 1,
        "classification": "CONDITIONALLY_RECOMMENDED",
        "reason_code": "GUARDDUTY_SSH_BRUTE_FORCE_ACTOR",
        "evaluated_at": "2026-11-13T01:02:03.123456Z",
    }
    with Session(engine) as session:
        from db import models

        restored = mappers.to_candidate_data(session.get(models.RunbookCandidate, WITH_BASIS))
    assert restored == contract
    assert restored.recommendation_basis.evaluated_at == EVALUATED_AT


def test_absent_basis_is_sql_null_not_json_null(scratch_db):
    config, engine = scratch_db
    command.upgrade(config, REVISION)
    _persist(engine, _contract(NO_BASIS))
    assert _raw_basis(engine, NO_BASIS)[0] is True


def test_downgrade_cannot_erase_a_basis_committed_while_it_waits(scratch_db):
    """쓰기 트랜잭션이 열린 채 downgrade 가 시작되면 잠금에서 기다리고, 커밋된 근거를 본 뒤 멈춘다."""
    config, engine = scratch_db
    command.upgrade(config, REVISION)
    outcome: list[BaseException | None] = []

    def downgrade():
        try:
            command.downgrade(config, PARENT)
            outcome.append(None)
        except BaseException as exc:  # noqa: BLE001 — 스레드 밖에서 판정한다
            outcome.append(exc)

    with Session(engine) as writer:
        writer.execute(text("SET session_replication_role = replica"))
        writer.add(mappers.new_candidate(_contract(WITH_BASIS, _basis())))
        writer.flush()  # 아직 커밋 전 — 근거 행은 downgrade 의 COUNT 에 보이지 않는다
        thread = threading.Thread(target=downgrade)
        thread.start()
        # downgrade 가 이 쓰기에 막혀 잠금 대기에 들어간 것을 확인한 뒤에만 커밋한다(sleep 없음)
        with engine.connect() as probe:
            waiting = (
                "SELECT count(*) FROM pg_locks WHERE NOT granted"
                " AND relation = 'runbook_candidates'::regclass"
            )
            deadline = time.monotonic() + 30
            while not probe.execute(text(waiting)).scalar_one():
                probe.rollback()
                assert thread.is_alive() and time.monotonic() < deadline
        writer.commit()
    thread.join(30)

    assert not thread.is_alive()
    assert len(outcome) == 1 and isinstance(outcome[0], RuntimeError)
    assert "recommendation_basis" in str(outcome[0])
    assert _has_column(engine)
    assert _raw_basis(engine, WITH_BASIS)[0] is False


def test_downgrade_stops_while_a_basis_exists_then_drops_the_column(scratch_db):
    config, engine = scratch_db
    command.upgrade(config, REVISION)
    _persist(engine, _contract(WITH_BASIS, _basis()))
    _persist(engine, _contract(NO_BASIS))

    with pytest.raises(RuntimeError, match="recommendation_basis"):
        command.downgrade(config, PARENT)
    assert _has_column(engine)
    assert _raw_basis(engine, WITH_BASIS)[0] is False

    # 사람이 근거를 따로 처리한 뒤에는 내려가고, 후보 행은 그대로 남는다
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM runbook_candidates WHERE candidate_id = :id"), {"id": WITH_BASIS})
    command.downgrade(config, PARENT)
    assert not _has_column(engine)
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM runbook_candidates")).scalar_one() == 1
