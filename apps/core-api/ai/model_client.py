# ==============================================================================
# [파일 설명]  담당: 안성일 (AI/Guardrail · Architect)
# 모델 호출 경계입니다. (Issue #115 · #434)
# LangGraph 노드와 도메인 코드는 이 경계만 보고, 제공자 SDK 타입·자격증명·SDK 예외는
# 경계 밖으로 나가지 않습니다 (ADR-0005 설계 원칙 3). 구현체는 ai/openai_client.py
# (운영)와 ai/claude_client.py(비교 실험 후보)이며, 재시도·시도 기록은 두 구현이
# ai/model_retry.py의 엔진을 공유합니다.
#
# 계약 원칙
#   - 동기 호출. 저장소의 다른 외부 호출 경계(Repository·services/aws)와 같은 방식이다.
#   - 실패는 이 모듈의 예외로만 나간다. SDK 예외를 그대로 전파하지 않는다.
#   - 모델로 나가는 값은 build_outbound_payload()를 반드시 거친다. 구현체가 이 함수를
#     우회하면 마스킹이 적용되지 않으므로, 전송 페이로드는 여기서만 만든다.
#   - Prompt 전문과 모델 원문 응답은 저장·로그 어디에도 남기지 않는다(ADR-0005
#     미보존 대상). 남기는 것은 토큰 사용량·시도 횟수·소요 시간 같은 호출 메타뿐이며
#     단가·비용은 아니다.
#   - 실패는 세 축으로 적는다 — 예외 클래스(호출부가 분기하는 단위), phase(왕복의
#     어느 자리인지), kind(제공자 응답을 근거로 한 세부 종류). 계측은 kind로 전송·
#     요청·스키마·모델·어댑터 실패를 따로 센다(#434 §4-2).
#   - 제공자가 돌려주지 않은 값은 0으로 채우지 않고 미확인으로 남긴다(TokenUsage.
#     unreported · CallTrace의 Optional 필드). 비교표에서 "0"과 "모름"이 같은 칸에
#     들어가면 캐시·tier 차이를 말할 수 없다.
# ==============================================================================

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Generic, Optional, Protocol, TypeVar

from pydantic import BaseModel

StructuredOutputT = TypeVar("StructuredOutputT", bound=BaseModel)


# ------------------------------------------------------------------------------
# 실패 종류 — 제공자 응답을 근거로 가른 세부 축 (#434 §4-2)
# ------------------------------------------------------------------------------


class FailureKind(str, Enum):
    """호출 실패의 세부 종류. 예외 클래스·phase 위에 더하는 세 번째 축이다.

    값은 계측 원자료(JSON)에 그대로 적히므로 바꾸면 과거 기록과 집계가 어긋난다.
    """

    # --- 전송: 왕복이 서지 못했거나 서버가 일시적으로 거절 — 전송 예산으로 재시도 ---
    TRANSPORT_TIMEOUT = "transport_timeout"  # 제한시간 초과(연결·읽기)
    TRANSPORT_CONNECTION = "transport_connection"  # 연결 실패·응답 없음
    RATE_LIMITED = "rate_limited"  # 429(한도 소진 제외) · 408 · 409
    SERVER_ERROR = "server_error"  # 5xx · 529 overloaded · 503

    # --- 요청: 다시 보내도 같다 — 재시도하지 않는다 ---
    QUOTA_EXHAUSTED = "quota_exhausted"  # 결제·크레딧·지출 한도 소진(OpenAI insufficient_quota · 402)
    REQUEST_REJECTED = "request_rejected"  # 인증·권한·요청 형식·크기(400·401·403·404·413·422)
    SCHEMA_REJECTED = "schema_rejected"  # 제공자가 구조화 출력 스키마를 거절(400) — 어댑터·스키마 몫
    REQUEST_SERIALIZATION = "request_serialization"  # user_payload가 JSON으로 직렬화되지 않음(호출부 몫)

    # --- 응답: 왕복은 섰고 토큰도 발생했지만 출력을 쓸 수 없다 ---
    REFUSAL = "refusal"  # 모델이 응답을 거절(OpenAI refusal · Claude stop_reason=refusal)
    TRUNCATED = "truncated"  # 출력 길이 상한에서 잘림(finish_reason=length · stop_reason=max_tokens)
    OUTPUT_INVALID = "output_invalid"  # 응답이 요구한 구조로 검증되지 않음(출력 예산으로만 재시도)

    # --- 어댑터: SDK 응답 모양이 기대와 다르거나 분류하지 못한 예외 — 모델 실패로 세지 않는다 ---
    RESPONSE_SHAPE = "response_shape"  # choices·content 블록 없음 등 응답 구조 불일치
    ADAPTER_ERROR = "adapter_error"  # 어댑터가 분류하지 못한 예외(SDK 계열 아님)


