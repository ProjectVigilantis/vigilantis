"""준비된 실행기의 승인·호출·진단 경계를 합성 응답으로 확인한다. 실제 모델 호출은 없다."""

import io
import json
import tempfile
import unittest
from collections import Counter
from contextlib import contextmanager, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import run_comparison as runner
from ai.model_client import AIModelContractError, AIModelResponse, TokenUsage

SOURCE_ROUND = runner.ROUND
FROZEN, ASSETS = runner.current_preflight()
SPEC = runner.read(runner.SPEC_PATH)


class SyntheticClient:
    def __init__(self, *, fail_at=None, quality_defects=False, unavailable=0):
        self.count = 0
        self.fail_at = fail_at
        self.quality_defects = quality_defects
        self.unavailable = unavailable

    def complete(self, request, response_model):
        self.count += 1
        expected = runner.SCHEDULE[self.count - 1]
        stored = runner.read(runner.ROUND / "metadata.json")["calls"]
        assert len(stored) == self.count and stored[-1]["status"] == "STARTED"
        assert response_model is runner.ProposedHourlyRates
        assert runner.digest(runner.build_outbound_payload(request)) == (
            FROZEN["requests"][expected["case_id"]]["outbound_sha256"][expected["column"]]
        )
        if self.count == self.fail_at:
            raise AIModelContractError(
                "private-provider-text", phase="response", usage=TokenUsage(10, 5, 15),
            )
        context = request.user_payload["savings_context"]
        rates = SPEC["price_reference"]["hourly_rates"]
        values = {
            "status": "ESTIMATED",
            **{k: context[k] for k in (
                "target_arn", "region", "current_instance_type", "target_instance_type",
            )},
            "current_hourly_rate": rates[context["current_instance_type"]]["usd"],
            "target_hourly_rate": rates[context["target_instance_type"]]["usd"],
            "explanation": "오프라인 검증용 합성 응답이다.",
        }
        if self.count <= self.unavailable:
            values.update(status="UNAVAILABLE", current_hourly_rate=None, target_hourly_rate=None)
        if self.quality_defects and self.count == 1:
            values.update(current_hourly_rate="0.230400", target_hourly_rate="0.052800")
        output = response_model(**values)
        if self.quality_defects and self.count == 2:
            output = output.model_copy(update={"current_hourly_rate": "private-rejected-value"})
        return AIModelResponse(output=output, usage=TokenUsage(10, 5, 15), model="synthetic-only")


class PreparationChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="offline-check-", dir=SOURCE_ROUND)
        self.root = Path(self.temp.name).resolve()
        if not self.root.is_relative_to(SOURCE_ROUND.resolve()):
            raise ValueError("TEMPORARY_PATH_OUTSIDE_ROUND")
        self.addCleanup(self.temp.cleanup)
        self.round_patch = patch.object(runner, "ROUND", self.root)
        self.round_patch.start()
        self.addCleanup(self.round_patch.stop)
        runner.write(self.root / "preflight.json", FROZEN)

    def approve_offline_fixture(self):
        runner.write(self.root / "approval.json", {
            "approved": True, "maximum_calls": 60,
            "preflight_sha256": runner.sha(self.root / "preflight.json"),
            "note": "Synthetic test fixture only; not user approval for model calls.",
        })

    def run_with(self, fake):
        @contextmanager
        def synthetic_client():
            yield fake

        with patch.object(runner, "live_client", synthetic_client), redirect_stdout(io.StringIO()):
            code = runner.execute(FROZEN, ASSETS)
        return code, runner.read(self.root / "analysis.json")

    def test_schedule_and_unapproved_execution(self):
        counts = Counter((s["column"], s["case_id"]) for s in runner.SCHEDULE)
        self.assertEqual(counts, Counter({(c, k): 10 for c in runner.COLUMNS for k in runner.CASE_IDS}))
        first = Counter((s["column"], s["case_id"]) for s in runner.SCHEDULE[::2])
        self.assertEqual(first, Counter({(c, k): 5 for c in runner.COLUMNS for k in runner.CASE_IDS}))
        with patch.object(runner, "live_client") as factory:
            with self.assertRaises(FileNotFoundError):
                runner.execute(FROZEN, ASSETS)
            factory.assert_not_called()
        self.assertFalse((self.root / "started.json").exists())

    def test_approval_or_preflight_drift_blocks_before_credentials(self):
        self.approve_offline_fixture()
        runner.write(self.root / "preflight.json", {**FROZEN, "maximum_calls": 61})
        with patch.object(runner, "live_client") as factory:
            with self.assertRaises(ValueError):
                runner.execute(FROZEN, ASSETS)
            factory.assert_not_called()
        runner.write(self.root / "preflight.json", FROZEN)
        runner.write(self.root / "approval.json", {"approved": True, "maximum_calls": 61})
        with patch.object(runner, "live_client") as factory:
            with self.assertRaises(ValueError):
                runner.execute(FROZEN, ASSETS)
            factory.assert_not_called()

    def test_price_and_contract_defects_retained_and_all_60_calls_compared(self):
        self.approve_offline_fixture()
        fake = SyntheticClient(quality_defects=True)
        code, analysis = self.run_with(fake)
        self.assertEqual((code, fake.count), (0, 60))
        self.assertTrue(analysis["comparison_complete"])
        self.assertFalse(analysis["service_baseline_evaluated"])
        old, new = (analysis["columns"][c] for c in runner.COLUMNS)
        self.assertEqual(old["over_limit_count"], 1)
        self.assertEqual(new["status_counts"]["INVALID"], 1)
        self.assertFalse(old["probe_passed"] or new["probe_passed"])
        saved = (self.root / "metadata.json").read_text("utf-8")
        self.assertNotIn("private-rejected-value", saved)
        self.assertEqual(analysis["usage"]["prompt_tokens"], 600)
        with patch.object(runner, "live_client") as factory:
            with self.assertRaises(ValueError):
                runner.execute(FROZEN, ASSETS)
            factory.assert_not_called()

    def test_failed_response_stops_without_retry_and_partial_cannot_pass(self):
        self.approve_offline_fixture()
        fake = SyntheticClient(fail_at=3)
        code, analysis = self.run_with(fake)
        self.assertEqual((code, fake.count), (2, 3))
        self.assertEqual(analysis["execution_status"], "ABORTED_MODEL_ERROR")
        self.assertEqual(analysis["usage"]["prompt_tokens"], 30)
        self.assertFalse(analysis["comparison_complete"])
        self.assertTrue(all(not c["probe_passed"] for c in analysis["columns"].values()))
        self.assertNotIn("private-provider-text", (self.root / "metadata.json").read_text("utf-8"))

    def test_unavailable_is_counted_in_full_denominator(self):
        self.approve_offline_fixture()
        _, analysis = self.run_with(SyntheticClient(unavailable=3))
        # 첫 3건은 old, new, old다. old 28/30은 미충족, new 29/30은 진단 기준 충족이다.
        old, new = (analysis["columns"][c] for c in runner.COLUMNS)
        self.assertEqual((old["estimated"], new["estimated"]), (28, 29))
        self.assertFalse(old["probe_passed"])
        self.assertTrue(new["probe_passed"])
        self.assertFalse(analysis["service_baseline_evaluated"])

    def test_incomplete_or_duplicate_saved_schedule_is_not_complete(self):
        metadata = {**FROZEN, "status": "COMPLETED", "calls": []}
        analysis = runner.analyze(metadata, SPEC)
        self.assertFalse(analysis["comparison_complete"])
        self.assertTrue(all(not c["probe_passed"] for c in analysis["columns"].values()))
        wrong = {**runner.SCHEDULE[0], "number": 2, "status": "STARTED"}
        with self.assertRaises(ValueError):
            runner.analyze({**metadata, "calls": [wrong]}, SPEC)


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(PreparationChecks)
    outcome = unittest.TextTestRunner(verbosity=2).run(suite)
    report = {
        "scope": "offline runner verification with synthetic responses; no model execution",
        "live_model_calls": 0, "checks_run": outcome.testsRun,
        "failures": len(outcome.failures), "errors": len(outcome.errors),
        "passed": outcome.wasSuccessful(),
        "runner_sha256": runner.sha(SOURCE_ROUND / "run_comparison.py"),
        "verification_source_sha256": runner.sha(Path(__file__)),
        "preflight_sha256": runner.sha(SOURCE_ROUND / "preflight.json"),
    }
    runner.write(SOURCE_ROUND / "preparation-verification.json", report)
    print(json.dumps(report))
    raise SystemExit(0 if outcome.wasSuccessful() else 1)
