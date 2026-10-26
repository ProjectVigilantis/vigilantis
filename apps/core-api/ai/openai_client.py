# ==============================================================================
# [파일 설명]  담당: 안성일 (AI/Guardrail · Architect)
# ai/model_client.py 경계의 OpenAI 구현입니다. (Issue #115 · #434 §4-2)
# SDK를 부르는 유일한 지점이며, SDK 예외·응답을 FailureKind로 분류해 엔진에 넘깁니다.
#
#   - 재시도·대기·시도 기록·로그·경계 예외 생성은 ai/model_retry.py의 엔진이 소유한다.
#     이 파일은 SDK 호출 1회(attempt)와 "SDK 예외·응답 → FailureKind" 분류만 맡는다.
#   - 분류는 제공자 응답을 근거로 한다. 제한시간·연결은 전송, 429(insufficient_quota
#     제외)·408·409·5xx는 Retry-After를 따르는 전송 재시도 대상이다. 한도 소진
#     (insufficient_quota)·인증·요청 오류·스키마 거절은 다시 불러도 같으므로 재시도하지
#     않는다. 잘림·검증 실패는 출력 예산(output_retries)으로만 재시도한다.
#   - SDK 예외는 except 안에서 AttemptFailure(타입 이름 문자열만)로 바꿔 반환한다 —
#     예외 객체·SDK 메시지는 경계 밖은 물론 체인에도 남기지 않는다.
#   - 구조화 출력은 SDK의 parse 경로로 받는다 — 응답 텍스트를 직접 파싱하지 않는다.
#   - 처리 tier(service_tier)는 값이 있을 때만 요청에 싣고, 응답이 밝힌 tier를 기록한다.
#     제공자가 돌려주지 않은 사용량은 0으로 채우되 TokenUsage.unreported에 이름을 남긴다.
#   - 모델 동작 파라미터(temperature·reasoning_effort)는 설정에 값이 있을 때만 싣는다
#     (#237). 모델 계열마다 받는 것이 달라(gpt-4o는 temperature, gpt-5 계열 추론
#     모델은 reasoning_effort) 안 쓰는 노브는 요청 본문에 나타나지도 않아야 한다.
#   - 남기는 로그는 토큰 사용량·시도 횟수 같은 호출 메타뿐이다. Prompt 전문과 원문
#     응답은 남기지 않는다(ADR-0005 미보존 대상).
# ==============================================================================

from __future__ import annotations

import logging
from typing import Any, Optional

from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    ContentFilterFinishReasonError,
    InternalServerError,
    LengthFinishReasonError,
    OpenAI,
    OpenAIError,
    RateLimitError,
)
from pydantic import ValidationError

from ai.model_client import (
    AIModelRequest,
    AIModelResponse,
    FailureKind,
    StructuredOutputT,
    TokenUsage,
    build_outbound_payload,
)
from ai.model_retry import (
    AttemptFailure,
    AttemptOutcome,
    AttemptSuccess,
    ModelCallEngine,
    RetryPolicy,
    retry_after_seconds,
)
from config import Settings, get_settings

# ADR-0005: Prompt 전문은 로그 레벨과 무관하게 출력하지 않는다. SDK는 DEBUG에서
# 요청 본문(messages 전문)을 로그로 남기므로("Request options", openai/_base_client.py)
# 앱 LOG_LEVEL 설정과 무관하게 SDK 계열 로거를 여기서 차단한다.
# httpcore2(httpx2의 전송층)는 DEBUG에서 헤더 전송 실패를 헤더 값째로 남긴다 — 같이 막는다.
for _sdk_logger_name in ("openai", "httpx2", "httpcore2"):
    logging.getLogger(_sdk_logger_name).setLevel(logging.WARNING)


