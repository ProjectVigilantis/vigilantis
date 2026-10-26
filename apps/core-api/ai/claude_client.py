# ==============================================================================
# [파일 설명]  담당: 안성일 (AI/Guardrail · Architect)
# ai/model_client.py 경계의 Claude(Anthropic) 구현입니다. (Issue #434 §4-2)
# 모델 비교 실험(claude-sonnet-5-5 · claude-haiku-5-5)이 같은 LangGraph 노드를 Claude로
# 돌리기 위한 어댑터이며, 운영 경로(OpenAI)에는 연결하지 않습니다.
#
#   - anthropic SDK를 쓰지 않는다. Messages API는 HTTPS POST 하나라 openai>=3의 전송
#     라이브러리인 httpx2로 직접 부른다(새 의존성 없음, pyproject 조율 불필요).
#     SDK가 해 주던 것 중 필요한 셋만 여기서 맡는다: 인증·버전 헤더, 상태 코드 분류,
#     구조화 출력용 JSON 스키마 변환(_transform_schema — SDK lib/_parse/_transform.py와
#     같은 규칙).
#   - 재시도·대기·시도 기록·로그·경계 예외 생성은 ai/model_retry.py의 엔진이 소유한다.
#     이 파일은 HTTP 호출 1회(attempt)와 "전송 예외·응답 → FailureKind" 분류만 맡는다.
#   - 구조화 출력은 output_config.format(json_schema)로 요청하고 stop_reason을 먼저 판정한
#     뒤 model_validate_json으로 받는다. refusal·max_tokens 응답은 usage를 실어 분류하고,
#     검증 실패는 usage를 실어 OUTPUT_INVALID로 접는다.
#   - 토큰은 경계 의미(OpenAI식: prompt_tokens = 캐시 포함 전체 입력)로 정규화한다.
#     Anthropic의 input_tokens는 캐시 읽기·쓰기를 제외하므로
#     prompt = input + cache_read + cache_creation 이다. 캐시 읽기 필드가 없으면 0으로
#     채우고 unreported에 적는다. 캐시 쓰기는 None을 그대로 둔다(미확인).
#   - config.py에 키를 두지 않는다. 키·모델·노브는 실험 도구가 build_claude_model_client()로
#     직접 넘긴다(.env의 ANTHROPIC_API_KEY는 compare/cli.py가 읽는다).
#   - 분류 한계: 사용 한도(tier spend cap)로 인한 429는 retry-after가 없고 계속 실패하지만
#     일반 rate_limit_error와 응답으로 구분할 수 없어 RATE_LIMITED로 접는다. 402만
#     QUOTA_EXHAUSTED다. 조직 지출 한도는 400 invalid_request_error로 와 REQUEST_REJECTED다.
#   - 전송 예외는 except 안에서 AttemptFailure(타입 이름·상태 코드만)로 바꿔 반환한다.
#     httpx 예외는 request(헤더에 x-api-key)를 물고 있으므로 체인에 남기지 않는다.
#     오류 본문은 분류에만 쓰고 저장·로그하지 않는다. ValidationError 메시지는 모델 원문을
#     담으므로 어디에도 싣지 않는다(ADR-0005).
# ==============================================================================

from __future__ import annotations

import logging
from typing import Any, Optional

import httpx2
from pydantic import BaseModel, ValidationError

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

# ADR-0005: httpx2는 INFO에서 요청 URL을 남기고, 그 아래 전송층 httpcore2는 DEBUG에서
# 헤더 전송 실패를 헤더 값(x-api-key)째로 남긴다 — 앱 LOG_LEVEL과 무관하게 둘 다 차단한다
# (openai_client.py와 같은 이름).
for _transport_logger_name in ("httpx2", "httpcore2"):
    logging.getLogger(_transport_logger_name).setLevel(logging.WARNING)

DEFAULT_BASE_URL = "https://api.anthropic.com"
MESSAGES_PATH = "/v1/messages"
# 공식 Python SDK 2.3.0이 output_config·speed와 함께 보내는 버전(anthropic/_constants.py).
API_VERSION = "2026-10-21"

# 응답 스키마 클래스별 변환 결과. 변환은 순수 함수라 클래스당 한 번이면 된다.
_SCHEMA_CACHE: dict[type[BaseModel], dict[str, Any]] = {}

_TRUNCATED_STOP_REASONS = ("max_tokens", "model_context_window_exceeded")

# 제공자 오류 본문의 error.type — 진단 문자열로 남기는 값은 이 목록으로 제한한다
_KNOWN_ERROR_TYPES = frozenset(
    {
        "invalid_request_error",
        "authentication_error",
        "permission_error",
        "not_found_error",
        "request_too_large",
        "rate_limit_error",
        "billing_error",
        "api_error",
        "overloaded_error",
        "timeout_error",
    }
)

