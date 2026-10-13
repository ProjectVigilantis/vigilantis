# ==============================================================================
# [파일 설명]
# 격리 지연 계측 (Issue #447) — 위협 접수부터 격리 확인까지의 단조 시계 구간.
#
#   - begin_trace()가 ContextVar에 IsolationTrace를 싣고 나갈 때 되돌린다. 하위 계층은
#     current_trace()로 얻어 mark()만 호출한다(러너 시그니처 불변). 전역 저장소 없음.
#   - 경과 시간은 perf_counter_ns(단조) 차이다. UTC 시각은 시작 기록과 Medium 대기
#     마감(response_deadline_at) 대조에만 쓰며, deadline_to_*_ms는 UTC 차이로 단조
#     구간(elapsed)과 다른 양이다. 마감 시각은 저장된 값을 bind로 받을 뿐 새로 만들지 않는다.
#   - 보장을 분리한다: 총 소요(end_to_end_ms)는 finish(success, confirmed=True)로 실제
#     격리 종결이 확인됐을 때만 채운다. 실행 생성·AWS 요청 수락·phase mark로는
#     확인을 추론하지 않는다. 그 외 종료는 elapsed_to_outcome_ms만 갖는다.
#   - isolation_confirmed phase는 mark()가 아니라 확인된 finish만 찍는다.
# ==============================================================================

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

PHASES = (
    "risk_evaluated",
    "incident_committed",
    "execution_reserved",
    "guardrail_passed",
    "backup_committed",
    "execution_started",
    "isolation_confirmed",
)
_CONFIRM_PHASE = "isolation_confirmed"
TIMEOUT_SCAN_TRIGGER = "TIMEOUT_ISOLATION_1M"

OUTCOMES = ("prerequisite_blocked", "failed", "interrupted", "unconfirmed", "success")

_logger = logging.getLogger("vigilantis.isolation_timing")

_current: ContextVar[IsolationTrace | None] = ContextVar(
    "isolation_trace", default=None
)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _ms(ns: int) -> float:
    return ns / 1_000_000


def _utc_diff_ms(later: datetime, earlier: datetime) -> float:
    return (later - earlier).total_seconds() * 1000