# 전송 예산(RetryPolicy.max_attempts)으로 재시도하는 종류. 서버 지시 대기(Retry-After)를 따른다.
TRANSPORT_RETRYABLE: frozenset[FailureKind] = frozenset(
    {
        FailureKind.TRANSPORT_TIMEOUT,
        FailureKind.TRANSPORT_CONNECTION,
        FailureKind.RATE_LIMITED,
        FailureKind.SERVER_ERROR,
    }
)
# 출력 예산(RetryPolicy.output_retries)으로만 재시도하는 종류. 다시 부르면 통과할 수
# 있지만 토큰이 또 나가므로 기본 예산은 0이다(운영 동작 유지) — 늘리는 것은 정책 결정이다.
# REFUSAL은 넣지 않는다. 거절을 재질문으로 뚫는 것은 가드레일 취지와 어긋난다.
OUTPUT_RETRYABLE: frozenset[FailureKind] = frozenset(
    {FailureKind.TRUNCATED, FailureKind.OUTPUT_INVALID}
)

# 집계 책임 — 비교 실험이 "모델 품질 실패"와 "어댑터·전송 실패"를 섞지 않기 위한 분류.
#   transport: 네트워크·제공자 가용성  request: 호출 설정·계정  schema: 우리 스키마와 제공자 제약
#   model: 모델 출력 품질  adapter: 어댑터 구현
FAILURE_OWNER: dict[FailureKind, str] = {
    FailureKind.TRANSPORT_TIMEOUT: "transport",
    FailureKind.TRANSPORT_CONNECTION: "transport",
    FailureKind.RATE_LIMITED: "transport",
    FailureKind.SERVER_ERROR: "transport",
    FailureKind.QUOTA_EXHAUSTED: "request",
    FailureKind.REQUEST_REJECTED: "request",
    FailureKind.SCHEMA_REJECTED: "schema",
    FailureKind.REQUEST_SERIALIZATION: "adapter",
    FailureKind.REFUSAL: "model",
    FailureKind.TRUNCATED: "model",
    FailureKind.OUTPUT_INVALID: "model",
    FailureKind.RESPONSE_SHAPE: "adapter",
    FailureKind.ADAPTER_ERROR: "adapter",
}

# kind → phase. 예외 클래스보다 kind가 더 세밀하므로 phase는 kind에서 파생한다.
FAILURE_PHASE: dict[FailureKind, str] = {
    **{kind: "transport" for kind in TRANSPORT_RETRYABLE},
    FailureKind.QUOTA_EXHAUSTED: "request",
    FailureKind.REQUEST_REJECTED: "request",
    FailureKind.SCHEMA_REJECTED: "request",
    FailureKind.REQUEST_SERIALIZATION: "request",
    FailureKind.REFUSAL: "response",
    FailureKind.TRUNCATED: "response",
    FailureKind.OUTPUT_INVALID: "response",
    FailureKind.RESPONSE_SHAPE: "response",
    # 분류하지 못한 예외는 왕복 여부를 알 수 없다 — 어댑터가 phase를 지정하지 않으면 request
    FailureKind.ADAPTER_ERROR: "request",
}


def failure_owner(kind: Optional[FailureKind]) -> Optional[str]:
    """kind가 없는(구버전·주입 구현) 실패는 None — 집계가 "미분류"로 따로 센다."""
    return None if kind is None else FAILURE_OWNER[kind]


# ------------------------------------------------------------------------------
# 시도 기록 — 시도별 시간·결과·대기. 비교 실험이 지연·재시도를 호출 단위로 귀속한다
# ------------------------------------------------------------------------------


