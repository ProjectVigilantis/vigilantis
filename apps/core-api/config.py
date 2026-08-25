# ==============================================================================
# [파일 설명]
# pydantic-settings 기반 런타임 환경설정 로더입니다. (Issue #60·#68·#128, ADR-0001)
#
# 설정 단위를 셋으로 나눈다 — 필수 항목이 서로 다르기 때문이다.
#   Settings          : Core API 프로세스 설정. DATABASE_URL 필수.
#   AwsSettings       : AWS 접근 설정(리전·엔드포인트). DB와 무관.
#   CollectorSettings : CloudWatch 조회 창 설정.
#
# AWS 설정을 Settings에 합치지 않는 이유: scripts/seed_localstack.py와
# scripts/probe_dryrun.py(#130)는 DB 없이 도는 스크립트인데, 합치면 AWS 클라이언트
# 하나 만드는 데 DATABASE_URL이 필요해진다.
#
# 필수 설정 누락 시 get_*() 호출 단계에서 검증 오류가 난다. SQLite 등으로 조용히
# 대체하지 않는다(SSOT 확정 범위가 PostgreSQL). OPENAI_API_KEY 등 나머지 설정은
# 해당 기능을 붙이는 단계에서 추가한다.
# ==============================================================================

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# .env에는 POSTGRES_USER 등 다른 서비스용 변수도 있으므로 extra는 무시한다.
# compose의 api·migrate 서비스는 env_file로 .env를 실제 환경변수로 주입하므로,
# 여기서 .env를 읽는 것은 호스트 직접 실행 편의를 위한 것이다.
_ENV_FILE = SettingsConfigDict(env_file=".env", extra="ignore")

# AWS·수집 설정은 .env를 읽지 않는다 — .env의 AWS_ENDPOINT_URL·DATABASE_URL은
# compose 네트워크 안에서만 유효한 호스트명(localstack·db)이다. 호스트에서 직접
# 도는 시드·실측 스크립트가 그 값을 집으면 붙지도 않는 대상에 조용히 붙으려 한다.
# 컨테이너 안에서는 env_file이 이미 실제 환경변수로 넣어 주므로 손해가 없다.
_ENV_ONLY = SettingsConfigDict(extra="ignore")


class Settings(BaseSettings):
    """프로세스 전역 런타임 설정. 환경변수(또는 루트 .env)에서 읽는다."""

    model_config = _ENV_FILE

    DATABASE_URL: str
    LOG_LEVEL: str = "INFO"
    # 콤마 구분 허용 출처 목록 — 기본값은 FE 개발 서버. CORS와 WebSocket
    # Handshake Origin 검증이 같은 목록을 쓴다
    CORS_ALLOW_ORIGINS: str = "http://localhost:3000"
    # WebSocket 연결별 전송 제한시간(초) — 초과한 연결은 발행에서 제거.
    # 제거·종료 시 연결 close() 정리도 같은 값을 상한으로 쓴다.
    # 0 이하면 모든 연결이 즉시 제거되므로 양수만 허용한다 (Issue #75)
    WS_SEND_TIMEOUT_SECONDS: float = Field(default=5.0, gt=0)

    def cors_allow_origins_list(self) -> list[str]:
        return [
            origin.strip()
            for origin in self.CORS_ALLOW_ORIGINS.split(",")
            if origin.strip()
        ]


class AwsSettings(BaseSettings):
    """AWS 접근 설정 — 수집·실행·시드가 공유한다.

    AWS_ENDPOINT_URL은 ADR-0006 §3의 전환 스위치다(값이 있으면 LocalStack, 없으면
    실 AWS). **이 값을 해석하는 코드는 services/aws/client.py 하나뿐이어야 한다** —
    같은 조건이 여러 곳에 복사되는 순간 §3의 "코드 분기 금지"는 지킬 수 없게 된다.
    """

    model_config = _ENV_ONLY

    AWS_REGION: str = "ap-northeast-2"
    # 복수 리전은 콤마 구분. 비어 있으면 AWS_REGION 단일 값으로 해석한다.
    AWS_REGIONS: str = ""
    AWS_ENDPOINT_URL: str = ""

    def regions_list(self) -> list[str]:
        raw = self.AWS_REGIONS.strip() or self.AWS_REGION
        return [region.strip() for region in raw.split(",") if region.strip()]

    def endpoint_url(self) -> str | None:
        """LocalStack 엔드포인트. 실 AWS면 None."""
        return self.AWS_ENDPOINT_URL.strip() or None


class CollectorSettings(BaseSettings):
    """CloudWatch 조회 창 설정 — 수집 비용·판정 신뢰도에 직접 영향을 준다."""

    model_config = _ENV_ONLY

    METRIC_LOOKBACK_DAYS: int = Field(default=14, gt=0)
    METRIC_PERIOD_SECONDS: int = Field(default=3600, gt=0)


@lru_cache
def get_settings() -> Settings:
    """검증된 Settings 싱글턴. 테스트는 cache_clear() 후 환경변수로 주입한다."""
    return Settings()


@lru_cache
def get_aws_settings() -> AwsSettings:
    """검증된 AwsSettings 싱글턴. 테스트는 cache_clear() 후 환경변수로 주입한다."""
    return AwsSettings()


@lru_cache
def get_collector_settings() -> CollectorSettings:
    """검증된 CollectorSettings 싱글턴. 테스트는 cache_clear() 후 주입한다."""
    return CollectorSettings()
