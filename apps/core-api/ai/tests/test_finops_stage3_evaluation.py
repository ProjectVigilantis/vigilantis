"""FinOps 3단계 오프라인 지표·호출 계상·의미 검토 테스트.

모델 호출 0회다. 여기서 만든 원자료와 응답은 테스트 더블이라 모델 품질을 뜻하지 않는다 —
검증하는 것은 계측 도구가 실패·누락을 분모에 남기고 호출 단위를 섞지 않는가다.
"""

from datetime import UTC, datetime

import pytest
from ai.evaluation.common.usage import UsageRecordingClient
from ai.evaluation.summary.finops_stage3 import (
    ReviewFile,
    build_review_packet,
    call_accounting,
    compare_no_proposal,
    decision_flip_metrics,
    decision_mismatch_metrics,
    review_template,
    score_reviews,
)
from ai.model_client import (
    AIModelContractError,
    AIModelRequest,
    AIModelResponse,
    AIModelUnavailableError,
    AttemptRecord,
    CallTrace,
    TokenUsage,
)
from pydantic import BaseModel

REQUEST = AIModelRequest(system_prompt="s", user_payload={})


class Out(BaseModel):
    x: int = 1


def _trace(*usages):
    now = datetime.now(UTC)
    return CallTrace(
        provider="openai", requested_model="m", returned_model="m-2026", requested_service_tier=None,
        returned_service_tier=None,
        attempts=tuple(
            AttemptRecord(attempt=i, started_at=now, elapsed_seconds=0.1, outcome="ok", usage=u)
            for i, u in enumerate(usages, start=1)
        ),
        max_attempts=3, output_retries=0, timeout_seconds=1.0, elapsed_seconds=0.2,
    )


class ScriptedClient:
    def __init__(self, steps):
        self.steps = list(steps)

    def complete(self, request, response_model):
        step = self.steps.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


def _usage(p=10, c=5):
    return TokenUsage(prompt_tokens=p, completion_tokens=c, total_tokens=p + c)


# --- 호출 계상 ---------------------------------------------------------------------


def test_call_records_survive_reset_and_distinguish_attempts_and_unknown_usage():
    usage = _usage()
    secret_text = "sk-SECRETSECRETSECRET1234"
    client = UsageRecordingClient(
        ScriptedClient(
            [
                AIModelResponse(output=Out(), usage=usage, model="m-2026", trace=_trace(None, usage)),
                AIModelUnavailableError(secret_text, kind=None),
                AIModelContractError(secret_text, usage=_usage(7, 3), phase="response"),
            ]
        )
    )
    client.reset()
    client.complete(REQUEST, Out)
    with pytest.raises(AIModelUnavailableError):
        client.complete(REQUEST, Out)
    first_run = client.run_records_json()
    client.reset()
    with pytest.raises(AIModelContractError):
        client.complete(REQUEST, Out)

    assert [r["run_call_index"] for r in first_run] == [1, 2]
    assert first_run[0]["attempts"] == 2 and first_run[0]["attempts_without_usage"] == 1
    assert first_run[1]["usage_known"] is False and first_run[1]["prompt_tokens"] is None
    assert first_run[1]["attempts"] is None
    # reset 뒤에도 전체 기록은 남고 회차 몫만 갈린다
    assert [r.index for r in client.all_call_records] == [1, 2, 3]
    assert [r.run_call_index for r in client.run_records()] == [1]
    assert client.run_records()[0].usage_known and client.run_records()[0].prompt_tokens == 7
    assert client.unknown_usage_calls == 1
    assert client.physical_attempts is None  # trace 없는 호출이 있어 총 시도 수를 모른다
    assert secret_text not in repr(client.all_call_records_json())
    assert client.calls == 1 and client.prompt_tokens == 7  # 기존 reset 의미 유지


# --- 지표 --------------------------------------------------------------------------


def _run(case, status, ids=(), reason=None):
    return {
        "case_id": case, "invocation_status": status, "failure_reason": reason,
        "candidates": [{"runbook_id": i} for i in ids], "summary_lines": ["a", "b", "c"]
        if status != "FAILED" else [],
        "prompt_tokens": 1, "completion_tokens": 1,
    }


def _raw(runs, path="x.json", repeats=3, cases=("A1", "A2"), source="test_double"):
    return {
        "_path": path, "label": "L", "repeats": repeats, "fixed_set": {"s": 1},
        "planned": {"case_ids": list(cases), "repeats": repeats}, "data_source": source, "runs": runs,
    }


def test_flip_uses_canonical_sets_excludes_failed_and_reports_coverage():
    raw = _raw([
        _run("A1", "SUCCEEDED", ["R2", "R1", "R1"]),
        _run("A1", "SUCCEEDED", ["R1", "R2"]),
        _run("A1", "FAILED", reason="DECISION_MISMATCH"),
        _run("A2", "SUCCEEDED", ["R1"]),
        _run("A2", "NO_PROPOSAL"),
    ])
    metrics = decision_flip_metrics(raw)

    assert metrics["cases_with_flip"] == 1 and metrics["cases_measured"] == 2
    assert metrics["cases"]["A1"]["flip_ratio"] == 0
    assert metrics["cases"]["A2"]["decisions"] == {"R1": 1, "NO_PROPOSAL": 1}
    cov = metrics["coverage"]
    assert (cov["planned_runs"], cov["recorded_runs"], cov["missing_runs"], cov["failed_runs"]) == (6, 5, 1, 1)
    assert cov["real_model_measurement"] is False