@dataclass(frozen=True)
class AttemptRecord:
    """SDK 호출 1회의 기록. 한 complete()는 재시도 수만큼 이 기록을 남긴다."""

    attempt: int  # 1부터. 전송·출력 재시도를 합친 순번
    started_at: datetime  # UTC
    elapsed_seconds: float  # 이 시도의 SDK 왕복 시간(대기 제외)
    outcome: str  # "ok" 또는 FailureKind 값
    error_class: Optional[str] = None  # SDK 예외 타입 이름만(객체·메시지는 보존하지 않는다)
    status_code: Optional[int] = None  # HTTP 상태(응답이 있을 때만)
    retry_delay_seconds: Optional[float] = None  # 이 시도 뒤 실제로 기다린 시간(재시도했을 때만)
    server_directed: Optional[bool] = None  # 대기가 Retry-After를 따른 것인지(재시도했을 때만)
    # 이 시도가 쓴 토큰. 응답이 없는 실패(전송)는 None = 미확인. 출력 재시도는 시도마다
    # 토큰을 쓰므로 호출 단위 usage(AIModelResponse.usage · AIModelError.usage)는 이 값의 합이다
    usage: Optional["TokenUsage"] = None


@dataclass(frozen=True)
class CallTrace:
    """complete() 1회의 전체 기록. Optional 필드의 None은 "제공자가 돌려주지 않음"이다."""

    provider: str  # "openai" · "anthropic" …
    requested_model: str
    returned_model: Optional[str]  # 응답이 밝힌 실제 모델(스냅샷). None = 미확인
    requested_service_tier: Optional[str]  # 요청에 실은 처리 tier(OpenAI service_tier · Claude speed)
    returned_service_tier: Optional[str]  # 응답이 밝힌 처리 tier. None = 미확인
    attempts: tuple[AttemptRecord, ...]
    max_attempts: int  # 전송 예산
    output_retries: int  # 출력 예산
    timeout_seconds: float  # 시도당 제한시간
    elapsed_seconds: float  # 첫 시도 시작 → 종료(재시도 대기 포함)

    @property
    def attempt_count(self) -> int:
        return len(self.attempts)


# ------------------------------------------------------------------------------
# 경계 예외 — 호출부는 SDK 예외가 아니라 이 5종만 다룬다
# ------------------------------------------------------------------------------


class AIModelError(Exception):
    """모델 호출 실패의 최상위 타입.

    phase는 실패가 왕복의 어느 자리에서 났는지다 — "transport"(왕복 자체가 못 섬),
    "request"(호출 준비·수락 단계의 거절), "response"(응답은 받았지만 쓸 수 없음).
    예외 클래스만으로는 이 축이 갈리지 않아서(예: 계약 위반은 요청 직렬화 실패와
    응답 파싱 실패 양쪽에 쓰인다) 따로 든다 — 계측이 response만 모델 품질로 센다(#237).

    kind는 제공자 응답을 근거로 한 세부 종류(FailureKind)다. 구현체가 분류하면 싣고,
    주입 구현·구버전 경로는 None일 수 있다 — 집계는 None을 "미분류"로 따로 센다.
    trace는 시도별 기록이다. 없으면 호출 단위 지연·재시도 귀속이 불가능하므로 미확인이다.

    usage는 응답을 받은 실패에서 이미 발생한 토큰이다. 예외에 실어 보내지 않으면
    그 호출의 토큰이 어느 집계에도 남지 않아 비용이 실제 청구보다 적게 잡힌다.
    """

    phase: str = "request"

    def __init__(
        self,
        message: str,
        *,
        usage: Optional["TokenUsage"] = None,
        phase: Optional[str] = None,
        kind: Optional[FailureKind] = None,
        trace: Optional[CallTrace] = None,
    ) -> None:
        super().__init__(message)
        self.usage = usage
        self.kind = kind
        self.trace = trace
        if phase is not None:
            self.phase = phase
        elif kind is not None:
            self.phase = FAILURE_PHASE[kind]

    @property
    def retryable(self) -> bool:
        """전송 예산으로 재시도할 종류였는지. kind가 없으면 클래스로 판정한다."""
        if self.kind is not None:
            return self.kind in TRANSPORT_RETRYABLE
        return isinstance(self, (AIModelTimeoutError, AIModelUnavailableError))

    @property
    def owner(self) -> Optional[str]:
        return failure_owner(self.kind)


class AIModelTimeoutError(AIModelError):
    """제한시간 초과. 재시도 상한을 소진한 뒤에 나온다."""

    phase = "transport"


