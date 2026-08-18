# ==============================================================================
# [파일 설명]
# pydantic-settings 기반 런타임 환경설정 로더입니다. (Issue #60, ADR-0001)
#
# 현재 범위: DATABASE_URL 1개 — 기본값 없는 필수 설정. 누락 시 get_settings()
# 호출 단계에서 검증 오류가 난다. SQLite 등으로 조용히 대체하지 않는다
# (SSOT 확정 범위가 PostgreSQL). OPENAI_API_KEY·AWS 리전 등 나머지 설정은
# 해당 기능을 붙이는 단계에서 추가한다.
# ==============================================================================

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """프로세스 전역 런타임 설정. 환경변수(또는 루트 .env)에서 읽는다."""

    # .env에는 POSTGRES_USER 등 다른 서비스용 변수도 있으므로 extra는 무시한다
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    DATABASE_URL: str


@lru_cache
def get_settings() -> Settings:
    """검증된 Settings 싱글턴. 테스트는 cache_clear() 후 환경변수로 주입한다."""
    return Settings()
