# ==============================================================================
# [파일 설명]
# 앱·라우터 테스트 픽스처. (Issue #68)
#
#   - import 경로: db/tests와 같은 방식(apps/core-api·packages 삽입, 저장소
#     루트 삽입 금지 — schemas identity 분열 방지).
#   - DB 비의존 테스트(client): 더미 DATABASE_URL로 앱만 구성한다. 엔진은
#     lazy라 접속이 일어나지 않는다 — /health·오류 봉투·로깅 검증용.
#   - PostgreSQL 통합(client_pg): db/tests/conftest.py와 같은 일회용 DB +
#     Alembic upgrade head 방식. DB 미기동 시 통합 테스트만 skip한다.
#     라우터에는 get_db 의존성 재정의로 테스트 세션을 주입한다(테스트 종료 시
#     외부 트랜잭션 rollback — Repository는 commit하지 않으므로 성립).
# ==============================================================================

from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

import pytest

CORE_API = Path(__file__).resolve().parents[1]
REPO_ROOT = Path(__file__).resolve().parents[3]
for p in (str(CORE_API), str(REPO_ROOT / "packages")):
    if p not in sys.path:
        sys.path.insert(0, p)

# DB 비의존 테스트용 더미 — 엔진 생성만 되고 접속은 일어나지 않는다
os.environ.setdefault(
    "DATABASE_URL", "postgresql+psycopg://vigilantis:vigilantis@localhost:5432/vigilantis"
)

TEST_DB_NAME = f"vigilantis_test_{uuid.uuid4().hex[:8]}"
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


def _build_app():
    """환경변수 반영을 위해 설정 캐시를 비우고 앱을 새로 만든다."""
    from config import get_settings

    get_settings.cache_clear()
    from main import create_app

    return create_app()


@pytest.fixture()
def client():
    """DB 비의존 클라이언트 — DB에 접속하는 엔드포인트 검증에 쓰지 않는다."""
    from fastapi.testclient import TestClient

    with TestClient(_build_app()) as test_client:
        yield test_client


@pytest.fixture(scope="session")
def pg_engine():
    """일회용 테스트 DB 생성 → Alembic head 적용 → 세션 종료 시 DB 삭제."""
    if not _postgres_available():
        pytest.skip("PostgreSQL(localhost:5432) 미기동 — 통합 테스트 skip")

    from sqlalchemy import create_engine, text

    admin = create_engine(ADMIN_URL, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
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


@pytest.fixture()
def client_pg(pg_engine, db):
    """통합 클라이언트 — 라우터의 get_db를 테스트 세션으로 재정의한다."""
    from fastapi.testclient import TestClient

    import db.session as db_session_module

    app = _build_app()

    def _override_get_db():
        yield db

    app.dependency_overrides[db_session_module.get_db] = _override_get_db
    with TestClient(app) as test_client:
        yield test_client
