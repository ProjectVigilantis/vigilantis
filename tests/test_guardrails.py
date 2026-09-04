# ==============================================================================
# [파일 설명]  담당: 박지현 (QA & Scenario)
# 4단계 Execution Guardrail 통과/차단 회귀 테스트입니다.
#
# 계층 구분 — 단위 테스트와 겹치지 않게 나눈다.
#   apps/core-api/ai/tests/test_guardrail_steps.py (안성일) = 단계 함수와 4단계 조립의
#     동작. 구조적 오류 전수·목록 대조·단계 순서(막힌 단계 기록, 뒤 단계 NOT_RUN,
#     앞이 막으면 AWS 미호출)를 parametrize 로 덮는다. **순서 규약은 그쪽이 원본이다.**
#   이 파일 = 팀 공용 회귀. 유닛이 보지 않는 것만 본다.
#     ① 문서에서 확정한 결정이 코드로 지켜지는가 (폐기 Runbook ID·롤백 3종 정책)
#     ② Golden Dataset 의 실제 자산이 4단계를 통과하는가
#     ③ 범위를 벗어난 ARN 이 ③ 에서 차단되는가 (Scope Escalation)
#
# 구현 현황 (#114 / PR #123 · #177 / PR #202 · #208 / PR #213)
#   ①②③④ 전부 구현됨. 4단계 종합 판정 진입점은 run_guardrail_validation 이다.
#
# **진입점을 직접 부른다.** 단계 함수를 손으로 이어 붙이면 그 조립이 프로덕션의
# 복사본이 되어, 실제 순서가 깨져도 이 파일은 초록불이 된다. ④ 가 서기 전에는
# 진입점이 없어 손으로 이었는데(#224), 이제 갈아탄다.
# ==============================================================================

from __future__ import annotations

