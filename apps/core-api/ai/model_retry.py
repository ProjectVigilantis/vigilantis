# ==============================================================================
# [파일 설명]  담당: 안성일 (AI/Guardrail · Architect)
# 모델 호출 재시도 정책과 시도 기록 엔진입니다. (Issue #434 §4-2)
# ai/openai_client.py와 ai/claude_client.py가 같은 엔진 위에서 돈다 — 제공자마다 다른 것은
# SDK 호출과 "SDK 예외·응답 → FailureKind" 분류만이고, 재시도 횟수·대기·타임아웃·시도 기록·
# 로그·경계 예외 생성은 여기 한 곳이 소유한다.
#
#   - 예산은 둘이다. 전송 예산(max_attempts)은 일시 오류(TRANSPORT_RETRYABLE)에, 출력 예산
#     (output_retries)은 응답은 받았지만 구조로 쓸 수 없는 경우(OUTPUT_RETRYABLE)에 쓴다.
#     출력 예산의 기본값은 0이다 — 운영 동작(#115: 파싱 실패는 즉시 실패)을 유지하고,
#     늘리는 것은 비용이 따르는 정책 결정이라 설정·실험 조건으로만 켠다.
#   - SDK 자체 재시도는 꺼 둔다(max_retries=0). SDK가 하던 Retry-After 존중과 지터를 이
#     엔진이 승계한다. 서버 지시 대기가 상한을 넘으면 따르지 않고 backoff로 간다.
#   - 어댑터는 SDK 예외를 **던지지 않고 AttemptFailure로 반환**한다. 경계 예외는 except
#     블록 밖에서 만들어 올리므로 __cause__/__context__에 SDK 예외(Authorization 헤더를
#     문 request 객체)가 남지 않는다(ADR-0005 원칙 3).
#   - 어댑터가 분류하지 못한 예외는 ADAPTER_ERROR로 접는다. 경계 밖으로 새면 그래프가
#     FAILED로 접지 못하고 디스패처 선점이 IN_PROGRESS로 남아 회수 뒤 과금 호출이
#     반복된다(#434 §4-2). 예외 메시지는 남기지 않는다 — pydantic ValidationError 메시지는
#     모델 원문 응답(input_value)을 담는다.
#   - 로그는 호출 메타뿐이다(모델·시도·토큰·소요 시간·tier). Prompt·응답·단가·비용은 없다.
# ==============================================================================

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Generic, Optional, Union

from ai.model_client import (
    FAILURE_EXCEPTION,
    FAILURE_PHASE,
    OUTPUT_RETRYABLE,
    TRANSPORT_RETRYABLE,
    AIModelError,
    AIModelResponse,
    AttemptRecord,
    CallTrace,
    FailureKind,
    StructuredOutputT,
    TokenUsage,
)

logger = logging.getLogger("vigilantis.ai")


@dataclass(frozen=True)
class RetryPolicy:
    """호출 1회의 재시도·대기·제한시간. 운영값은 from_settings()로 Settings에서 읽는다."""

    timeout_seconds: float
    max_attempts: int  # 전송 예산. 1이면 재시도 없음
    retry_backoff_seconds: float
    max_retry_after_seconds: float = 60.0
    # 출력 예산 — TRUNCATED·OUTPUT_INVALID 뒤 다시 부르는 횟수. 기본 0 = 현행 유지
    output_retries: int = 0

    def __post_init__(self) -> None:
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds는 0보다 커야 합니다")
        if self.max_attempts < 1:
            raise ValueError("max_attempts는 1 이상이어야 합니다")
        if self.retry_backoff_seconds < 0 or self.max_retry_after_seconds < 0:
            raise ValueError("대기 시간은 음수일 수 없습니다")
        if self.output_retries < 0:
            raise ValueError("output_retries는 0 이상이어야 합니다")

    @property
    def max_calls(self) -> int:
        """한 complete()가 SDK를 부를 수 있는 최대 횟수(전송 + 출력 예산)."""
        return self.max_attempts + self.output_retries

    def worst_case_seconds(self) -> float:
        """매 시도가 제한시간을 다 쓰고 시도 사이마다 상한 대기를 하는 경우.

        agent_dispatcher.stale_claim_ceiling_seconds가 호출 1회 최악값으로 이 값을 쓴다 —
        출력 재시도 예산이 생기면 그 호출 수까지 Claim 상한에 함께 들어간다.
        """
        calls = self.max_calls
        return self.timeout_seconds * calls + self.max_retry_after_seconds * (calls - 1)

    @classmethod
    def from_settings(cls, settings: Any) -> "RetryPolicy":
        """운영 설정(config.py OPENAI_*)에서 만든다. 출력 예산은 설정 키가 없어 0이다."""
        return cls(
            timeout_seconds=settings.OPENAI_TIMEOUT_SECONDS,
            max_attempts=settings.OPENAI_MAX_ATTEMPTS,
            retry_backoff_seconds=settings.OPENAI_RETRY_BACKOFF_SECONDS,
            max_retry_after_seconds=settings.OPENAI_MAX_RETRY_AFTER_SECONDS,
        )