def test_inconsistent_run_is_not_a_decision():
    raw = _raw([_run("A1", "SUCCEEDED"), _run("A1", "NO_PROPOSAL", ["R1"])], repeats=2, cases=("A1",))
    metrics = decision_flip_metrics(raw)
    assert metrics["cases_measured"] == 0
    assert metrics["coverage"]["inconsistent_runs"] == 2


def test_decision_mismatch_counts_failures_over_planned_denominator():
    raw = _raw([
        _run("A1", "FAILED", reason="DECISION_MISMATCH"),
        _run("A1", "SUCCEEDED", ["R1"]),
        _run("A2", "FAILED", reason=None),
    ])
    result = decision_mismatch_metrics(raw)
    assert result["count"] == 1 and result["denominator"] == 6
    assert result["rate"] == pytest.approx(1 / 6)
    assert result["rate_upper_bound"] == pytest.approx(4 / 6)  # 누락 3건
    assert result["failed_without_reason"] == 1


def test_no_proposal_comparison_requires_same_inputs_and_flags_pairing():
    cand = _raw([_run("A1", "NO_PROPOSAL")], path="c.json", repeats=1, cases=("A1",))
    old = _raw([_run("A1", "NO_PROPOSAL"), ], path="v2.json", repeats=1, cases=("A1",))
    def judged_source(path, error=False):
        verdict = {"error": "AIModelError"} if error else {"verdict_ok": True}
        return {"path": path, "runs": [{"case_id": "A1", "run_index": 0,
                                        "restoration": [verdict], "defects": [{}]}]}

    judged = {"sources": [judged_source("c.json"), judged_source("v2.json")]}
    ok = compare_no_proposal(cand, old, judged)
    assert ok["comparable"] and ok["paired_judging"] is True and ok["verdict_eligible"]
    assert ok["candidate_not_greater_than_v2"] is True

    # 같은 파일의 다른 표기도 짝으로 본다
    alt = {"sources": [judged_source("./c.json"), judged_source("sub/../v2.json")]}
    assert compare_no_proposal(cand, old, alt)["paired_judging"] is True

    # 경로만 있고 판정이 없거나 오류인 경우, 판정 파일이 없는 경우는 판정을 내리지 않는다
    for weak in (
        {"sources": [{"path": "c.json"}, {"path": "v2.json"}]},  # runs 키 자체가 없는 경로 목록
        {"sources": [{"path": "c.json", "runs": []}, {"path": "v2.json", "runs": []}]},
        {"sources": [judged_source("c.json"), judged_source("v2.json", error=True)]},
        {"sources": [judged_source("c.json")]},
        None,
    ):
        result = compare_no_proposal(cand, old, weak)
        assert result["comparable"] and result["verdict_eligible"] is False
        assert result["candidate_not_greater_than_v2"] is None
        assert result["pending"]
        assert result["candidate"]["count"] == 1  # 진단값은 남는다
    partial = compare_no_proposal(cand, old, {"sources": [judged_source("c.json"), judged_source("v2.json", True)]})
    assert partial["paired_judging_coverage"]["v2"]["judgment_errors"] == 1

    old["fixed_set"] = {"s": 2}
    old["label"] = "other"
    bad = compare_no_proposal(cand, old, judged)
    assert not bad["comparable"] and bad["verdict_eligible"] is False
    assert bad["candidate_not_greater_than_v2"] is None
    assert set(bad["mismatches"]) == {"fixed_set", "label(model_and_knobs)"}


def test_call_accounting_separates_logical_calls_from_attempts():
    record = {"response_model": "Out", "outcome": "ok", "error_class": None, "error_phase": None,
              "usage_status": "partial", "prompt_tokens": 10, "completion_tokens": 5,
              "cached_prompt_tokens": None, "attempts": 2, "attempts_without_usage": 1,
              "attempt_details": [{}, {}]}
    unknown = {**record, "outcome": "error", "error_class": "E", "error_phase": "transport",
               "usage_status": "unknown", "prompt_tokens": None, "completion_tokens": None,
               "attempts": None, "attempts_without_usage": None, "attempt_details": None}
    run = {**_run("A1", "SUCCEEDED", ["R1"]), "call_records": [record, unknown]}
    result = call_accounting(_raw([run]))
    assert result["logical_calls"] == 2 and result["physical_attempts_observed"] == 2
    assert result["calls_without_attempt_trace"] == 1
    assert result["usage_status_counts"] == {"partial": 1, "unknown": 1}
    cached = result["known_usage_subtotals"]["cached_prompt_tokens"]
    assert cached == {"known_subtotal": 0, "calls_missing_field": 2}  # 0이 아니라 "모름" 2건
    assert result["known_usage_subtotals"]["prompt_tokens"]["calls_missing_field"] == 1


