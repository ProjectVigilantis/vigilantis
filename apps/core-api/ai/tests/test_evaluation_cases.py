"""고정 입력 세트 변환기 테스트 (Issue #237).

모델 호출 0회다 — 여기서 보는 것은 골든 자산이 그래프 입력 계약으로 옮겨지는가와,
그 입력이 **몇 번을 만들어도 같은가**다. 후자가 깨지면 N회 반복 재현성 계측이
성립하지 않는다(매 회차가 서로 다른 입력을 넣은 것이 된다).
"""

import json
from pathlib import Path

import pytest
from ai.evaluation import EvalCase, finops_cases
from schemas.api.assets import AssetType, RelationType, Verdict
from schemas.assets import AssetInventory
from schemas.runbooks import RunbookId
from services.rule_engine import evaluate_ec2

GOLDEN = Path(__file__).resolve().parents[4] / "datasets" / "golden" / "finops"
# scripts/finops_eval.py와 같은 목록 — 004는 EBS 전용이라 케이스 0건, EC2가 든 파일이 늘면 양쪽에 추가한다
INVENTORY_IDS = ("001", "002", "003")

# 정답지 verdict가 COST_CANDIDATE인 자산 — 인벤토리 순서 그대로다
EXPECTED_CASE_IDS = ["A1", "A7", "A11", "A12", "A14", "A16"]


def _pair(inventory_id: str):
    inventory = AssetInventory.model_validate(
        json.loads((GOLDEN / "input" / f"asset_inventory_{inventory_id}.json").read_text("utf-8"))
    )
    expected = json.loads(
        (GOLDEN / "expected" / f"asset_inventory_{inventory_id}.json").read_text("utf-8")
    )
    return inventory, expected


def _all_cases() -> list[EvalCase]:
    cases: list[EvalCase] = []
    for inventory_id in INVENTORY_IDS:
        cases.extend(finops_cases(*_pair(inventory_id)))
    return cases


# --- 고정 세트의 범위 -------------------------------------------------------------


def test_only_cost_candidates_become_incidents():
    cases = _all_cases()

    assert [case.case_id for case in cases] == EXPECTED_CASE_IDS
    assert {case.verdict for case in cases} == {Verdict.COST_CANDIDATE}
    assert {case.skip_reason_code for case in cases} == {None}


def test_cases_carry_the_golden_purpose():
    # 계측 결과를 읽을 때 "이 케이스가 무엇을 강제하는가"가 함께 보여야 한다
    cases = {case.case_id: case for case in _all_cases()}

    assert cases["A11"].purpose.startswith("Environment=prod-us-east")


# --- 결정성 ----------------------------------------------------------------------


def test_same_inventory_produces_byte_identical_input():
    first = finops_cases(*_pair("001"))
    second = finops_cases(*_pair("001"))

    dumped = [
        [case.graph_input.model_dump_json() for case in batch] for batch in (first, second)
    ]
    assert dumped[0] == dumped[1]


def test_timestamps_come_from_the_inventory_not_the_clock():
    inventory, expected = _pair("001")
    case = finops_cases(inventory, expected)[0]

    assert case.graph_input.asset_context.collected_at == inventory.collected_at
    assert case.graph_input.rule_evaluation.evaluated_at == inventory.collected_at


# --- 프로덕션 값 재현 --------------------------------------------------------------


def test_reason_is_the_production_string_without_metric_numbers():
    case = finops_cases(*_pair("001"))[0]

    # services/rule_engine.py가 실제로 넣는 문자열. 수치가 여기 들어가면 모델이
    # 프로덕션에서는 받지 못할 숫자를 받게 된다
    assert case.graph_input.rule_evaluation.reason == "EC2 rule evaluation: verdict=COST_CANDIDATE"


def test_health_score_matches_the_rule_engine_calculation():
    inventory, expected = _pair("001")
    case = finops_cases(inventory, expected)[0]
    ec2 = inventory.ec2_instances[0]
    _, _, health = evaluate_ec2(
        ec2.metric_summary.cpu_avg,
        ec2.metric_summary.cpu_max,
        ec2.metric_summary.cpu_datapoints,
        ec2.tags,
    )

    assert case.graph_input.asset_context.health_score == int(round(health))
    assert case.graph_input.rule_evaluation.health_score == int(round(health))


def test_metric_numbers_reach_the_model_through_the_metric_evidence():
    inventory, expected = _pair("001")
    case = finops_cases(inventory, expected)[0]
    metric = [e for e in case.graph_input.evidences if e.evidence_type.value == "METRIC"][0]

    assert metric.content.summary.cpu_avg == inventory.ec2_instances[0].metric_summary.cpu_avg
    assert metric.content.window_end == inventory.collected_at


def test_security_group_relationship_uses_the_collector_arn_format():
    inventory, expected = _pair("001")
    case = finops_cases(inventory, expected)[0]
    relationship = case.graph_input.asset_context.relationships[0]

    # 가드레일 ③ ARN Match가 이 문자열을 그대로 비교한다
    assert relationship.relation_type is RelationType.SECURED_BY
    assert relationship.target_arn == (
        "arn:aws:ec2:ap-northeast-2:123456789012:security-group/sg-0a1b2c3d4e5f00005"
    )


# --- 모델에게 주는 메뉴 ------------------------------------------------------------


def test_capabilities_hold_only_finops_runbooks_with_a_real_target():
    for case in _all_cases():
        offered = [capability.runbook_id for capability in case.graph_input.capabilities]

        # 골든 케이스의 subject는 전부 COST_CANDIDATE EC2다. EBS_DELETE_UNATTACHED는
        # 대상 유형이 EBS라 빠지고, SECOPS 도메인 런북(격리·NACL)은 판정이 UNUSED가
        # 아니라 빠지며, 롤백 3종은 AI 추천 대상이 아니다 (ai/capabilities.py 축 ①②)
        assert offered == [
            RunbookId.RUNBOOK_EC2_RIGHTSIZING,
            RunbookId.RUNBOOK_EC2_ENABLE_AUTOSCALING,
        ]
        assert all(
            capability.allowed_target_asset_types == [AssetType.EC2]
            for capability in case.graph_input.capabilities
        )


# --- 세우는 경우 ------------------------------------------------------------------


def test_asset_missing_from_the_answer_key_raises():
    inventory, expected = _pair("001")
    expected["evaluations"] = [
        item for item in expected["evaluations"] if not item["asset_arn"].endswith("00001")
    ]

    with pytest.raises(ValueError, match="정답지에 없는 자산"):
        finops_cases(inventory, expected)


def test_answer_key_drifting_from_the_rule_engine_raises():
    # 정답지가 COST_CANDIDATE라 적었는데 판정 코드가 그렇게 보지 않으면, "낭비 후보라서
    # 골랐다"는 고정 세트의 전제가 사실이 아니게 된다
    inventory, expected = _pair("001")
    for item in expected["evaluations"]:
        if item["case_id"] == "A2":  # 실제 판정은 SKIP_ACTIVE
            item["verdict"] = Verdict.COST_CANDIDATE.value
            item["skip_reason_code"] = None

    with pytest.raises(ValueError, match="rule_engine"):
        finops_cases(inventory, expected)