# ------------------------------------------------------------------------------
# 시도 결과 — 어댑터가 엔진에 돌려주는 값. 예외를 던지지 않는다
# ------------------------------------------------------------------------------


@dataclass(frozen=True)
class AttemptSuccess(Generic[StructuredOutputT]):
    output: StructuredOutputT
    usage: TokenUsage
    returned_model: Optional[str] = None  # None = 응답에 모델 표기 없음(미확인)
    returned_service_tier: Optional[str] = None  # None = 응답에 tier 표기 없음(미확인)


@dataclass(frozen=True)
class AttemptFailure:
    kind: FailureKind
    message: str  # 우리가 쓴 설명. SDK 예외 메시지·응답 본문은 넣지 않는다
    error_class: Optional[str] = None  # SDK 예외 타입 이름(진단용 문자열만)
    status_code: Optional[int] = None
    retry_after_seconds: Optional[float] = None  # 서버가 지시한 대기(초). 없으면 None
    usage: Optional[TokenUsage] = None  # 응답을 받은 실패는 토큰이 이미 발생했다
    returned_model: Optional[str] = None
    returned_service_tier: Optional[str] = None
    phase: Optional[str] = None  # 지정하지 않으면 FAILURE_PHASE[kind]


AttemptOutcome = Union[AttemptSuccess[StructuredOutputT], AttemptFailure]


