"""되읽기 진단·LLM 판정자 요청·채점 테스트 (Issue #243).

모델 호출 0회다. 여기서 보는 것은 **판정 도구 자체가 제 일을 하는가**다 — 되읽은 토큰을
줄별로 지목하는지, 복원 요청이 입력을 새지 않는지, 복원 결과가 골든과 값으로 대조되는지.
"""

import json
from pathlib import Path

import pytest

from ai.agent import _incident_payload
from ai.evaluation import (
    CaseRun,
    DefectJudgement,
    FactCheckResult,
    RestorationOutput,
    build_column_report,
    check_readback,
    cites_input,
    defect_request,
    expected_deciding_values,
    fact_sheet,
    finops_cases,
    judge_fingerprint,
    observation_cites_input,
    proposal_cards,
    restoration_request,
    score_restoration,
)
from ai.model_client import FakeAIModelClient
from schemas.agents import AgentGraphOutput
from schemas.assets import AssetInventory
from schemas.incidents import AgentInvocationStatus

GOLDEN = Path(__file__).resolve().parents[4] / "datasets" / "golden" / "finops"
EC2_ARN = "arn:aws:ec2:ap-northeast-2:123456789012:instance/i-0a1b2c3d4e5f00001"


@pytest.fixture(scope="module")
def payload():
    """A1 케이스(t3.xlarge · cpu_avg 4.9 · cpu_max 10.0 · 336 datapoints)의 요약 페이로드."""
    inventory = AssetInventory.model_validate(
        json.loads((GOLDEN / "input" / "asset_inventory_001.json").read_text("utf-8"))
    )
    expected = json.loads((GOLDEN / "expected" / "asset_inventory_001.json").read_text("utf-8"))
    return _incident_payload(finops_cases(inventory, expected)[0].graph_input)


def _output(lines, target_type="t3.medium"):
    return AgentGraphOutput.model_validate(
        {
            "invocation_status": "SUCCEEDED",
            "summary_lines": list(lines),
            "candidates": [
                {
                    "runbook_id": "RUNBOOK_EC2_RIGHTSIZING",
                    "target_arn": EC2_ARN,
                    "parameters": {"target_instance_type": target_type},
                    "evidence_ids": ["ev-a1-rule"],
                }
            ],
        }
    )


def _restored(**slots):
    base = {
        "verdict": "COST_CANDIDATE",
        "skip_reason_code": None,
        "cpu_avg": None,
        "cpu_max": None,
        "cpu_datapoints": None,
        "other_values": [],
    }
    base.update(slots)
    return RestorationOutput.model_validate(base)


# ------------------------------------------------------------------------------
# 되읽기 — 결함 5·1의 어휘 근사
# ------------------------------------------------------------------------------


def test_readback_names_the_structured_values_per_line(payload):
    output = _output(
        [
            "EC2(t3.xlarge)가 평가 완료되었고 health_score는 5, verdict는 COST_CANDIDATE입니다.",
            "14일 CPU 평균 4.9%, 최대 10.0%로 사용률이 낮습니다.",
            "t3.medium으로 다운사이징을 권장합니다.",
        ]
    )

    result = check_readback(payload, output)

    assert result.hits[0] == ("COST_CANDIDATE", "health_score", "verdict")
    assert result.hits[1] == ()
    # 카드의 표시 파라미터 값을 문장이 되풀이하면 되읽기다
    assert result.hits[2] == ("t3.medium",)
    assert result.lines_with_readback == 2


def test_readback_ignores_values_that_are_not_structured_fields(payload):
    # 현재 타입·수치는 카드에 없는 인용이라 되읽기가 아니다
    output = _output(["t3.xlarge의 14일 평균 CPU가 4.9%다.", "과대 스펙으로 보인다.", "축소 근거다."])

    assert not check_readback(payload, output).any


