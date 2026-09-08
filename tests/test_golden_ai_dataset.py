# ==============================================================================
# [파일 설명]  담당: 박지현 (QA & Scenario)
# 골든 FinOps → AI 그래프 축의 회귀. 정답지는 datasets/golden/finops/expected_ai/ 다.
# (Issue #234 — 판정 기준 ⓒ "AI가 골든 FinOps에서 CoT 3줄 + Runbook 추천 산출")
#
# **왜 이 파일이 필요한가**: 그래프를 검증하는 자리가 종전에 둘이었는데 둘 다 합성
# 입력을 썼다 — apps/core-api/ai/tests/test_finops_graph.py(make_input 상수)와
# scripts/smoke_finops_graph.py(합성 인시던트 1건). 골든 FinOps 는 규칙 엔진 축에만
# 쓰였고, 같은 입력이 AI 그래프로 흐를 때의 정답지가 없었다.
#
# **모델을 부르지 않는다.** FakeAIModelClient 로만 돌린다 — 실제 호출은 과금이라 CI
# 인자에 넣지 않는다(#234 §실행 방식). 그래서 이 파일이 재는 것은 산출의 품질이
# 아니라 셋이다:
#   ① 골든 입력이 그래프 계약을 통과하는가 (지금까지 어느 자리도 확인하지 않았다)
#   ② 그래프의 불변식이 **골든 입력에서** 실제로 작동하는가 (음성 케이스)
#   ③ 정답 명세가 계약·변환기 산출과 어긋나지 않는가 (정답지가 낡으면 실패)
# 문장 품질 판정은 #237 의 결함 체크리스트·사실 정합성 검사 몫이다.
#
# **정답을 값이 아니라 명세로 적은 이유**는 정답지 파일의 design_note 에 있다.
# 여기서 복제하지 않는다 — 두 곳에 적히면 한쪽만 고쳐진다.
# ==============================================================================

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# import 경로 추가 (tests/test_golden_dataset.py 와 동일 관례)
ROOT = Path(__file__).resolve().parents[1]
for _path in (ROOT / "apps" / "core-api", ROOT / "packages"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from ai.agent import (  # noqa: E402
    CandidateProposalOutput,
    EvidenceSummaryOutput,
    ProposedCandidate,
    run_finops_graph,
)
from ai.evaluation.cases import finops_cases  # noqa: E402
from ai.model_client import FakeAIModelClient  # noqa: E402
from schemas.agents import AgentInvocationStatus  # noqa: E402
from schemas.assets import AssetInventory  # noqa: E402

GOLDEN = ROOT / "datasets" / "golden" / "finops"
INPUT = GOLDEN / "input"
EXPECTED = GOLDEN / "expected"
EXPECTED_AI = GOLDEN / "expected_ai"


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _cases_of(stem: str):
    """입력 1파일의 고정 세트(COST_CANDIDATE) 케이스. 변환기는 #237 것을 그대로 쓴다."""
    inventory = AssetInventory.model_validate_json(
        (INPUT / f"{stem}.json").read_text(encoding="utf-8")
    )
    return finops_cases(inventory, _load(EXPECTED / f"{stem}.json"))


def _expectation_pairs():
    """(stem, expectation) 목록. 파라미터화 id 로 case_id 를 쓴다."""
    pairs = []
    for path in sorted(EXPECTED_AI.glob("*.json")):
        for item in _load(path)["expectations"]:
            pairs.append(pytest.param(path.stem, item, id=item["case_id"]))
    return pairs


def _stub_client(case) -> FakeAIModelClient:
    """계약을 지키는 모델 응답을 그 케이스의 **입력에서** 만들어 준다.

    값을 손으로 적지 않는 것이 요점이다. 손으로 적으면 골든이 바뀔 때 스텁만 낡아
    그래프가 아니라 스텁을 검증하게 된다. 요약 3줄은 EvidenceSummaryOutput 이 필드
    3개로 받으므로 개수를 여기서 셀 필요가 없다(구조가 보장한다).
    """
    graph_input = case.graph_input
    runbook_id = sorted(c.runbook_id.value for c in graph_input.capabilities)[0]
    return FakeAIModelClient(
        [
            EvidenceSummaryOutput(
                observation="관측 한 줄", diagnosis="진단 한 줄", rationale="결론 근거 한 줄"
            ),
            CandidateProposalOutput(
                candidates=[
                    ProposedCandidate(
                        runbook_id=runbook_id,
                        target_arn=graph_input.asset_context.arn,
                        evidence_ids=[graph_input.evidences[0].evidence_id],
                        **_parameters_for(runbook_id),
                    )
                ]
            ),
        ]
    )


def _parameters_for(runbook_id: str) -> dict:
    """스텁이 채울 AI 몫 파라미터. 조회값은 넣지 않는다(계약 원칙 ①)."""
    if runbook_id == "RUNBOOK_EC2_RIGHTSIZING":
        return {"target_instance_type": "t3.small"}
    if runbook_id == "RUNBOOK_EC2_ENABLE_AUTOSCALING":
        return {"min_size": 1, "max_size": 2}
    raise AssertionError(
        f"고정 세트의 capabilities 에 새 런북이 들어왔다 — 스텁 파라미터를 추가할 것: {runbook_id}"
    )


# ------------------------------------------------------------------------------
# ③ 정답지가 계약·변환기와 어긋나지 않는가
# ------------------------------------------------------------------------------


def test_expected_ai_pairs_with_every_input_that_has_cost_candidates():
    """짝이 없는 입력도, 짝이 없는 정답지도 실패한다. 건수를 손으로 세지 않기 위해서다."""
    with_cases = {p.stem for p in sorted(INPUT.glob("*.json")) if _cases_of(p.stem)}
    have_expected = {p.stem for p in EXPECTED_AI.glob("*.json")}
    assert have_expected == with_cases, (
        f"정답지 없는 입력: {sorted(with_cases - have_expected)} / "
        f"입력 없는 정답지: {sorted(have_expected - with_cases)}"
    )


def test_expected_ai_covers_the_whole_fixed_set():
    """고정 세트 6건을 빠짐없이 덮는다 — 건수가 아니라 case_id 집합으로 본다."""
    from_converter = {c.case_id for p in INPUT.glob("*.json") for c in _cases_of(p.stem)}
    from_expected = {
        item["case_id"] for p in EXPECTED_AI.glob("*.json") for item in _load(p)["expectations"]
    }
    assert from_expected == from_converter, (
        f"정답지에 없는 케이스: {sorted(from_converter - from_expected)} / "
        f"변환기가 안 만드는 케이스: {sorted(from_expected - from_converter)}"
    )


@pytest.mark.parametrize("stem, item", _expectation_pairs())
def test_expected_ai_input_facts_match_the_converter(stem, item):
    """정답지가 인용한 입력 사실이 변환기 산출과 같다.

    target_arn·evidence_ids·capabilities 는 **입력이 정하는 값**이라 정답지에 그대로
    적었다. 골든이나 변환기가 움직이면 여기서 먼저 걸려 정답지를 고치라고 알린다.
    """
    case = next(c for c in _cases_of(stem) if c.case_id == item["case_id"])
    graph_input = case.graph_input
    spec = item["expected"]["candidates"]

    assert item["asset_arn"] == graph_input.asset_context.arn
    assert spec["target_arn_exactly"] == graph_input.asset_context.arn
    assert spec["evidence_ids_subset_of"] == [e.evidence_id for e in graph_input.evidences]
    assert spec["runbook_ids_subset_of"] == sorted(
        c.runbook_id.value for c in graph_input.capabilities
    )
    from ai.agent import _allowed_target_arns

    assert item["input_facts"]["allowed_target_arns"] == _allowed_target_arns(graph_input)


@pytest.mark.parametrize("stem, item", _expectation_pairs())
def test_expected_ai_spec_does_not_contradict_the_contract(stem, item):
    """명세가 AgentGraphOutput 계약과 모순되지 않는다.

    summary_line_count 는 독립 축이 아니라 invocation_status 에 딸린 값이다
    (SUCCEEDED·NO_PROPOSAL 이 3, FAILED 가 0 — packages/schemas/agents.py).
    """
    spec = item["expected"]
    status = AgentInvocationStatus(spec["invocation_status"])

    assert status is AgentInvocationStatus.SUCCEEDED
    assert spec["summary_line_count"] == 3
    assert spec["candidates"]["min_count"] >= 1
    # FINOPS 는 계약상 항상 null 이다 — 정답지가 다른 값을 적으면 계약 위반이다
    assert spec["reviewed_risk_level"] is None
    # 정확한 값으로 못박은 축은 멤버십 축과 모순되지 않아야 한다
    assert spec["candidates"]["target_arn_exactly"] == item["asset_arn"]


# ------------------------------------------------------------------------------
# ① 골든 입력이 그래프 계약을 통과하는가
# ------------------------------------------------------------------------------


@pytest.mark.parametrize("stem, item", _expectation_pairs())
def test_golden_input_satisfies_the_expected_ai_spec(stem, item):
    """골든 입력 1건을 그래프에 그대로 넣고 산출이 명세를 만족하는지 본다.

    모델 응답은 스텁이므로 **문장이 좋은지는 재지 않는다.** 이 테스트가 재는 것은
    골든 입력이 그래프의 계약 조립(_to_draft → RunbookCandidateDraft →
    AgentGraphOutput)을 통과하는가다. 종전에 이 경로에 골든이 닿은 적이 없다.
    """
    case = next(c for c in _cases_of(stem) if c.case_id == item["case_id"])
    spec = item["expected"]

    output = run_finops_graph(case.graph_input, client=_stub_client(case))

    assert output.invocation_status.value == spec["invocation_status"]
    assert len(output.summary_lines) == spec["summary_line_count"]
    assert output.reviewed_risk_level is None
    assert len(output.candidates) >= spec["candidates"]["min_count"]

    runbook_ids = [c.runbook_id.value for c in output.candidates]
    assert len(runbook_ids) == len(set(runbook_ids)), "candidates 의 runbook_id 는 중복될 수 없다"
    assert set(runbook_ids) <= set(spec["candidates"]["runbook_ids_subset_of"])

    allowed_evidence = set(spec["candidates"]["evidence_ids_subset_of"])
    for candidate in output.candidates:
        assert candidate.target_arn == spec["candidates"]["target_arn_exactly"]
        assert len(candidate.evidence_ids) >= spec["candidates"]["evidence_ids_min_per_candidate"]
        assert set(candidate.evidence_ids) <= allowed_evidence, (
            "입력 Evidence 밖의 근거를 인용했다 — 그래프는 이 관계를 보지 않으므로"
            "(ai/agent.py 헤더) 정답지가 보는 축이다"
        )


# ------------------------------------------------------------------------------
# ② 불변식이 골든 입력에서 실제로 작동하는가 (음성 케이스)
# ------------------------------------------------------------------------------
# 위 양성 케이스만 두면 "그래프가 스텁을 그대로 흘려보내는" 상태도 통과한다.
# 아래 둘은 스텁이 계약을 깨뜨렸을 때 그래프가 FAILED 로 닫는지를 골든 입력에서 본다.


def test_graph_rejects_a_target_arn_outside_the_input_assets():
    """다른 골든 케이스의 ARN을 붙이면 FAILED 다 (ai/agent.py:294).

    allowed_target_arns 는 케이스마다 2개(자산 EC2 + 관계 SG)라 목록이 비어 있지 않다
    — 그래서 이 불변식이 실제로 거르는지 확인할 값이 있다.
    """
    first, second = _cases_of("asset_inventory_003")[0], _cases_of("asset_inventory_003")[1]
    runbook_id = sorted(c.runbook_id.value for c in first.graph_input.capabilities)[0]
    client = FakeAIModelClient(
        [
            EvidenceSummaryOutput(observation="관측", diagnosis="진단", rationale="근거"),
            CandidateProposalOutput(
                candidates=[
                    ProposedCandidate(
                        runbook_id=runbook_id,
                        target_arn=second.graph_input.asset_context.arn,  # 남의 자산
                        evidence_ids=[first.graph_input.evidences[0].evidence_id],
                        **_parameters_for(runbook_id),
                    )
                ]
            ),
        ]
    )

    output = run_finops_graph(first.graph_input, client=client)

    assert output.invocation_status is AgentInvocationStatus.FAILED
    assert output.candidates == []
    assert output.summary_lines == []


def test_graph_rejects_a_runbook_outside_the_offered_capabilities():
    """메뉴에 없는 런북을 고르면 FAILED 다.

    RUNBOOK_EBS_DELETE_UNATTACHED 는 AI 추천 가능하지만 대상 자산 유형이 EBS 라
    EC2 인시던트의 capabilities 에 오르지 않는다(ai/capabilities.py:104).
    """
    case = _cases_of("asset_inventory_001")[0]
    offered = {c.runbook_id.value for c in case.graph_input.capabilities}
    assert "RUNBOOK_EBS_DELETE_UNATTACHED" not in offered, "이 케이스의 전제가 깨졌다"

    client = FakeAIModelClient(
        [
            EvidenceSummaryOutput(observation="관측", diagnosis="진단", rationale="근거"),
            CandidateProposalOutput(
                candidates=[
                    ProposedCandidate(
                        runbook_id="RUNBOOK_EBS_DELETE_UNATTACHED",
                        target_arn=case.graph_input.asset_context.arn,
                        evidence_ids=[case.graph_input.evidences[0].evidence_id],
                    )
                ]
            ),
        ]
    )

    output = run_finops_graph(case.graph_input, client=client)

    assert output.invocation_status is AgentInvocationStatus.FAILED
    assert output.candidates == []
