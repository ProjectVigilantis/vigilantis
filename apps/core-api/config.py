# ==============================================================================
# [파일 설명]
# pydantic-settings 기반 런타임 환경설정 로더입니다. (Issue #60·#68·#115·#128·#424, ADR-0001)
#
# 설정 단위를 넷으로 나눈다 — 필수 항목이 서로 다르기 때문이다.
#   AuthSettings      : 로그인(Cognito)·세션 설정(#492). 전부 선택 — 비면 auth API만 503.
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

from pydantic import Field, SecretStr, field_validator
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
    # 작동 로그 파일 출력(Issue #424) — 기본은 stdout만. 켜면 같은 JSON 줄을 LOG_FILE_PATH의
    # 회전 파일에도 쓴다. 상대 경로는 apps/core-api 기준(logging_config.resolve_log_file_path)이라
    # compose가 공유하는 폴더에 남아 컨테이너를 다시 만들어도 사라지지 않는다. 같은 체크아웃에서
    # 뜨는 다른 스택(stepper 테스트 서버)은 override에서 경로를 따로 준다
    LOG_FILE_ENABLED: bool = False
    LOG_FILE_PATH: str = ".logs/api.log"
    # 회전 백업 파일 수(파일당 10 MiB, 압축 없음) — 양수만 허용. 0이면 회전 없이 계속 커진다
    LOG_FILE_BACKUP_COUNT: int = Field(default=20, ge=1)
    # 콤마 구분 허용 출처 목록 — 기본값은 FE 개발 서버. CORS가 쓰고, AUTH_ALLOWED_ORIGINS가
    # 비면 상태 변경 · WebSocket Handshake Origin 검증도 이 목록을 쓴다(#492 PR 2)
    CORS_ALLOW_ORIGINS: str = "http://localhost:3000"
    # WebSocket 연결별 전송 제한시간(초) — 초과한 연결은 발행에서 제거.
    # 제거·종료 시 연결 close() 정리도 같은 값을 상한으로 쓴다.
    # 0 이하면 모든 연결이 즉시 제거되므로 양수만 허용한다 (Issue #75)
    WS_SEND_TIMEOUT_SECONDS: float = Field(default=5.0, gt=0)
    # 열린 WebSocket 의 세션 재확인 주기(초, #492 PR 2). 로그아웃 · 재로그인 회수 · 사용자 비활성이
    # 열린 연결에 반영되는 지연의 상한(+ 확인 조회 시간)이다. 유휴 · 최대 수명 만료 시각에는 주기와
    # 관계없이 그 시각에 다시 확인한다. 확인은 유휴 시간을 늘리지 않는다(routers/ws.py)
    WS_SESSION_CHECK_SECONDS: float = Field(default=30, gt=0)
    # API 문서(/docs · /redoc · /openapi.json, #492 PR 2) — 기본 꺼짐(셋 다 404). 로컬 개발만
    # .env 로 켠다. 배포는 태스크 환경변수를 명시 목록으로 넘기므로(scripts/provision_platform_aws.py)
    # 이 값이 따라가지 않는다
    API_DOCS_ENABLED: bool = False
    # 접수된 조치 실행 디스패치·회수 스캔 주기(초) — dispatcher.py 잡 1개가 쓴다.
    # 부분 인덱스(ix_action_executions_non_terminal) 조회 빈도다.
    # 실행·DB 대기로 밀릴 수 있어 승인부터 AWS 호출까지의 지연 상한은 아니다.
    DISPATCH_INTERVAL_SECONDS: int = Field(default=10, gt=0)
    # AI 분석 대기 Incident 스캔 주기(초) — agent_dispatcher.py 잡 1개가 쓴다.
    # Incident 생성부터 그래프 호출까지의 지연 상한이다. 실행 디스패치보다 성긴 것은
    # 그 앞단인 수집·판정 주기가 SCAN_INTERVAL_SECONDS(기본 300초)라 이 값을 더 조여도
    # 체감 지연이 그만큼 줄지 않기 때문이다. 한 주기가 대상 전부를 직렬로 돌고 건마다
    # 모델을 부르므로, 밀리면 다음 주기는 max_instances=1이 미룬다 (Issue #285)
    AGENT_DISPATCH_INTERVAL_SECONDS: int = Field(default=30, gt=0)
    # DB 승인 대기 기한 감시. 주기는 지연 상한이 아니다 — 실행·DB 대기도 계측한다.
    AGENT_WAIT_POLL_SECONDS: float = Field(default=1.0, gt=0)
    # 스캔 잡 기동 스위치 — 테스트가 앱을 띄울 때 스캔이 따라 돌지 않게 끈다
    # (apps/core-api/tests/conftest.py, PR #236 리뷰). 실행 디스패치와 AI 디스패치가
    # 이 하나를 공유한다 — 끄는 목적이 같다
    DISPATCH_ENABLED: bool = True
    # 모의 관측 공급자는 경로를 지정했을 때만 기동한다(#322). 수집·AI 실행 스위치와
    # 독립이다. 폴더 안 JSON을 소비하며 DB 결과는 앱의 조회 API에서 확인한다.
    MOCK_THREAT_INBOX_DIR: str = ""
    MOCK_THREAT_POLL_SECONDS: float = Field(default=1.0, gt=0)
    # #493: 명시적 opt-in. 수신처 파일에는 webhook 자체가 아닌 비밀 환경변수 이름만 둔다.
    # 설정은 기동 시 고정되며 변경 적용에는 재기동이 필요하다.
    NOTIFICATION_ENABLED: bool = False
    NOTIFICATION_ROUTES_FILE: str = ""
    NOTIFICATION_PUBLIC_BASE_URL: str = ""
    NOTIFICATION_POLL_SECONDS: float = Field(default=1.0, ge=0.1, le=60)
    NOTIFICATION_BATCH_SIZE: int = Field(default=20, ge=1, le=100)
    NOTIFICATION_MAX_ATTEMPTS: int = Field(default=5, ge=1, le=10)
    NOTIFICATION_BACKOFF_SECONDS: float = Field(default=2.0, ge=1, le=300)
    NOTIFICATION_MAX_BACKOFF_SECONDS: float = Field(default=300.0, ge=1, le=3600)
    NOTIFICATION_CONNECT_TIMEOUT_SECONDS: float = Field(default=3.0, gt=0, le=10)
    NOTIFICATION_READ_TIMEOUT_SECONDS: float = Field(default=5.0, gt=0, le=30)
    NOTIFICATION_LEASE_SECONDS: float = Field(default=120.0, ge=30, le=600)
    # 2/2 Status Check 대기 — services/aws/rollback.py의 waiter 설정 (Issue #240).
    # 기본 15초×12회=3분으로 boto3 기본값(15초×40회=10분)보다 짧다. 판정 1건이
    # 그만큼 다음 스캔을 미루므로(max_instances=1) 시연에서 조일 수 있어야 한다.
    STATUS_CHECK_WAIT_DELAY_SECONDS: int = Field(default=15, gt=0)
    STATUS_CHECK_WAIT_MAX_ATTEMPTS: int = Field(default=12, ge=1)
    # EC2_ENABLE_AUTOSCALING 대기 (Issue #446) — **조회 횟수의 상한**이다. 벽시계 상한이 아니다
    # (조회 호출 시간·SDK 재시도가 그 위에 얹힌다). 둘 다 dispatcher 한 주기를 붙잡는다.
    #   IMAGE: 실행 단계 1이 AMI available을 기다린다 — 기본 15초 간격 40회 조회.
    #   READY: 판정이 ASG 인스턴스 InService·2/2를 기다린다 — 기본 15초 간격 20회 조회.
    AUTOSCALING_IMAGE_WAIT_DELAY_SECONDS: int = Field(default=15, gt=0)
    AUTOSCALING_IMAGE_WAIT_MAX_ATTEMPTS: int = Field(default=40, ge=1)
    AUTOSCALING_READY_WAIT_DELAY_SECONDS: int = Field(default=15, gt=0)
    AUTOSCALING_READY_WAIT_MAX_ATTEMPTS: int = Field(default=20, ge=1)
    # 판정 불가 재시도 — AWS에 물어보지 못해 실행 결과를 확정하지 못했을 때 (Issue #249).
    # MAX_ATTEMPTS는 받아 주는 실패 횟수(첫 실패 포함)이고, 소진하면 자동 원복하지 않고
    # 결과 확인 불가(UNVERIFIED)로 확정해 관제자에게 넘긴다. INTERVAL은 마지막 실패로부터
    # 다음 질문까지의 최소 간격이다 — 스캔 주기(DISPATCH_INTERVAL_SECONDS)마다 되물으면
    # 스로틀링 같은 일시 오류를 우리가 키운다. 기본 5회·60초면 첫 실패부터 최소 4분을
    # 기다린 뒤 사람에게 넘긴다. 권한 거부처럼 다시 물어도 같은 사유는 이 값과 무관하게
    # 첫 실패에서 넘긴다(services/aws/errors.py RETRYABLE_REASON_CODES).
    VERIFICATION_RETRY_MAX_ATTEMPTS: int = Field(default=5, ge=1)
    VERIFICATION_RETRY_INTERVAL_SECONDS: int = Field(default=60, ge=0)

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

    @field_validator("CORS_ALLOW_ORIGINS", mode="after")
    @classmethod
    def _no_wildcard_cors_origin(cls, value: str) -> str:
        # CORS 가 자격증명(로그인 쿠키)을 허용하므로(#492) `*` 는 받지 않는다 — Starlette 는
        # `*` + allow_credentials 에서 요청 Origin 을 그대로 되돌려 모든 출처에 쿠키 요청을 연다
        if "*" in value:
            raise ValueError("CORS_ALLOW_ORIGINS 는 명시 Origin 목록이어야 한다(* 금지)")
        return value

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


