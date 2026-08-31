# ==============================================================================
# [파일 설명]  담당: 박지현 (QA & Scenario)
# 4단계 Execution Guardrail 통과/차단 회귀 테스트입니다.
#
# 계층 구분 — 단위 테스트와 겹치지 않게 나눈다.
#   apps/core-api/ai/tests/test_guardrail_steps.py (안성일) = 각 단계 함수의 동작.
#     구조적 오류(추가 필드·타입 불일치·빈 문자열)와 목록 대조를 전수로 본다.
#   이 파일 = 팀 공용 회귀. ① 문서에서 확정한 결정이 코드로 지켜지는가
#     ② Golden Dataset 자산이 실제로 가드레일을 통과하는가
#     ③ 단계 순서와 거절 사유가 관제자에게 나가는 기록대로인가.
#
# 구현 현황 (#114 / PR #123 · #177)
#   ① Schema Check      구현됨
#   ② Action Whitelist  구현됨
#   ③ ARN Match         구현됨 — 수집 자산 조회를 인자로 받는다
#   ④ AWS Dry-Run       미구현 — executor precheck 대기(ADR-0007)
# 4단계 종합 판정(GuardrailValidationResult)은 ④가 붙어야 조립되므로, 지금은
# 단계 함수를 직접 불러 _Run.failed_step 으로 순서를 본다.
# ==============================================================================

from __future__ import annotations