# JSON 스키마 변환 규칙(SDK transform_schema와 동일)에서 그대로 통과시키는 string format
_SUPPORTED_STRING_FORMATS = frozenset(
    {"date-time", "time", "date", "duration", "email", "hostname", "uri", "ipv4", "ipv6", "uuid"}
)


def _transform_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """pydantic JSON 스키마 → 구조화 출력 API가 받는 부분집합.

    규칙은 공식 SDK `anthropic.transform_schema`(lib/_parse/_transform.py, 2.3.0)와 같다:
    object는 additionalProperties=false 강제, 지원하지 않는 키워드(minimum, pattern 등)는
    description 끝에 `{key: value}`로 붙여 모델이 따르도록만 한다. $defs는 $ref보다 먼저
    변환해 RootModel의 루트 $ref가 정의를 잃지 않게 한다.
    """
    rest = dict(schema)
    out: dict[str, Any] = {}

    defs = rest.pop("$defs", None)
    if defs is not None:
        out["$defs"] = {name: _transform_schema(sub) for name, sub in defs.items()}

    ref = rest.pop("$ref", None)
    if ref is not None:
        out["$ref"] = ref
        return out

    type_ = rest.pop("type", None)
    any_of = rest.pop("anyOf", None)
    one_of = rest.pop("oneOf", None)
    all_of = rest.pop("allOf", None)
    if isinstance(any_of, list):
        out["anyOf"] = [_transform_schema(v) for v in any_of]
    elif isinstance(one_of, list):
        out["anyOf"] = [_transform_schema(v) for v in one_of]
    elif isinstance(all_of, list):
        out["allOf"] = [_transform_schema(v) for v in all_of]
    else:
        if type_ is None:
            raise ValueError("스키마에 type·anyOf·oneOf·allOf 중 하나가 있어야 합니다")
        out["type"] = type_

    enum = rest.pop("enum", None)
    if isinstance(enum, list):
        out["enum"] = enum
    for key in ("description", "title"):
        value = rest.pop(key, None)
        if value is not None:
            out[key] = value

    if type_ == "object":
        out["properties"] = {
            key: _transform_schema(sub) for key, sub in rest.pop("properties", {}).items()
        }
        rest.pop("additionalProperties", None)
        out["additionalProperties"] = False
        required = rest.pop("required", None)
        if required is not None:
            out["required"] = required
    elif type_ == "string":
        fmt = rest.pop("format", None)
        if fmt and fmt in _SUPPORTED_STRING_FORMATS:
            out["format"] = fmt
        elif fmt:
            rest["format"] = fmt  # 미지원 format은 description으로 보낸다
    elif type_ == "array":
        items = rest.pop("items", None)
        if items is not None:
            out["items"] = _transform_schema(items)
        min_items = rest.pop("minItems", None)
        if min_items in (0, 1):
            out["minItems"] = min_items
        elif min_items is not None:
            rest["minItems"] = min_items
    elif type_ not in ("boolean", "integer", "number", "null", None):
        raise ValueError(f"지원하지 않는 스키마 type: {type_}")

    if rest:
        tail = "{" + ", ".join(f"{k}: {v}" for k, v in rest.items()) + "}"
        head = out.get("description")
        out["description"] = (head + "\n\n" if head is not None else "") + tail
    return out


def _schema_for(response_model: type[BaseModel]) -> dict[str, Any]:
    schema = _SCHEMA_CACHE.get(response_model)
    if schema is None:
        schema = _transform_schema(response_model.model_json_schema())
        _SCHEMA_CACHE[response_model] = schema
    return schema