class OpenAIModelClient:
    """AIModelClient 구현. SDK 객체를 주입받아 테스트에서 교체할 수 있다."""

    provider = "openai"

    def __init__(
        self,
        *,
        client: Any,
        model: str,
        policy: RetryPolicy,
        temperature: Optional[float] = None,
        reasoning_effort: Optional[str] = None,
        service_tier: Optional[str] = None,
    ) -> None:
        self._client = client
        self._model = model
        self._policy = policy
        self._service_tier = service_tier
        # 값이 없는 노브는 키 자체를 만들지 않는다 — 모델이 받지 않는 파라미터는
        # 요청 본문에 나타나지도 않아야 한다. 전 호출이 같은 dict를 쓰므로 요약·후보
        # 호출에 서로 다른 설정이 실릴 여지가 없다.
        self._model_params: dict[str, Any] = {
            name: value
            for name, value in (
                ("temperature", temperature),
                ("reasoning_effort", reasoning_effort),
            )
            if value is not None
        }

    def complete(
        self,
        request: AIModelRequest,
        response_model: type[StructuredOutputT],
    ) -> AIModelResponse[StructuredOutputT]:
        # 마스킹과 JSON 직렬화 모두 경계 함수가 끝낸 상태다 — 여기서 다시 만들지 않는다
        payload = build_outbound_payload(request)
        messages = [
            {"role": "system", "content": payload["system_prompt"]},
            {"role": "user", "content": payload["user_json"]},
        ]

        extra_params = dict(self._model_params)
        if self._service_tier is not None:
            extra_params["service_tier"] = self._service_tier

        def attempt() -> AttemptOutcome[StructuredOutputT]:
            # SDK 호출 1회. 예외는 except 안에서 AttemptFailure 값으로 바꿔 반환한다 —
            # SDK 예외는 request 객체가 Authorization 헤더를 물고 있어 체인에 남으면
            # 자격증명이 경계 밖으로 샌다(ADR-0005 원칙 3). 예외 객체·SDK 메시지는
            # 보존하지 않고 타입 이름 문자열만 남긴다.
            try:
                completion = self._client.chat.completions.parse(
                    model=self._model,
                    messages=messages,
                    response_format=response_model,
                    timeout=self._policy.timeout_seconds,
                    **extra_params,
                )
            except APITimeoutError as exc:
                return AttemptFailure(
                    FailureKind.TRANSPORT_TIMEOUT,
                    "모델 호출 제한시간을 초과했습니다",
                    error_class=type(exc).__name__,
                )
            except APIConnectionError as exc:
                return AttemptFailure(
                    FailureKind.TRANSPORT_CONNECTION,
                    "모델에 연결하지 못했습니다",
                    error_class=type(exc).__name__,
                )
            except LengthFinishReasonError as exc:
                # 실제 SDK는 finish_reason=length를 completion 반환 전에 예외로 던진다.
                # 응답은 받았으므로 exc.completion에서 usage·모델·tier를 건진다
                return AttemptFailure(
                    FailureKind.TRUNCATED,
                    "출력이 길이 상한에서 잘렸습니다",
                    error_class=type(exc).__name__,
                    usage=_token_usage(getattr(exc.completion, "usage", None)),
                    returned_model=getattr(exc.completion, "model", None),
                    returned_service_tier=getattr(exc.completion, "service_tier", None),
                )
            except ContentFilterFinishReasonError as exc:
                # 이 예외는 completion을 싣지 않는다 — usage를 알 수 없어 None(미확인)이다
                return AttemptFailure(
                    FailureKind.REFUSAL,
                    "콘텐츠 필터가 응답을 거절했습니다",
                    error_class=type(exc).__name__,
                )
            except APIStatusError as exc:
                return _classify_status_error(exc)
            except ValidationError as exc:
                # SDK가 응답을 구조화 출력으로 검증하다 실패 — 응답은 받았지만 usage는
                # SDK가 예외로 던져 없다. 메시지는 원문 응답을 담으므로 버린다
                return AttemptFailure(
                    FailureKind.OUTPUT_INVALID,
                    "응답을 요구한 구조로 파싱하지 못했습니다",
                    error_class=type(exc).__name__,
                )
            except OpenAIError as exc:
                return AttemptFailure(
                    FailureKind.REQUEST_REJECTED,
                    "모델 호출을 시작하지 못했습니다",
                    error_class=type(exc).__name__,
                )
            return self._classify_completion(completion, response_model)

        return ModelCallEngine(
            provider=self.provider,
            model=self._model,
            policy=self._policy,
            requested_service_tier=self._service_tier,
        ).run(attempt)

    @staticmethod
    def _classify_completion(
        completion: Any, response_model: type[StructuredOutputT]
    ) -> AttemptOutcome[StructuredOutputT]:
        # 사용량은 응답 판정보다 먼저 계산한다 — refusal·계약 위반 응답도 토큰을 쓴다.
        # 여기서부터의 실패는 usage를 실어, 계측이 실패한 호출의 비용을 잃지 않게 한다
        usage = _token_usage(getattr(completion, "usage", None))
        returned_model = getattr(completion, "model", None)
        returned_tier = getattr(completion, "service_tier", None)

        def failure(kind: FailureKind, message: str) -> AttemptFailure:
            return AttemptFailure(
                kind,
                message,
                usage=usage,
                returned_model=returned_model,
                returned_service_tier=returned_tier,
            )

        choices = getattr(completion, "choices", None) or []
        if not choices:
            return failure(FailureKind.RESPONSE_SHAPE, "응답에 선택지가 없습니다")

        message = choices[0].message
        if getattr(message, "refusal", None):
            return failure(FailureKind.REFUSAL, "모델이 응답을 거절했습니다")
        if getattr(choices[0], "finish_reason", None) == "length":
            return failure(FailureKind.TRUNCATED, "출력이 길이 상한에서 잘렸습니다")

        parsed = getattr(message, "parsed", None)
        if not isinstance(parsed, response_model):
            return failure(
                FailureKind.OUTPUT_INVALID,
                f"응답을 {response_model.__name__}으로 파싱하지 못했습니다",
            )
        return AttemptSuccess(
            output=parsed,
            usage=usage,
            returned_model=returned_model,
            returned_service_tier=returned_tier,
        )


