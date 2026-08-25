# ==============================================================================
# [파일 설명]  담당: 안성일 (AI/Guardrail · Architect)
# ai/model_client.py 경계의 OpenAI 구현입니다. (Issue #115)
# SDK를 부르는 유일한 지점이며, SDK 예외를 경계 예외로 바꿔서 내보냅니다.
#
#   - 재시도는 일시 오류(제한시간·연결·408·409·429·5xx)만 대상으로 한다. 인증·요청
#     오류와 구조화 출력 파싱 실패는 다시 불러도 결과가 같으므로 즉시 올린다.
#   - SDK 자체 재시도를 껐으므로(max_retries=0) SDK가 하던 Retry-After 존중과 지터를
#     이 래퍼가 승계한다. 서버 지시 대기가 상한을 넘으면 무시하고 backoff로 간다.
#   - 구조화 출력은 SDK의 parse 경로로 받는다 — 응답 텍스트를 직접 파싱하지 않는다.
#   - 남기는 로그는 토큰 사용량·시도 횟수 같은 호출 메타뿐이다. Prompt 전문과 원문
#     응답은 남기지 않는다(ADR-0005 미보존 대상).
# ==============================================================================

from __future__ import annotations

import logging
import random
import time
from typing import Any, Optional

from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    InternalServerError,
    OpenAI,
    OpenAIError,
    RateLimitError,
)
from pydantic import ValidationError

from ai.model_client import (
    AIModelContractError,
    AIModelError,
    AIModelRejectedError,
    AIModelRequest,
    AIModelResponse,
    AIModelTimeoutError,
    AIModelUnavailableError,
    StructuredOutputT,
    TokenUsage,
    build_outbound_payload,
)
from config import Settings, get_settings

logger = logging.getLogger("vigilantis.ai")

# ADR-0005: Prompt 전문은 로그 레벨과 무관하게 출력하지 않는다. SDK는 DEBUG에서
# 요청 본문(messages 전문)을 로그로 남기므로("Request options", openai/_base_client.py)
# 앱 LOG_LEVEL 설정과 무관하게 SDK 계열 로거를 여기서 차단한다.
for _sdk_logger_name in ("openai", "httpx2"):
    logging.getLogger(_sdk_logger_name).setLevel(logging.WARNING)