class AIModelUnavailableError(AIModelError):
    """일시적 실패(연결 오류·408·409·429·5xx). 재시도 상한을 소진한 뒤에 나온다."""

    phase = "transport"


class AIModelRejectedError(AIModelError):
    """호출이 거절됨(인증·요청 오류·한도 소진·모델 refusal). 재시도하지 않는다."""


class AIModelContractError(AIModelError):
    """계약 위반. 전송 예산으로는 재시도하지 않는다.

    양방향이다 — 요청 페이로드가 JSON으로 직렬화되지 않는 경우, 제공자가 스키마를
    거절한 경우, 응답이 요구한 구조화 출력으로 파싱되지 않거나 잘린 경우.
    """


class AIModelAdapterError(AIModelError):
    """어댑터 구현 실패. SDK 응답 모양이 기대와 다르거나 분류하지 못한 예외가 났다.

    경계 밖으로 날 예외(AttributeError 등)를 이 타입으로 접는 이유는, 그래프가
    AIModelError만 FAILED로 접고 나머지는 디스패처까지 올라가 선점이 IN_PROGRESS로
    남기 때문이다(#434 §4-2 — 회수 뒤 같은 건에 과금 호출이 반복된다). 계측은 이
    타입을 모델 품질 실패에 섞지 않는다.
    """


# kind → 호출부가 분기하는 예외 클래스. 구현체는 이 표로만 예외를 만든다(model_retry.py).
FAILURE_EXCEPTION: dict[FailureKind, type[AIModelError]] = {
    FailureKind.TRANSPORT_TIMEOUT: AIModelTimeoutError,
    FailureKind.TRANSPORT_CONNECTION: AIModelUnavailableError,
    FailureKind.RATE_LIMITED: AIModelUnavailableError,
    FailureKind.SERVER_ERROR: AIModelUnavailableError,
    FailureKind.QUOTA_EXHAUSTED: AIModelRejectedError,
    FailureKind.REQUEST_REJECTED: AIModelRejectedError,
    FailureKind.SCHEMA_REJECTED: AIModelContractError,
    FailureKind.REQUEST_SERIALIZATION: AIModelContractError,
    FailureKind.REFUSAL: AIModelRejectedError,
    FailureKind.TRUNCATED: AIModelContractError,
    FailureKind.OUTPUT_INVALID: AIModelContractError,
    FailureKind.RESPONSE_SHAPE: AIModelAdapterError,
    FailureKind.ADAPTER_ERROR: AIModelAdapterError,
}


# ------------------------------------------------------------------------------
# 요청·응답
# ------------------------------------------------------------------------------


@dataclass(frozen=True)
class AIModelRequest:
    """모델 호출 1회의 입력. user_payload는 JSON 직렬화 가능한 값만 담는다."""

    system_prompt: str
    user_payload: Mapping[str, Any]


@dataclass(frozen=True)
class TokenUsage:
    """호출 메타. 단가·계산된 비용은 담지 않는다(ADR-0005 미보존 대상)."""

    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    # prompt_tokens 중 제공자 캐시에서 온 몫. 청구 단가가 달라 이 값을 빼고는 실제
    # 비용을 계산할 수 없다. 같은 프롬프트를 반복하는 계측에서는 크게 걸리고, 인시던트
    # 마다 입력이 다른 실경로에서는 대개 0이다 — 그 차이를 구분해 말하려면 기록해야
    # 한다(#237). 제공자가 이 정보를 주지 않으면 0으로 두고 unreported에 이름을 적는다.
    cached_prompt_tokens: int = 0
    # 캐시 쓰기 토큰(Claude cache_creation_input_tokens). 쓰기 단가가 따로 있어 비용에
    # 필요하다. 제공자가 주지 않으면(OpenAI) None = 미확인.
    cache_creation_prompt_tokens: Optional[int] = None
    # 제공자 응답에 없어 0으로 채운 필드 이름. 비교표는 이 필드를 "미확인"으로 적는다.
    unreported: tuple[str, ...] = ()


@dataclass(frozen=True)
class AIModelResponse(Generic[StructuredOutputT]):
    """구조화 출력 1건 + 호출 메타. output 타입은 호출부가 넘긴 모델과 같다."""

    output: StructuredOutputT
    usage: TokenUsage
    model: str
    # 시도별 기록. 주입 구현(FakeAIModelClient)은 None — 실제 왕복이 없다
    trace: Optional[CallTrace] = None