def test_readback_matches_whole_tokens_only(payload):
    # 표시 파라미터가 짧은 숫자여도 다른 수 안에서 걸리지 않는다 — "2026"·"1.5%" 모두
    output = AgentGraphOutput.model_validate(
        {
            "invocation_status": "SUCCEEDED",
            "summary_lines": ["2026년 관측이다.", "CPU 평균 1.5%, 최대 2.0%다.", "min 1 · max 2로 전환한다."],
            "candidates": [
                {
                    "runbook_id": "RUNBOOK_EC2_ENABLE_AUTOSCALING",
                    "target_arn": EC2_ARN,
                    "parameters": {"min_size": 1, "max_size": 2},
                    "evidence_ids": ["ev-a1-rule"],
                }
            ],
        }
    )

    result = check_readback(payload, output)

    assert result.hits[0] == () and result.hits[1] == ()
    # 값을 낱개로 되읽은 3줄만 걸린다
    assert result.hits[2] == ("1", "2")


def test_observation_citation_uses_input_values(payload):
    assert cites_input(payload, "14일 평균 CPU 4.9%로 관측됐다.")
    assert cites_input(payload, "t3.xlarge 인스턴스다.")
    assert not cites_input(payload, "사용률이 낮은 인스턴스다.")
    assert observation_cites_input(payload, ["평균 CPU 4.9%", "x", "y"])
    assert not observation_cites_input(payload, [])


# ------------------------------------------------------------------------------
# 복원 대조 — 화면 조합만 보여 주고 골든과 값으로 대조한다
# ------------------------------------------------------------------------------


def test_restoration_request_carries_only_the_screen_combination(payload):
    output = _output(["a", "b", "c"])
    request = restoration_request(output.summary_lines, proposal_cards(output.candidates))

    assert set(request.user_payload) == {
        "summary_lines",
        "proposal_cards",
        "verdict_options",
        "skip_reason_options",
    }
    assert request.user_payload["proposal_cards"] == [
        {
            "runbook_id": "RUNBOOK_EC2_RIGHTSIZING",
            "target_arn": EC2_ARN,
            "display_parameters": {"target_instance_type": "t3.medium"},
        }
    ]
    assert "COST_CANDIDATE" in request.user_payload["verdict_options"]


def test_expected_deciding_values_come_from_the_metric_evidence(payload):
    assert expected_deciding_values(payload) == {
        "cpu_avg": "4.9",
        "cpu_max": "10",
        "cpu_datapoints": "336",
    }


def test_restoration_is_scored_per_slot_with_normalized_numbers(payload):
    score = score_restoration(_restored(cpu_avg="4.9%", cpu_max="10.0%"), payload)

    assert score.verdict_ok and score.skip_reason_ok
    assert score.restored == ("cpu_avg", "cpu_max")
    assert score.missing == ("cpu_datapoints",)
    assert score.unexpected == ()
    assert score.expected == 3


def test_restoration_catches_misattributed_numbers(payload):
    # "평균 CPU 5%"는 health_score 5를 평균에 잘못 붙인 것이다 — 값 집합 대조(factcheck)는
    # 통과시키지만 슬롯 대조는 cpu_avg 자리에 4.9가 없으므로 미복원으로 센다(#243 §한계)
    score = score_restoration(_restored(cpu_avg="5%", cpu_datapoints="336개"), payload)

    assert score.restored == ("cpu_datapoints",)
    assert "cpu_avg" in score.missing


def test_restoration_scores_wrong_verdict_and_invented_skip_reason(payload):
    score = score_restoration(_restored(verdict="SKIP", skip_reason_code="SKIP_LOW_UTIL"), payload)

    assert not score.verdict_ok and not score.skip_reason_ok
    assert score.restored == () and len(score.missing) == 3


