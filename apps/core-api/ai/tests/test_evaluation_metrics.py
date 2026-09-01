"""사실 정합성·재현성·집계 테스트 (Issue #237).

모델 호출 0회다. 여기서 보는 것은 **계측 도구 자체가 제 일을 하는가**다 —
지어낸 값을 실제로 잡는지, 흔들린 필드를 이름으로 지목하는지, 빈 후보를 세는지.
"""

import json
from pathlib import Path

import pytest
from ai.agent import _incident_payload
from ai.evaluation import (
    CaseRun,
    build_column_report,
    check_summary_facts,
    field_agreement,
    finops_cases,
    unstable_fields,
)
from ai.evaluation.reproducibility import MISSING
from schemas.agents import AgentGraphOutput
from schemas.assets import AssetInventory
from schemas.incidents import AgentInvocationStatus

GOLDEN = Path(__file__).resolve().parents[4] / "datasets" / "golden" / "finops"

EC2_ARN = "arn:aws:ec2:ap-northeast-2:123456789012:instance/i-0a1b2c3d4e5f00001"


@pytest.fixture(scope="module")
def payload():
    """A1 케이스가 요약 노드로 실제로 내보내는 값."""
    inventory = AssetInventory.model_validate(
        json.loads((GOLDEN / "input" / "asset_inventory_001.json").read_text("utf-8"))
    )
    expected = json.loads((GOLDEN / "expected" / "asset_inventory_001.json").read_text("utf-8"))
    return _incident_payload(finops_cases(inventory, expected)[0].graph_input)


def _output(status=AgentInvocationStatus.SUCCEEDED, target_type="t3.large", arn=EC2_ARN):
    if status is AgentInvocationStatus.FAILED:
        return AgentGraphOutput(invocation_status=status)
    candidates = (
        []
        if status is AgentInvocationStatus.NO_PROPOSAL
        else [
            {
                "runbook_id": "RUNBOOK_EC2_RIGHTSIZING",
                "target_arn": arn,
                "parameters": {"target_instance_type": target_type},
                "evidence_ids": ["ev-a1-rule"],
            }
        ]
    )
    return AgentGraphOutput.model_validate(
        {
            "invocation_status": status.value,
            "summary_lines": ["상황", "분석", "권고"],
            "candidates": candidates,
        }
    )


# --- 사실 정합성 -----------------------------------------------------------------


def test_quoting_only_input_values_passes(payload):
    lines = [
        f"인스턴스 {EC2_ARN}의 14일 관측에서 평균 CPU가 4.9%다.",
        "규칙 판정은 COST_CANDIDATE이고 health_score는 5다.",
        "관측치는 336건이며 최대 CPU는 10%다.",
    ]
    # "14"는 입력에 문자로 없다 — window_start·window_end의 차이로 나온 값이다.
    # 실패가 아니라 파생으로 세야 한다(#237 실측: 이것을 실패로 세면 관측 창을
    # 아예 언급하지 않는 모델이 만점을 받는다)
    result = check_summary_facts(payload, lines)

    assert result.identifier_violations == ()
    assert result.number_violations == ()
    assert result.derived_numbers == ("14",)
    assert result.passed


def test_invented_instance_id_fails(payload):
    lines = [
        "인스턴스 i-0fff888877776666e의 평균 CPU가 4.9%다.",
        "규칙 판정은 COST_CANDIDATE다.",
        "다운사이징을 권고한다.",
    ]

    result = check_summary_facts(payload, lines)

    assert result.identifier_violations == ("i-0fff888877776666e",)
    assert not result.passed


def test_invented_number_fails(payload):
    lines = ["평균 CPU가 87.5%다.", "판정은 COST_CANDIDATE다.", "다운사이징을 권고한다."]

    result = check_summary_facts(payload, lines)

    assert result.number_violations == ("87.5",)
    assert not result.passed


def test_downsizing_target_type_is_reported_but_does_not_fail(payload):
    # 다운사이징 대상 타입은 정의상 입력에 없다 — 실패로 세면 정상 응답이 전부 FAIL이 된다
    lines = ["t3.xlarge 인스턴스다.", "평균 CPU가 4.9%다.", "t3.medium으로 줄일 것을 권고한다."]

    result = check_summary_facts(payload, lines)

    assert result.instance_types_outside_input == ("t3.medium",)
    assert result.passed


def test_digits_inside_identifiers_do_not_become_numbers(payload):
    # t3.xlarge의 3, i-0a1b...의 0이 숫자로 새면 입력에 없는 수치가 늘 통과한다
    result = check_summary_facts(payload, [f"{EC2_ARN}는 t3.xlarge다.", "분석", "권고"])

    assert result.number_violations == ()


def test_number_forms_are_normalized(payload):
    # 입력의 cpu_max는 10.0이다 — 요약이 "10%"로 써도 같은 값이다
    result = check_summary_facts(payload, ["최대 CPU 10%다.", "평균 4.90%다.", "권고"])

    assert result.number_violations == ()