class AIModelClient(Protocol):
    """모델 호출 경계. 구현체는 이 시그니처만 노출한다."""

    def complete(
        self,
        request: AIModelRequest,
        response_model: type[StructuredOutputT],
    ) -> AIModelResponse[StructuredOutputT]:
        ...


# ------------------------------------------------------------------------------
# 전송 직전 마스킹 (#115 확정 범위)
# ------------------------------------------------------------------------------

_MASK = "[REDACTED]"

# 가리는 것은 자격증명 형태 문자열뿐이다. ARN·리소스 ID·계정 ID·리전은 대상이 아니다 —
# 가리면 모델이 조치 대상을 지목할 수 없고(RunbookCandidateDraft.target_arn), 이미
# GET /api/v1/assets로 대시보드에 나가는 값이라 감출 대상도 아니다.
_PREFIXED_SECRETS: tuple[re.Pattern[str], ...] = (
    # AWS 액세스 키 ID(장기·임시)
    re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    # OpenAI 형태 키. 꼬리에 하이픈·밑줄을 허용하지 않아 sk-로 시작하는 사람 이름표
    # (예: sk-prod-cluster)와 구분된다.
    re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9]{20,}\b"),
    # GitHub 토큰(ghp_·gho_·ghu_·ghs_·ghr_)
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
)

# `Bearer <토큰>` 형태 — 값이 8자 미만이면 영어 일반 명사("bearer of ...")로 본다
_BEARER_TOKEN = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}")

# Authorization 헤더는 값 전체(스킴 포함)를 가린다 — 인증 방식을 열거하면 목록에 없는
# 방식이 올 때마다 구멍이 생기고, 라벨 규칙만으로는 스킴 한 단어만 가려 자격증명 본체가
# 남는다(예: "Basic dXNlcjpwYXNz"). 값은 따옴표·줄바꿈에서 끊는다 — 줄 끝까지 먹으면
# 단일 라인 JSON 근거(CloudTrail·VPC flow log)의 뒤따르는 판단 재료까지 사라진다.
_AUTHORIZATION_HEADER = re.compile(
    r"(?i)\b(authorization\b[\"']?\s*[=:]\s*[\"']?)([^\r\n\"']+)"
)

# 비밀값 라벨 목록 — 문자열 안 표기(`password: x`·`"password": "x"`)와 dict 키
# (태그처럼 키가 자유 문자열인 자리) 판정이 같은 목록을 쓴다.
_SECRET_LABELS = (
    r"aws_secret_access_key|aws_session_token|secret[_-]?key"
    r"|api[_-]?key|password|passwd|token|authorization"
)
# 라벨이 붙은 비밀값. 라벨은 남기고 값만 가려서 무엇이 지워졌는지 읽을 수 있게 한다.
# 라벨 앞 [\w-]* 는 db_password 같은 접두 변형용, 따옴표 허용은 JSON 표기용이다.
# 값은 공백·따옴표에서 끊는다 — 닫는 따옴표까지 먹으면 JSON 근거의 나머지 항목이
# 마스킹 안으로 딸려 들어가 판단 재료가 사라진다.
_LABELED_SECRET = re.compile(
    r"(?i)\b([\w-]*(?:" + _SECRET_LABELS + r"))([\"']?\s*[=:]\s*[\"']?)([^\s\"']+)"
)
# 비밀 라벨로 끝나는 dict 키 — 그 키의 문자열 값은 통째로 가린다
_SECRET_KEY_NAME = re.compile(r"(?i)[\w-]*(?:" + _SECRET_LABELS + r")")


def _mask_text(text: str) -> str:
    # Authorization 헤더 → Bearer → 라벨 순서다. 헤더 규칙이 줄 전체를 먼저 가려야
    # 뒤 규칙들이 스킴 한 단어만 가리고 자격증명을 남기는 일이 없다
    masked = _AUTHORIZATION_HEADER.sub(r"\1" + _MASK, text)
    masked = _BEARER_TOKEN.sub(_MASK, masked)
    masked = _LABELED_SECRET.sub(r"\1\2" + _MASK, masked)
    for pattern in _PREFIXED_SECRETS:
        masked = pattern.sub(_MASK, masked)
    return masked


