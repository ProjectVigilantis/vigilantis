# ==============================================================================
# [파일 설명]
# pydantic-settings 기반 런타임 환경설정 로더입니다. (Issue #60·#68·#115·#128, ADR-0001)
#
# 설정 단위를 셋으로 나눈다 — 필수 항목이 서로 다르기 때문이다.
#   Settings          : Core API 프로세스 설정. DATABASE_URL 필수이고
#                       로깅·CORS·WebSocket·OpenAI는 기본값이 있다.
#   AwsSettings       : AWS 접근 설정(리전·엔드포인트). DB와 무관.
#   CollectorSettings : CloudWatch 조회 창 설정.
#
# AWS 설정을 Settings에 합치지 않는 이유: scripts/seed_localstack.py와
# scripts/probe_dryrun.py(#130)는 DB 없이 도는 스크립트인데, 합치면 AWS 클라이언트
# 하나 만드는 데 DATABASE_URL이 필요해진다.
#
# 필수 설정 누락 시 get_*() 호출 단계에서 검증 오류가 난다. SQLite 등으로 조용히
# 대체하지 않는다(SSOT 확정 범위가 PostgreSQL).
# ==============================================================================

from __future__ import annotations

from functools import lru_cache
from typing import Literal, Optional

from pydantic import Field, field_validator
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
    # 접수된 조치 실행 디스패치·회수 스캔 주기(초) — dispatcher.py 잡 1개가 쓴다.
    # 승인부터 AWS 호출까지의 지연 상한이자 부분 인덱스
    # (ix_action_executions_non_terminal) 조회 빈도다 (Issue #232)
    DISPATCH_INTERVAL_SECONDS: int = Field(default=10, gt=0)
    # 스캔 잡 기동 스위치 — 테스트가 앱을 띄울 때 스캔이 따라 돌지 않게 끈다
    # (apps/core-api/tests/conftest.py, PR #236 리뷰)
    DISPATCH_ENABLED: bool = True
    # 2/2 Status Check 대기 — services/aws/rollback.py의 waiter 설정 (Issue #240).
    # 기본 15초×12회=3분으로 boto3 기본값(15초×40회=10분)보다 짧다. 판정 1건이
    # 그만큼 다음 스캔을 미루므로(max_instances=1) 시연에서 조일 수 있어야 한다.
    STATUS_CHECK_WAIT_DELAY_SECONDS: int = Field(default=15, gt=0)
    STATUS_CHECK_WAIT_MAX_ATTEMPTS: int = Field(default=12, ge=1)

    # --- AI 모델 호출 (Issue #115) ---
    # 키는 Optional이다 — AI 호출 경로가 앱에 배선되기 전이라 키 없이도 기동해야 하고,
    # 누락은 실제 클라이언트를 만드는 build_openai_model_client()가 거절한다.
    OPENAI_API_KEY: Optional[str] = None
    OPENAI_MODEL: str = "gpt-5.6-luna"
    # 모델 동작 노브 2종. **값이 있는 것만 호출에 실린다**(#237) — 모델 계열마다
    # 받는 파라미터가 다르기 때문이다. gpt-4o는 temperature를 받고 reasoning_effort가
    # 없으며, gpt-5 계열 추론 모델은 그 반대다. 받지 않는 쪽을 켜면 호출이 400으로
    # 거절된다(ai/openai_client.py가 AIModelRejectedError로 옮기며 재시도하지 않는다)
    # — 조용히 무시되지 않으므로 오설정이 드러난다.
    #
    # **두 노브의 기본값은 위 OPENAI_MODEL과 한 쌍이다**(#237 비교표). 기본 모델이
    # 추론 모델이라 temperature는 미설정이고 reasoning_effort만 켜져 있다. 벤더 기본값에
    # 맡기지 않고 low로 박는 것은 그 기본값이 우리 것이 아니라 우리 코드 변경 없이 바뀔
    # 수 있기 때문이다 — 비교표가 보증하는 것은 low라고 명시한 열이지 그때의 벤더
    # 기본값이 아니다. 모델 계열은 두 노브를 뒤집어 환경변수만으로 바꾼다(gpt-4o 계열이면
    # OPENAI_TEMPERATURE=0 · OPENAI_REASONING_EFFORT=unset).
    OPENAI_TEMPERATURE: Optional[float] = Field(default=None, ge=0, le=2)
    # 값 집합은 SDK의 openai.types.shared.ReasoningEffort에 "unset" 하나를 더한 것이다.
    # 기본값이 low라 노브를 끌 표기가 필요한데, 빈 값은 아래 Literal 검증에 걸리고
    # "none"은 모델에게 실제로 보내는 값이라 끄기로 못 쓴다. "unset"은 아래 검증기가
    # None으로 접으므로, 이 필드를 읽는 쪽(호출 경계·계측 도구)은 미설정과 구별하지
    # 않는다. SDK 타입을 여기서 import하지 않는 것은 SDK를 부르는 지점을
    # ai/openai_client.py 하나로 유지하기 위해서다(ADR-0005 설계 원칙 3).
    #
    # 이 Literal은 **오타 방지용이며 모델별 지원 목록이 아니다.** 어느 값을 실제로
    # 받는지는 모델마다 다르다 — 계측 대상 3종(gpt-5.6-luna·terra·gpt-5.4-nano)은
    # none·low·medium·high·xhigh를 받고 minimal·max를 400으로 거절했다(#237 실측).
    # 좁히지 않는 것은 모델이 늘 때마다 이 목록을 고쳐야 하기 때문이고, 지원하지 않는
    # 값은 기동이 아니라 첫 호출에서 드러난다.
    OPENAI_REASONING_EFFORT: Optional[
        Literal["unset", "none", "minimal", "low", "medium", "high", "xhigh", "max"]
    ] = "low"
    OPENAI_TIMEOUT_SECONDS: float = Field(default=30.0, gt=0)
    # 재시도 대상은 일시 오류뿐이다(ai/openai_client.py). 1이면 재시도 없음
    OPENAI_MAX_ATTEMPTS: int = Field(default=3, ge=1)
    OPENAI_RETRY_BACKOFF_SECONDS: float = Field(default=1.0, ge=0)
    # 서버가 Retry-After로 지시한 대기의 상한. 이보다 길게 지시하면 따르지 않고
    # backoff로 간다 — 요청 경로에서 부르는 호출이라 무한정 붙잡지 않는다
    OPENAI_MAX_RETRY_AFTER_SECONDS: float = Field(default=60.0, ge=0)

    @field_validator("OPENAI_REASONING_EFFORT", mode="after")
    @classmethod
    def _fold_unset_reasoning_effort(cls, value: Optional[str]) -> Optional[str]:
        # 접는 자리를 호출 경계가 아니라 여기로 둔다 — 이 필드를 읽는 곳이 호출 경계
        # 말고도 있어(scripts/finops_eval.py의 열 이름) 경계에서 접으면 "unset"이 값인
        # 것처럼 표에 찍힌다.
        return None if value == "unset" else value

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
    """CloudWatch 조회 창·스캔 주기 설정 — 수집 비용·판정 신뢰도에 직접 영향을 준다.

    SCAN_INTERVAL_SECONDS 는 인벤토리 describe 주기다. METRIC_PERIOD_SECONDS(메트릭 입자)
    보다 짧아도 된다 — SG 전체개방 같은 위협은 빨리 봐야 하기 때문이다. 그 경우 collector 가
    같은 입자를 다시 받아오지 않도록 적재된 요약을 재사용한다(#255).
    """

    model_config = _ENV_ONLY

    METRIC_LOOKBACK_DAYS: int = Field(default=14, gt=0)
    METRIC_PERIOD_SECONDS: int = Field(default=3600, gt=0)
    SCAN_INTERVAL_SECONDS: int = Field(default=300, gt=0)


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
