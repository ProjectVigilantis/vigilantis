# ==============================================================================
# Alembic 실행 환경. (Issue #60)
#   - import 경로: 저장소 테스트와 같은 방식으로 apps/core-api·packages를 삽입한다
#     (저장소 루트 삽입 금지 — schemas 모듈 identity가 갈라진다).
#   - 접속 URL: config.get_settings().DATABASE_URL 단일 경로. SQLite 대체 없음.
#   - 대상 메타데이터: db.models.Base.metadata (ORM 13종).
# ==============================================================================

from __future__ import annotations

import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import create_engine, pool

# env.py 위치: apps/core-api/db/migrations/env.py
_CORE_API = Path(__file__).resolve().parents[2]
_PACKAGES = Path(__file__).resolve().parents[4] / "packages"
for p in (str(_CORE_API), str(_PACKAGES)):
    if p not in sys.path:
        sys.path.insert(0, p)

from config import get_settings  # noqa: E402
from db.models import Base  # noqa: E402  (모델 등록 포함)

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """DB 연결 없이 SQL 스크립트만 출력한다 (--sql 모드)."""
    context.configure(
        url=get_settings().DATABASE_URL,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(get_settings().DATABASE_URL, poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
