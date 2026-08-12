# ==============================================================================
# [파일 설명]
# SQLAlchemy 엔진 및 세션 구성 파일입니다. PostgreSQL 연결과 FastAPI 의존성
# 주입용 세션 제너레이터를 제공합니다.
#
# 구현: DeclarativeBase(Base), engine, SessionLocal, get_db(), init_db().
#   DATABASE_URL 은 임시로 환경변수에서 읽는다(기본값 sqlite, 로컬/LocalStack 개발용).
#   config.get_settings() 확정 시 DATABASE_URL 주입부만 교체하면 된다. (TODO: config)
# ==============================================================================

from __future__ import annotations

import os

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

# TODO(config): get_settings().DATABASE_URL 로 교체. Postgres 예:
#   postgresql+psycopg://user:pass@localhost:5432/vigilantis
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./vigilantis_dev.db")

_connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, future=True, pool_pre_ping=True, connect_args=_connect_args)

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, class_=Session)


class Base(DeclarativeBase):
    """models.py 의 모든 ORM 모델이 상속하는 declarative base."""


def init_db() -> None:
    """등록된 모든 테이블을 생성한다(개발/테스트용). 운영은 Alembic 마이그레이션 사용."""
    from . import models  # noqa: F401  (모델 등록을 위해 import)

    Base.metadata.create_all(bind=engine)


def get_db():
    """FastAPI 의존성 주입용 세션 제너레이터."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