def test_zero_filled_usage_is_missing_not_known_zero_and_attempts_are_retained():
    zero_filled = TokenUsage(
        prompt_tokens=10, completion_tokens=0, total_tokens=0,
        unreported=("completion_tokens", "total_tokens", "cached_prompt_tokens"),
    )
    now = datetime.now(UTC)
    trace = CallTrace(
        provider="openai", requested_model="m", returned_model="m-2026", requested_service_tier=None,
        returned_service_tier=None,
        attempts=(
            AttemptRecord(attempt=1, started_at=now, elapsed_seconds=0.1, outcome="rate_limited",
                          error_class="RateLimitError", status_code=429, retry_delay_seconds=1.5,
                          server_directed=True),
            AttemptRecord(attempt=2, started_at=now, elapsed_seconds=0.2, outcome="ok", usage=zero_filled),
        ),
        max_attempts=3, output_retries=0, timeout_seconds=1.0, elapsed_seconds=2.0,
    )
    client = UsageRecordingClient(
        ScriptedClient([AIModelResponse(output=Out(), usage=zero_filled, model="m-2026", trace=trace)])
    )
    client.complete(REQUEST, Out)

    record = client.all_call_records_json()[0]
    assert record["usage_status"] == "partial" and record["usage_known"] is False
    assert record["prompt_tokens"] == 10
    assert record["completion_tokens"] is None and record["cached_prompt_tokens"] is None
    first, second = record["attempt_details"]
    assert (first["outcome"], first["error_class"], first["status_code"]) == ("rate_limited", "RateLimitError", 429)
    assert first["retry_delay_seconds"] == 1.5 and first["usage_status"] == "unknown"
    assert first["prompt_tokens"] is None
    assert second["usage_status"] == "partial" and second["completion_tokens"] is None
    assert client.partial_usage_calls == 1 and client.unknown_usage_calls == 0
    totals = client.usage_totals()
    assert totals["completion_tokens"] == {
        "known_subtotal": 0, "calls_missing_field": 1, "calls_not_fully_known": 1,
    }
    assert totals["prompt_tokens"]["calls_not_fully_known"] == 1  # 시도 일부 사용량 미확인


# --- 의미 검토 ---------------------------------------------------------------------


def _packet():
    cand = _raw([_run("A1", "SUCCEEDED", ["R1"]), _run("A1", "FAILED", reason="DECISION_MISMATCH")],
                repeats=2, cases=("A1",))
    old = _raw([_run("A1", "NO_PROPOSAL")], repeats=1, cases=("A1",))
    return build_review_packet({"candidate": cand, "v2": old})


def _review(packet, model, verdicts, **overrides):
    data = review_template(packet, model, model, "high")
    for entry, (verdict, quote) in zip(data["reviews"], verdicts):
        entry.update(verdict=verdict, quote=quote, reason="r")
    data.update(overrides)
    return ReviewFile.model_validate(data)


def test_packet_covers_v2_by_its_candidate_set_and_counts_unreviewable():
    packet = _packet()
    assert [i["item_id"] for i in packet["items"]] == ["candidate:A1#1", "v2:A1#1"]
    assert packet["items"][1]["decision_runbooks"] == []
    assert packet["unreviewable_runs"] == {"candidate": 1, "v2": 0}


def test_review_scoring_keeps_missing_invalid_and_disagreement_visible():
    packet = _packet()
    claude = _review(packet, "claude", [("PASS", "a"), ("FAIL", "")])
    gpt = _review(packet, "gpt", [("FAIL", ""), ("PASS", "not in summary")])
    gpt.reviews.pop(0)  # 첫 항목 누락

    result = score_reviews(packet, [claude, gpt])

    assert result["independent_reviewers"] is True
    assert result["reviewers"][0]["counts"]["PASS"] == 1
    assert result["reviewers"][1]["counts"]["MISSING"] == 1
    assert result["reviewers"][1]["problems"] == {"v2:A1#1": "quote_not_in_summary"}
    assert result["agreement"]["incomplete"] == 2 and result["agreement"]["agree"] == 0


def test_review_for_other_packet_or_swapped_output_is_not_counted():
    packet = _packet()
    stale = _review(packet, "claude", [("PASS", "a")] * 2, packet_sha256="0" * 64)
    swapped = _review(packet, "gpt", [("PASS", "a")] * 2)
    swapped.reviews[0].output_sha256 = "bad"

    result = score_reviews(packet, [stale, swapped])

    assert result["reviewers"][0]["rejected"] == "packet_sha256_mismatch"
    assert result["reviewers"][0]["counts"]["MISSING"] == 2
    assert result["reviewers"][1]["problems"]["candidate:A1#1"] == "output_sha256_mismatch"
    assert result["data_sources"] == {"candidate": "test_double", "v2": "test_double"}
