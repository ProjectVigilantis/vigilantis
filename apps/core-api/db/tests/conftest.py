# ==============================================================================
# [파일 설명]
# db 계층 테스트 픽스처. (Issue #60)
#
#   - import 경로: services/tests와 같은 방식(apps/core-api·packages 삽입,
#     저장소 루트 삽입 금지 — schemas identity 분열 방지).
#   - PostgreSQL 통합 테스트: 실행마다 고유 이름의 일회용 DB를 새로 만들고
#     Alembic upgrade head로 스키마를 적용한다 — create_all을 쓰지 않는다.
#     기존 DB를 DROP하지 않는다(고정 이름 선-DROP은 수동 생성 DB를 지울 수 있음).
#   - DB 미기동 시 통합 테스트만 skip한다(collector의 LocalStack skip과 동일
#     방식). Mapper·메타데이터 테스트는 DB 없이 항상 실행된다.
#   - 테스트 격리: 테스트마다 외부 트랜잭션을 열고 끝나면 rollback한다
#     (Repository는 commit하지 않으므로 성립).
# ==============================================================================

from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

import pytest

CORE_API = Path(__file__).resolve().parents[2]
REPO_ROOT = Path(__file__).resolve().parents[4]
for p in (str(CORE_API), str(REPO_ROOT / "packages")):
    if p not in sys.path:
        sys.path.insert(0, p)


def _host_db_port() -> str:
    """호스트에서 compose `db` 서비스에 붙을 포트.

    실제 환경변수 > 루트 `.env`의 `POSTGRES_PORT` > 5432 순으로 고른다. compose가
    호스트 포트를 `${POSTGRES_PORT:-5432}`로 열기 때문에, `.env`만 고친 팀원의 pytest가
    5432를 보고 **조용히 skip되고 초록불이 나는** 것을 막는다 — #92가 CI에서 막은
    사각지대의 로컬판이다. CI에는 `.env`가 없어 기본값 5432로 떨어진다. (Issue #111)

    `.env` 파싱은 pydantic-settings(`env_file=`)가 내부에서 쓰는 python-dotenv다.
    """
    from dotenv import dotenv_values

    return (
        os.getenv("POSTGRES_PORT")
        or dotenv_values(REPO_ROOT / ".env").get("POSTGRES_PORT")
        or "5432"
    )


# 접속 대상 — 전체 재지정은 TEST_DATABASE_ADMIN_URL 하나로 계속 가능하다
DB_HOSTPORT = f"localhost:{_host_db_port()}"
ADMIN_URL = os.getenv(
    "TEST_DATABASE_ADMIN_URL",
    f"postgresql+psycopg://vigilantis:vigilantis@{DB_HOSTPORT}/postgres",
)
# skip 메시지가 실제 접속 대상을 적게 한다(자격증명 제외) — 포트를 바꿔도 거짓말하지 않게
PG_TARGET = ADMIN_URL.rsplit("@", 1)[-1].split("/")[0]

# 실행마다 고유 이름 — 이미 존재하는 어떤 DB와도 충돌·삭제가 일어나지 않는다
TEST_DB_NAME = f"vigilantis_test_{uuid.uuid4().hex[:8]}"
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
        pytest.skip(f"PostgreSQL({PG_TARGET}) 미기동 — 통합 테스트 skip")

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
    # create_savepoint 명시 — 기본값(conditional_savepoint)은 세션 commit 이후의
    # rollback이 외부 트랜잭션까지 되돌려, 그 전에 commit된 시드가 통째로 사라진다.
    # 프로덕션 rollback 경로(dispatcher 예외 처리 등)를 지나는 테스트가 걸린다 (Issue #232)
    session = Session(
        bind=conn,
        autoflush=False,
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )
    try:
        yield session
    finally:
        session.close()
        if trans.is_active:
            trans.rollback()
        conn.close()