def mask_outbound(value: Any) -> Any:
    """모델로 나가는 값에서 자격증명 형태 문자열을 가린다.

    dict·list를 재귀로 훑는다. 키 이름은 바꾸지 않되, 키가 비밀 라벨로 끝나면
    (예: 태그 {"db_password": ...}) 그 문자열 값을 통째로 가린다. 라벨 없는
    40자짜리 시크릿 문자열은 대상이 아니다 — 해시·base64 데이터와 구분되지 않아
    오탐으로 판단 근거를 지운다.
    """
    if isinstance(value, str):
        return _mask_text(value)
    if isinstance(value, Mapping):
        return {
            key: (
                _MASK
                if isinstance(key, str)
                and isinstance(item, str)
                and _SECRET_KEY_NAME.fullmatch(key)
                else mask_outbound(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [mask_outbound(item) for item in value]
    return value


def build_outbound_payload(request: AIModelRequest) -> dict[str, Any]:
    """실제로 모델에 보낼 값. 이 함수를 지나지 않은 값은 전송하지 않는다.

    system_prompt도 마스킹을 지난다 — 프롬프트 본문에 `token:` 같은 라벨 표기를 쓰면
    지침 자체가 조용히 잘리므로, 프롬프트에는 비밀값 라벨 표기를 쓰지 않는다.
    """
    masked = mask_outbound(dict(request.user_payload))
    # 직렬화 불가 값은 여기서 계약 위반으로 세운다. 이 검사가 없으면 호출부가 python
    # 모드 model_dump()를 넘겼을 때 TypeError가 경계 밖으로 그대로 나간다 —
    # AssetItem.collected_at의 UtcDateTime은 when_used="json"이라 python 모드에서
    # datetime 객체로 남는다.
    # raise는 except 밖에서 한다 — 안에서 던지면 from None으로도 __context__에 원본이
    # 남는다. 경계 예외에 다른 예외를 매달지 않는 규칙을 SDK 예외와 같게 지킨다.
    user_json = ""
    serialize_error: Optional[str] = None
    try:
        user_json = json.dumps(masked, ensure_ascii=False, sort_keys=True)
    except TypeError as exc:
        serialize_error = str(exc)
    if serialize_error is not None:
        raise AIModelContractError(
            f"user_payload를 JSON으로 직렬화하지 못했습니다 ({serialize_error}) — "
            'model_dump(mode="json")으로 넘기세요',
            kind=FailureKind.REQUEST_SERIALIZATION,
        )
    return {
        "system_prompt": _mask_text(request.system_prompt),
        "user_payload": masked,
        # 구현체가 다시 직렬화하지 않도록 여기서 만든 것을 함께 넘긴다
        "user_json": user_json,
    }


# ------------------------------------------------------------------------------
# 테스트 주입용 구현 (ADR-0005 — 실제 API 없이 두 경로 검증)
# ------------------------------------------------------------------------------


class FakeAIModelClient:
    """미리 준비한 출력을 순서대로 돌려주고, 전송됐을 페이로드를 sent에 기록한다.

    마스킹을 실제 구현과 같은 지점에서 적용한다 — 주입 구현이 마스킹을 우회하면
    주입 테스트가 검증하는 경로가 실제 경로와 달라진다.

    provider="fake"는 계측이 "실제 모델 측정이 아니다"를 가르는 표식이다. 실제 구현은
    제공자 이름("openai"·"anthropic")을 둔다.
    """

    provider = "fake"

    def __init__(
        self,
        outputs: Sequence[BaseModel],
        *,
        model: str = "fake-model",
    ) -> None:
        self._outputs = list(outputs)
        self._model = model
        self.sent: list[dict[str, Any]] = []

    def complete(
        self,
        request: AIModelRequest,
        response_model: type[StructuredOutputT],
    ) -> AIModelResponse[StructuredOutputT]:
        self.sent.append(build_outbound_payload(request))
        if not self._outputs:
            raise AIModelUnavailableError("준비된 응답이 남아 있지 않습니다")
        output = self._outputs.pop(0)
        if not isinstance(output, response_model):
            raise AIModelContractError(
                f"{response_model.__name__}이 아닌 출력이 준비돼 있습니다"
            )
        return AIModelResponse(
            output=output,
            usage=TokenUsage(prompt_tokens=0, completion_tokens=0, total_tokens=0),
            model=self._model,
        )