def test_iso_timestamp_components_are_quotable(payload):
    # 입력 window는 2026-08-05~19다. 날짜는 숫자로 쪼개지 않고 날짜 갈래끼리 대조한다 —
    # 부분 표기("08-19")도 입력 날짜의 일부면 인용이다(실측: luna·terra·5.4-nano가 씀)
    result = check_summary_facts(
        payload, ["2026-08-05~08-19 측정이다.", "평균 CPU 4.9%다.", "권고"]
    )

    assert result.number_violations == ()


def test_date_fragments_do_not_justify_invented_measurements(payload):
    # A1의 cpu_avg는 4.9다 — "8"은 입력의 2026-08-…에서만 나온다. 타임스탬프를 숫자로
    # 쪼개 허용 집합에 넣으면 이런 지어낸 측정값이 통과한다(PR #248 리뷰 재현)
    result = check_summary_facts(payload, ["평균 CPU는 8%다.", "분석", "권고"])

    assert result.number_violations == ("8",)
    assert not result.passed


def test_full_timestamp_and_year_citations_pass(payload):
    result = check_summary_facts(
        payload, ["수집 2026-08-19T06:00:00Z 기준이다.", "2026년 관측이다.", "권고"]
    )

    assert result.number_violations == ()


def test_invented_date_fails(payload):
    # 입력 창(08-05~08-19) 밖의 날짜는 수치 주장이다 — 숫자 위반으로 보고한다
    result = check_summary_facts(payload, ["수집 2026-07-19 기준이다.", "분석", "권고"])

    assert result.number_violations == ("2026-07-19",)


def test_week_conversion_of_the_window_is_derived_not_invented():
    # 두 시각이 14일 차이면 "2주"도 계산해 낸 값이다 — 일 표기만 인정하면 같은 기간을
    # 주로 쓰는 요약이 문체 때문에 위반이 된다. 합성 페이로드를 쓰는 것은 골든 A1에는
    # 리전 문자열(ap-northeast-2)에서 나온 낱개 '2'가 이미 허용돼 있어 이 경로가
    # 구분되지 않기 때문이다
    window = {"window_start": "2026-08-05T06:00:00Z", "window_end": "2026-08-19T06:00:00Z"}

    ok = check_summary_facts(window, ["약 2주 관측이다.", "분석", "권고"])
    bad = check_summary_facts(window, ["약 3주 관측이다.", "분석", "권고"])

    assert ok.derived_numbers == ("2",)
    assert ok.passed
    assert bad.number_violations == ("3",)
    assert not bad.passed


def test_thousands_separator_is_not_split(payload):
    # 입력 network_in_avg는 1024다 — 요약이 "1,024"로 써도 같은 값이다.
    # 쪼개지면 1과 024(→24)가 지어낸 수치로 잡힌다
    result = check_summary_facts(payload, ["네트워크 입력 평균 1,024바이트다.", "분석", "권고"])

    assert result.number_violations == ()


def test_short_resource_id_inside_an_input_arn_is_quotable(payload):
    # 입력은 보안그룹을 ARN 통째로 담는다. 요약이 짧은 ID로 쓰는 것은 정확한 인용이다 —
    # ARN 패턴이 먼저 먹어 짧은 ID가 허용 집합에서 빠지면 그 인용이 위반이 된다
    sg_arn = next(
        item["target_arn"]
        for item in payload["asset"]["relationships"]
        if item["relation_type"] == "SECURED_BY"
    )
    sg_id = sg_arn.rsplit("/", 1)[1]

    result = check_summary_facts(payload, [f"보안그룹 {sg_id}에 연결돼 있다.", "분석", "권고"])

    assert result.identifier_violations == ()


def test_enumeration_markers_are_not_data(payload):
    # `(1)`·`(2)`는 글의 구조지 인용한 수치가 아니다
    result = check_summary_facts(payload, ["상황", "근거는 (1) 규칙 판정과 (2) 지표다.", "권고"])

    assert result.number_violations == ()


def test_invented_number_is_not_excused_as_derived(payload):
    # 파생 허용은 두 시각의 차이(일)뿐이다. 그 집합에 없는 수치는 그대로 실패다 —
    # 넓히면 검사기가 잡을 수 있는 환각이 줄어든다
    result = check_summary_facts(payload, ["평균 CPU가 87.5%다.", "분석", "권고"])

    assert result.number_violations == ("87.5",)
    assert result.derived_numbers == ()
    assert not result.passed


def test_list_commas_still_separate_numbers(payload):
    # 천 단위 쉼표만 지운다 — 나열 쉼표까지 지우면 87.5,99가 8,7599로 붙어 위반을 놓친다
    result = check_summary_facts(payload, ["값은 87.5, 99다.", "분석", "권고"])

    assert result.number_violations == ("87.5", "99")


# --- 재현성 ----------------------------------------------------------------------


def test_identical_outputs_have_no_unstable_field():
    assert unstable_fields([_output(), _output(), _output()]) == []