class ClaudeModelClient:
    """AIModelClient 구현. 인증 헤더·base_url이 설정된 httpx2.Client를 주입받는다.

    테스트는 httpx2.MockTransport로 전송 계층만 바꾼다(유료 호출 없음).
    """

    provider = "anthropic"

    def __init__(
        self,
        *,
        client: httpx2.Client,
        model: str,
        policy: RetryPolicy,
        max_tokens: int,
        effort: Optional[str] = None,
        speed: Optional[str] = None,
    ) -> None:
        if max_tokens < 1:
            raise ValueError("max_tokens는 1 이상이어야 합니다")
        self._client = client
        self._model = model
        self._policy = policy
        self._max_tokens = max_tokens
        self._effort = effort
        self._speed = speed

    def complete(
        self,
        request: AIModelRequest,
        response_model: type[StructuredOutputT],
    ) -> AIModelResponse[StructuredOutputT]:
        # 마스킹과 JSON 직렬화 모두 경계 함수가 끝낸 상태다 — system_prompt·user_json만 보낸다
        payload = build_outbound_payload(request)

        def attempt() -> AttemptOutcome[StructuredOutputT]:
            # 스키마 변환 실패(예: type 없는 스키마)도 엔진이 ADAPTER_ERROR로 접도록
            # attempt 안에서 계산한다(클래스별 캐시는 유지).
            output_config: dict[str, Any] = {
                "format": {"type": "json_schema", "schema": _schema_for(response_model)}
            }
            if self._effort is not None:
                output_config["effort"] = self._effort
            body: dict[str, Any] = {
                "model": self._model,
                "max_tokens": self._max_tokens,
                "system": payload["system_prompt"],
                "messages": [{"role": "user", "content": payload["user_json"]}],
                "output_config": output_config,
            }
            if self._speed is not None:
                body["speed"] = self._speed
            # 예외는 except 안에서 AttemptFailure 값으로 바꿔 반환한다 — httpx 예외는
            # request(x-api-key 헤더)를 물고 있어 체인에 남으면 자격증명이 샌다(ADR-0005).
            try:
                response = self._client.post(
                    MESSAGES_PATH, json=body, timeout=self._policy.timeout_seconds
                )
            except Exception as exc:  # noqa: BLE001 — 전송 계열만 분류하고 나머지는 다시 올린다
                failure = _classify_exception(exc)
                if failure is None:
                    raise
                return failure
            return _classify_response(response, response_model)

        return ModelCallEngine(
            provider=self.provider,
            model=self._model,
            policy=self._policy,
            requested_service_tier=self._speed,
        ).run(attempt)


def _classify_exception(exc: Exception) -> Optional[AttemptFailure]:
    """전송 예외 → AttemptFailure. httpx 계열이 아니면 None(엔진이 ADAPTER_ERROR로 접는다)."""
    name = type(exc).__name__
    if isinstance(exc, httpx2.TimeoutException):
        return AttemptFailure(
            FailureKind.TRANSPORT_TIMEOUT, "모델 호출 제한시간을 초과했습니다", error_class=name
        )
    if isinstance(exc, httpx2.TransportError):
        return AttemptFailure(
            FailureKind.TRANSPORT_CONNECTION, "모델에 연결하지 못했습니다", error_class=name
        )
    if isinstance(exc, httpx2.HTTPError):
        # 전송 전·후의 그 밖의 오류(디코딩·리다이렉트 등) — 응답을 쓸 수 없으니 요청 실패다
        return AttemptFailure(
            FailureKind.REQUEST_REJECTED, "모델 호출을 시작하지 못했습니다", error_class=name
        )
    return None


def _json_body(response: httpx2.Response) -> Optional[Any]:
    try:
        return response.json()
    except (ValueError, UnicodeDecodeError):
        return None


def _error_fields(body: Any) -> tuple[str, str]:
    """오류 본문에서 (error.type, error.message)만 판정용으로 읽는다. 저장·로그하지 않는다."""
    if not isinstance(body, dict):
        return "", ""
    error = body.get("error")
    if not isinstance(error, dict):
        return "", ""
    type_ = error.get("type")
    message = error.get("message")
    return (
        type_ if isinstance(type_, str) else "",
        message if isinstance(message, str) else "",
    )


def _classify_status(response: httpx2.Response) -> AttemptFailure:
    status = response.status_code
    error_type, message = _error_fields(_json_body(response))
    # 진단 문자열은 알려진 error.type만 남긴다 — 임의 문자열이 로그로 흘러가지 않게
    name = error_type if error_type in _KNOWN_ERROR_TYPES else f"http_{status}"

    if status in (429, 408, 409):
        kind = FailureKind.RATE_LIMITED
    elif status >= 500:
        # 500·503·504·529(overloaded) 모두 일시 장애
        kind = FailureKind.SERVER_ERROR
    elif status == 402:
        return AttemptFailure(
            FailureKind.QUOTA_EXHAUSTED,
            "결제·크레딧 한도가 소진됐습니다",
            error_class=name,
            status_code=status,
        )
    else:
        lowered = message.lower()
        # output_config.effort 같은 요청 설정 오류와 구분하려고 format·schema 언급만 본다
        is_schema = status == 400 and ("schema" in lowered or "output_config.format" in lowered)
        return AttemptFailure(
            FailureKind.SCHEMA_REJECTED if is_schema else FailureKind.REQUEST_REJECTED,
            "제공자가 구조화 출력 스키마를 거절했습니다"
            if is_schema
            else "모델이 호출을 거절했습니다",
            error_class=name,
            status_code=status,
        )
    return AttemptFailure(
        kind,
        "모델을 일시적으로 사용할 수 없습니다",
        error_class=name,
        status_code=status,
        retry_after_seconds=retry_after_seconds(response.headers),
    )