def _classify_status_error(exc: APIStatusError) -> AttemptFailure:
    """HTTP 응답을 받은 SDK 예외를 FailureKind로 가른다. 예외 객체는 보존하지 않는다."""
    name = type(exc).__name__
    status = exc.status_code
    code = exc.code if isinstance(exc.code, str) else ""
    err_type = exc.type if isinstance(exc.type, str) else ""
    param = exc.param if isinstance(getattr(exc, "param", None), str) else ""
    retry_after = retry_after_seconds(getattr(exc.response, "headers", None))

    if isinstance(exc, RateLimitError):
        # 한도 소진은 기다려도 풀리지 않는다 — 같은 429라도 재시도하지 않는다
        if "insufficient_quota" in (code, err_type):
            return AttemptFailure(
                FailureKind.QUOTA_EXHAUSTED,
                "결제·크레딧 한도가 소진됐습니다",
                error_class=name,
                status_code=status,
            )
        kind = FailureKind.RATE_LIMITED
    elif isinstance(exc, InternalServerError):
        kind = FailureKind.SERVER_ERROR
    elif status in (408, 409):
        # 전용 예외 클래스가 없는 408·409 — SDK 재시도 정책(_should_retry) 승계
        kind = FailureKind.RATE_LIMITED
    else:
        is_schema = status == 400 and (
            param.startswith("response_format")
            or "invalid_json_schema" in (code, err_type)
            or "schema" in code.lower()
            or "schema" in err_type.lower()
        )
        return AttemptFailure(
            FailureKind.SCHEMA_REJECTED if is_schema else FailureKind.REQUEST_REJECTED,
            "제공자가 구조화 출력 스키마를 거절했습니다"
            if is_schema
            else f"모델이 호출을 거절했습니다 ({name})",
            error_class=name,
            status_code=status,
        )
    return AttemptFailure(
        kind,
        "모델을 일시적으로 사용할 수 없습니다",
        error_class=name,
        status_code=status,
        retry_after_seconds=retry_after,
    )


def _token_usage(raw: Any) -> TokenUsage:
    """사용량이 비어 오는 응답도 있으므로 0으로 채우되, 채운 필드는 unreported에 적는다."""
    if raw is None:
        return TokenUsage(
            prompt_tokens=0,
            completion_tokens=0,
            total_tokens=0,
            unreported=(
                "prompt_tokens",
                "completion_tokens",
                "total_tokens",
                "cached_prompt_tokens",
            ),
        )
    missing = [
        name
        for name in ("prompt_tokens", "completion_tokens", "total_tokens")
        if getattr(raw, name, None) is None
    ]
    prompt = int(getattr(raw, "prompt_tokens", 0) or 0)
    completion = int(getattr(raw, "completion_tokens", 0) or 0)
    total = int(getattr(raw, "total_tokens", 0) or 0) or (prompt + completion)
    # 캐시된 입력은 prompt_tokens에 포함돼 오지만 단가가 다르다. 필드가 없는 응답도
    # 있으므로(구버전·타 제공자) 0으로 떨어뜨리고 미확인으로 표시한다
    details = getattr(raw, "prompt_tokens_details", None)
    cached_raw = getattr(details, "cached_tokens", None)
    return TokenUsage(
        prompt_tokens=prompt,
        completion_tokens=completion,
        total_tokens=total,
        cached_prompt_tokens=min(int(cached_raw or 0), prompt),
        unreported=tuple(missing)
        + (() if cached_raw is not None else ("cached_prompt_tokens",)),
    )


def build_openai_model_client(settings: Optional[Settings] = None) -> OpenAIModelClient:
    """설정으로 실제 SDK 클라이언트를 만들어 경계 구현을 구성한다."""
    settings = settings or get_settings()
    if not settings.OPENAI_API_KEY:
        raise ValueError("OPENAI_API_KEY가 설정되지 않아 모델 클라이언트를 만들 수 없습니다")
    return OpenAIModelClient(
        # 재시도 소유자는 이 래퍼 하나다 — SDK 기본 재시도(2회)와 중첩되면
        # 최대 시도가 곱으로 늘어나므로 SDK 쪽은 0으로 끈다
        client=OpenAI(
            api_key=settings.OPENAI_API_KEY,
            timeout=settings.OPENAI_TIMEOUT_SECONDS,
            max_retries=0,
        ),
        model=settings.OPENAI_MODEL,
        policy=RetryPolicy.from_settings(settings),
        temperature=settings.OPENAI_TEMPERATURE,
        reasoning_effort=settings.OPENAI_REASONING_EFFORT,
        service_tier=None,  # 설정 키가 없다 — 실험 도구가 생성자로 직접 지정한다
    )
