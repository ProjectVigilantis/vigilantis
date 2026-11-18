"""Luna의 Chat Completions 형식으로 호출하는 Bedrock InvokeModel 어댑터.

다른 OpenAI 모델의 호환성을 보장하지 않는다. 모델·리전은 호출자가 명시한다.
AWS의 GPT-5.6 이후 캐시 계약에 따라 explicit 모드에서 breakpoint를 넣지 않는다.
이는 프롬프트 캐시 비사용 제어이며, 공급자 보관·ZDR 준수의 증거는 아니다.
https://docs.aws.amazon.com/bedrock/latest/userguide/prompt-caching.html#prompt-caching-openai-56
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

# SDK 비공개 모듈(같은 기능의 공개 API 없음). openai를 올릴 때 이 이름이 남아 있는지 확인한다.
from openai.lib._parsing import type_to_response_format_param
from pydantic import ValidationError

from ai.bedrock_anthropic_client import BedrockModelClient, bedrock_retry_policy
from ai.model_client import FailureKind, StructuredOutputT, TokenUsage
from ai.model_retry import AttemptFailure, AttemptOutcome, AttemptSuccess
from ai.openai_client import _token_usage
from services.aws.client import TenantAccess, tenant_bedrock_runtime_client


def _usage(message: dict[str, Any]) -> TokenUsage:
    raw = message.get("usage")
    if not isinstance(raw, dict):
        return _token_usage(None)
    details = raw.get("prompt_tokens_details")
    details = details if isinstance(details, dict) else {}
    usage = _token_usage(SimpleNamespace(
        prompt_tokens=raw.get("prompt_tokens"),
        completion_tokens=raw.get("completion_tokens"),
        total_tokens=raw.get("total_tokens"),
        prompt_tokens_details=SimpleNamespace(cached_tokens=details.get("cached_tokens")),
    ))
    cached = details.get("cached_tokens")
    if raw.get("prompt_tokens") is None and cached is not None:
        # 전체 입력이 미보고이면, 보고된 캐시 부분을 0이라는 가짜 상한으로 자르지 않는다.
        usage = replace(usage, cached_prompt_tokens=int(cached))
    written = details.get("cache_write_tokens")
    return replace(
        usage,
        cache_creation_prompt_tokens=int(written) if written is not None else None,
    )


class BedrockOpenAIModelClient(BedrockModelClient):
    """공통 고객 자격·인가·재시도 경계를 유지하고 GPT 요청·응답 형식만 바꾼다."""

    def __init__(self, *, reasoning_effort: str | None = None, **kwargs: Any) -> None:
        if reasoning_effort is not None and reasoning_effort not in {
            "none", "low", "medium", "high", "xhigh", "max",
        }:
            raise ValueError("reasoning_effort는 Luna가 지원하는 값이어야 합니다")
        super().__init__(**kwargs)
        self._reasoning_effort = reasoning_effort

    def _request_body(
        self, payload: dict[str, Any], response_model: type[StructuredOutputT]
    ) -> dict[str, Any]:
        body = {
            "model": self._model_id,
            "messages": [
                {"role": "system", "content": payload["system_prompt"]},
                {"role": "user", "content": payload["user_json"]},
            ],
            "max_completion_tokens": self._max_tokens,
            "response_format": type_to_response_format_param(response_model),
            "prompt_cache_options": {"mode": "explicit"},
        }
        if self._reasoning_effort is not None:
            body["reasoning_effort"] = self._reasoning_effort
        return body

    def _classify_body(
        self, message: dict[str, Any], response_model: type[StructuredOutputT]
    ) -> AttemptOutcome[StructuredOutputT]:
        usage = _usage(message)
        model = message.get("model")
        tier = message.get("service_tier")
        metadata = {
            "usage": usage,
            "returned_model": model if isinstance(model, str) else None,
            "returned_service_tier": tier if isinstance(tier, str) else None,
        }

        def failure(kind: FailureKind, text: str) -> AttemptFailure:
            return AttemptFailure(kind, text, **metadata)

        choices = message.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            return failure(FailureKind.RESPONSE_SHAPE, "응답에 유효한 선택지가 없습니다")
        choice = choices[0]
        reply = choice.get("message")
        if not isinstance(reply, dict) or reply.get("role") != "assistant":
            return failure(FailureKind.RESPONSE_SHAPE, "유효한 assistant 응답이 아닙니다")
        if reply.get("refusal"):
            return failure(FailureKind.REFUSAL, "모델이 응답을 거절했습니다")
        finish = choice.get("finish_reason")
        if finish == "length":
            return failure(FailureKind.TRUNCATED, "출력이 길이 상한에서 잘렸습니다")
        if finish == "content_filter":
            return failure(FailureKind.REFUSAL, "콘텐츠 필터가 응답을 거절했습니다")
        content = reply.get("content")
        if finish != "stop" or not isinstance(content, str):
            return failure(FailureKind.RESPONSE_SHAPE, "완료된 텍스트 응답이 아닙니다")
        try:
            output = response_model.model_validate_json(content)
        except ValidationError:
            return failure(FailureKind.OUTPUT_INVALID, "응답이 요구한 스키마와 다릅니다")
        return AttemptSuccess(output=output, **metadata)


def build_bedrock_openai_model_client(
    *,
    owner: TenantAccess | None,
    consent_check: Callable[[], bool],
    region: str,
    model_id: str,
    max_tokens: int = 8192,
    connect_timeout: float = 5.0,
    read_timeout: float = 120.0,
    max_attempts: int = 3,
    reasoning_effort: str | None = None,
    runtime_factory: Callable[..., Any] = tenant_bedrock_runtime_client,
) -> BedrockOpenAIModelClient:
    return BedrockOpenAIModelClient(
        owner=owner,
        consent_check=consent_check,
        region=region,
        model_id=model_id,
        policy=bedrock_retry_policy(
            connect_timeout=connect_timeout, read_timeout=read_timeout, max_attempts=max_attempts,
        ),
        max_tokens=max_tokens,
        connect_timeout=connect_timeout,
        read_timeout=read_timeout,
        reasoning_effort=reasoning_effort,
        runtime_factory=runtime_factory,
    )