class ModelCallEngine:
    """complete() 1회의 시도 반복·대기·기록·예외 변환.

    어댑터는 `attempt()`(SDK 호출 1회 → AttemptOutcome)만 넘긴다. 같은 요청의 재실행이므로
    모델 파라미터는 어댑터가 attempt 안에 고정해 둔다 — 1회차와 다른 설정으로 재시도되면
    재현성 측정에서 같은 케이스가 두 설정으로 돈 셈이 된다.
    """

    def __init__(
        self,
        *,
        provider: str,
        model: str,
        policy: RetryPolicy,
        requested_service_tier: Optional[str] = None,
    ) -> None:
        self._provider = provider
        self._model = model
        self._policy = policy
        self._requested_service_tier = requested_service_tier

    @property
    def policy(self) -> RetryPolicy:
        return self._policy

    def run(
        self, attempt: Callable[[], AttemptOutcome[StructuredOutputT]]
    ) -> AIModelResponse[StructuredOutputT]:
        policy = self._policy
        records: list[AttemptRecord] = []
        transport_used = 0
        output_used = 0
        call_started = time.monotonic()
        final: Optional[AttemptFailure] = None
        success: Optional[AttemptSuccess[StructuredOutputT]] = None

        while True:
            number = len(records) + 1
            started_at = datetime.now(timezone.utc)
            started = time.monotonic()
            outcome: AttemptOutcome[StructuredOutputT]
            adapter_error_class: Optional[str] = None
            try:
                outcome = attempt()
            except Exception as exc:  # noqa: BLE001 — 분류되지 않은 예외는 어댑터 몫으로 접는다
                adapter_error_class = type(exc).__name__
                outcome = AttemptFailure(
                    FailureKind.ADAPTER_ERROR,
                    f"어댑터가 분류하지 못한 예외입니다 ({adapter_error_class})",
                    error_class=adapter_error_class,
                )
            elapsed = time.monotonic() - started
            if adapter_error_class is not None:
                # traceback·메시지를 남기지 않는다 — ValidationError 메시지는 원문 응답을 담는다
                logger.error(
                    "ai_model_adapter_error",
                    extra={
                        "provider": self._provider,
                        "model": self._model,
                        "attempt": number,
                        "error_class": adapter_error_class,
                    },
                )

            if isinstance(outcome, AttemptSuccess):
                records.append(
                    AttemptRecord(
                        attempt=number,
                        started_at=started_at,
                        elapsed_seconds=elapsed,
                        outcome="ok",
                        usage=outcome.usage,
                    )
                )
                success = outcome
                break

            failure = outcome
            if failure.kind in TRANSPORT_RETRYABLE:
                transport_used += 1
                can_retry = transport_used < policy.max_attempts
            elif failure.kind in OUTPUT_RETRYABLE:
                output_used += 1
                can_retry = output_used <= policy.output_retries
            else:
                can_retry = False

            delay: Optional[float] = None
            server_directed: Optional[bool] = None
            if can_retry:
                server_directed = (
                    failure.retry_after_seconds is not None
                    and 0 < failure.retry_after_seconds <= policy.max_retry_after_seconds
                )
                delay = self._retry_delay(number, failure.retry_after_seconds)
            records.append(
                AttemptRecord(
                    attempt=number,
                    started_at=started_at,
                    elapsed_seconds=elapsed,
                    outcome=failure.kind.value,
                    error_class=failure.error_class,
                    status_code=failure.status_code,
                    retry_delay_seconds=delay,
                    server_directed=server_directed,
                    usage=failure.usage,
                )
            )
            if not can_retry:
                final = failure
                break
            # 마지막 실패는 예외로 올라간다 — retry 로그는 실제 재시도 전에만 남긴다
            logger.warning(
                "ai_model_retry",
                extra={
                    "provider": self._provider,
                    "model": self._model,
                    "attempt": number,
                    "max_attempts": policy.max_attempts,
                    "output_retries": policy.output_retries,
                    "reason": FAILURE_EXCEPTION[failure.kind].__name__,
                    "kind": failure.kind.value,
                    "delay_seconds": round(delay or 0.0, 3),
                    "server_directed": bool(server_directed),
                },
            )
            time.sleep(delay or 0.0)

        last = success if success is not None else final
        assert last is not None  # 루프는 success 또는 final로만 끝난다
        trace = CallTrace(
            provider=self._provider,
            requested_model=self._model,
            returned_model=last.returned_model,
            requested_service_tier=self._requested_service_tier,
            returned_service_tier=last.returned_service_tier,
            attempts=tuple(records),
            max_attempts=policy.max_attempts,
            output_retries=policy.output_retries,
            timeout_seconds=policy.timeout_seconds,
            elapsed_seconds=time.monotonic() - call_started,
        )
        # 호출 단위 usage는 시도별 usage의 합이다 — 출력 재시도는 시도마다 토큰을 쓰므로
        # 마지막 시도만 세면 앞 시도의 과금이 어느 집계에도 남지 않는다. 응답이 없는 시도
        # (전송 실패)는 토큰을 알 수 없어, 그런 시도가 섞이면 unreported에 표시하고
        # 전부 그렇다면 None(미확인)이다
        usage = accumulate_usage(records)
        logger.info(
            "ai_model_call",
            extra={
                "provider": self._provider,
                "model": last.returned_model or self._model,
                "attempts": len(records),
                "outcome": "ok" if success is not None else final.kind.value,  # type: ignore[union-attr]
                "prompt_tokens": usage.prompt_tokens if usage else None,
                "completion_tokens": usage.completion_tokens if usage else None,
                "total_tokens": usage.total_tokens if usage else None,
                "cached_prompt_tokens": usage.cached_prompt_tokens if usage else None,
                "service_tier": last.returned_service_tier,
                "elapsed_seconds": round(trace.elapsed_seconds, 3),
            },
        )

        if success is not None:
            assert usage is not None  # 성공 시도는 항상 usage를 가진다
            return AIModelResponse(
                output=success.output,
                usage=usage,
                model=success.returned_model or self._model,
                trace=trace,
            )
        assert final is not None
        # raise는 try/except 밖이다 — SDK 예외가 체인에 남지 않는다
        raise self._to_exception(final, usage, trace)

    def _retry_delay(self, attempt: int, retry_after: Optional[float]) -> float:
        """서버가 지시한 대기를 우선하고, 없으면 backoff에 지터를 얹는다.

        상한을 넘는 Retry-After는 따르지 않는다 — 요청 경로에서 부르는 호출이라
        무한정 붙잡고 있는 것보다 실패로 돌려주고 다시 태우는 편이 낫다.
        지터는 동시에 실패한 호출이 같은 시각에 다시 몰리는 것을 막는다(SDK와 같은 형태).
        """
        if retry_after is not None and 0 < retry_after <= self._policy.max_retry_after_seconds:
            return retry_after
        return self._policy.retry_backoff_seconds * attempt * (1 - 0.25 * random.random())

    @staticmethod
    def _to_exception(
        failure: AttemptFailure, usage: Optional[TokenUsage], trace: CallTrace
    ) -> AIModelError:
        return FAILURE_EXCEPTION[failure.kind](
            failure.message,
            usage=usage,
            phase=failure.phase or FAILURE_PHASE[failure.kind],
            kind=failure.kind,
            trace=trace,
        )