import json
import sys
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for _path in (ROOT / "apps" / "core-api", ROOT / "packages"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from ai.guardrails import (  # noqa: E402
    ARN_TARGET_NOT_MANAGED,
    WHITELIST_NOT_AI_RECOMMENDABLE,
    WHITELIST_UNKNOWN_RUNBOOK,
    GuardrailOutcome,
    run_guardrail_validation,
)
from ai.whitelist import ROLLBACK_RUNBOOK_IDS, RunbookId  # noqa: E402
from schemas.agents import RunbookCandidateDraft  # noqa: E402
from schemas.guardrails import (  # noqa: E402
    GUARDRAIL_STEP_ORDER,
    GuardrailDecision,
    GuardrailStep,
    GuardrailStepResult,
    GuardrailStepStatus,
    GuardrailValidationContext,
    GuardrailValidationRequest,
)
from schemas.precheck import PrecheckOutcome  # noqa: E402

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


# ④ 가 부르는 AWS 판정 경계를 대신한다. **실제 AWS 판정은 이 계층 몫이 아니다** —
# 확정 10종의 실물 precheck 는 services/tests/test_precheck_localstack.py 가 덮는다.
# 여기서는 "골든 자산이 ④ 까지 도달하는가"와 조립 결과만 본다.
_STUB_SUMMARY = "DRY_RUN | 확인: 형식·권한 | 미확인: 실행 시점 자원 상태"


@dataclass
class _StubPrecheck:
    """통과만 하는 precheck. 물어본 draft 를 쌓아 둔다 — 호출 여부가 곧 도달 증거다."""

    asked: list = field(default_factory=list)

    def __call__(self, draft: RunbookCandidateDraft, /) -> PrecheckOutcome:
        self.asked.append(draft)
        return PrecheckOutcome(passed=True, verification_summary=_STUB_SUMMARY)


def _validate(
    payload: dict,
    collected: Iterable[str] = (),
    *,
    precheck: _StubPrecheck | None = None,
) -> GuardrailOutcome:
    """4단계 진입점을 그대로 부른다(ai/guardrails.py::run_guardrail_validation).

    collected 는 DB 에 수집된 자산 ARN 목록이다 — ③ 이 받는 조회를 완전 일치 집합으로
    대신한다(apps/core-api/db/repositories/assets.py::get_asset_by_arn 과 같은 판정).
    """
    managed = frozenset(collected)
    return run_guardrail_validation(
        GuardrailValidationRequest(
            validation_context=GuardrailValidationContext.AI_CANDIDATE,
            candidate_id="cand-qa",
            command_payload=payload,
        ),
        is_managed_arn=lambda target_arn: target_arn in managed,
        precheck=precheck or _StubPrecheck(),
    )


def _failed_step_result(outcome: GuardrailOutcome) -> GuardrailStepResult:
    """FAIL 로 기록된 단계 결과. steps 는 항상 4개(뒤는 NOT_RUN)라 [-1] 로 잡으면 틀린다."""
    failed = [s for s in outcome.result.steps if s.result is GuardrailStepStatus.FAIL]
    assert len(failed) == 1, f"FAIL 단계가 {len(failed)}개다 — 첫 FAIL 에서 멈춰야 한다"
    return failed[0]


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
    outcome = _validate(_payload(runbook_id=runbook_id))

    assert outcome.result.steps[0].result is GuardrailStepStatus.PASS, (
        f"{runbook_id} 가 ①에서 걸렸다 — 목록 대조는 ②의 몫이다"
    )
    assert outcome.result.failed_step is GuardrailStep.ACTION_WHITELIST
    assert outcome.command is None
    assert _failed_step_result(outcome).reason_code == WHITELIST_UNKNOWN_RUNBOOK


@pytest.mark.parametrize("runbook_id", sorted(ROLLBACK_RUNBOOK_IDS))
def test_rollback_runbooks_are_listed_but_not_ai_recommendable(runbook_id: str) -> None:
    """롤백 3종은 "등록됨"과 "AI 추천 가능"이 다른 축임을 고정한다.

    ADR-0004 정책 ①②: Whitelist 에는 정식 등록해 가드레일 우회 경로를 없애고,
    ai_recommendable=false 로 AI 제안에서만 뺀다. 사유 코드가
    WHITELIST_UNKNOWN_RUNBOOK 으로 바뀌면 등록이 풀린 것이고(우회 경로 발생),
    통과해버리면 AI 가 롤백을 제안할 수 있게 된 것이다 — 양쪽 다 회귀다.
    """
    outcome = _validate(_payload(runbook_id=runbook_id))

    assert outcome.result.failed_step is GuardrailStep.ACTION_WHITELIST
    assert outcome.command is None
    assert _failed_step_result(outcome).reason_code == WHITELIST_NOT_AI_RECOMMENDABLE


# ---------------------------------------------------------------- Golden 연동


# 자산 종류마다 그 자산을 대상으로 하는 Runbook 을 짝지어 둔다 — ARN 만 바꾸면
# SG ARN 이 EC2 Rightsizing 에 붙는 조합이 생긴다. Runbook 별 파라미터 계약(#154)이
# 선 지금은 파라미터도 함께 갈리므로 그 조합은 ① 에서 걸린다. (PR #141 리뷰 반영)
_GOLDEN_ASSET_RUNBOOKS = {
    "ec2_instances": RunbookId.RUNBOOK_EC2_RIGHTSIZING.value,
    "security_groups": RunbookId.RUNBOOK_SG_DELETE_ISOLATED.value,
    "ebs_volumes": RunbookId.RUNBOOK_EBS_DELETE_UNATTACHED.value,
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


def test_golden_asset_arns_pass_all_four_steps() -> None:
    """Golden Dataset 의 실제 자산 ARN 이 4단계를 전부 통과한다.

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
        precheck = _StubPrecheck()
        outcome = _validate(
            _payload(target_arn=arn, runbook_id=runbook_id), collected, precheck=precheck
        )
        blocked = (
            outcome.result.failed_step.value if outcome.result.failed_step else None
        )
        assert blocked is None, (
            f"골든 자산 {arn} ({runbook_id}) 이 {blocked} 에서 거절됐다"
        )
        assert outcome.result.result is GuardrailDecision.PASS
        assert outcome.command is not None, "통과했는데 승격된 후보가 없다"
        # ④ 까지 실제로 도달했는가. 앞 단계가 조용히 막으면 failed_step 만 보고는
        # "통과"로 읽히지 않지만, 조립이 바뀌어 ④ 를 건너뛰면 여기서 걸린다.
        assert len(precheck.asked) == 1, (
            f"골든 자산 {arn} 이 ④ 에 도달하지 않았다 — precheck 호출 {len(precheck.asked)}회"
        )
        assert [step.step for step in outcome.result.steps] == list(
            GUARDRAIL_STEP_ORDER
        ), "4단계 고정 계약이 깨졌다"


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
    precheck = _StubPrecheck()
    outcome = _validate(_payload(target_arn=target_arn), [_SAFE_ARN], precheck=precheck)

    assert outcome.result.failed_step is GuardrailStep.ARN_MATCH
    assert outcome.command is None
    assert _failed_step_result(outcome).reason_code == ARN_TARGET_NOT_MANAGED
    assert precheck.asked == [], (
        "③ 이 막은 ARN 을 ④ 가 물었다 — 범위 밖 자원에 실제 조회가 나간다"
    )


def test_failed_step_reports_the_first_failing_step() -> None:
    """미등록 Runbook 과 미수집 ARN 을 동시에 준 입력은 ② 에서 멈춘다(#134 이관).

    ③ 도 거절할 입력이라, 단계 순서가 무너지면 거절 기록이 ARN_MATCH 로 남는다 —
    관제자에게 나가는 사유가 "목록에 없는 조치" 대신 "관리 대상 아님" 이 된다.
    """
    outcome = _validate(_payload(runbook_id="RUNBOOK_EC2_DOWNSIZE", target_arn="*"))

    assert outcome.result.failed_step is GuardrailStep.ACTION_WHITELIST
    assert _failed_step_result(outcome).reason_code == WHITELIST_UNKNOWN_RUNBOOK
    ran = [s.step for s in outcome.result.steps if s.result is not GuardrailStepStatus.NOT_RUN]
    assert ran == [
        GuardrailStep.SCHEMA_CHECK,
        GuardrailStep.ACTION_WHITELIST,
    ], "③ 이 실행됐다 — 앞 단계가 FAIL 하면 뒤 단계는 돌지 않는다"