def test_restoration_reports_slots_filled_beyond_golden(payload):
    # A1 골든에는 셋 다 있으므로 unexpected는 비고, 골든에 없는 슬롯을 채우면 거기 남는다
    a7_like = dict(payload)
    a7_like["evidences"] = [
        dict(e, content=dict(e["content"], summary=dict(e["content"]["summary"], cpu_max=None)))
        if e.get("evidence_type") == "METRIC"
        else e
        for e in payload["evidences"]
    ]

    score = score_restoration(_restored(cpu_avg="4.9%", cpu_max="10%"), a7_like)

    assert score.restored == ("cpu_avg",)
    assert score.unexpected == ("cpu_max",)


# ------------------------------------------------------------------------------
# 결함 판정 — 압축 사실표
# ------------------------------------------------------------------------------


def test_fact_sheet_is_a_compact_view_of_the_payload(payload):
    sheet = fact_sheet(payload)

    assert sheet["instance_type"] == "t3.xlarge"
    assert sheet["verdict"] == "COST_CANDIDATE"
    assert sheet["metric"]["summary"]["cpu_avg"] == 4.9
    # 요약이 "평가 완료"·"규칙 평가로 판정"이라 쓰는 사실도 확정값이다 — 빠지면 결함 2 오탐
    assert sheet["evaluation_status"] == "COMPLETED"
    # 식별자·가용 영역도 확정값이다 — 빠지면 요약의 정확한 인용이 결함 2로 오탐된다
    assert sheet["resource_id"] == "i-0a1b2c3d4e5f00001"
    assert sheet["availability_zone"] == "ap-northeast-2a"
    assert sheet["rule_evaluation"]["verdict"] == "COST_CANDIDATE"
    assert "rule evaluation" in sheet["rule_evaluation"]["reason"]
    assert "evidences" not in sheet


def test_defect_request_carries_sheet_lines_and_cards(payload):
    output = _output(["a", "b", "c"])
    request = defect_request(payload, output.summary_lines, proposal_cards(output.candidates))

    assert set(request.user_payload) == {"fact_sheet", "summary_lines", "proposal_cards"}


def test_judge_requests_pass_the_model_boundary(payload):
    # 요청 페이로드가 경계의 직렬화·마스킹을 지나는지 — 지나지 못하면 실행에서야 드러난다
    restoration = _restored(verdict=None)
    defects = DefectJudgement(
        defect_2={"flagged": False, "reason": ""},
        defect_3={"flagged": True, "reason": "3줄이 카드에 없는 중지를 권함"},
        defect_4={"flagged": False, "reason": ""},
    )
    client = FakeAIModelClient([restoration, defects])
    output = _output(["a", "b", "c"])
    cards = proposal_cards(output.candidates)

    assert client.complete(restoration_request(output.summary_lines, cards), RestorationOutput).output == restoration
    assert client.complete(defect_request(payload, output.summary_lines, cards), DefectJudgement).output == defects
    assert len(client.sent) == 2


def test_judge_fingerprint_is_stable():
    assert judge_fingerprint() == judge_fingerprint()
    assert len(judge_fingerprint()) == 64


# ------------------------------------------------------------------------------
# 일치율 분모 — 유효 출력 2회 미만 케이스는 뺀다 (#243 §선행 계측의 한계)
# ------------------------------------------------------------------------------


def _case_run(case_id, output, error_phase=None):
    return CaseRun(
        case_id=case_id,
        output=output,
        prompt_tokens=1,
        completion_tokens=1,
        fact=FactCheckResult(),
        error="AIModelTransportError" if error_phase else None,
        error_phase=error_phase,
    )


def test_stability_counts_only_cases_with_two_or_more_valid_outputs():
    good = _output(["a", "b", "c"])
    failed = AgentGraphOutput(invocation_status=AgentInvocationStatus.FAILED)
    report = build_column_report(
        "x",
        [
            _case_run("A1", good),
            _case_run("A1", failed, error_phase="transport"),
            _case_run("A7", good),
            _case_run("A7", good),
        ],
    )

    # A1은 유효 출력이 1건이라 흔들릴 자리가 없다 — 전에는 자동 "전회 일치"로 세어졌다
    assert report.stability_cases == 1
    assert report.field_stability == 1.0
    assert report.failed_transport == 1
