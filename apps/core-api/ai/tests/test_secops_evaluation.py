"""평가 도구의 동작을 검증한다. 실제 모델·검토자의 품질 증거는 아니다."""

import json
from copy import deepcopy
from dataclasses import replace

import pytest
from ai import agent
from ai.agent import (
    CandidateProposalOutput,
    EvidenceSummaryOutput,
    ProposedCandidate,
    RiskReassessmentOutput,
)
from ai.evaluation.common.fingerprints import input_fingerprint
from ai.evaluation.secops import cli, dataset, runner
from ai.evaluation.secops.dataset import (
    ROOT,
    check_sources,
    digest,
    load_cases,
    read_json,
    validate_case,
)
from ai.evaluation.secops.scoring import (
    ReviewFile,
    SummaryReview,
    load_answers,
    score_structured,
    score_summary,
    score_summary_identifiers,
)
from ai.model_client import (
    AIModelContractError,
    AIModelTimeoutError,
    FakeAIModelClient,
    TokenUsage,
)
from config import Settings
from schemas.agents import AgentGraphOutput
from schemas.runbooks import RunbookId


@pytest.fixture(autouse=True)
def isolated_sources(monkeypatch, tmp_path):
    """테스트는 다른 담당자의 현재 Golden·corpus를 읽을 수 없게 한다."""
    monkeypatch.setattr(dataset, "REPO_ROOT", tmp_path)
    return tmp_path


@pytest.fixture
def cli_source_cases(isolated_sources, monkeypatch):
    """CLI의 원본 검사는 임시 원자료와 그 지문으로 실제 실행한다."""
    cases = load_cases()
    provenance = deepcopy(cases[0].provenance)
    for key in provenance:
        if not key.endswith("_source"):
            continue
        source = key.removesuffix("_source")
        value = [{"message": "동결 로그"}] if source == "log_records" else {"source": source}
        if source == "asset":
            value = {"ec2_instances": []}
        path = isolated_sources / provenance[key]
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = "\n".join(json.dumps(row) for row in value) if source == "log_records" else json.dumps(value)
        path.write_text(payload, encoding="utf-8")
        provenance[f"{source}_sha256"] = digest(value)
    cases = [replace(case, provenance=deepcopy(provenance)) for case in cases]
    monkeypatch.setattr(cli, "load_cases", lambda: cases)
    return cases


def _client(case, *, risk=None, proposal=True, **parameters):
    data = case.graph_input
    event = data.evidences[0].content.event
    candidate = ProposedCandidate(
        runbook_id="RUNBOOK_NACL_ADD_DENY", target_arn=data.asset_context.relationships[0].target_arn,
        evidence_ids=[data.evidences[0].evidence_id],
        **{"rule_number": 100, "cidr_block": f"{event.payload.source_ip}/32", "protocol": "tcp", **parameters},
    )
    return FakeAIModelClient([
        RiskReassessmentOutput(reviewed_risk_level=risk or data.initial_risk.initial_risk_level),
        CandidateProposalOutput(candidates=[candidate] if proposal else []),
        EvidenceSummaryOutput(observation="SSH 실패가 관측됐다.", diagnosis="반복 인증 시도다.",
                              rationale="출발지 차단을 제안한다."),
    ])


def _run(cases, repeats=1):
    settings = Settings(_env_file=None, DATABASE_URL="unused", **{
        name: Settings.model_fields[name].default for name in runner.SETTING_NAMES
    })
    return runner.new_run(cases, repeats, settings, "fixture-run")


def _review(run):
    """합성 판정값으로 집계만 확인한다. 문장 의미는 평가하지 않는다."""
    result = runner.review_template(run)
    result["reviewer"] = "test fixture (not an actual quality review)"
    records = {(record["case_id"], record["repeat"]): record for record in run["records"]}
    kinds = {check["check_id"]: check["kind"] for check in read_json(ROOT / "rubric.json")["checks"]}
    for review in result["reviews"]:
        lines = records[(review["case_id"], review["repeat"])]["output"]["summary_lines"]
        for item in review["items"]:
            item.update(verdict="PASS", reason="synthetic wiring fixture",
                        quote=lines[0] if kinds[item["check_id"]] == "required" else "")
    return ReviewFile.model_validate(result)


def test_fixed_primary_set_includes_all_seven_mvp_scenarios_and_uses_db_uuid():
    cases = load_cases()
    assert [case.case_id for case in cases] == [f"C{i:02}" for i in range(1, 8)]
    assert input_fingerprint(cases) == input_fingerprint(load_cases())
    assert set(load_answers(cases)) == {case.case_id for case in cases}