def _classify_response(
    response: httpx2.Response, response_model: type[StructuredOutputT]
) -> AttemptOutcome[StructuredOutputT]:
    if response.status_code != 200:
        return _classify_status(response)
    message = _json_body(response)
    if not isinstance(message, dict):
        return AttemptFailure(
            FailureKind.RESPONSE_SHAPE,
            "응답 본문이 JSON 객체가 아닙니다",
            usage=_token_usage(None),
            status_code=200,
        )
    return _classify_message(message, response_model)


def _classify_message(
    message: dict[str, Any], response_model: type[StructuredOutputT]
) -> AttemptOutcome[StructuredOutputT]:
    # 사용량은 응답 판정보다 먼저 계산한다 — refusal·잘림 응답도 토큰을 쓴다
    raw_usage = message.get("usage")
    raw_usage = raw_usage if isinstance(raw_usage, dict) else None
    usage = _token_usage(raw_usage)
    returned_model = message.get("model") if isinstance(message.get("model"), str) else None
    returned_tier = _returned_tier(raw_usage)

    def failure(kind: FailureKind, text: str) -> AttemptFailure:
        return AttemptFailure(
            kind,
            text,
            usage=usage,
            returned_model=returned_model,
            returned_service_tier=returned_tier,
        )

    stop_reason = message.get("stop_reason")
    if stop_reason == "refusal":
        return failure(FailureKind.REFUSAL, "모델이 응답을 거절했습니다")
    if stop_reason in _TRUNCATED_STOP_REASONS:
        return failure(FailureKind.TRUNCATED, "출력이 길이 상한에서 잘렸습니다")

    content = message.get("content")
    text = next(
        (
            block.get("text")
            for block in (content if isinstance(content, list) else [])
            if isinstance(block, dict)
            and block.get("type") == "text"
            and isinstance(block.get("text"), str)
        ),
        None,
    )
    if text is None:
        return failure(FailureKind.RESPONSE_SHAPE, "응답에 텍스트 블록이 없습니다")

    try:
        output = response_model.model_validate_json(text)
    except ValidationError:
        # 메시지는 모델 원문(input_value)을 담으므로 버린다
        return failure(FailureKind.OUTPUT_INVALID, "응답을 요구한 구조로 파싱하지 못했습니다")
    return AttemptSuccess(
        output=output,
        usage=usage,
        returned_model=returned_model,
        returned_service_tier=returned_tier,
    )


def _returned_tier(raw: Optional[dict[str, Any]]) -> Optional[str]:
    """speed(fast 여부)를 우선하고, 없으면 service_tier. 둘 다 없으면 None(미확인)."""
    if raw is None:
        return None
    for key in ("speed", "service_tier"):
        value = raw.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _token_usage(raw: Optional[dict[str, Any]]) -> TokenUsage:
    """Anthropic 사용량 → 경계 의미. input_tokens는 캐시 읽기·쓰기를 제외하므로 더한다."""
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
    cache_read = raw.get("cache_read_input_tokens")
    cache_creation = raw.get("cache_creation_input_tokens")
    prompt = int(raw.get("input_tokens") or 0) + int(cache_read or 0) + int(cache_creation or 0)
    completion = int(raw.get("output_tokens") or 0)
    return TokenUsage(
        prompt_tokens=prompt,
        completion_tokens=completion,
        total_tokens=prompt + completion,
        cached_prompt_tokens=int(cache_read or 0),
        cache_creation_prompt_tokens=None if cache_creation is None else int(cache_creation),
        unreported=() if cache_read is not None else ("cached_prompt_tokens",),
    )


def build_http_client(
    *, api_key: str, timeout_seconds: float, base_url: str = DEFAULT_BASE_URL
) -> httpx2.Client:
    """인증·버전 헤더가 붙은 전송 클라이언트. 재시도는 없다(소유자는 엔진 하나)."""
    return httpx2.Client(
        base_url=base_url,
        headers={
            "x-api-key": api_key,
            "anthropic-version": API_VERSION,
            "content-type": "application/json",
        },
        timeout=timeout_seconds,
    )


def build_claude_model_client(
    *,
    api_key: Optional[str],
    model: str,
    policy: RetryPolicy,
    max_tokens: int,
    effort: Optional[str] = None,
    speed: Optional[str] = None,
    base_url: str = DEFAULT_BASE_URL,
) -> ClaudeModelClient:
    """실제 전송 클라이언트로 경계 구현을 구성한다. 키는 호출자(실험 도구)가 넘긴다."""
    if not api_key:
        raise ValueError("Anthropic API 키가 없어 모델 클라이언트를 만들 수 없습니다")
    return ClaudeModelClient(
        client=build_http_client(
            api_key=api_key, timeout_seconds=policy.timeout_seconds, base_url=base_url
        ),
        model=model,
        policy=policy,
        max_tokens=max_tokens,
        effort=effort,
        speed=speed,
    )
