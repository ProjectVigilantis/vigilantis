# ==============================================================================
# [파일 설명]
# SQLAlchemy 엔진·세션 구성입니다. (Issue #60)
#
#   - DATABASE_URL은 config.get_settings() 경유로만 읽는다 — SQLite 기본값·
#     자동 대체 없음.
#   - 엔진·세션 팩토리는 import 시점이 아니라 첫 호출 시 생성한다.
#   - 스키마 생성·변경은 Alembic 마이그레이션 전용이다 — create_all 경로
#     (구 init_db)를 두지 않는다.
# ==============================================================================

from __future__ import annotations

from functools import lru_cache

from sqlalchemy import Engine, MetaData, create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from config import get_settings

# Alembic 마이그레이션에서 제약 이름이 결정적으로 나오도록 명명 규칙을 고정한다
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """models.py 의 모든 ORM 모델이 상속하는 declarative base."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


@lru_cache
def get_engine() -> Engine:
    """프로세스당 1개 엔진. 첫 호출 시 Settings를 읽어 생성한다."""
    return create_engine(get_settings().DATABASE_URL, pool_pre_ping=True)


@lru_cache
def get_session_factory() -> sessionmaker[Session]:
    """get_engine()에 바인딩된 세션 팩토리. 세션은 호출부가 만들고 닫는다."""
    return sessionmaker(bind=get_engine(), autoflush=False, expire_on_commit=False)


def get_db():
    """FastAPI 의존성 주입용 세션 제너레이터."""
    db = get_session_factory()()
    try:
        yield db
    finally:
        db.close()
