"""#447 격리 지연 계측 — 독립 제어 단조/UTC 시계로 구간 계산과 종료 보장을 검증한다."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import isolation_timing
import pytest
from isolation_timing import PHASES, begin_trace, current_trace

T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


class Clocks:
    def __init__(self) -> None:
        self.ns = 5_000_000_000
        self.utc = T0

    def mono_adv(self, ms: float) -> None:
        self.ns += int(ms * 1_000_000)

    def kw(self) -> dict:
        return {"monotonic_ns": lambda: self.ns, "utc_now": lambda: self.utc}


@pytest.fixture
def clocks() -> Clocks:
    return Clocks()


def _events(caplog):
    return [r for r in caplog.records if r.getMessage() == "isolation_latency"]


def test_confirmed_success_exact_total_and_phase_deltas(clocks, caplog):
    caplog.set_level(logging.INFO, logger="vigilantis.isolation_timing")
    with begin_trace("mock", **clocks.kw()) as trace:
        clocks.mono_adv(10)
        trace.mark("risk_evaluated")
        clocks.mono_adv(5)
        trace.mark("incident_committed")
        trace.bind(incident_id="inc-1", execution_id="exe-1")
        clocks.mono_adv(20)
        trace.mark("execution_started")
        clocks.mono_adv(100)
        result = trace.finish("success", confirmed=True)
    assert result["outcome"] == "success" and result["confirmed"] is True
    assert result["end_to_end_ms"] == 135
    assert result["elapsed_to_outcome_ms"] == 135
    assert result["incident_id"] == "inc-1" and result["execution_id"] == "exe-1"
    assert result["environment"] == "runtime"
    assert result["started_at"] == T0.isoformat()
    p = result["phases"]
    assert list(p) == list(PHASES)
    assert p["risk_evaluated"] == {"elapsed_ms": 10, "delta_ms": 10}
    assert p["incident_committed"] == {"elapsed_ms": 15, "delta_ms": 5}
    assert p["execution_started"] == {"elapsed_ms": 35, "delta_ms": 20}
    assert p["isolation_confirmed"] == {"elapsed_ms": 135, "delta_ms": 100}
    # 빠진 phase는 null로 남고 delta는 마크된 이전 phase 기준이다
    assert p["execution_reserved"] == {"elapsed_ms": None, "delta_ms": None}
    assert len(_events(caplog)) == 1


def test_unconfirmed_success_has_no_total(clocks):
    with begin_trace(**clocks.kw()) as trace:
        trace.mark("execution_started")
        clocks.mono_adv(7)
        result = trace.finish("success")
    assert result["outcome"] == "unconfirmed"
    assert result["confirmed"] is False
    assert result["end_to_end_ms"] is None
    assert result["elapsed_to_outcome_ms"] == 7
    assert result["phases"]["isolation_confirmed"]["elapsed_ms"] is None


def test_confirmation_cannot_be_marked_or_misused(clocks):
    with begin_trace(**clocks.kw()) as trace:
        with pytest.raises(ValueError):
            trace.mark("isolation_confirmed")
        with pytest.raises(ValueError):
            trace.mark("nope")
        with pytest.raises(ValueError):
            trace.finish("failed", confirmed=True)
        with pytest.raises(ValueError):
            trace.finish("bogus")
        assert not trace.finished
        trace.finish("failed")


@pytest.mark.parametrize("outcome", ["prerequisite_blocked", "failed", "interrupted", "unconfirmed"])
def test_non_success_outcomes_keep_name_without_total(clocks, outcome):
    with begin_trace(**clocks.kw()) as trace:
        clocks.mono_adv(3)
        result = trace.finish(outcome)
    assert result["outcome"] == outcome
    assert result["end_to_end_ms"] is None
    assert result["elapsed_to_outcome_ms"] == 3


def test_finish_repeated_logs_once_and_returns_first(clocks, caplog):
    caplog.set_level(logging.INFO, logger="vigilantis.isolation_timing")
    with begin_trace(**clocks.kw()) as trace:
        first = trace.finish("failed")
        clocks.mono_adv(50)
        trace.mark("risk_evaluated")  # 종료 뒤 mark는 무시
        second = trace.finish("success", confirmed=True)
    assert first == second
    assert second["outcome"] == "failed"
    assert second["phases"]["risk_evaluated"]["elapsed_ms"] is None
    assert len(_events(caplog)) == 1  # 컨텍스트 종료도 중복 기록하지 않는다


def test_exception_records_interrupted_and_propagates(clocks, caplog):
    caplog.set_level(logging.INFO, logger="vigilantis.isolation_timing")
    with (
        pytest.raises(RuntimeError, match="boom"),
        begin_trace(**clocks.kw()) as trace,
    ):
        clocks.mono_adv(4)
        raise RuntimeError("boom")
    events = _events(caplog)
    assert len(events) == 1
    assert events[0].outcome == "interrupted"
    assert events[0].end_to_end_ms is None
    assert events[0].elapsed_to_outcome_ms == 4
    assert trace.finished
    assert current_trace() is None


def test_base_exception_records_interrupted(clocks, caplog):
    caplog.set_level(logging.INFO, logger="vigilantis.isolation_timing")
    with pytest.raises(KeyboardInterrupt), begin_trace(**clocks.kw()):
        raise KeyboardInterrupt
    assert _events(caplog)[0].outcome == "interrupted"


def test_clean_exit_without_finish_is_unconfirmed(clocks, caplog):
    caplog.set_level(logging.INFO, logger="vigilantis.isolation_timing")
    with begin_trace(**clocks.kw()):
        clocks.mono_adv(2)
    (event,) = _events(caplog)
    assert event.outcome == "unconfirmed" and event.end_to_end_ms is None


def test_context_restoration_and_nesting(clocks):
    assert current_trace() is None
    with begin_trace("outer", **clocks.kw()) as outer:
        assert current_trace() is outer
        with begin_trace("inner", **clocks.kw()) as inner:
            assert current_trace() is inner
            current_trace().mark("risk_evaluated")
        assert current_trace() is outer
        assert inner.finished and not outer.finished
        assert outer.finish("failed")["phases"]["risk_evaluated"]["elapsed_ms"] is None
    assert current_trace() is None


def test_marks_ordered_by_call_not_by_canonical_order(clocks):
    with begin_trace(**clocks.kw()) as trace:
        clocks.mono_adv(10)
        trace.mark("guardrail_passed")
        clocks.mono_adv(30)
        trace.mark("execution_reserved")
        result = trace.finish("failed")
    p = result["phases"]
    assert p["guardrail_passed"]["delta_ms"] == 10
    assert p["execution_reserved"] == {"elapsed_ms": 40, "delta_ms": 30}


def test_duplicate_mark_keeps_first(clocks):
    with begin_trace(**clocks.kw()) as trace:
        clocks.mono_adv(1)
        trace.mark("risk_evaluated")
        clocks.mono_adv(9)
        trace.mark("risk_evaluated")
        assert trace.finish("failed")["phases"]["risk_evaluated"]["elapsed_ms"] == 1


def test_simulation_environment_is_explicit_label_only(clocks):
    with begin_trace(environment="simulation", **clocks.kw()) as trace:
        result = trace.finish("success", confirmed=True)
    assert result["environment"] == "simulation"
    assert result["confirmed"] is True  # 라벨은 확인 여부를 바꾸지 않는다
    with begin_trace(**clocks.kw()) as trace:
        assert trace.finish("success")["confirmed"] is False


def test_medium_wait_latency_is_utc_and_separate_from_elapsed(clocks):
    deadline = T0 + timedelta(seconds=60)
    with begin_trace(**clocks.kw()) as trace:
        trace.bind(incident_id="inc", response_deadline_at=deadline)
        # 단조 시계는 2초만 흘렀는데 UTC(벽시계)는 61초 간 상황 — 두 양은 독립이다
        clocks.mono_adv(2000)
        clocks.utc = T0 + timedelta(seconds=61)
        trace.mark("execution_reserved")
        clocks.mono_adv(500)
        clocks.utc = T0 + timedelta(seconds=63)
        result = trace.finish("success", confirmed=True)
    assert result["deadline_to_reservation_ms"] == 1000
    assert result["deadline_to_confirmation_ms"] == 3000
    assert result["end_to_end_ms"] == 2500
    assert result["elapsed_to_outcome_ms"] == 2500
    assert result["response_deadline_at"] == deadline.isoformat()


def test_high_threat_received_keeps_total(clocks):
    with begin_trace("mock", **clocks.kw()) as trace:
        clocks.mono_adv(10)
        trace.mark("execution_reserved")
        clocks.mono_adv(40)
        r = trace.finish("success", confirmed=True)
    assert r["start_boundary"] == "threat_received"
    assert r["end_to_end_ms"] == 50
    assert r["scan_to_confirmation_ms"] is None
    assert r["reservation_to_confirmation_ms"] == 40


def test_medium_timeout_scan_is_not_full_intake_timing(clocks):
    deadline = T0 + timedelta(seconds=60)
    with begin_trace("TIMEOUT_ISOLATION_1M", **clocks.kw()) as trace:
        trace.bind(response_deadline_at=deadline)
        clocks.mono_adv(15)
        clocks.utc = T0 + timedelta(seconds=61)
        trace.mark("execution_reserved")
        clocks.mono_adv(85)
        clocks.utc = T0 + timedelta(seconds=62)
        r = trace.finish("success", confirmed=True)
    assert r["outcome"] == "success" and r["confirmed"] is True
    assert r["start_boundary"] == "timeout_scan"
    assert r["end_to_end_ms"] is None
    assert r["scan_to_confirmation_ms"] == 100
    assert r["reservation_to_confirmation_ms"] == 85
    assert r["deadline_to_reservation_ms"] == 1000
    assert r["deadline_to_confirmation_ms"] == 2000
    assert r["elapsed_to_outcome_ms"] == 100


def test_medium_unconfirmed_or_unreserved_has_no_confirmation_intervals(clocks):
    with begin_trace("TIMEOUT_ISOLATION_1M", **clocks.kw()) as trace:
        trace.mark("execution_reserved")
        r = trace.finish("unconfirmed")
    assert r["scan_to_confirmation_ms"] is None
    assert r["reservation_to_confirmation_ms"] is None
    with begin_trace("TIMEOUT_ISOLATION_1M", **clocks.kw()) as trace:
        r = trace.finish("success", confirmed=True)
    assert r["scan_to_confirmation_ms"] == 0
    assert r["reservation_to_confirmation_ms"] is None


def test_deadline_metrics_null_without_deadline_or_marks(clocks):
    with begin_trace(**clocks.kw()) as trace:
        trace.mark("execution_reserved")
        r1 = trace.finish("success", confirmed=True)
    assert r1["deadline_to_reservation_ms"] is None
    assert r1["deadline_to_confirmation_ms"] is None
    with begin_trace(**clocks.kw()) as trace:
        trace.bind(response_deadline_at=T0)
        r2 = trace.finish("failed")
    assert r2["deadline_to_reservation_ms"] is None
    assert r2["deadline_to_confirmation_ms"] is None


def test_deadline_to_confirmation_requires_confirmation(clocks):
    with begin_trace(**clocks.kw()) as trace:
        trace.bind(response_deadline_at=T0)
        trace.mark("execution_reserved")
        result = trace.finish("unconfirmed")
    assert result["deadline_to_reservation_ms"] == 0
    assert result["deadline_to_confirmation_ms"] is None


def test_naive_deadline_rejected(clocks):
    with begin_trace(**clocks.kw()) as trace:
        with pytest.raises(ValueError):
            trace.bind(response_deadline_at=datetime(2026, 1, 1))  # noqa: DTZ001 - naive input is the rejection under test (isolation_timing.bind raises on tzinfo is None)
        trace.finish("failed")


def test_bind_none_does_not_clear(clocks):
    with begin_trace(**clocks.kw()) as trace:
        trace.bind(incident_id="inc-1")
        trace.bind(execution_id="exe-1")
        trace.bind(incident_id=None)
        result = trace.finish("failed")
    assert result["incident_id"] == "inc-1" and result["execution_id"] == "exe-1"


def test_default_clocks_real_trace():
    with begin_trace() as trace:
        trace.mark("risk_evaluated")
        result = trace.finish("failed")
    assert result["phases"]["risk_evaluated"]["elapsed_ms"] >= 0
    assert isolation_timing.current_trace() is None
