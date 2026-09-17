"""서비스 유료 검증의 호출 한도·재실행 차단은 자격증명을 읽기 전에 확인한다."""

from types import SimpleNamespace

import dotenv
import pytest
from ai.agent import EvidenceSummaryOutput
from ai.evaluation.savings import service_validation as runner


def test_service_probe_call_limit_blocks_before_new_attempt():
    audit = runner.MeasuredClient(False)
    audit.db = SimpleNamespace(in_transaction=lambda: False)
    audit.record["calls"] = [{}] * 18
    with pytest.raises(AssertionError, match="CALL_BUDGET_EXCEEDED"):
        audit.complete(None, EvidenceSummaryOutput)
    assert len(audit.record["calls"]) == 18


def test_service_probe_restart_blocks_before_credentials(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "ROUND", tmp_path)
    runner.write(tmp_path / "preflight.json", {})
    runner.write(tmp_path / "started.json", {})
    monkeypatch.setattr(dotenv, "dotenv_values", lambda *a, **k: pytest.fail("credentials read"))
    with pytest.raises(AssertionError, match="ROUND_ALREADY_STARTED"):
        runner.execute(True)
    with pytest.raises(AssertionError, match="ROUND_ALREADY_STARTED"):
        runner.execute(False)
    assert not (tmp_path / "metadata.json").exists()
