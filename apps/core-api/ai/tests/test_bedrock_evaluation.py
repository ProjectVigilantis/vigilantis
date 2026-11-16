"""명시 주입한 Bedrock 클라이언트로 같은 평가 로직을 돌리는 경로를 검증한다. AWS·모델 호출 없음."""

import pytest
from ai.agent import SecOpsEvidenceDecisionOutput
from ai.bedrock_anthropic_client import BedrockModelClient
from ai.evaluation.secops import runner
from ai.evaluation.secops.dataset import load_cases as load_secops_cases
from ai.evaluation.secops.scoring import load_answers
from ai.evaluation.summary import build_column_report, cli
from ai.model_client import AIModelRejectedError, FakeAIModelClient
from ai.openai_client import OpenAIModelClient
from config import Settings


class _RecordingBedrock(BedrockModelClient):
    """실제 클라이언트 클래스의 하위형. 네트워크·자격 증명 없이 미리 정한 응답·오류만 낸다."""

    def __init__(self, inner):  # noqa: super().__init__ 은 AWS 설정을 요구하므로 부르지 않는다
        self._inner = inner
        self.requests = []

    def complete(self, request, response_model):
        self.requests.append(request)
        return self._inner.complete(request, response_model)


class _Raises:
    def __init__(self, error):
        self._error = error

    def complete(self, request, response_model):
        raise self._error


def _decision(case):
    return SecOpsEvidenceDecisionOutput(
        observation="SSH 실패가 관측됐다.", diagnosis="반복 인증 시도다.",
        rationale="출발지 차단을 제안한다.", decision=[1] if case.case_id != "C10" else [],
    )


def _settings():
    return Settings(_env_file=None, DATABASE_URL="unused")


def _finops_case():
    return cli.load_cases()[0]


# --- summary.cli.run: settings XOR client ---------------------------------------------


@pytest.mark.parametrize("settings,client", [("both", "both"), (None, None)])
def test_run_rejects_both_or_neither_before_any_model_call(monkeypatch, settings, client):
    built = []
    monkeypatch.setattr(cli, "build_openai_model_client", lambda s: built.append(s))
    fake = _RecordingBedrock(_Raises(AssertionError("model call")))
    with pytest.raises(ValueError):
        cli.run(_settings() if settings else None, [_finops_case()], 1,
                client=fake if client else None)
    assert built == [] and fake.requests == []


def test_injected_client_does_not_build_openai_client_and_matches_the_legacy_inputs(monkeypatch):
    case = _finops_case()
    error = AIModelRejectedError("no consent", phase="consent")
    legacy = _RecordingBedrock(_Raises(error))
    built = []
    monkeypatch.setattr(cli, "build_openai_model_client", lambda s: built.append(s) or legacy)
    cli.run(_settings(), [case], 1)
    assert len(built) == 1

    injected = _RecordingBedrock(_Raises(error))
    runs, recording = cli.run(None, [case], 1, client=injected)
    assert len(built) == 1  # 주입 경로는 OpenAI 팩토리를 부르지 않는다
    assert injected.requests == legacy.requests and injected.requests
    assert recording.last_error_phase == "consent"


@pytest.mark.parametrize("phase", ["consent", "credentials", "request"])
def test_pre_model_failures_count_as_request_rejections_but_keep_the_raw_phase(phase):
    client = _RecordingBedrock(_Raises(AIModelRejectedError("pre-model", phase=phase)))
    runs, _ = cli.run(None, [_finops_case()], 1, client=client)
    assert [run.error_phase for run in runs] == [phase]
    report = build_column_report("bedrock", runs)
    assert (report.failed, report.failed_request, report.failed_transport, report.failed_contract) == (
        1, 1, 0, 0)


# --- SecOps run_one / report ----------------------------------------------------------


def test_run_one_labels_sources_without_misclassifying_bedrock():
    case = load_secops_cases()[0]
    bedrock = _RecordingBedrock(FakeAIModelClient([_decision(case)]))
    assert runner.run_one(case, 1, bedrock)["data_source"] == "bedrock_model"
    assert runner._data_source(OpenAIModelClient.__new__(OpenAIModelClient)) == "real_model"
    assert runner.run_one(case, 1, FakeAIModelClient([_decision(case)]))["data_source"] == "test_double"


@pytest.mark.parametrize("phase", ["consent", "credentials", "request"])
def test_run_one_groups_pre_model_failures_as_tool_error_and_keeps_raw_phase(phase):
    case = load_secops_cases()[0]
    client = _RecordingBedrock(_Raises(AIModelRejectedError("pre-model", phase=phase)))
    record = runner.run_one(case, 1, client)
    assert record["failure_kind"] == "TOOL_ERROR" and record["error_phase"] == phase


def test_bedrock_is_real_model_measurement_but_never_current_service_or_baseline():
    cases = load_secops_cases()
    run = runner.new_run(cases, 3, _settings(), "fixture-run")  # 기본 OPENAI_* 설정이 남은 조립
    run["records"] = [runner.run_one(case, n, _RecordingBedrock(FakeAIModelClient([_decision(case)])))
                      for n in range(1, 4) for case in cases]
    for record in run["records"]:
        record["model_snapshots"] = ["snapshot-1"]
    run["state"] = "COMPLETE"
    assert {record["data_source"] for record in run["records"]} == {"bedrock_model"}
    result = runner.report(run, cases, load_answers(cases))
    assert result["real_model_measurement"] is True
    assert result["current_service"] is False
    assert result["baseline_candidate"] is False
    assert result["case_acceptance"]["baseline_candidate"] is False