class AuthSettings(BaseSettings):
    """로그인(Cognito) · 세션 설정 (#492).

    Cognito 4종은 Optional이다 — 비어 있어도 앱은 뜨고, /api/v1/auth/* 만 503
    AUTH_PROVIDER_UNAVAILABLE 로 답한다. 누락은 키 이름으로만 알린다(값은 로그에 싣지 않는다).
    AwsSettings 와 달리 .env 를 읽는다 — 호스트 직접 실행 편의(Settings 와 같다).
    """

    model_config = _ENV_FILE

    COGNITO_REGION: str | None = None
    COGNITO_USER_POOL_ID: str | None = None
    COGNITO_CLIENT_ID: str | None = None
    COGNITO_CLIENT_SECRET: SecretStr | None = None
    # 로컬 개발에서 이름 있는 프로파일로 Cognito 에 붙을 때만 — .env 에 LocalStack 더미 AWS 키가
    # 있어도 그것이 쓰이지 않게 한다. 비우면 기본 자격증명 체인(배포: 태스크 역할).
    COGNITO_AWS_PROFILE: str | None = None
    AUTH_SESSION_MAX_SECONDS: int = Field(default=28800, gt=0)
    AUTH_SESSION_IDLE_SECONDS: int = Field(default=1800, gt=0)
    AUTH_TOTP_MAX_ATTEMPTS: int = Field(default=3, ge=1)
    # 흐름을 가로지르는 코드 실패 한도 — 윈도 안에서 한도를 넘으면 새 코드 제출을 막는다
    AUTH_CODE_FAILURE_LIMIT: int = Field(default=10, ge=1)
    AUTH_CODE_FAILURE_WINDOW_SECONDS: int = Field(default=900, gt=0)
    # 콤마 구분 허용 Origin(정확히 일치) — 비우면 CORS_ALLOW_ORIGINS 와 같은 목록을 쓴다
    AUTH_ALLOWED_ORIGINS: str = ""

    @field_validator("AUTH_ALLOWED_ORIGINS", mode="after")
    @classmethod
    def _no_wildcard_origin(cls, value: str) -> str:
        if "*" in value:
            raise ValueError("AUTH_ALLOWED_ORIGINS 는 명시 Origin 목록이어야 한다(* 금지)")
        return value

    def missing_cognito_keys(self) -> list[str]:
        values = {
            "COGNITO_REGION": self.COGNITO_REGION,
            "COGNITO_USER_POOL_ID": self.COGNITO_USER_POOL_ID,
            "COGNITO_CLIENT_ID": self.COGNITO_CLIENT_ID,
            "COGNITO_CLIENT_SECRET": (
                self.COGNITO_CLIENT_SECRET.get_secret_value()
                if self.COGNITO_CLIENT_SECRET
                else None
            ),
        }
        return [key for key, value in values.items() if not (value or "").strip()]

    def allowed_origins(self, fallback: list[str]) -> list[str]:
        return _split_csv(self.AUTH_ALLOWED_ORIGINS) or list(fallback)


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
    # GET /api/v1/metrics/timeseries 의 기본 조회 창(시간). 화면은 hours 를 넘기지 않으므로 이 값이
    # 곧 그래프의 가로 폭이다. 줄이면(예: 1) 분 단위 변화가 보인다.
    METRICS_WINDOW_HOURS: int = Field(default=72, ge=1, le=336)
    # 스캔 파이프라인 잡 기동 여부. false 면 start_scheduler 가 None 을 돌려준다 —
    # 테스트가 앱을 띄울 때 실제 수집·판정이 도는 것을 막는다(DISPATCH_ENABLED 와 같은 결).
    SCAN_ENABLED: bool = True
    # 수집 범위 VPC(콤마 구분, 포함 목록). 비어 있으면 리전 전체를 수집한다(기존 동작).
    # 단일 계정에 플랫폼 자신(ECS·RDS·ALB가 든 별도 VPC)을 함께 배포할 때 계정 경계를
    # 흉내 내는 설정이다 — 플랫폼 ALB SG 의 0.0.0.0/0 이 THREAT 로 판정되는 일을 막는다.
    # Post-MVP SaaS 는 관제 범위 = 고객 계정이라 이 설정이 필요 없다. 거르는 규칙은
    # services/collector._apply_vpc_scope 가 단일 원천이다.
    COLLECT_VPC_IDS: str = ""

    @field_validator("COLLECT_VPC_IDS", mode="after")
    @classmethod
    def _check_vpc_ids(cls, value: str) -> str:
        # 여기서는 형식만 막는다. 형식은 맞는 오타(존재하지 않는 vpc-…)는 매 회차
        # services/collector._check_vpc_scope 가 존재 확인으로 잡고, 없으면 그 회차의
        # 소멸 표시·자산 수 스냅샷을 건너뛴다 — 범위 VPC 자산이 전부 소멸 표시되지 않게.
        bad = [v for v in _split_csv(value) if not v.startswith("vpc-")]
        if bad:
            raise ValueError(f"COLLECT_VPC_IDS 는 vpc- 로 시작하는 ID 목록이어야 한다: {bad}")
        return value

    def collect_vpc_ids(self) -> frozenset[str] | None:
        """수집 범위 VPC 집합. 비어 있으면 None — 범위 제한 없음(리전 전체)."""
        ids = frozenset(_split_csv(self.COLLECT_VPC_IDS))
        return ids or None


def _split_csv(raw: str) -> list[str]:
    return [item.strip() for item in raw.split(",") if item.strip()]


@lru_cache
def get_settings() -> Settings:
    """검증된 Settings 싱글턴. 테스트는 cache_clear() 후 환경변수로 주입한다."""
    return Settings()


@lru_cache
def get_aws_settings() -> AwsSettings:
    """검증된 AwsSettings 싱글턴. 테스트는 cache_clear() 후 환경변수로 주입한다."""
    return AwsSettings()


@lru_cache
def get_auth_settings() -> AuthSettings:
    """검증된 AuthSettings 싱글턴. 테스트는 cache_clear() 후 주입한다."""
    return AuthSettings()


@lru_cache
def get_collector_settings() -> CollectorSettings:
    """검증된 CollectorSettings 싱글턴. 테스트는 cache_clear() 후 주입한다."""
    return CollectorSettings()