class IsolationTrace:
    def __init__(
        self,
        trigger_source: str | None = None,
        *,
        environment: str = "runtime",
        monotonic_ns: Callable[[], int] = time.perf_counter_ns,
        utc_now: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._monotonic_ns = monotonic_ns
        self._utc_now = utc_now
        self.trigger_source = trigger_source
        self.environment = environment
        self.incident_id: str | None = None
        self.execution_id: str | None = None
        self.response_deadline_at: datetime | None = None
        self._start_ns = monotonic_ns()
        self.started_at = utc_now()
        self._marks: dict[str, int] = {}  # 삽입 순서 = mark 순서, 값 = 시작 이후 ns
        self._mark_utc: dict[str, datetime] = {}
        self._result: dict[str, Any] | None = None

    @property
    def finished(self) -> bool:
        return self._result is not None

    def bind(
        self,
        *,
        incident_id: str | None = None,
        execution_id: str | None = None,
        response_deadline_at: datetime | None = None,
    ) -> None:
        """식별자·저장된 대기 마감을 붙인다. None은 기존 값을 지우지 않는다."""
        if response_deadline_at is not None and response_deadline_at.tzinfo is None:
            raise ValueError("response_deadline_at must be timezone-aware (UTC)")
        if incident_id is not None:
            self.incident_id = str(incident_id)
        if execution_id is not None:
            self.execution_id = str(execution_id)
        if response_deadline_at is not None:
            self.response_deadline_at = response_deadline_at

    def mark(self, name: str) -> None:
        """phase 통과를 기록한다. 같은 phase는 첫 기록만 유지하고, 종료 뒤 호출은 무시한다."""
        if name not in PHASES or name == _CONFIRM_PHASE:
            raise ValueError(f"unmarkable phase: {name!r}")
        self._record(name)

    def _record(self, name: str) -> None:
        if self._result is not None or name in self._marks:
            return
        self._marks[name] = self._monotonic_ns() - self._start_ns
        self._mark_utc[name] = self._utc_now()

    def finish(self, outcome: str, *, confirmed: bool = False) -> dict[str, Any]:
        """한 번만 기록·로그한다. 재호출은 첫 결과를 그대로 돌려준다.

        confirmed=True는 outcome=success일 때만 유효하다. success인데 확인되지 않았으면
        unconfirmed로 기록한다 — 확인 없는 성공은 총 소요를 낼 수 없다.
        """
        if self._result is not None:
            return dict(self._result)
        if outcome not in OUTCOMES:
            raise ValueError(f"unknown outcome: {outcome!r}")
        if confirmed and outcome != "success":
            raise ValueError("confirmed=True requires outcome='success'")
        if outcome == "success" and not confirmed:
            outcome = "unconfirmed"
        if confirmed:
            self._record(_CONFIRM_PHASE)
        outcome_ns = self._monotonic_ns() - self._start_ns
        self._result = self._build(outcome, confirmed, outcome_ns)
        # 계측 로그 실패가 격리 흐름을 깨지 않는다
        with suppress(Exception):
            _logger.info("isolation_latency", extra=dict(self._result))
        return dict(self._result)

    def _build(self, outcome: str, confirmed: bool, outcome_ns: int) -> dict[str, Any]:
        phases: dict[str, dict[str, float | None]] = {}
        previous_ns = 0
        for name in sorted(self._marks, key=self._marks.__getitem__):
            ns = self._marks[name]
            phases[name] = {"elapsed_ms": _ms(ns), "delta_ms": _ms(ns - previous_ns)}
            previous_ns = ns
        ordered = {
            name: phases.get(name, {"elapsed_ms": None, "delta_ms": None})
            for name in PHASES
        }
        deadline = self.response_deadline_at
        reserved_utc = self._mark_utc.get("execution_reserved")
        confirmed_utc = self._mark_utc.get(_CONFIRM_PHASE)
        # Medium은 접수가 아니라 만료 스캔에서 시작한다 — 접수→확인 총 소요로 위장하지 않는다
        scan_start = self.trigger_source == TIMEOUT_SCAN_TRIGGER
        confirmed_ns = self._marks[_CONFIRM_PHASE] if confirmed else None
        reserved_ns = self._marks.get("execution_reserved")
        return {
            "start_boundary": "timeout_scan" if scan_start else "threat_received",
            "scan_to_confirmation_ms": (
                _ms(confirmed_ns) if scan_start and confirmed_ns is not None else None
            ),
            "reservation_to_confirmation_ms": (
                _ms(confirmed_ns - reserved_ns)
                if confirmed_ns is not None and reserved_ns is not None
                else None
            ),
            "event": "isolation_latency",
            "outcome": outcome,
            "confirmed": confirmed,
            "environment": self.environment,
            "trigger_source": self.trigger_source,
            "incident_id": self.incident_id,
            "execution_id": self.execution_id,
            "started_at": self.started_at.astimezone(UTC).isoformat(),
            "elapsed_to_outcome_ms": _ms(outcome_ns),
            "end_to_end_ms": (
                _ms(confirmed_ns) if confirmed_ns is not None and not scan_start else None
            ),
            "phases": ordered,
            "response_deadline_at": (
                deadline.astimezone(UTC).isoformat() if deadline else None
            ),
            "deadline_to_reservation_ms": (
                _utc_diff_ms(reserved_utc, deadline) if deadline and reserved_utc else None
            ),
            "deadline_to_confirmation_ms": (
                _utc_diff_ms(confirmed_utc, deadline)
                if deadline and confirmed and confirmed_utc
                else None
            ),
        }


def current_trace() -> IsolationTrace | None:
    return _current.get()


@contextmanager
def begin_trace(
    trigger_source: str | None = None,
    *,
    environment: str = "runtime",
    monotonic_ns: Callable[[], int] = time.perf_counter_ns,
    utc_now: Callable[[], datetime] = _utc_now,
) -> Iterator[IsolationTrace]:
    """trace를 싣고 나갈 때 되돌린다. finish 없이 나가면 예외는 interrupted, 정상 종료는
    unconfirmed로 남긴다. 예외는 삼키지 않는다."""
    trace = IsolationTrace(
        trigger_source,
        environment=environment,
        monotonic_ns=monotonic_ns,
        utc_now=utc_now,
    )
    token = _current.set(trace)
    try:
        yield trace
    except BaseException:
        trace.finish("interrupted")
        raise
    else:
        trace.finish("unconfirmed")
    finally:
        _current.reset(token)