def test_the_shaky_field_is_named_with_its_values():
    shaky = unstable_fields(
        [_output(target_type="t3.large"), _output(target_type="t3.small"), _output(target_type="t3.medium")]
    )

    assert [item.field for item in shaky] == ["RUNBOOK_EC2_RIGHTSIZING.target_instance_type"]
    assert shaky[0].values == ("t3.large", "t3.small", "t3.medium")
    assert shaky[0].distinct == 3
    assert shaky[0].top_share == pytest.approx(1 / 3)


def test_a_missing_candidate_counts_as_variation():
    # 어느 회차에만 후보가 있었다면 그것도 변동이다 — 빈 자리를 비워 두면 "전회 일치"가 된다
    shaky = {item.field: item.values for item in unstable_fields([_output(), _output(AgentInvocationStatus.NO_PROPOSAL)])}

    assert shaky["RUNBOOK_EC2_RIGHTSIZING.target_arn"] == (EC2_ARN, MISSING)
    assert shaky["candidate_count"] == ("1", "0")


def test_stable_fields_are_still_listed_by_field_agreement():
    agreements = {item.field: item for item in field_agreement([_output(), _output()])}

    assert agreements["invocation_status"].stable
    assert agreements["RUNBOOK_EC2_RIGHTSIZING.target_arn"].top_share == 1.0


# --- 집계 ------------------------------------------------------------------------


def _run(case_id, output, fact_ok=True, prompt=100, completion=20, error=None, error_phase=None):
    result = check_summary_facts({}, [] if fact_ok else ["i-0fff888877776666e"])
    return CaseRun(
        case_id=case_id,
        output=output,
        prompt_tokens=prompt,
        completion_tokens=completion,
        fact=result,
        error=error,
        error_phase=error_phase,
    )


def test_column_report_counts_statuses_and_tokens():
    runs = [
        _run("A1", _output()),
        _run("A1", _output(target_type="t3.small")),
        _run("A7", _output(AgentInvocationStatus.NO_PROPOSAL)),
        _run("A7", _output(AgentInvocationStatus.FAILED)),
    ]

    report = build_column_report("gpt-4o", runs)

    assert (report.succeeded, report.no_proposal, report.failed) == (2, 1, 1)
    assert report.case_count == 2 and report.repeats == 2
    assert (report.prompt_tokens, report.completion_tokens) == (400, 80)
    assert report.no_proposal_rate == pytest.approx(0.25)


def test_column_report_names_the_unstable_field():
    runs = [_run("A1", _output()), _run("A1", _output(target_type="t3.small"))]

    report = build_column_report("gpt-4o", runs)

    assert report.unstable_field_names == ["RUNBOOK_EC2_RIGHTSIZING.target_instance_type"]
    assert 0.0 < report.field_stability < 1.0


def test_column_report_counts_fact_failures():
    runs = [_run("A1", _output()), _run("A1", _output(), fact_ok=False)]

    report = build_column_report("gpt-4o", runs)

    assert (report.fact_failed, report.fact_checked) == (1, 2)


def test_failed_runs_are_split_by_phase():
    # 예외 클래스가 아니라 실패 위상으로 가른다 — 계약 위반 예외는 요청·응답 양쪽에
    # 쓰여서, 클래스로 갈랐다면 응답 계약 위반(모델 품질)이 호출 실패(인프라)로
    # 집계된다(PR #248 리뷰). 그래프 계약 검증 거절(예외 없음)은 응답 쪽과 같이 센다
    runs = [
        _run("A1", _output()),
        _run(
            "A1",
            _output(AgentInvocationStatus.FAILED),
            error="AIModelUnavailableError",
            error_phase="transport",
        ),
        _run(
            "A7",
            _output(AgentInvocationStatus.FAILED),
            error="AIModelRejectedError",
            error_phase="request",
        ),
        _run(
            "A7",
            _output(AgentInvocationStatus.FAILED),
            error="AIModelContractError",
            error_phase="response",
        ),
        _run("A11", _output(AgentInvocationStatus.FAILED)),
    ]

    report = build_column_report("gpt-4o", runs)

    assert (
        report.failed,
        report.failed_transport,
        report.failed_request,
        report.failed_contract,
    ) == (4, 1, 1, 2)


def test_failed_runs_do_not_pollute_field_agreement():
    # FAILED의 출력은 _failed_output()의 빈 자리표시자다 — 일치율 분모에 넣으면
    # API 장애가 모델 출력 변동으로 기록된다(PR #248 리뷰)
    runs = [
        _run("A1", _output()),
        _run("A1", _output()),
        _run(
            "A1",
            _output(AgentInvocationStatus.FAILED),
            error="AIModelUnavailableError",
            error_phase="transport",
        ),
    ]

    report = build_column_report("gpt-4o", runs)

    assert report.unstable_field_names == []
    assert report.field_stability == 1.0
    assert (report.failed, report.failed_transport) == (1, 1)


def test_failed_runs_are_not_counted_as_fact_passes():
    # FAILED는 요약이 비어 위반이 0건이다 — 통과로 세면 실패가 많을수록 지표가 좋아진다
    runs = [_run("A1", _output()), _run("A1", _output(AgentInvocationStatus.FAILED))]

    report = build_column_report("gpt-4o", runs)

    assert (report.fact_checked, report.fact_failed) == (1, 0)
