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
    SecOpsEvidenceDecisionOutput,
    SecOpsGraphVariant,
)
from ai.evaluation.common.fingerprints import input_fingerprint
from ai.capabilities import secops_action_targets
from ai.evaluation.secops import cli, dataset, derived_sources, runner
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
from schemas.api.incidents import RiskLevel
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


def _client(case, *, proposal=None):
    """근거 선행 그래프의 1회 호출 대역. 실행 계획은 코드가 조립한다.

    proposal을 생략하면 답지가 허용하는 결정을 따른다(C10은 후보 없이 무제안만 허용).
    """
    if proposal is None:
        proposal = case.case_id != "C10"
    return FakeAIModelClient([
        SecOpsEvidenceDecisionOutput(
            observation="SSH 실패가 관측됐다.", diagnosis="반복 인증 시도다.",
            rationale="출발지 차단을 제안한다.", decision=[1] if proposal else [],
        ),
    ])


def _fallback_client(case, *, proposal=True, **parameters):
    data = case.graph_input
    event = data.evidences[0].content.event
    candidate = ProposedCandidate(
        runbook_id="RUNBOOK_NACL_ADD_DENY", target_arn=data.asset_context.relationships[0].target_arn,
        evidence_ids=[data.evidences[0].evidence_id],
        **{"rule_number": 100, "cidr_block": f"{event.payload.source_ip}/32", "protocol": "tcp", **parameters},
    )
    return FakeAIModelClient([
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


def test_fixed_primary_set_includes_all_ten_scenarios_and_uses_db_uuid():
    cases = load_cases()
    assert [case.case_id for case in cases] == [f"C{i:02}" for i in range(1, 11)]
    assert input_fingerprint(cases) == input_fingerprint(load_cases())
    assert set(load_answers(cases)) == {case.case_id for case in cases}


def test_secops_prompt_matches_explicit_snapshot():
    snapshot = read_json(ROOT / "prompt_snapshot.json")
    candidate = snapshot.get("candidate")
    active = candidate or snapshot["approved"]
    assert active["version"] == agent.SECOPS_PROMPT_VERSION
    assert runner.prompt_fingerprint() == active["prompt_sha256"], (
        "프롬프트가 동결 지문과 다릅니다. baseline.md의 재검증 절차와 후보 등록을 확인하세요."
    )
    if candidate is not None:
        # 후보의 실측 결과와 승인판을 구분한다. 지문 일치는 품질 승인을 뜻하지 않는다.
        assert candidate["quality_status"] in {"PENDING", "PASS", "FAIL"}
        if candidate["quality_status"] != "PENDING":
            assert (ROOT / candidate["result"]).is_file()
        assert candidate["prompt_sha256"] != snapshot["approved"]["prompt_sha256"]
        assert (ROOT / candidate["plan"]).is_file()
        current = runner.fingerprints(load_cases())
        assert candidate["inputs_sha256"] == current["inputs_sha256"]
        assert candidate["request_sha256"] == current["request_sha256"]
    assert active["rubric_sha256"] == digest(read_json(ROOT / "rubric.json"))


def test_approved_snapshot_is_the_measured_evidence_first_version():
    snapshot = read_json(ROOT / "prompt_snapshot.json")
    approved = snapshot["approved"]
    # 후보가 등록된 동안 현재 프롬프트는 후보판이다. 승인판은 측정된 지문을 그대로 보존한다.
    if snapshot.get("candidate") is None:
        assert approved["version"] == agent.SECOPS_PROMPT_VERSION
    assert approved["measured_prompt_sha256"] == approved["prompt_sha256"]
    assert (ROOT / approved["result"]).is_file()
    # 대비책의 프롬프트 지문은 근거 선행 지문과 다르고 v1.1.0 지문을 그대로 재현한다.
    fallback = snapshot["fallback"]
    assert fallback["version"] == "v1.1.0" and fallback["graph_variant"] == "CANDIDATE_FIRST"
    assert runner.prompt_fingerprint(SecOpsGraphVariant.CANDIDATE_FIRST) == fallback["prompt_sha256"]
    assert fallback["prompt_sha256"] != approved["prompt_sha256"]
    assert (ROOT / fallback["plan"]).is_file()


def test_rubric_adds_decision_support_check_and_drops_reassessment_wording():
    rubric = read_json(ROOT / "rubric.json")
    checks = {item["check_id"]: item for item in rubric["checks"]}
    assert checks["rationale_supports_decision"]["kind"] == "required"
    assert len(checks) == 8 + 1
    assert "실제 재평가" not in json.dumps(rubric, ensure_ascii=False)
    assert "서버 초기 판정" in checks["risk_reason"]["criterion"]


def test_request_manifest_covers_both_allowed_paths_without_paid_calls(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("요청 지문 계산은 유료 클라이언트를 만들면 안 된다")

    monkeypatch.setattr(cli, "build_openai_model_client", forbidden)
    cases = load_cases()
    manifest = runner.request_manifest(cases, load_answers(cases))
    assert len(manifest) == 2 * 9 + 1
    for case in cases:
        variants = [item for item in manifest if item["case_id"] == case.case_id]
        # C10은 NACL 범위가 맞지 않아 무제안 경로만 답지가 허용한다.
        assert {item["status"] for item in variants} == (
            {"NO_PROPOSAL"} if case.case_id == "C10" else {"SUCCEEDED", "NO_PROPOSAL"})
        # 근거 선행 그래프는 건당 요청 1건이다.
        assert all(len(item["requests"]) == agent.SECOPS_MODEL_CALLS == 1 for item in variants)


def test_assembled_candidate_must_equal_the_frozen_answer():
    cases = load_cases()
    answers = load_answers(cases)
    answers["C01"].candidate.cidr_block = "203.0.113.0/24"
    with pytest.raises(ValueError, match="differs from the frozen answer"):
        runner.request_manifest(cases[:1], answers)


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


def test_secops_parameter_constraints_are_not_sent_to_the_model(monkeypatch):
    cases = load_cases()
    run = _run(cases)
    monkeypatch.setitem(agent._PARAMETER_CONSTRAINTS,
                        RunbookId.RUNBOOK_NACL_ADD_DENY, ("SecOps 제약 변경",))
    result = runner.report(run, cases, load_answers(cases))
    assert runner.prompt_fingerprint() == run["prompt_sha256"]
    assert result["request_sha256"] == run["request_sha256"]
    # 대비책은 제약을 여전히 보내므로 지문이 달라진다.
    assert runner.prompt_fingerprint(SecOpsGraphVariant.CANDIDATE_FIRST) != read_json(
        ROOT / "prompt_snapshot.json")["fallback"]["prompt_sha256"]


@pytest.mark.parametrize("change", ["prompt", "payload", "response_schema"])
def test_request_change_requires_remeasurement(monkeypatch, change):
    cases = load_cases()
    run = _run(cases)
    if change == "prompt":
        monkeypatch.setattr(agent, "_SECOPS_EVIDENCE_PROMPT", agent._SECOPS_EVIDENCE_PROMPT + "\n새 판단 기준")
    elif change == "payload":
        original = agent._secops_evidence_payload
        monkeypatch.setattr(agent, "_secops_evidence_payload", lambda value: {**original(value), "new_context": True})
    else:
        original = SecOpsEvidenceDecisionOutput.model_json_schema
        monkeypatch.setattr(SecOpsEvidenceDecisionOutput, "model_json_schema",
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
    "reviewed_risk_level", "initial_risk_label", "failed_attempt_count", "SSH_BRUTEFORCE",
    "RISK_SSH_BRUTEFORCE", "HIGH", "observation", "decision",
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
    assert len(cases) == 10
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
    ("risk", "reviewed_risk_present"),
    ("target", "target_arn"), ("cidr", "cidr_block"), ("evidence", "evidence_ids"),
])
def test_deterministic_wrong_answers_fail(change, reason):
    cases = load_cases()
    record = runner.run_one(cases[0], 1, _client(cases[0]))
    output = record["output"]
    if change == "risk":
        # 서버 초기 판정과 같은 값이라도 출력에 실리면 실패다 — 그래프는 등급을 내지 않는다.
        output["reviewed_risk_level"] = cases[0].graph_input.initial_risk.initial_risk_level.value
    elif change == "target":
        output["candidates"][0]["target_arn"] = cases[0].graph_input.asset_context.arn
    elif change == "cidr":
        output["candidates"][0]["parameters"]["cidr_block"] = "203.0.113.0/24"
    else:
        output["candidates"][0]["evidence_ids"] = ["unknown"]
    score = score_structured(AgentGraphOutput.model_validate(output), load_answers(cases)["C01"])
    assert score.verdict == "FAIL" and reason in score.reasons


@pytest.mark.parametrize("case_id", [f"C{i:02}" for i in range(1, 10)])
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
    assert output.reviewed_risk_level is None
    assert score_structured(output, answer).verdict == "PASS"
    assert record["model_calls"] == 1 and record["data_source"] == "test_double"


C10_SOURCES = ROOT / "sources" / "C10"
LOOPBACK = derived_sources.LOOPBACK_SOURCE_IP
ORIGINAL = derived_sources.ORIGINAL_SOURCE_IP


def _case(case_id):
    return next(case for case in load_cases() if case.case_id == case_id)


def test_c10_keeps_c01_strength_and_menu_with_only_the_source_changed():
    c01, c10 = _case("C01").graph_input, _case("C10").graph_input
    old, new = c01.evidences[0].content, c10.evidences[0].content
    assert new.event.payload.source_ip == LOOPBACK and old.event.payload.source_ip == ORIGINAL
    assert new.event.payload.model_copy(update={"source_ip": ORIGINAL}) == old.event.payload
    assert (new.event.target_arn, new.event.occurred_at) == (old.event.target_arn, old.event.occurred_at)
    assert c10.initial_risk.initial_risk_level.value == "HIGH" == c01.initial_risk.initial_risk_level.value
    assert c10.initial_risk.reason_codes == c01.initial_risk.reason_codes
    assert c10.current_rule_risk_basis == c01.current_rule_risk_basis
    # 서비스 조립이 만든 메뉴는 비어 있지 않은 한 항목의 NACL이고 C01과 같다.
    assert c10.capabilities == c01.capabilities and len(c10.capabilities) == 1
    targets = secops_action_targets(c10.asset_context)["RUNBOOK_NACL_ADD_DENY"]
    assert len(targets) == 1 and targets == secops_action_targets(c01.asset_context)["RUNBOOK_NACL_ADD_DENY"]
    logs, original_logs = new.context.log_evidence, old.context.log_evidence
    assert logs.source_ip == LOOPBACK
    assert (logs.record_count, logs.failed_attempt_count, logs.accepted_count, logs.auxiliary_count) == (
        original_logs.record_count, original_logs.failed_attempt_count,
        original_logs.accepted_count, original_logs.auxiliary_count) == (200, 120, 0, 80)
    assert [row.record_id for row in logs.records] == [row.record_id for row in original_logs.records]
    assert all(LOOPBACK in row.message and ORIGINAL not in row.message for row in logs.records)
    # 변환한 전체 자료의 지문은 원본 지문을 재사용하지 않는다.
    assert logs.corpus_fingerprint != original_logs.corpus_fingerprint


def test_c10_frozen_sources_are_wholly_loopback_and_hash_the_transformed_corpus():
    case = _case("C10")
    provenance, logs = case.provenance, case.graph_input.evidences[0].content.context.log_evidence
    records = [json.loads(line) for line in (C10_SOURCES / "logs.jsonl").read_text(encoding="utf-8").splitlines()
               if line.strip()]
    threat, manifest, expected = (read_json(C10_SOURCES / f"{name}.json")
                                  for name in ("threat", "manifest", "expected"))
    assert len(records) == 200
    assert all(LOOPBACK in row["message"] and ORIGINAL not in row["message"] for row in records)
    assert sum(row["message"].startswith("Failed password") for row in records) == 120
    assert threat["source_ip"] == manifest["selection"]["source_ip"] == LOOPBACK
    assert digest(records) == provenance["log_records_sha256"] == logs.corpus_fingerprint
    assert (digest(threat), digest(manifest), digest(expected)) == (
        provenance["threat_sha256"], provenance["log_manifest_sha256"], provenance["log_expected_sha256"])
    assert manifest["golden_fingerprint"] == digest(threat)
    assert derived_sources.TRANSFORMATION in manifest["transformations"]
    excerpt = records[:6] + records[-6:]
    assert [(row.record_id, row.message) for row in logs.records] == [
        (row["record_id"], row["message"]) for row in excerpt]
    original = provenance["derivation"]["original"]
    assert original["records_sha256"] != provenance["log_records_sha256"]
    assert original["threat_sha256"] != provenance["threat_sha256"]
    assert original["manifest_sha256"] != provenance["log_manifest_sha256"]


def _mini_original():
    row = {"record_id": "C01-000001", "source_time": "t", "host": "mock-ec2-C01",
           "message": f"Failed password for invalid user mock-user from {ORIGINAL} port 41000 ssh2"}
    return {"threat": {"source_ip": ORIGINAL, "failed_attempt_count": 1},
            "manifest": {"selection": {"source_ip": ORIGINAL, "host": "mock-ec2-C01"},
                         "transformations": ["a"], "golden_input": "x.json", "golden_fingerprint": "old"},
            "expected": {"aggregate": {"failed": 1}}, "records": [row]}


def test_derivation_changes_only_the_source_and_never_mutates_the_original():
    original = _mini_original()
    snapshot = deepcopy(original)
    derived = derived_sources.derive_loopback_sources(original)
    assert original == snapshot
    assert derived["threat"] == {**snapshot["threat"], "source_ip": LOOPBACK}
    assert derived["expected"] == snapshot["expected"]
    assert derived["records"][0] == {
        **snapshot["records"][0], "message": snapshot["records"][0]["message"].replace(ORIGINAL, LOOPBACK)}
    assert derived["manifest"]["selection"] == {"source_ip": LOOPBACK, "host": "mock-ec2-C01"}
    assert derived["manifest"]["golden_fingerprint"] == digest(derived["threat"]) != "old"
    assert derived["manifest"]["transformations"] == ["a", derived_sources.TRANSFORMATION]


@pytest.mark.parametrize("flaw", ["threat", "manifest", "record"])
def test_derivation_refuses_sources_that_are_not_the_expected_c01_original(flaw):
    original = _mini_original()
    if flaw == "threat":
        original["threat"]["source_ip"] = "198.51.100.1"
    elif flaw == "manifest":
        original["manifest"]["selection"]["source_ip"] = "198.51.100.1"
    else:
        original["records"][0]["message"] = "Failed password for invalid user mock-user from 198.51.100.1 port 1 ssh2"
    with pytest.raises(ValueError):
        derived_sources.derive_loopback_sources(original)


@pytest.mark.parametrize("tamper", [None, "original", "frozen"])
def test_derived_source_check_recomputes_the_transformation_from_the_original(isolated_sources, tamper):
    original = _mini_original()
    frozen = derived_sources.derive_loopback_sources(original)
    names = {"threat": "threat", "manifest": "log_manifest", "expected": "log_expected", "records": "log_records"}
    provenance = {"derivation": {"original": {}}}
    for name, kind in names.items():
        for label, value in (("original", original[name]), ("frozen", frozen[name])):
            path = isolated_sources / label / f"{name}.json"
            path.parent.mkdir(exist_ok=True)
            path.write_text("\n".join(json.dumps(row) for row in value) if name == "records"
                            else json.dumps(value), encoding="utf-8")
            target = provenance["derivation"]["original"] if label == "original" else provenance
            key = f"{name}_source" if label == "original" else f"{kind}_source"
            target[key] = f"{label}/{name}.json"
            target[key.replace("_source", "_sha256")] = digest(value)
    case = replace(_case("C10"), provenance=provenance)
    if tamper:
        path = isolated_sources / tamper / "threat.json"
        path.write_text(json.dumps({"source_ip": "9.9.9.9"}), encoding="utf-8")
    if tamper is None:
        derived_sources.check_derived_sources([case, _case("C01")])
        return
    with pytest.raises(ValueError, match="changed|differs from its derivation"):
        derived_sources.check_derived_sources([case])


def test_c10_accepts_only_absence_and_rejects_a_proposal_that_nacl_scope_cannot_support():
    case = _case("C10")
    answer = load_answers(load_cases())["C10"]
    assert answer.action_policy == "OBSERVE" and answer.allowed_statuses == ["NO_PROPOSAL"]
    assert answer.candidate is None
    # 같은 메뉴가 C01에서는 후보를 조립한다 — C10의 기대는 메뉴 부재가 아니라 범위 판단이다.
    proposal = AgentGraphOutput.model_validate(runner.run_one(case, 1, _client(case, proposal=True))["output"])
    assert proposal.invocation_status.value == "SUCCEEDED" and len(proposal.candidates) == 1
    score = score_structured(proposal, answer)
    assert score.verdict == "FAIL"
    assert {"inappropriate_proposal_or_absence", "unexpected_candidate"} <= set(score.reasons)
    absence = AgentGraphOutput.model_validate(runner.run_one(case, 1, _client(case, proposal=False))["output"])
    assert absence.invocation_status.value == "NO_PROPOSAL" and absence.reviewed_risk_level is None
    assert score_structured(absence, answer).verdict == "PASS"


@pytest.mark.parametrize("proposal", [True, False])
def test_candidate_first_fallback_variant_is_measurable_with_two_calls(proposal):
    case = load_cases()[0]
    record = runner.run_one(case, 1, _fallback_client(case, proposal=proposal),
                            SecOpsGraphVariant.CANDIDATE_FIRST)
    assert record["model_calls"] == agent.SECOPS_FALLBACK_MODEL_CALLS == 2
    assert record["output"]["invocation_status"] == ("SUCCEEDED" if proposal else "NO_PROPOSAL")


def test_new_run_records_variant_and_sizes_calls_by_it():
    cases = load_cases()
    settings = Settings(_env_file=None, DATABASE_URL="unused", **{
        name: Settings.model_fields[name].default for name in runner.SETTING_NAMES
    })
    default = runner.new_run(cases, 3, settings, "default")
    fallback = runner.new_run(cases, 3, settings, "fallback", SecOpsGraphVariant.CANDIDATE_FIRST)
    assert default["graph_variant"] == "EVIDENCE_FIRST" and default["model_calls_upper_bound"] == 30
    assert default["http_attempts_upper_bound"] == 30 * settings.OPENAI_MAX_ATTEMPTS
    assert fallback["graph_variant"] == "CANDIDATE_FIRST" and fallback["model_calls_upper_bound"] == 60
    assert fallback["prompt_sha256"] != default["prompt_sha256"]


def test_dispatcher_rejection_is_preserved_after_a_valid_graph_dto(monkeypatch):
    case = load_cases()[0]
    original = agent.run_secops_graph

    def with_reviewed_risk(*args, **kwargs):
        # 그래프 DTO는 유효하지만 Dispatcher 계약(등급 없음)은 어기는 출력을 만든다.
        return original(*args, **kwargs).model_copy(update={"reviewed_risk_level": RiskLevel.HIGH})

    monkeypatch.setattr(agent, "run_secops_graph", with_reviewed_risk)
    record = runner.run_one(case, 1, _client(case))
    assert record["graph_output"]["invocation_status"] == "SUCCEEDED"
    assert record["output"]["invocation_status"] == "FAILED"
    assert record["dispatcher_rejected"]
    assert record["failure_kind"] == "OUTPUT_CONTRACT_FAILURE"
    assert record["model_calls"] == 1 and record["data_source"] == "test_double"


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


def test_consistently_present_reviewed_risk_is_not_a_reproducible_success():
    """그래프는 등급을 내지 않으므로, 보존 결과에 등급이 반복해 실려 있어도 통과가 아니다."""
    cases = load_cases()
    run = _run([cases[0]], 3)
    run["records"] = [runner.run_one(cases[0], n, _client(cases[0])) for n in range(1, 4)]
    for record in run["records"]:
        record["output"]["reviewed_risk_level"] = "HIGH"
    run["state"] = "COMPLETE"
    result = runner.report(run, [cases[0]], load_answers(cases), _review(run))
    assert result["counts"]["FAIL"] == 3
    assert all("reviewed_risk_present" in item["structured"]["reasons"] for item in result["scores"])
    assert not result["baseline_candidate"]


def test_missing_runs_and_reviews_cannot_pass_and_records_cannot_duplicate():
    cases = load_cases()
    run = _run(cases, 3)
    run["records"] = [runner.run_one(cases[0], 1, _client(cases[0]))]
    result = runner.report(run, cases, load_answers(cases))
    assert result["planned"] == 30 and result["counts"]["PENDING"] == 30
    assert not result["quality_pass"] and not result["baseline_candidate"]
    run["records"].append(deepcopy(run["records"][0]))
    with pytest.raises(ValueError, match="Duplicate"):
        runner.report(run, cases, load_answers(cases))


def _ten_repeat_real_run(cases, drop=None):
    """drop: {case_id: 제외할 repeat 목록}. 제외된 기록은 계획에는 남고 관측에서만 빠진다."""
    drop = drop or {}
    run = _run(cases, 10)
    run["records"] = [runner.run_one(case, n, _client(case))
                      for n in range(1, 11) for case in cases
                      if n not in drop.get(case.case_id, ())]
    for record in run["records"]:
        record["data_source"] = "real_model"
        record["model_snapshots"] = ["snapshot-1"]
    run["state"] = "COMPLETE"
    return run


def test_case_acceptance_applies_eight_of_ten_per_case_with_missing_records_as_non_pass():
    cases = load_cases()
    first, second = cases[0].case_id, cases[1].case_id
    run = _ten_repeat_real_run(cases, {first: (9, 10), second: (8, 9, 10)})
    result = runner.report(run, cases, load_answers(cases), _review(run))
    acceptance = result["case_acceptance"]
    assert (acceptance["required_repeats"], acceptance["required_passes"]) == (10, 8)
    assert acceptance["measurement"] == {
        "planned_fixed_set_x10": True, "fully_observed": False,
        "fixed_case_count": 10, "missing_case_ids": [],
    }
    assert acceptance["cases"][first] == {
        "planned": 10, "recorded": 8, "threshold_met": True,
        "counts": {"PASS": 8, "FAIL": 0, "PENDING": 2, "TOOL_ERROR": 0, "TRANSPORT_ERROR": 0}}
    assert acceptance["cases"][second]["counts"]["PASS"] == 7
    assert acceptance["cases"][second]["threshold_met"] is False
    assert acceptance["cases"][cases[2].case_id]["threshold_met"] is True
    assert acceptance["threshold_met"] is False
    # 기존 필드는 그대로 전체 계획 분모로 판정한다.
    assert not result["quality_pass"] and not result["baseline_candidate"]
    assert result["repeat_requirement_met"]


def test_case_acceptance_threshold_and_baseline_eligibility_are_independent():
    cases = load_cases()
    run = _ten_repeat_real_run(cases, {cases[0].case_id: (10,)})
    result = runner.report(run, cases, load_answers(cases), _review(run))
    # 산술 기준은 충족하지만 기록이 모자라 완전 관측이 아니므로 기준선 후보가 아니다.
    assert result["case_acceptance"]["threshold_met"] is True
    assert result["case_acceptance"]["measurement"]["fully_observed"] is False
    assert not result["case_acceptance"]["baseline_candidate"]

    run = _ten_repeat_real_run(cases)
    run["state"] = "RUNNING"
    result = runner.report(run, cases, load_answers(cases), _review(run))
    assert result["case_acceptance"]["threshold_met"] is True
    assert result["case_acceptance"]["measurement"]["fully_observed"] is False
    assert not result["case_acceptance"]["baseline_candidate"]

    run["state"] = "COMPLETE"
    # 검토 누락은 PASS를 줄이지만 기록 누락이 아니므로 관측 완전성은 유지된다.
    unreviewed = runner.report(run, cases, load_answers(cases))
    assert unreviewed["case_acceptance"]["measurement"]["fully_observed"] is True
    assert unreviewed["case_acceptance"]["threshold_met"] is False
    full = runner.report(run, cases, load_answers(cases), _review(run))
    assert full["case_acceptance"]["threshold_met"] is True
    assert full["case_acceptance"]["baseline_candidate"] and full["baseline_candidate"]


def test_case_acceptance_threshold_is_unknown_outside_the_fixed_set_times_ten_plan():
    cases = load_cases()
    run = _run(cases[:1], 10)
    run["records"] = [runner.run_one(cases[0], n, _client(cases[0])) for n in range(1, 11)]
    partial_set = runner.report(run, cases[:1], load_answers(cases), _review(run))["case_acceptance"]
    assert partial_set["measurement"]["planned_fixed_set_x10"] is False
    # 빠진 사례를 이름으로 드러내며, 일부 세트가 전체 기준을 충족한 것처럼 보이지 않는다.
    assert partial_set["measurement"]["missing_case_ids"] == [case.case_id for case in cases[1:]]
    assert partial_set["threshold_met"] is False and partial_set["cases"][cases[0].case_id]["threshold_met"] is None
    assert not partial_set["baseline_candidate"]
    run = _run(cases, 3)
    run["records"] = []
    measurement = runner.report(run, cases, load_answers(cases))["case_acceptance"]["measurement"]
    assert measurement["planned_fixed_set_x10"] is False and measurement["missing_case_ids"] == []


def test_dropping_the_last_fixed_case_from_a_ten_repeat_run_is_not_accepted():
    """이전 9건 세트 결과처럼 C10이 빠진 계획은 8/10 기준을 충족해도 수용되지 않는다."""
    cases = load_cases()
    run = _run(cases[:-1], 10)
    run["records"] = [runner.run_one(case, n, _client(case)) for n in range(1, 11) for case in cases[:-1]]
    run["state"] = "COMPLETE"
    for record in run["records"]:
        record["data_source"] = "real_model"
        record["model_snapshots"] = ["snapshot-1"]
    result = runner.report(run, cases[:-1], load_answers(cases), _review(run))["case_acceptance"]
    assert result["measurement"]["missing_case_ids"] == ["C10"]
    assert result["threshold_met"] is False and not result["baseline_candidate"]


def test_fake_measurements_never_establish_a_real_baseline():
    cases = load_cases()
    run = _run(cases, 3)
    run["records"] = [runner.run_one(case, n, _client(case))
                      for n in range(1, 4) for case in cases]
    run["state"] = "COMPLETE"
    result = runner.report(run, cases, load_answers(cases), _review(run))
    assert result["quality_pass"]  # 합성 판정값의 집계가 맞다는 뜻으로 한정한다.
    assert result["counts"]["PASS"] == 30  # AI가 낸 번호 대신 오프라인 서버 값 대역을 사용한다.
    assert not result["real_model_measurement"] and not result["baseline_candidate"]


@pytest.mark.parametrize("variant,eligible", [
    (SecOpsGraphVariant.EVIDENCE_FIRST, True), (SecOpsGraphVariant.CANDIDATE_FIRST, False),
])
def test_only_evidence_first_runs_can_be_current_service_or_baseline_candidate(variant, eligible):
    cases = load_cases()
    settings = Settings(_env_file=None, DATABASE_URL="unused", **{
        name: Settings.model_fields[name].default for name in runner.SETTING_NAMES
    })
    run = runner.new_run(cases, 3, settings, "fixture-run", variant)
    run["records"] = [runner.run_one(case, n, _client(case)) for n in range(1, 4) for case in cases]
    for record in run["records"]:
        record["data_source"] = "real_model"
        record["model_snapshots"] = ["snapshot-1"]
    run["state"] = "COMPLETE"
    result = runner.report(run, cases, load_answers(cases), _review(run))
    assert result["graph_variant"] == variant.value
    assert result["counts"]["PASS"] == 30
    assert result["current_service"] is eligible
    assert result["baseline_candidate"] is eligible
    # 변종 기록이 없는 과거 결과는 기록된 프롬프트 지문이 현재 기본 지문과 같을 때만 인정한다.
    del run["graph_variant"]
    assert runner.report(run, cases, load_answers(cases), _review(run))["current_service"] is eligible


@pytest.mark.parametrize("argv,calls", [
    ([], None), (["--check-inputs"], None),
    # 10건 × 1회 × 호출 수. 근거 선행은 1회, 대비책은 2회다.
    (["--estimate"], 10), (["--estimate", "--variant", "EVIDENCE_FIRST"], 10),
    (["--estimate", "--variant", "CANDIDATE_FIRST"], 20),
])
def test_offline_cli_never_builds_a_paid_client(monkeypatch, capsys, cli_source_cases, argv, calls):
    def forbidden(*args, **kwargs):
        raise AssertionError("paid client must not be created")
    monkeypatch.setattr(cli, "build_openai_model_client", forbidden)
    assert cli.main(argv) == 0
    payload = json.loads(capsys.readouterr().out)
    if calls is not None:
        assert payload["model_calls_upper_bound"] == calls
        assert payload["http_attempts_upper_bound"] == calls * 3
    assert "OPENAI_API_KEY" not in json.dumps(payload)


def _flip_report(proposals):
    case = load_cases()[0]
    run = _run([case], len(proposals))
    run["records"] = [runner.run_one(case, n, _client(case, proposal=value))
                      for n, value in enumerate(proposals, 1)]
    run["state"] = "COMPLETE"
    return runner.report(run, [case], load_answers(load_cases()))


def test_decision_flip_counts_candidate_to_no_proposal_as_a_flip_separately_from_structured_pass():
    result = _flip_report([True, False, True])
    flip = result["decision_flip"]
    assert flip["cases_measured"] == 1 and flip["cases_with_flip"] == 1
    assert flip["cases"]["C01"]["flip_ratio"] == pytest.approx(1 / 3, abs=1e-4)
    assert flip["cases"]["C01"]["decisions"] == [
        "RUNBOOK_NACL_ADD_DENY", "NO_PROPOSAL", "RUNBOOK_NACL_ADD_DENY",
    ]
    # EITHER 허용이라 구조화 판정에는 뒤집힘이 드러나지 않는다.
    assert all(item["structured"]["verdict"] == "PASS" for item in result["scores"])


@pytest.mark.parametrize("proposal", [True, False])
def test_decision_flip_is_zero_when_decisions_agree(proposal):
    flip = _flip_report([proposal] * 3)["decision_flip"]
    assert flip["cases"]["C01"]["flip_ratio"] == 0
    assert flip["cases_with_flip"] == 0 and flip["cases_measured"] == 1


def test_decision_flip_needs_two_error_free_outputs():
    flip = _flip_report([True])["decision_flip"]
    assert flip["cases"] == {} and flip["cases_with_flip"] == 0 and flip["cases_measured"] == 0
    assert flip["unmeasured"] == {"C01": {"recorded": 1, "decided": 1, "excluded_failed": 0}}


class _TimeoutClient:
    def complete(self, request, response_model):
        raise AIModelTimeoutError("test timeout")


def test_decision_flip_excludes_failed_outputs_instead_of_treating_them_as_no_proposal():
    case = load_cases()[0]
    failed = AgentGraphOutput.model_validate(runner.run_one(case, 1, _TimeoutClient())["output"])
    proposed = AgentGraphOutput.model_validate(runner.run_one(case, 1, _client(case, proposal=True))["output"])
    absent = AgentGraphOutput.model_validate(runner.run_one(case, 1, _client(case, proposal=False))["output"])
    assert failed.invocation_status.value == "FAILED" and not failed.candidates
    # 후보 하나와 실패 하나는 결정이 하나뿐이라 흔들림을 측정할 수 없다. 실패를 무제안으로 세면 1/2이 된다.
    only_one = runner.decision_flip({"C01": [proposed, failed]})
    assert only_one["cases"] == {} and only_one["cases_measured"] == 0 and only_one["cases_with_flip"] == 0
    assert only_one["excluded_failed"] == 1
    assert only_one["unmeasured"]["C01"] == {"recorded": 2, "decided": 1, "excluded_failed": 1}
    # 실제 무제안은 그대로 결정이고, 실패는 분모와 결정 목록에서 빠지며 제외 건수만 남는다.
    mixed = runner.decision_flip({"C01": [proposed, failed, absent, failed]})
    assert mixed["cases"]["C01"]["decisions"] == ["RUNBOOK_NACL_ADD_DENY", "NO_PROPOSAL"]
    assert mixed["cases"]["C01"]["flip_ratio"] == pytest.approx(0.5)
    assert mixed["cases"]["C01"]["recorded"] == 4 and mixed["cases"]["C01"]["decided"] == 2
    assert mixed["cases"]["C01"]["excluded_failed"] == 2 and mixed["excluded_failed"] == 2
    agreeing = runner.decision_flip({"C01": [absent, failed, absent]})
    assert agreeing["cases"]["C01"]["flip_ratio"] == 0 and agreeing["cases_with_flip"] == 0


def test_run_records_expose_per_call_usage_attempts_and_unknown_usage():
    case = load_cases()[0]
    ok = runner.run_one(case, 1, _client(case))
    assert [item["outcome"] for item in ok["call_records"]] == ["ok"] and ok["unknown_usage_calls"] == 0
    assert ok["call_records"][0]["usage_known"] is True
    # 대역은 trace가 없어 물리 시도 수를 모른다 — 0이 아니라 None이다.
    assert ok["physical_attempts"] is None
    timeout = runner.run_one(case, 1, _TimeoutClient())
    assert timeout["call_records"][0]["usage_known"] is False
    assert timeout["call_records"][0]["prompt_tokens"] is None and timeout["unknown_usage_calls"] == 1


def test_report_call_accounting_keeps_unknown_values_unknown_for_legacy_records():
    case = load_cases()[0]
    run = _run([case], 2)
    run["records"] = [runner.run_one(case, 1, _client(case)), runner.run_one(case, 2, _TimeoutClient())]
    run["state"] = "COMPLETE"
    accounting = runner.report(run, [case], load_answers(load_cases()))["call_accounting"]
    assert accounting == {"logical_calls": 2, "unknown_usage_calls": 1, "physical_attempts": None}
    for record in run["records"]:
        del record["unknown_usage_calls"]
    assert runner.report(run, [case], load_answers(load_cases()))["call_accounting"]["unknown_usage_calls"] is None


def test_report_exposes_failed_runs_in_decision_flip_coverage():
    case = load_cases()[0]
    run = _run([case], 3)
    run["records"] = [
        runner.run_one(case, 1, _client(case, proposal=True)),
        runner.run_one(case, 2, _TimeoutClient()),
        runner.run_one(case, 3, _client(case, proposal=True)),
    ]
    run["state"] = "COMPLETE"
    result = runner.report(run, [case], load_answers(load_cases()))
    flip = result["decision_flip"]
    assert result["counts"]["TRANSPORT_ERROR"] == 1
    assert flip["cases"]["C01"]["flip_ratio"] == 0 and flip["cases"]["C01"]["decided"] == 2
    assert flip["excluded_failed"] == 1


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
    assert result["counts"]["PASS"] == 30
    assert result["current_service"] and not result["source_files_match"]
    assert not result["real_model_measurement"] and not result["baseline_candidate"]


def _assembled(case_id):
    return next(case for case in load_cases() if case.case_id == case_id).graph_input


def test_prior_execution_cases_reproduce_the_recorded_isolation_through_the_assembler():
    ec2 = _assembled("C01").asset_context.arn
    assert _assembled("C01").isolation_execution is None
    success = _assembled("C08").isolation_execution
    assert success.status.value == "SUCCESS" and success.affected_arns == [ec2]
    assert success.runbook_id.value == "RUNBOOK_EC2_ISOLATE" and success.result_summary is None
    failed = _assembled("C09").isolation_execution
    assert failed.status.value == "FAILED" and failed.affected_arns == []
    assert failed.result_summary and "권한 거부" in failed.result_summary


def test_non_either_policy_still_rejects_a_disallowed_status():
    cases = load_cases()
    answer = load_answers(cases)["C08"].model_copy(update={
        "action_policy": "NACL_PROPOSAL", "allowed_statuses": ["SUCCEEDED"],
    })
    record = runner.run_one(cases[7], 1, _client(cases[7], proposal=False))
    output = AgentGraphOutput.model_validate(record["output"])
    assert output.invocation_status.value == "NO_PROPOSAL"
    assert score_structured(output, answer).verdict == "FAIL"