# 응답을 받지 못한 시도가 섞여 호출 단위 합이 실제보다 작을 수 있음을 표시하는 이름
USAGE_PARTIAL_ATTEMPTS = "attempts_without_usage"


def accumulate_usage(records: Sequence[AttemptRecord]) -> Optional[TokenUsage]:
    """시도별 usage를 호출 단위로 합친다. 알려진 시도가 하나도 없으면 None."""
    known = [record.usage for record in records if record.usage is not None]
    if not known:
        return None
    # 어댑터가 적은 순서를 보존한 채 중복만 없앤다
    unreported: dict[str, None] = {}
    for item in known:
        for name in item.unreported:
            unreported.setdefault(name, None)
    if len(known) < len(records):
        unreported.setdefault(USAGE_PARTIAL_ATTEMPTS, None)
    creations = [
        item.cache_creation_prompt_tokens
        for item in known
        if item.cache_creation_prompt_tokens is not None
    ]
    # 일부 시도만 캐시 쓰기를 보고했다면 소계는 남기되 완전한 값이 아님을 표시한다.
    # 전부 None(OpenAI처럼 제공자가 주지 않음)이면 표시 없이 None이다
    if creations and len(creations) < len(known):
        unreported.setdefault("cache_creation_prompt_tokens", None)
    return TokenUsage(
        prompt_tokens=sum(item.prompt_tokens for item in known),
        completion_tokens=sum(item.completion_tokens for item in known),
        total_tokens=sum(item.total_tokens for item in known),
        cached_prompt_tokens=sum(item.cached_prompt_tokens for item in known),
        cache_creation_prompt_tokens=sum(creations) if creations else None,
        unreported=tuple(unreported),
    )


def retry_after_seconds(headers: Any) -> Optional[float]:
    """retry-after-ms(OpenAI) · retry-after(초, 양쪽)만 읽는다.

    HTTP-date 형식은 두 제공자가 쓰지 않으므로 다루지 않는다 — 읽지 못하면 None으로
    두고 backoff로 간다. 응답이 없는 예외(연결 오류)는 headers가 None이다.
    """
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
