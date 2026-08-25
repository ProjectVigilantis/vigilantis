# ==============================================================================
# [파일 설명]
# pydantic-settings 기반 런타임 환경설정 로더입니다. (Issue #60·#68·#115, ADR-0001)
#
# 현재 범위: DATABASE_URL(필수) + 로깅·CORS·WebSocket·OpenAI(기본값 있음). 필수 설정
# 누락 시 get_settings() 호출 단계에서 검증 오류가 난다. SQLite 등으로 조용히 대체하지
# 않는다(SSOT 확정 범위가 PostgreSQL). AWS 리전 등 나머지 설정은 해당 기능을 붙이는
# 단계에서 추가한다.
# ==============================================================================

from __future__ import annotations

from functools import lru_cache
from typing import Optional

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """프로세스 전역 런타임 설정. 환경변수(또는 루트 .env)에서 읽는다."""

    # .env에는 POSTGRES_USER 등 다른 서비스용 변수도 있으므로 extra는 무시한다
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    DATABASE_URL: str
    LOG_LEVEL: str = "INFO"
    # 콤마 구분 허용 출처 목록 — 기본값은 FE 개발 서버. CORS와 WebSocket
    # Handshake Origin 검증이 같은 목록을 쓴다
    CORS_ALLOW_ORIGINS: str = "http://localhost:3000"
    # WebSocket 연결별 전송 제한시간(초) — 초과한 연결은 발행에서 제거.
    # 제거·종료 시 연결 close() 정리도 같은 값을 상한으로 쓴다.
    # 0 이하면 모든 연결이 즉시 제거되므로 양수만 허용한다 (Issue #75)
    WS_SEND_TIMEOUT_SECONDS: float = Field(default=5.0, gt=0)

    # --- AI 모델 호출 (Issue #115) ---
    # 키는 Optional이다 — AI 호출 경로가 앱에 배선되기 전이라 키 없이도 기동해야 하고,
    # 누락은 실제 클라이언트를 만드는 build_openai_model_client()가 거절한다.
    OPENAI_API_KEY: Optional[str] = None
    OPENAI_MODEL: str = "gpt-4o"
    OPENAI_TIMEOUT_SECONDS: float = Field(default=30.0, gt=0)
    # 재시도 대상은 일시 오류뿐이다(ai/openai_client.py). 1이면 재시도 없음
    OPENAI_MAX_ATTEMPTS: int = Field(default=3, ge=1)
    OPENAI_RETRY_BACKOFF_SECONDS: float = Field(default=1.0, ge=0)

    def cors_allow_origins_list(self) -> list[str]:
        return [
            origin.strip()
            for origin in self.CORS_ALLOW_ORIGINS.split(",")
            if origin.strip()
        ]


@lru_cache
def get_settings() -> Settings:
    """검증된 Settings 싱글턴. 테스트는 cache_clear() 후 환경변수로 주입한다."""
    return Settings()
