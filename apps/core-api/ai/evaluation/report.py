# ==============================================================================
# [파일 설명]  담당: 안성일 (AI/Guardrail · Architect)
# 계측 실행 결과를 비교표 한 줄로 접습니다. (Issue #237)
#
# 한 줄 = 모델·파라미터 조합 하나. 담는 것은 넷이다.
#   ① 계약 실패(FAILED) 건수 — 호출이 결과를 못 낸 횟수. **원인 둘을 갈라 센다** —
#      모델이 답했지만 계약을 어긴 것과, 경계에서 왕복 자체가 못 선 것(타임아웃·
#      5xx·한도)은 다른 사실이다. 섞으면 API가 흔들린 시간대에 측정한 조합이 모델
#      품질 때문에 나쁜 것처럼 보인다(실측: gpt-4o 24회 중 3회가 후자였다).
#   ② NO_PROPOSAL 건수 — 요약은 냈지만 후보가 빈 횟수. **고정 세트가 전부 낭비 후보라
#      기준선은 0이어야 한다.** 제약을 더할수록 빈 배열이 가장 안전한 답이 되어 모델이
#      아무것도 제안하지 않는 쪽으로 기우는데, 그 위축을 잡을 수단이 이것 말고 없다.
#   ③ 사실 정합성 실패 건수 — factcheck.py 판정
#   ④ 필드 안정도와 흔들린 필드 목록 — reproducibility.py 판정
#
# 단가·비용은 여기 담지 않는다. ADR-0005 §Consequences가 LLM 운영비 지표를 제품 기능에서
# 빼고 "토큰 사용량을 집계해 오프라인으로 추정"으로 못 박았고, 단가는 계속 낡는 값이라
# 실행 인자로 받는다(scripts/finops_eval.py).
# ==============================================================================

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Optional

from schemas.agents import AgentGraphOutput
from schemas.incidents import AgentInvocationStatus

from .factcheck import FactCheckResult
from .reproducibility import FieldAgreement, output_fields, unstable_fields


@dataclass(frozen=True)
class CaseRun:
    """케이스 1건의 1회차 결과."""

    case_id: str
    output: AgentGraphOutput
    prompt_tokens: int
    completion_tokens: int
    fact: FactCheckResult
    # prompt_tokens 중 제공자 캐시분. 계측은 같은 프롬프트를 N회 보내 크게 걸리지만
    # 실경로는 인시던트마다 입력이 달라 대개 0이다 — 모델 선택 판단에 쓸 비용은
    # 캐시를 빼지 않은 쪽이다(#237)
    cached_prompt_tokens: int = 0
    # 이 회차에 모델 경계가 세운 예외의 클래스 이름. None이면 왕복은 섰다는 뜻이라,
    # 그때의 FAILED는 모델이 계약을 어긴 것이다. 메시지는 담지 않는다 —
    # 원문 응답이 섞여 들어올 수 있어서다(ADR-0005 미보존 대상).
    error: Optional[str] = None


@dataclass(frozen=True)
class ColumnReport:
    """비교표 한 줄."""

    label: str
    case_count: int
    repeats: int
    succeeded: int
    no_proposal: int
    failed: int
    # FAILED 중 경계 예외가 있었던 회차. 모델 품질이 아니라 왕복이 못 선 것이다
    failed_transport: int
    # 사실 정합성은 요약이 나온 회차에서만 판정한다 — FAILED는 요약이 비어 있어
    # 위반이 0건인데, 그것을 통과로 세면 실패가 많을수록 지표가 좋아진다
    fact_checked: int
    fact_failed: int
    prompt_tokens: int
    completion_tokens: int
    cached_prompt_tokens: int
    # 케이스별 흔들린 필드. 안정된 필드는 담지 않는다 — 담으면 볼 줄이 묻힌다
    unstable: dict[str, list[FieldAgreement]] = field(default_factory=dict)
    stable_slots: int = 0
    field_slots: int = 0

    @property
    def runs(self) -> int:
        return self.succeeded + self.no_proposal + self.failed

    @property
    def failed_contract(self) -> int:
        """모델이 답했는데 계약을 어긴 회차. 조합 비교에서 실제로 볼 수는 이쪽이다."""
        return self.failed - self.failed_transport

    @property
    def no_proposal_rate(self) -> float:
        return self.no_proposal / self.runs if self.runs else 0.0

    @property
    def field_stability(self) -> float:
        """(케이스, 필드) 자리 중 N회 내내 같은 값이었던 비율."""
        return self.stable_slots / self.field_slots if self.field_slots else 0.0

    @property
    def unstable_field_names(self) -> list[str]:
        """흔들린 필드 이름 — 케이스가 달라도 같은 필드면 한 번만 센다."""
        names: dict[str, None] = {}
        for agreements in self.unstable.values():
            for agreement in agreements:
                names.setdefault(agreement.field, None)
        return list(names)


def build_column_report(label: str, runs: Sequence[CaseRun]) -> ColumnReport:
    """한 조합의 전 회차 결과를 표 한 줄로 접는다.

    회차 수는 케이스별 실행 수에서 읽는다 — 케이스마다 회차가 다르면 가장 많은 쪽을
    적어 둔다(중간에 끊긴 실행을 "적게 돌린 것"으로 감추지 않는다).
    """
    by_case: dict[str, list[CaseRun]] = {}
    for run in runs:
        by_case.setdefault(run.case_id, []).append(run)

    statuses = [run.output.invocation_status for run in runs]
    unstable: dict[str, list[FieldAgreement]] = {}
    stable_slots = 0
    field_slots = 0
    for case_id, case_runs in by_case.items():
        outputs: list[AgentGraphOutput] = [run.output for run in case_runs]
        shaky = unstable_fields(outputs)
        # 필드 자리 수는 그 케이스에서 한 번이라도 나타난 필드 이름의 개수다
        names = {name for output in outputs for name in output_fields(output)}
        field_slots += len(names)
        stable_slots += len(names) - len(shaky)
        if shaky:
            unstable[case_id] = shaky

    return ColumnReport(
        label=label,
        case_count=len(by_case),
        repeats=max((len(case_runs) for case_runs in by_case.values()), default=0),
        succeeded=statuses.count(AgentInvocationStatus.SUCCEEDED),
        no_proposal=statuses.count(AgentInvocationStatus.NO_PROPOSAL),
        failed=statuses.count(AgentInvocationStatus.FAILED),
        failed_transport=sum(
            1
            for run in runs
            if run.output.invocation_status is AgentInvocationStatus.FAILED and run.error
        ),
        fact_checked=sum(1 for run in runs if run.output.summary_lines),
        fact_failed=sum(1 for run in runs if run.output.summary_lines and not run.fact.passed),
        prompt_tokens=sum(run.prompt_tokens for run in runs),
        completion_tokens=sum(run.completion_tokens for run in runs),
        cached_prompt_tokens=sum(run.cached_prompt_tokens for run in runs),
        unstable=unstable,
        stable_slots=stable_slots,
        field_slots=field_slots,
    )
