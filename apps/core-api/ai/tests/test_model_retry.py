"""재시도 엔진의 호출 단위 usage 합산 (Issue #434 §4-2).

어댑터 테스트(test_model_client.py·test_claude_client.py)가 다루지 않는 합산 경계만 본다 —
시도마다 보고 범위가 다를 때 "모름"이 "0"이나 "완전 보고"로 둔갑하지 않는지.
"""

from datetime import datetime, timezone

from ai.model_client import AttemptRecord, TokenUsage
from ai.model_retry import USAGE_PARTIAL_ATTEMPTS, accumulate_usage

_NOW = datetime(2026, 10, 26, tzinfo=timezone.utc)


def _record(attempt, usage, outcome="ok"):
    return AttemptRecord(
        attempt=attempt, started_at=_NOW, elapsed_seconds=0.1, outcome=outcome, usage=usage
    )


def test_no_known_usage_is_none():
    assert accumulate_usage([_record(1, None, "transport_timeout")]) is None


def test_mixed_cache_creation_keeps_subtotal_but_marks_it_unreported():
    # 1회차는 캐시 쓰기를 보고하지 않았고 2회차만 4를 보고했다 — 합계 4는 완전한 값이 아니다
    first = TokenUsage(10, 2, 12, cached_prompt_tokens=0, cache_creation_prompt_tokens=None)
    second = TokenUsage(20, 3, 23, cached_prompt_tokens=5, cache_creation_prompt_tokens=4)

    usage = accumulate_usage([_record(1, first, "output_invalid"), _record(2, second)])

    assert (usage.prompt_tokens, usage.completion_tokens, usage.total_tokens) == (30, 5, 35)
    assert usage.cached_prompt_tokens == 5
    assert usage.cache_creation_prompt_tokens == 4
    assert "cache_creation_prompt_tokens" in usage.unreported


def test_provider_without_cache_creation_stays_none_without_marker():
    # OpenAI처럼 전 시도가 None이면 "제공자가 주지 않는 값"이다 — 표시하지 않는다
    usage = accumulate_usage(
        [_record(1, TokenUsage(10, 2, 12), "output_invalid"), _record(2, TokenUsage(10, 2, 12))]
    )

    assert usage.cache_creation_prompt_tokens is None
    assert usage.unreported == ()


def test_attempt_without_usage_is_marked_and_order_is_preserved():
    reported = TokenUsage(10, 2, 12, unreported=("cached_prompt_tokens",))

    usage = accumulate_usage(
        [_record(1, reported, "output_invalid"), _record(2, None, "transport_timeout")]
    )

    assert usage.total_tokens == 12
    assert usage.unreported == ("cached_prompt_tokens", USAGE_PARTIAL_ATTEMPTS)
