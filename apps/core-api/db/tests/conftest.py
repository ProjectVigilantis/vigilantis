# ==============================================================================
# [파일 설명]  담당: 안성일 (AI/Guardrail · Architect)
# db 계층 테스트 픽스처. (Issue #60)
#
#   - import 경로: services/tests와 같은 방식(apps/core-api·packages 삽입,
#     저장소 루트 삽입 금지 — schemas identity 분열 방지).
#   - PostgreSQL 통합 테스트: 일회용 DB(vigilantis_test)를 만들고 Alembic
#     upgrade head로 스키마를 적용한다 — create_all을 쓰지 않는다.
#   - DB 미기동 시 통합 테스트만 skip한다(collector의 LocalStack skip과 동일
#     방식). Mapper·메타데이터 테스트는 DB 없이 항상 실행된다.
#   - 테스트 격리: 테스트마다 외부 트랜잭션을 열고 끝나면 rollback한다
#     (Repository는 commit하지 않으므로 성립).
# ==============================================================================

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

CORE_API = Path(__file__).resolve().parents[2]
REPO_ROOT = Path(__file__).resolve().parents[4]
for p in (str(CORE_API), str(REPO_ROOT / "packages")):
    if p not in sys.path:
        sys.path.insert(0, p)

TEST_DB_NAME = "vigilantis_test"
# compose db 서비스(localhost:5432) 기본값 — 환경변수로 재지정 가능
ADMIN_URL = os.getenv(
    "TEST_DATABASE_ADMIN_URL",
    "postgresql+psycopg://vigilantis:vigilantis@localhost:5432/postgres",
)
TEST_URL = ADMIN_URL.rsplit("/", 1)[0] + "/" + TEST_DB_NAME


def _postgres_available() -> bool:
    from sqlalchemy import create_engine, text

    try:
        engine = create_engine(ADMIN_URL, connect_args={"connect_timeout": 2})
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        engine.dispose()
        return True
    except Exception:
        return False


@pytest.fixture(scope="session")
def pg_engine():
    """일회용 테스트 DB 생성 → Alembic head 적용 → 세션 종료 시 DB 삭제."""
    if not _postgres_available():
        pytest.skip("PostgreSQL(localhost:5432) 미기동 — 통합 테스트 skip")

    from sqlalchemy import create_engine, text

    admin = create_engine(ADMIN_URL, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{TEST_DB_NAME}"'))
        conn.execute(text(f'CREATE DATABASE "{TEST_DB_NAME}"'))

    os.environ["DATABASE_URL"] = TEST_URL
    from config import get_settings

    get_settings.cache_clear()

    from alembic import command
    from alembic.config import Config

    command.upgrade(Config(str(CORE_API / "alembic.ini")), "head")

    engine = create_engine(TEST_URL)
    yield engine
    engine.dispose()

    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{TEST_DB_NAME}" WITH (FORCE)'))
    admin.dispose()


@pytest.fixture()
def db(pg_engine):
    """외부 트랜잭션에 묶인 세션 — 테스트 종료 시 전부 rollback."""
    from sqlalchemy.orm import Session

    conn = pg_engine.connect()
    trans = conn.begin()
    session = Session(bind=conn, autoflush=False, expire_on_commit=False)
    try:
        yield session
    finally:
        session.close()
        if trans.is_active:
            trans.rollback()
        conn.close()
