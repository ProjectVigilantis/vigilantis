# ==============================================================================
# [파일 설명]  담당: 박지현 (QA & Scenario)
# 4단계 Execution Guardrail 통과/차단 회귀 테스트입니다.
#
# 계층 구분 — 단위 테스트와 겹치지 않게 나눈다.
#   apps/core-api/ai/tests/test_guardrail_steps.py (안성일) = 각 단계 함수의 동작.
#     구조적 오류(추가 필드·타입 불일치·빈 문자열)와 목록 대조를 전수로 본다.
#   이 파일 = 팀 공용 회귀. ① 문서에서 확정한 결정이 코드로 지켜지는가
#     ② Golden Dataset 자산이 실제로 가드레일을 통과하는가
#     ③ 지금 무엇이 막히고 무엇이 아직 안 막히는가(방어 경계).
#
# 구현 현황 (#114 / PR #123 기준)
#   ① Schema Check      구현됨
#   ② Action Whitelist  구현됨
#   ③ ARN Match         미구현 — apps/core-api/ai/guardrails.py [남은 작업]
#   ④ AWS Dry-Run       미구현 — executor precheck 대기(ADR-0007)
# 4단계 종합 판정(GuardrailValidationResult)은 ③④가 붙어야 조립되므로, 지금은
# 단계 함수를 직접 불러 검증한다.
# ==============================================================================

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for _path in (ROOT / "apps" / "core-api", ROOT / "packages"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from ai.guardrails import (  # noqa: E402
    WHITELIST_NOT_AI_RECOMMENDABLE,
    WHITELIST_UNKNOWN_RUNBOOK,
    run_action_whitelist,
    run_schema_check,
)
from ai.whitelist import ROLLBACK_RUNBOOK_IDS, RunbookId  # noqa: E402
from schemas.guardrails import (  # noqa: E402
    GuardrailStep,
    GuardrailStepStatus,
    GuardrailValidationContext,
    GuardrailValidationRequest,
)

GOLDEN_FINOPS_INPUT = ROOT / "datasets" / "golden" / "finops" / "input"

_SAFE_ARN = "arn:aws:ec2:ap-northeast-2:123456789012:instance/i-0123456789abcdef0"


def _payload(**overrides) -> dict:
    base = {
        "runbook_id": RunbookId.RUNBOOK_EC2_RIGHTSIZING.value,
        "target_arn": _SAFE_ARN,
        "display_parameters": {"target_instance_type": "t3.small"},
        "evidence_ids": ["ev-1"],
    }
    base.update(overrides)
    return base


def _run_first_two_steps(payload: dict):
    """①②를 순서대로 태우고 (schema_result, whitelist_outcome) 을 돌려준다.

    ①에서 걸리면 whitelist_outcome 은 None 이다 — 어느 단계가 막았는지가
    거절 기록의 핵심이므로 뭉뚱그리지 않는다.
    """
    request = GuardrailValidationRequest(
        validation_context=GuardrailValidationContext.AI_CANDIDATE,
        candidate_id="cand-qa",
        command_payload=payload,
    )
    schema = run_schema_check(request)
    if schema.command is None:
        return schema.step_result, None
    return schema.step_result, run_action_whitelist(schema.command)


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
    schema_result, whitelist = _run_first_two_steps(_payload(runbook_id=runbook_id))

    assert schema_result.result is GuardrailStepStatus.PASS, (
        f"{runbook_id} 가 ①에서 걸렸다 — 목록 대조는 ②의 몫이다"
    )
    assert whitelist is not None
    assert whitelist.draft is None
    assert whitelist.step_result.step is GuardrailStep.ACTION_WHITELIST
    assert whitelist.step_result.result is GuardrailStepStatus.FAIL
    assert whitelist.step_result.reason_code == WHITELIST_UNKNOWN_RUNBOOK


@pytest.mark.parametrize("runbook_id", sorted(ROLLBACK_RUNBOOK_IDS))
def test_rollback_runbooks_are_listed_but_not_ai_recommendable(runbook_id: str) -> None:
    """롤백 3종은 "등록됨"과 "AI 추천 가능"이 다른 축임을 고정한다.

    ADR-0004 정책 ①②: Whitelist 에는 정식 등록해 가드레일 우회 경로를 없애고,
    ai_recommendable=false 로 AI 제안에서만 뺀다. 사유 코드가
    WHITELIST_UNKNOWN_RUNBOOK 으로 바뀌면 등록이 풀린 것이고(우회 경로 발생),
    통과해버리면 AI 가 롤백을 제안할 수 있게 된 것이다 — 양쪽 다 회귀다.
    """
    _, whitelist = _run_first_two_steps(_payload(runbook_id=runbook_id))

    assert whitelist is not None
    assert whitelist.draft is None
    assert whitelist.step_result.reason_code == WHITELIST_NOT_AI_RECOMMENDABLE


# ---------------------------------------------------------------- Golden 연동


# 자산 종류마다 그 자산을 대상으로 하는 Runbook 을 짝지어 둔다 — ARN 만 바꾸면
# SG ARN 이 EC2 Rightsizing 에 붙는 조합이 생기고, Runbook 별 파라미터 계약(#49)이
# 들어올 때 그 조합부터 깨진다. (PR #141 리뷰 반영)
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
    instance/i-… 와 달라, ③ ARN Match 가 붙을 때 가장 먼저 깨질 부류다.
    지금은 ①② 가 ARN 내용을 보지 않아 동작 차이가 없지만, 이 테스트의 가치는
    전부 ③ 이후에 있으므로 커버리지를 미리 채워 둔다.

    데이터셋 갱신과 가드레일 변경 어느 쪽이 깨져도 드러나게 하는 것이 목적이며,
    ③ 이 붙기 전까지 실질 방어값은 골든 파일 경로·구조 가드와 Runbook 등록
    여부다(PR #141 리뷰 관찰).
    """
    pairs = _golden_asset_pairs()
    assert pairs, "Golden Dataset 에 자산이 없다 — 경로나 파일 구조가 바뀌었다"
    # 한 종류가 빠져도 나머지가 남아 assert pairs 는 통과한다 — SG 누락이 그렇게
    # 지나갔다(#134). 건수는 골든이 늘 때마다 바뀌므로(#127) 종류만 고정한다.
    missing = _golden_asset_kinds() - set(_GOLDEN_ASSET_RUNBOOKS)
    assert not missing, f"골든에 있는데 매핑에 없는 자산 종류: {sorted(missing)}"

    for arn, runbook_id in pairs:
        _, whitelist = _run_first_two_steps(
            _payload(target_arn=arn, runbook_id=runbook_id)
        )
        assert whitelist is not None and whitelist.draft is not None, (
            f"골든 자산 {arn} ({runbook_id}) 이 구현된 가드레일 단계에서 거절됐다"
        )


# ---------------------------------------------------------------- ③ 방어 경계
# 아래 두 테스트는 짝이다. 지금은 앞의 것이 돌고 뒤의 것은 skip 이며,
# ③ ARN Match 가 구현되면 앞이 실패하고 뒤를 열게 된다.


# ① 은 target_arn 을 "비어있지 않은 문자열"로만 본다. RunbookCandidateDraft 도
# 같다(packages/schemas/agents.py: target_arn: str = Field(min_length=1)).
# 즉 ARN 의 형식·계정·범위를 보는 것은 ③ 이 유일하다.
_SCOPE_ESCALATION_ARNS = [
    pytest.param("arn:aws:iam::999999999999:role/Admin", id="other_account_iam_role"),
    pytest.param("*", id="wildcard"),
    pytest.param("arn:aws:ec2:ap-northeast-2:123456789012:instance/*", id="wildcard_resource"),
    pytest.param("'; DROP TABLE assets; --", id="not_an_arn"),
]


@pytest.mark.parametrize("target_arn", _SCOPE_ESCALATION_ARNS)
def test_scope_escalation_arns_are_not_blocked_before_arn_match(target_arn: str) -> None:
    """③ 이 없는 동안 범위 초과 ARN 이 ①② 를 통과한다는 사실을 고정한다.

    실패하면 좋은 소식이다 — ③ ARN Match(또는 다른 방어)가 붙었다는 뜻이므로,
    이 테스트를 지우고 아래 test_guardrail_blocks_arn_scope_escalation 의
    skip 을 해제하면 된다.

    현재 상태를 과장하지 않기 위해 적어둔다: 4단계 종합 판정
    (GuardrailValidationResult)이 아직 조립되지 않아 가드레일은 실행 경로에
    붙어 있지 않다. 뚫린 것이 아니라 방어선이 아직 세워지지 않은 것이다.

    ③ 구현자에게: 이 목록을 계정 접두어 매칭으로만 막으면 wildcard_resource
    (arn:...:123456789012:instance/*)가 그대로 통과한다. 같은 계정·같은 리전
    문자열로 시작하기 때문이다. 범위 판정은 접두어가 아니라 DB에 수집된
    자산 ARN 과의 대조여야 한다(Scope Escalation 차단의 원래 정의).
    """
    _, whitelist = _run_first_two_steps(_payload(target_arn=target_arn))

    assert whitelist is not None
    assert whitelist.draft is not None, (
        "①② 가 ARN 을 거절했다 — 방어가 앞당겨졌다면 이 테스트를 "
        "test_guardrail_blocks_arn_scope_escalation 으로 교체할 것"
    )
    assert whitelist.draft.target_arn == target_arn


@pytest.mark.skip(reason="③ ARN Match 미구현 — ai/guardrails.py [남은 작업] 3번")
def test_guardrail_blocks_arn_scope_escalation() -> None:
    # DB에 수집되지 않은 ARN·타 계정·와일드카드는 ③ ARN Match 에서 차단되어야 한다.
    # 위 test_scope_escalation_arns_are_not_blocked_before_arn_match 가 실패하는
    # 시점이 이 테스트를 여는 시점이다.
    ...