import json
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for _path in (ROOT / "apps" / "core-api", ROOT / "packages"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from ai.guardrails import (  # noqa: E402
    ARN_TARGET_NOT_MANAGED,
    SCHEMA_INVALID_PAYLOAD,
    WHITELIST_NOT_AI_RECOMMENDABLE,
    WHITELIST_UNKNOWN_RUNBOOK,
    run_action_whitelist,
    run_arn_match,
    run_schema_check,
)
from ai.whitelist import ROLLBACK_RUNBOOK_IDS, RunbookId  # noqa: E402
from schemas.agents import RunbookCandidateDraft  # noqa: E402
from schemas.guardrails import (  # noqa: E402
    GuardrailStep,
    GuardrailStepResult,
    GuardrailStepStatus,
    GuardrailValidationContext,
    GuardrailValidationRequest,
)

GOLDEN_FINOPS_INPUT = ROOT / "datasets" / "golden" / "finops" / "input"

_SAFE_ARN = "arn:aws:ec2:ap-northeast-2:123456789012:instance/i-0123456789abcdef0"


# Runbook 별로 AI 가 정하는 값 (#154). ① Schema Check 가 이 계약과 대조하므로,
# 런북과 무관한 파라미터를 실으면 목록 대조(②) 전에 형식 위반으로 걸린다.
# 목록에 없는 ID 는 typed 계약이 없어 빈 dict 로 둔다 — 판정은 ② 의 몫이다.
_PARAMS_BY_RUNBOOK = {
    RunbookId.RUNBOOK_EC2_ISOLATE.value: {},
    RunbookId.RUNBOOK_NACL_ADD_DENY.value: {
        "rule_number": 100, "cidr_block": "203.0.113.5/32", "protocol": "-1",
    },
    RunbookId.RUNBOOK_NACL_RESTORE.value: {"rule_number": 100, "egress": False},
    RunbookId.RUNBOOK_SG_DELETE_ISOLATED.value: {},
    RunbookId.RUNBOOK_EC2_RIGHTSIZING.value: {"target_instance_type": "t3.small"},
    RunbookId.RUNBOOK_EC2_ENABLE_AUTOSCALING.value: {"min_size": 1, "max_size": 2},
    RunbookId.RUNBOOK_EBS_DELETE_UNATTACHED.value: {},
}


def _payload(**overrides) -> dict:
    """Runbook 에 맞는 파라미터를 실어 준다.

    이 헬퍼가 런북을 무시하고 한 벌만 싣던 때는 SG 삭제 후보에 rightsizing
    파라미터가 붙어도 통과했다 (#154). 이제 그 조합은 ① 에서 걸린다.
    """
    runbook_id = overrides.get("runbook_id", RunbookId.RUNBOOK_EC2_RIGHTSIZING.value)
    base = {
        "runbook_id": runbook_id,
        "target_arn": _SAFE_ARN,
        "parameters": _PARAMS_BY_RUNBOOK.get(runbook_id, {}),
        "evidence_ids": ["ev-1"],
    }
    base.update(overrides)
    return base


@dataclass(frozen=True)
class _Run:
    """구현된 단계를 순서대로 태운 결과.

    failed_step 은 4단계 종합 판정(GuardrailValidationResult)의 같은 이름 필드와
    같은 뜻이다 — 처음 FAIL 한 단계이고 그 뒤 단계는 실행되지 않는다. 종합 판정
    자체는 ④ AWS Dry-Run 이 붙어야 조립되므로(steps 고정 4개), 그 전까지 단계
    순서만 이 필드로 먼저 고정한다.
    """

    steps: list[GuardrailStepResult]
    failed_step: GuardrailStep | None
    draft: RunbookCandidateDraft | None


def _run_implemented_steps(payload: dict, collected: Iterable[str] = ()) -> _Run:
    """① Schema Check → ② Action Whitelist → ③ ARN Match 를 순서대로 태운다.

    collected 는 DB 에 수집된 자산 ARN 목록이다 — ③ 이 받는 조회를 완전 일치
    집합으로 대신한다(apps/core-api/db/repositories/assets.py::get_asset_by_arn
    과 같은 판정이다). 기본값이 빈 목록이라, ②에서 멈추는 입력은 그대로 두면 된다.
    """
    request = GuardrailValidationRequest(
        validation_context=GuardrailValidationContext.AI_CANDIDATE,
        candidate_id="cand-qa",
        command_payload=payload,
    )
    steps: list[GuardrailStepResult] = []

    schema = run_schema_check(request)
    steps.append(schema.step_result)
    if schema.command is None:
        return _Run(steps, GuardrailStep.SCHEMA_CHECK, None)

    whitelist = run_action_whitelist(schema.command)
    steps.append(whitelist.step_result)
    if whitelist.draft is None:
        return _Run(steps, GuardrailStep.ACTION_WHITELIST, None)

    managed = frozenset(collected)
    arn_match = run_arn_match(whitelist.draft, lambda target_arn: target_arn in managed)
    steps.append(arn_match.step_result)
    if arn_match.draft is None:
        return _Run(steps, GuardrailStep.ARN_MATCH, None)

    return _Run(steps, None, arn_match.draft)


# ---------------------------------------------------------------- ② 목록 대조
# 문서에서 확정한 결정이 코드로 지켜지는지 본다. 단위 테스트가 보는 것은
# "목록 밖이면 거절한다"는 동작이고, 여기서 보는 것은 "그 목록이 SSOT 결정과
# 같은가"다 — 폐기된 ID가 되살아나면 여기서 걸린다.


# SSOT §확정 결정 로그 2026-08-12: 구 Whitelist 예시 2종 폐기
_RETIRED_RUNBOOK_IDS = ["RUNBOOK_EC2_DOWNSIZE", "RUNBOOK_IP_BLOCK"]


@pytest.mark.parametrize("runbook_id", _RETIRED_RUNBOOK_IDS)
def test_guardrail_blocks_unlisted_runbook(runbook_id: str) -> None:
    """폐기 확정된 구버전 Runbook ID는 ② Action Whitelist 에서 차단된다.

    거절이 ②에 기록되는지까지 본다. ①에서 터지면 "무엇이 막았는가"가 사라지고
    관제자에게 나가는 사유가 틀린다(#114 설계 의도).
    """
    run = _run_implemented_steps(_payload(runbook_id=runbook_id))

    assert run.steps[0].result is GuardrailStepStatus.PASS, (
        f"{runbook_id} 가 ①에서 걸렸다 — 목록 대조는 ②의 몫이다"
    )
    assert run.failed_step is GuardrailStep.ACTION_WHITELIST
    assert run.draft is None
    assert run.steps[-1].reason_code == WHITELIST_UNKNOWN_RUNBOOK


@pytest.mark.parametrize("runbook_id", sorted(ROLLBACK_RUNBOOK_IDS))
def test_rollback_runbooks_are_listed_but_not_ai_recommendable(runbook_id: str) -> None:
    """롤백 3종은 "등록됨"과 "AI 추천 가능"이 다른 축임을 고정한다.

    ADR-0004 정책 ①②: Whitelist 에는 정식 등록해 가드레일 우회 경로를 없애고,
    ai_recommendable=false 로 AI 제안에서만 뺀다. 사유 코드가
    WHITELIST_UNKNOWN_RUNBOOK 으로 바뀌면 등록이 풀린 것이고(우회 경로 발생),
    통과해버리면 AI 가 롤백을 제안할 수 있게 된 것이다 — 양쪽 다 회귀다.
    """
    run = _run_implemented_steps(_payload(runbook_id=runbook_id))

    assert run.failed_step is GuardrailStep.ACTION_WHITELIST
    assert run.draft is None
    assert run.steps[-1].reason_code == WHITELIST_NOT_AI_RECOMMENDABLE


# ---------------------------------------------------------------- Golden 연동


# 자산 종류마다 그 자산을 대상으로 하는 Runbook 을 짝지어 둔다 — ARN 만 바꾸면
# SG ARN 이 EC2 Rightsizing 에 붙는 조합이 생긴다. Runbook 별 파라미터 계약(#154)이
# 선 지금은 파라미터도 함께 갈리므로 그 조합은 ① 에서 걸린다. (PR #141 리뷰 반영)
_GOLDEN_ASSET_RUNBOOKS = {
    "ec2_instances": RunbookId.RUNBOOK_EC2_RIGHTSIZING.value,
    "security_groups": RunbookId.RUNBOOK_SG_DELETE_ISOLATED.value,
}


def _golden_asset_pairs() -> list[tuple[str, str]]:
    """Golden Dataset 자산 파일에서 (ARN, 그 자산을 대상으로 하는 Runbook) 을 모은다."""
    pairs: list[tuple[str, str]] = []
    for path in sorted(GOLDEN_FINOPS_INPUT.glob("*.json")):
        with path.open(encoding="utf-8") as fp:
            data = json.load(fp)
        for key, runbook_id in _GOLDEN_ASSET_RUNBOOKS.items():
            pairs.extend((asset["arn"], runbook_id) for asset in data.get(key, []))
    return pairs


def _golden_asset_kinds() -> set[str]:
    """골든 파일에 실제로 들어 있는 자산 종류. 매핑이 이걸 전부 덮어야 한다.

    arn 을 가진 dict 의 리스트를 자산 목록으로 본다 — account_id·region 같은
    스칼라 키와 갈린다.
    """
    kinds: set[str] = set()
    for path in sorted(GOLDEN_FINOPS_INPUT.glob("*.json")):
        with path.open(encoding="utf-8") as fp:
            data = json.load(fp)
        kinds.update(
            key
            for key, value in data.items()
            if isinstance(value, list)
            and any(isinstance(item, dict) and "arn" in item for item in value)
        )
    return kinds


def test_golden_asset_arns_pass_the_implemented_steps() -> None:
    """Golden Dataset 의 실제 자산 ARN 이 구현된 단계를 통과한다.

    EC2 와 SG 를 모두 본다. SG ARN 은 형식이 security-group/sg-… 로 EC2 의
    instance/i-… 와 달라, ③ ARN Match 가 형식을 보는 판정으로 바뀌면 가장 먼저
    깨질 부류다 — 수집 목록에 있으면 형식과 무관하게 통과해야 한다.

    수집분은 골든 파일이 가진 ARN 전부로 둔다. 데이터셋 갱신과 가드레일 변경
    어느 쪽이 깨져도 드러나게 하는 것이 목적이다.
    """
    pairs = _golden_asset_pairs()
    assert pairs, "Golden Dataset 에 자산이 없다 — 경로나 파일 구조가 바뀌었다"
    # 한 종류가 빠져도 나머지가 남아 assert pairs 는 통과한다 — SG 누락이 그렇게
    # 지나갔다(#134). 건수는 골든이 늘 때마다 바뀌므로(#127) 종류만 고정한다.
    missing = _golden_asset_kinds() - set(_GOLDEN_ASSET_RUNBOOKS)
    assert not missing, f"골든에 있는데 매핑에 없는 자산 종류: {sorted(missing)}"

    collected = [arn for arn, _ in pairs]
    for arn, runbook_id in pairs:
        run = _run_implemented_steps(
            _payload(target_arn=arn, runbook_id=runbook_id), collected
        )
        blocked = run.failed_step.value if run.failed_step else None
        assert blocked is None, (
            f"골든 자산 {arn} ({runbook_id}) 이 {blocked} 에서 거절됐다"
        )


# ------------------------------------------------------------- ③ 범위 초과 차단
# ① 은 target_arn 을 "비어있지 않은 문자열"로만 본다. RunbookCandidateDraft 도
# 같다(packages/schemas/agents.py: target_arn: str = Field(min_length=1)).
# 즉 ARN 이 우리 관리 범위인지 보는 것은 ③ 이 유일하다.
_SCOPE_ESCALATION_ARNS = [
    pytest.param("arn:aws:iam::999999999999:role/Admin", id="other_account_iam_role"),
    pytest.param("*", id="wildcard"),
    pytest.param(
        "arn:aws:ec2:ap-northeast-2:123456789012:instance/*", id="wildcard_resource"
    ),
    pytest.param("'; DROP TABLE assets; --", id="not_an_arn"),
]


@pytest.mark.parametrize("target_arn", _SCOPE_ESCALATION_ARNS)
def test_guardrail_blocks_arn_scope_escalation(target_arn: str) -> None:
    """DB 에 수집되지 않은 대상은 ③ ARN Match 에서 차단된다.

    수집분은 _SAFE_ARN 하나로 둔다. wildcard_resource 는 그 ARN 과 계정·리전까지
    같은 문자열로 시작하므로, 판정을 접두어 검사로 바꾸면 이 케이스부터 통과한다 —
    Scope Escalation 차단이 접두어가 아니라 수집 자산 대조인 이유다.
    """
    run = _run_implemented_steps(_payload(target_arn=target_arn), [_SAFE_ARN])

    assert run.failed_step is GuardrailStep.ARN_MATCH
    assert run.draft is None
    assert run.steps[-1].reason_code == ARN_TARGET_NOT_MANAGED


def test_failed_step_reports_the_first_failing_step() -> None:
    """미등록 Runbook 과 미수집 ARN 을 동시에 준 입력은 ② 에서 멈춘다(#134 이관).

    ③ 도 거절할 입력이라, 단계 순서가 무너지면 거절 기록이 ARN_MATCH 로 남는다 —
    관제자에게 나가는 사유가 "목록에 없는 조치" 대신 "관리 대상 아님" 이 된다.
    """
    run = _run_implemented_steps(
        _payload(runbook_id="RUNBOOK_EC2_DOWNSIZE", target_arn="*")
    )

    assert run.failed_step is GuardrailStep.ACTION_WHITELIST
    assert run.steps[-1].reason_code == WHITELIST_UNKNOWN_RUNBOOK
    assert [step.step for step in run.steps] == [
        GuardrailStep.SCHEMA_CHECK,
        GuardrailStep.ACTION_WHITELIST,
    ], "③ 이 실행됐다 — 앞 단계가 FAIL 하면 뒤 단계는 돌지 않는다"


@pytest.mark.parametrize(
    "payload, why",
    [
        (
            _payload(runbook_id="RUNBOOK_EC2_DOWNSIZE", target_arn="", severity="HIGH"),
            "폐기 Runbook(②) · 빈 ARN(①) · 추가 필드(①) 동시",
        ),
        (
            _payload(
                runbook_id=RunbookId.RUNBOOK_NACL_ADD_DENY.value,
                target_arn="arn:aws:ec2:ap-northeast-2:123456789012:instance/i-uncollected",
                parameters={"target_instance_type": "t3.small"},
            ),
            "미수집 ARN(③) · 런북과 안 맞는 파라미터(①) 동시",
        ),
    ],
    ids=["retired_id_with_empty_arn", "unmanaged_arn_with_wrong_params"],
)
def test_schema_failure_stops_before_the_later_steps(payload: dict, why: str) -> None:
    """① 이 거절한 입력은 ②·③ 을 아예 태우지 않는다.

    ① 자체의 거절 판정은 apps/core-api/ai/tests/test_guardrail_steps.py 가 20종으로
    덮는다. 여기서 보는 것은 **파이프라인 순서**다 — 두 테스트가 보는 대상이 다르다.

    뒤 단계도 거절할 입력을 일부러 준다. 단계 순서가 무너지면 거절 기록이
    ACTION_WHITELIST·ARN_MATCH 로 남아, 관제자에게 나가는 사유가 "형식이 깨진 요청"
    대신 "목록에 없는 조치"·"관리 대상 아님" 이 된다. 원인을 엉뚱한 곳에서 찾게 된다.

    test_failed_step_reports_the_first_failing_step 는 ②↔③ 경계만 본다.
    이 테스트가 그 앞 경계(①↔②)를 막는다.
    """
    run = _run_implemented_steps(payload, [_SAFE_ARN])

    assert run.failed_step is GuardrailStep.SCHEMA_CHECK, why
    assert run.draft is None
    assert run.steps[-1].reason_code == SCHEMA_INVALID_PAYLOAD
    assert [step.step for step in run.steps] == [GuardrailStep.SCHEMA_CHECK], (
        f"① 이 FAIL 했는데 뒤 단계가 실행됐다 ({why}) — "
        f"실행된 단계: {[s.step.value for s in run.steps]}"
    )


def test_schema_check_failure_carries_no_verification_summary() -> None:
    """① 거절에는 verification_summary 가 붙지 않는다.

    그 필드는 ④ AWS Dry-Run 이 "무엇을 어디까지 확인했는가" 를 담는 자리다
    (ADR-0007 §3). ① 은 payload 형식만 보므로 확인 범위라는 개념이 없고, 값이
    채워지면 관제자가 AWS 를 조회해 본 것으로 읽는다.
    """
    run = _run_implemented_steps(_payload(target_arn=""))

    assert run.failed_step is GuardrailStep.SCHEMA_CHECK
    assert run.steps[-1].verification_summary is None