def test_secops_prompt_matches_explicit_snapshot():
    snapshot = read_json(ROOT / "prompt_snapshot.json")
    active = snapshot["candidate"] or snapshot["approved"]
    assert active["version"] == agent.SECOPS_PROMPT_VERSION
    assert runner.prompt_fingerprint() == active["prompt_sha256"], (
        "프롬프트가 동결 지문과 다릅니다. baseline.md의 재검증 절차와 후보 등록을 확인하세요."
    )
    if snapshot["candidate"] is not None:
        # 후보의 실측 결과와 승인판을 구분한다. 지문 일치는 품질 승인을 뜻하지 않는다.
        assert snapshot["candidate"]["quality_status"] in {"PENDING", "PASS", "FAIL"}
        if snapshot["candidate"]["quality_status"] != "PENDING":
            assert (ROOT / snapshot["candidate"]["result"]).is_file()
        assert snapshot["candidate"]["prompt_sha256"] != snapshot["approved"]["prompt_sha256"]
        assert (ROOT / snapshot["candidate"]["plan"]).is_file()
    assert active["rubric_sha256"] == digest(read_json(ROOT / "rubric.json"))


def test_request_manifest_covers_both_allowed_paths_without_paid_calls(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("요청 지문 계산은 유료 클라이언트를 만들면 안 된다")

    monkeypatch.setattr(cli, "build_openai_model_client", forbidden)
    cases = load_cases()
    manifest = runner.request_manifest(cases, load_answers(cases))
    assert len(manifest) == 14
    for case in cases:
        variants = [item for item in manifest if item["case_id"] == case.case_id]
        assert {item["status"] for item in variants} == {"SUCCEEDED", "NO_PROPOSAL"}
        assert all(len(item["requests"]) == 3 for item in variants)
        assert variants[0]["requests"][-1]["sha256"] != variants[1]["requests"][-1]["sha256"]


@pytest.mark.parametrize("change", ["prompt", "constraints"])
def test_unrelated_finops_or_file_changes_do_not_invalidate_secops_requests(monkeypatch, change):
    cases = load_cases()
    run = _run(cases)
    run["service_sha256"] = "이전 파일 전문 지문"
    if change == "prompt":
        monkeypatch.setattr(agent, "_FINOPS_SUMMARY_SYSTEM_PROMPT", "FinOps만 변경했다.")
    else:
        monkeypatch.setitem(agent._PARAMETER_CONSTRAINTS,
                            RunbookId.RUNBOOK_EC2_ENABLE_AUTOSCALING, ("FinOps 전용 제약 변경",))
    result = runner.report(run, cases, load_answers(cases))
    assert runner.prompt_fingerprint() == run["prompt_sha256"]
    assert result["request_sha256"] == run["request_sha256"]
    assert result["current_service"]
    assert not result["source_files_match"]
    assert not result["quality_pass"]


@pytest.mark.parametrize("change", ["prompt", "payload", "response_schema", "constraints"])
def test_request_change_requires_remeasurement(monkeypatch, change):
    cases = load_cases()
    run = _run(cases)
    if change == "prompt":
        monkeypatch.setattr(agent, "_SECOPS_RISK_PROMPT", agent._SECOPS_RISK_PROMPT + "\n새 판단 기준")
    elif change == "payload":
        original = agent._secops_payload
        monkeypatch.setattr(agent, "_secops_payload", lambda value: {**original(value), "new_context": True})
    elif change == "constraints":
        monkeypatch.setitem(agent._PARAMETER_CONSTRAINTS,
                            RunbookId.RUNBOOK_NACL_ADD_DENY, ("SecOps 제약 변경",))
    else:
        original = RiskReassessmentOutput.model_json_schema
        monkeypatch.setattr(RiskReassessmentOutput, "model_json_schema",
                            classmethod(lambda cls, **kwargs: {**original(**kwargs), "description": "새 계약 설명"}))
    result = runner.report(run, cases, load_answers(cases))
    if change != "payload":
        assert runner.prompt_fingerprint() != run["prompt_sha256"]
    assert result["request_sha256"] != run["request_sha256"]
    assert not result["current_service"]


def test_legacy_run_without_request_fingerprint_is_not_assumed_compatible():
    cases = load_cases()
    run = _run(cases)
    del run["request_sha256"]
    result = runner.report(run, cases, load_answers(cases))
    assert not result["request_fingerprint_recorded"]
    assert not result["current_service"]
    assert not result["baseline_candidate"]


@pytest.mark.parametrize("token", [
    "reviewed_risk_level", "reviewed_risk_label", "failed_attempt_count", "SSH_BRUTEFORCE",
    "RISK_SSH_BRUTEFORCE", "HIGH", "observation",
])
def test_internal_summary_identifiers_fail_without_a_semantic_review(token):
    case = load_cases()[0]
    raw = runner.run_one(case, 1, _client(case))["output"]
    raw["summary_lines"][1] = f"{token}은 이번 판단이다."
    result = score_summary(AgentGraphOutput.model_validate(raw), None, read_json(ROOT / "rubric.json"))
    assert result.verdict == "FAIL"
    assert f"internal_identifier:{token}" in result.reasons


def test_operator_language_and_resource_values_are_not_internal_identifiers():
    case = load_cases()[0]
    raw = runner.run_one(case, 1, _client(case))["output"]
    raw["summary_lines"] = [
        "SSH 인증 실패가 120회 발생했고 위험은 높음이다.",
        f"EC2 {case.graph_input.asset_context.arn}에서 관측했다.",
        "NACL에 203.0.113.1/32의 TCP 전체 포트를 거부하는 규칙을 제안한다.",
    ]
    assert score_summary_identifiers(AgentGraphOutput.model_validate(raw)).verdict == "PASS"


@pytest.mark.parametrize("change", ["event", "capability", "asset"])
def test_frozen_fixture_or_service_input_drift_is_not_silently_accepted(change):
    record = deepcopy(read_json(ROOT / "inputs.json")["cases"][0])
    if change == "event":
        record["graph_input"]["evidences"][0]["content"]["event"]["payload"]["failed_attempt_count"] += 1
    elif change == "capability":
        record["graph_input"]["capabilities"][0]["purpose"] = "different menu"
    else:
        record["graph_input"]["asset_context"]["collected_at"] = "2026-09-01T00:00:00Z"
    with pytest.raises(ValueError):
        validate_case(record)


@pytest.mark.parametrize("source", ["threat", "risk", "asset", "log_manifest", "log_expected", "log_records", "context"])
def test_source_drift_is_checked_separately_from_frozen_input_loading(isolated_sources, cli_source_cases, source):
    check_sources(cli_source_cases)
    before = input_fingerprint(load_cases())
    path = isolated_sources / cli_source_cases[0].provenance[f"{source}_source"]
    if source == "log_records":
        with path.open("a", encoding="utf-8") as stream:
            stream.write('\n{"message": "추가된 로그"}\n')
    else:
        value = read_json(path)
        if source == "asset":
            value["ec2_instances"].append({"instance_id": "i-unrelated"})
        else:
            value["unrelated"] = True
        path.write_text(json.dumps(value), encoding="utf-8")
    assert input_fingerprint(load_cases()) == before
    with pytest.raises(ValueError, match="Frozen source changed"):
        check_sources(cli_source_cases)


def test_missing_sources_do_not_block_frozen_case_loading(isolated_sources):
    assert not (isolated_sources / "datasets").exists()
    cases = load_cases()
    assert len(cases) == 7
    with pytest.raises(ValueError, match="Source outside repository or missing"):
        check_sources(cases)


def test_missing_answer_cannot_shrink_the_denominator(tmp_path):
    data = read_json(ROOT / "answers.json")
    data["cases"].pop()
    path = tmp_path / "answers.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match="match the full"):
        load_answers(load_cases(), path)


@pytest.mark.parametrize("change,reason", [
    ("risk", "reviewed_risk_outside_answer"),
    ("target", "target_arn"), ("cidr", "cidr_block"), ("evidence", "evidence_ids"),
])
def test_deterministic_wrong_answers_fail(change, reason):
    cases = load_cases()
    record = runner.run_one(cases[0], 1, _client(cases[0]))
    output = record["output"]
    if change == "risk":
        output["reviewed_risk_level"] = "LOW"
    elif change == "target":
        output["candidates"][0]["target_arn"] = cases[0].graph_input.asset_context.arn
    elif change == "cidr":
        output["candidates"][0]["parameters"]["cidr_block"] = "203.0.113.0/24"
    else:
        output["candidates"][0]["evidence_ids"] = ["unknown"]
    score = score_structured(AgentGraphOutput.model_validate(output), load_answers(cases)["C01"])
    assert score.verdict == "FAIL" and reason in score.reasons


@pytest.mark.parametrize("case_id", [f"C{i:02}" for i in range(1, 8)])
@pytest.mark.parametrize("proposal", [True, False])
def test_current_answers_allow_proposal_and_absence_without_changing_risk(case_id, proposal):
    cases = load_cases()
    case = next(case for case in cases if case.case_id == case_id)
    answer = load_answers(cases)[case_id]
    assert answer.action_policy == "EITHER"
    assert set(answer.allowed_statuses) == {"SUCCEEDED", "NO_PROPOSAL"}
    record = runner.run_one(case, 1, _client(case, proposal=proposal))
    output = AgentGraphOutput.model_validate(record["output"])
    assert output.invocation_status.value == ("SUCCEEDED" if proposal else "NO_PROPOSAL")
    assert output.reviewed_risk_level == case.graph_input.initial_risk.initial_risk_level
    assert score_structured(output, answer).verdict == "PASS"
    assert record["model_calls"] == 3 and record["data_source"] == "test_double"


def test_dispatcher_rejection_is_preserved_after_a_valid_graph_dto():
    case = load_cases()[0]
    record = runner.run_one(case, 1, _client(case, cidr_block="203.0.113.0/24"))
    assert record["graph_output"]["invocation_status"] == "SUCCEEDED"
    assert record["output"]["invocation_status"] == "FAILED"
    assert record["dispatcher_rejected"]
    assert record["failure_kind"] == "OUTPUT_CONTRACT_FAILURE"
    assert record["model_calls"] == 3 and record["data_source"] == "test_double"


@pytest.mark.parametrize("phase,kind", [
    ("transport", "TRANSPORT_ERROR"), ("request", "TOOL_ERROR"), ("response", "MODEL_RESPONSE_ERROR"),
])
def test_failure_attribution_and_reported_usage(phase, kind):
    class FailedClient:
        def complete(self, request, response_model):
            if phase == "transport":
                raise AIModelTimeoutError("test timeout")
            raise AIModelContractError("test contract", phase=phase, usage=TokenUsage(12, 4, 16))

    record = runner.run_one(load_cases()[0], 1, FailedClient())
    assert record["failure_kind"] == kind and record["model_calls"] == 1
    assert record["reported_prompt_tokens"] == (0 if phase == "transport" else 12)
    assert record["error_phase"] == phase


@pytest.mark.parametrize("change", ["hash", "duplicate", "missing", "quote", "empty_quote"])
def test_review_binding_and_evidence_errors_are_not_quality_passes(change):
    case = load_cases()[0]
    run = _run([case])
    run["records"].append(runner.run_one(case, 1, _client(case)))
    review = _review(run).reviews[0].model_dump()
    if change == "hash":
        review["output_sha256"] = "different"
    elif change == "duplicate":
        review["items"].append(review["items"][0])
    elif change == "missing":
        review["items"].pop()
    elif change == "quote":
        review["items"][0]["quote"] = "this is absent from the output"
    else:
        review["items"][0]["quote"] = ""
    output = AgentGraphOutput.model_validate(run["records"][0]["output"])
    assert score_summary(output, SummaryReview.model_validate(review), read_json(ROOT / "rubric.json")).verdict == "TOOL_ERROR"


def test_known_summary_omission_survives_another_uncertain_check():
    case = load_cases()[0]
    run = _run([case])
    run["records"].append(runner.run_one(case, 1, _client(case)))
    review = _review(run).reviews[0]
    review.items[0].verdict, review.items[0].quote = "FAIL", ""
    review.items[0].reason = "발생 시각, 출발지, 횟수, 창 길이가 없다."
    review.items[1].verdict = "UNSURE"
    output = AgentGraphOutput.model_validate(run["records"][0]["output"])
    score = score_summary(output, review, read_json(ROOT / "rubric.json"))
    assert score.verdict == "FAIL" and "observed_facts" in score.reasons


def test_consistently_wrong_risk_is_not_a_reproducible_success():
    cases = load_cases()
    run = _run([cases[0]], 3)
    run["records"] = [runner.run_one(cases[0], n, _client(cases[0], risk="LOW")) for n in range(1, 4)]
    run["state"] = "COMPLETE"
    result = runner.report(run, [cases[0]], load_answers(cases), _review(run))
    assert result["counts"]["FAIL"] == 3
    assert result["risk_agreement"]["C01"] == ["LOW"] * 3
    assert not result["baseline_candidate"]


def test_missing_runs_and_reviews_cannot_pass_and_records_cannot_duplicate():
    cases = load_cases()
    run = _run(cases, 3)
    run["records"] = [runner.run_one(cases[0], 1, _client(cases[0]))]
    result = runner.report(run, cases, load_answers(cases))
    assert result["planned"] == 21 and result["counts"]["PENDING"] == 21
    assert not result["quality_pass"] and not result["baseline_candidate"]
    run["records"].append(deepcopy(run["records"][0]))
    with pytest.raises(ValueError, match="Duplicate"):
        runner.report(run, cases, load_answers(cases))


def test_fake_measurements_never_establish_a_real_baseline():
    cases = load_cases()
    run = _run(cases, 3)
    run["records"] = [runner.run_one(case, n, _client(case, rule_number=100 + n))
                      for n in range(1, 4) for case in cases]
    run["state"] = "COMPLETE"
    result = runner.report(run, cases, load_answers(cases), _review(run))
    assert result["quality_pass"]  # 합성 판정값의 집계가 맞다는 뜻으로 한정한다.
    assert result["counts"]["PASS"] == 21  # 허용된 rule_number 차이는 진단값으로만 남긴다.
    assert not result["real_model_measurement"] and not result["baseline_candidate"]


@pytest.mark.parametrize("argv", [[], ["--check-inputs"], ["--estimate"]])
def test_offline_cli_never_builds_a_paid_client(monkeypatch, capsys, cli_source_cases, argv):
    def forbidden(*args, **kwargs):
        raise AssertionError("paid client must not be created")
    monkeypatch.setattr(cli, "build_openai_model_client", forbidden)
    assert cli.main(argv) == 0
    payload = json.loads(capsys.readouterr().out)
    if argv == ["--estimate"]:
        assert payload["model_calls_upper_bound"] == 21
        assert payload["http_attempts_upper_bound"] == 63
    assert "OPENAI_API_KEY" not in json.dumps(payload)


@pytest.mark.parametrize("mode", [None, "--check-inputs", "--estimate", "--run", "--report", "--review-template"])
def test_cli_rejects_source_drift_before_settings_model_or_output(
    isolated_sources, cli_source_cases, monkeypatch, mode,
):
    path = isolated_sources / cli_source_cases[0].provenance["asset_source"]
    path.write_text('{"ec2_instances": [{"instance_id": "i-unrelated"}]}', encoding="utf-8")
    output = isolated_sources / "result.json"
    argv = [] if mode is None else [mode]
    if mode in ("--report", "--review-template"):
        argv.append(str(isolated_sources / "not-read.json"))
    argv.extend(["--output", str(output)])

    def forbidden(*args, **kwargs):
        raise AssertionError("원본 검사 실패 뒤 설정이나 모델 클라이언트를 준비하면 안 된다")

    monkeypatch.setattr(cli, "_settings", forbidden)
    monkeypatch.setattr(cli, "build_openai_model_client", forbidden)
    with pytest.raises(ValueError, match="Frozen source changed"):
        cli.main(argv)
    assert not output.exists()


def test_new_result_file_is_exclusive(tmp_path):
    path = tmp_path / "result.json"
    cli._create(path, {"original": True})
    with pytest.raises(FileExistsError):
        cli._create(path, {"original": False})
    assert read_json(path) == {"original": True}


def test_report_cli_revalidates_saved_requests_without_paid_calls(
    cli_source_cases, isolated_sources, monkeypatch, capsys,
):
    run = _run(cli_source_cases, 3)
    run["records"] = [runner.run_one(case, repeat, _client(case))
                      for repeat in range(1, 4) for case in cli_source_cases]
    run["state"] = "COMPLETE"
    run["service_sha256"] = "요청에 영향 없는 파일 변경 전 지문"
    run_path, review_path = isolated_sources / "run.json", isolated_sources / "review.json"
    run_path.write_text(json.dumps(run), encoding="utf-8")
    review_path.write_text(_review(run).model_dump_json(), encoding="utf-8")

    def forbidden(*args, **kwargs):
        raise AssertionError("보존 결과 재검증에서 유료 클라이언트를 만들면 안 된다")

    monkeypatch.setattr(cli, "build_openai_model_client", forbidden)
    assert cli.main(["--report", str(run_path), "--review", str(review_path)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["counts"]["PASS"] == 21
    assert result["current_service"] and not result["source_files_match"]
    assert not result["real_model_measurement"] and not result["baseline_candidate"]