class OpenAIModelClient:
    """AIModelClient 구현. SDK 객체를 주입받아 테스트에서 교체할 수 있다."""

    def __init__(
        self,
        *,
        client: Any,
        model: str,
        timeout_seconds: float,
        max_attempts: int,
        retry_backoff_seconds: float,
        max_retry_after_seconds: float = 60.0,
    ) -> None:
        self._client = client
        self._model = model
        self._timeout_seconds = timeout_seconds
        self._max_attempts = max_attempts
        self._retry_backoff_seconds = retry_backoff_seconds
        self._max_retry_after_seconds = max_retry_after_seconds

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

        # SDK 예외는 경계 예외의 __cause__/__context__ 어디에도 보존하지 않는다 —
        # request 객체가 Authorization 헤더를 물고 있어 체인에 남으면 자격증명이
        # 경계 밖으로 새는 셈이다(ADR-0005 원칙 3). 그래서 except 안에서는 경계
        # 예외 객체만 만들고(진단용으로 SDK 예외의 타입 이름 문자열만 보존),
        # raise는 try/except 문이 끝난 뒤에 한다.
        transient: AIModelError = AIModelUnavailableError("모델 호출에 실패했습니다")
        for attempt in range(1, self._max_attempts + 1):
            completion: Any = None
            rejected: Optional[AIModelError] = None
            retry_after: Optional[float] = None
            try:
                completion = self._client.chat.completions.parse(
                    model=self._model,
                    messages=messages,
                    response_format=response_model,
                    timeout=self._timeout_seconds,
                )
            except APITimeoutError:
                transient = AIModelTimeoutError("모델 호출 제한시간을 초과했습니다")
            except (APIConnectionError, RateLimitError, InternalServerError) as exc:
                transient = AIModelUnavailableError(
                    f"모델을 일시적으로 사용할 수 없습니다 ({type(exc).__name__})"
                )
                retry_after = _retry_after_seconds(exc)
            except ValidationError:
                # SDK가 응답을 구조화 출력으로 검증하다 실패 — 재호출해도 같다
                rejected = AIModelContractError("응답을 요구한 구조로 파싱하지 못했습니다")
            except APIStatusError as exc:
                # SDK 재시도 정책(_base_client._should_retry)은 408·409·429·5xx를
                # 일시 오류로 본다. SDK 재시도를 꺼 놨으므로(max_retries=0) 전용
                # 예외가 없는 408·409도 여기서 래퍼 재시도로 옮긴다
                if exc.status_code in (408, 409):
                    transient = AIModelUnavailableError(
                        f"모델을 일시적으로 사용할 수 없습니다 (HTTP {exc.status_code})"
                    )
                    retry_after = _retry_after_seconds(exc)
                else:
                    rejected = AIModelRejectedError(
                        f"모델이 호출을 거절했습니다 ({type(exc).__name__})"
                    )
            except OpenAIError as exc:
                rejected = AIModelRejectedError(
                    f"모델 호출을 시작하지 못했습니다 ({type(exc).__name__})"
                )

            if rejected is not None:
                raise rejected
            if completion is not None:
                return self._to_response(completion, response_model, attempt)

            # 마지막 실패는 예외로 올라간다 — retry 로그는 실제 재시도 전에만 남긴다
            if attempt < self._max_attempts:
                delay = self._retry_delay(attempt, retry_after)
                logger.warning(
                    "ai_model_retry",
                    extra={
                        "model": self._model,
                        "attempt": attempt,
                        "max_attempts": self._max_attempts,
                        "reason": type(transient).__name__,
                        "delay_seconds": round(delay, 3),
                        "server_directed": retry_after is not None,
                    },
                )
                time.sleep(delay)

        raise transient

    def _retry_delay(self, attempt: int, retry_after: Optional[float]) -> float:
        """서버가 지시한 대기를 우선하고, 없으면 backoff에 지터를 얹는다.

        상한을 넘는 Retry-After는 따르지 않는다 — 요청 경로에서 부르는 호출이라
        무한정 붙잡고 있는 것보다 실패로 돌려주고 다시 태우는 편이 낫다.
        지터는 동시에 실패한 호출이 같은 시각에 다시 몰리는 것을 막는다(SDK와 같은 형태).
        """
        if retry_after is not None and 0 < retry_after <= self._max_retry_after_seconds:
            return retry_after
        return self._retry_backoff_seconds * attempt * (1 - 0.25 * random.random())

    def _to_response(
        self,
        completion: Any,
        response_model: type[StructuredOutputT],
        attempt: int,
    ) -> AIModelResponse[StructuredOutputT]:
        # 사용량 집계는 응답 판정보다 먼저 남긴다 — refusal·계약 위반 응답도 토큰을 쓴다
        usage = _token_usage(getattr(completion, "usage", None))
        model = getattr(completion, "model", None) or self._model
        logger.info(
            "ai_model_call",
            extra={
                "model": model,
                "attempts": attempt,
                "prompt_tokens": usage.prompt_tokens,
                "completion_tokens": usage.completion_tokens,
                "total_tokens": usage.total_tokens,
            },
        )

        choices = getattr(completion, "choices", None) or []
        if not choices:
            raise AIModelContractError("응답에 선택지가 없습니다")

        message = choices[0].message
        if getattr(message, "refusal", None):
            raise AIModelRejectedError("모델이 응답을 거절했습니다")

        parsed = getattr(message, "parsed", None)
        if not isinstance(parsed, response_model):
            raise AIModelContractError(
                f"응답을 {response_model.__name__}으로 파싱하지 못했습니다"
            )

        return AIModelResponse(output=parsed, usage=usage, model=model)


def _retry_after_seconds(exc: Any) -> Optional[float]:
    """OpenAI가 보내는 retry-after-ms·retry-after(초) 헤더만 읽는다.

    HTTP-date 형식은 OpenAI가 쓰지 않으므로 다루지 않는다 — 읽지 못하면 None으로
    두고 backoff로 간다. 연결 오류처럼 응답이 없는 예외도 None이다.
    """
    headers = getattr(getattr(exc, "response", None), "headers", None)
    if headers is None:
        return None
    for name, scale in (("retry-after-ms", 0.001), ("retry-after", 1.0)):
        raw = headers.get(name)
        if raw is None:
            continue
        try:
            return float(raw) * scale
        except (TypeError, ValueError):
            continue
    return None


def _token_usage(raw: Any) -> TokenUsage:
    """사용량이 비어 오는 응답도 있으므로 0으로 채운다 — 호출 자체는 성공이다."""
    prompt = int(getattr(raw, "prompt_tokens", 0) or 0)
    completion = int(getattr(raw, "completion_tokens", 0) or 0)
    total = int(getattr(raw, "total_tokens", 0) or 0) or (prompt + completion)
    return TokenUsage(
        prompt_tokens=prompt, completion_tokens=completion, total_tokens=total
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
        timeout_seconds=settings.OPENAI_TIMEOUT_SECONDS,
        max_attempts=settings.OPENAI_MAX_ATTEMPTS,
        retry_backoff_seconds=settings.OPENAI_RETRY_BACKOFF_SECONDS,
        max_retry_after_seconds=settings.OPENAI_MAX_RETRY_AFTER_SECONDS,
    )
