"""플랫폼 이벤트 대상 종류 TENANT_GROUP migration (#589 B3) — 실제 PostgreSQL/Alembic.

지키는 것: 올린 DB 의 platform_event_target_type 라벨이 공유 계약 enum 과 같다 ·
기존 라벨은 그대로 · 표 구성은 바뀌지 않는다 · downgrade 뒤에도 라벨이 남고 다시 upgrade 해도 안전하다.
테스트마다 고유 이름의 일회용 DB 를 만들고 지운다(공용 pg_engine DB 의 head 를 건드리지 않는다).
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from config import get_settings
from schemas.platform_events import PlatformEventTargetType
from sqlalchemy import create_engine, inspect, text

REVISION = "e589b3a1c204"
ADDED = "TENANT_GROUP"


@pytest.fixture()
def scratch_db(pg_engine, monkeypatch):
    """(Config, engine) — 같은 서버의 새 빈 DB. 끝나면 DROP."""
    name = f"vigilantis_event_target_{uuid.uuid4().hex[:8]}"
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


def _labels(engine) -> list[str]:
    with engine.connect() as conn:
        return list(
            conn.execute(text("SELECT unnest(enum_range(NULL::platform_event_target_type))::text")).scalars()
        )


def _columns(engine) -> dict[str, list[str]]:
    inspector = inspect(engine)
    return {t: [c["name"] for c in inspector.get_columns(t)] for t in sorted(inspector.get_table_names())}


def _parent(config: Config) -> str:
    # 부모는 재부모화될 수 있으므로 리비전 파일에서 읽는다
    return ScriptDirectory.from_config(config).get_revision(REVISION).down_revision


def test_upgrade_adds_only_the_label_and_matches_the_shared_contract(scratch_db):
    config, engine = scratch_db
    command.upgrade(config, _parent(config))
    before_labels = _labels(engine)
    before_columns = _columns(engine)
    assert ADDED not in before_labels

    command.upgrade(config, REVISION)

    after_labels = _labels(engine)
    assert set(after_labels) - set(before_labels) == {ADDED}
    assert set(before_labels) <= set(after_labels)
    assert _columns(engine) == before_columns
    assert set(after_labels) == {member.value for member in PlatformEventTargetType}


def test_added_label_survives_downgrade_and_reupgrade(scratch_db):
    config, engine = scratch_db
    command.upgrade(config, REVISION)
    labels = _labels(engine)

    # PG 는 enum 값을 안전하게 지우지 못한다 — downgrade 는 값을 남긴다
    command.downgrade(config, _parent(config))
    assert _labels(engine) == labels
    assert ADDED in _labels(engine)

    command.upgrade(config, REVISION)
    assert _labels(engine) == labels
