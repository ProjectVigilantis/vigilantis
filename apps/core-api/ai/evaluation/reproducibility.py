# ==============================================================================
# [파일 설명]  담당: 안성일 (AI/Guardrail · Architect)
# 같은 입력을 N회 넣었을 때 **어느 필드가 흔들리는지**를 이름과 값으로 냅니다. (Issue #237)
#
# "결과가 매번 다르다"는 관찰만으로는 고칠 자리를 못 찾는다. PR #226 리뷰의 4회 실측이
# 보여준 것은 흔들린 값(target_instance_type)이 **서버가 선택지를 주지 않은 값**과
# 정확히 일치한다는 것이었고, 그 대조를 하려면 필드 단위로 갈라 봐야 한다.
#
# 요약 3줄은 여기서 보지 않는다. 산문은 같은 뜻을 다르게 쓸 수 있어 문자열 일치가
# 품질을 뜻하지 않는다 — 요약은 factcheck.py(사실 정합성)와 사람 판정이 맡는다.
# 여기서 보는 것은 계약 필드, 즉 실행으로 나가는 값뿐이다.
# ==============================================================================

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass

from schemas.agents import AgentGraphOutput

# 그 회차에 그 필드가 아예 없었다는 표시. 회차마다 후보 구성이 달라지는 것도 변동이라,
# 빠진 자리를 비워 두면 "3회 중 3회 일치"로 잘못 읽힌다.
MISSING = "(없음)"


@dataclass(frozen=True)
class FieldAgreement:
    """필드 1개의 회차별 값."""

    field: str
    values: tuple[str, ...]

    @property
    def distinct(self) -> int:
        return len(set(self.values))

    @property
    def stable(self) -> bool:
        return self.distinct == 1

    @property
    def top_share(self) -> float:
        """최빈값 비율. 회차가 0이면 0.0."""
        if not self.values:
            return 0.0
        return Counter(self.values).most_common(1)[0][1] / len(self.values)


def output_fields(output: AgentGraphOutput) -> dict[str, str]:
    """출력 1건을 비교 가능한 필드 표로 편다.

    후보를 순서가 아니라 runbook_id로 잡는다 — 계약이 같은 runbook_id의 중복을 막으므로
    (schemas/agents.py) 키가 겹치지 않고, 후보 순서가 바뀐 것을 값 변동으로 오해하지 않는다.
    """
    fields: dict[str, str] = {
        "invocation_status": output.invocation_status.value,
        "candidate_count": str(len(output.candidates)),
        "runbook_ids": ",".join(sorted(c.runbook_id.value for c in output.candidates)),
    }
    for candidate in sorted(output.candidates, key=lambda c: c.runbook_id.value):
        prefix = candidate.runbook_id.value
        fields[f"{prefix}.target_arn"] = candidate.target_arn
        fields[f"{prefix}.evidence_ids"] = ",".join(candidate.evidence_ids)
        for name, value in candidate.parameters.model_dump().items():
            fields[f"{prefix}.{name}"] = str(value)
    return fields


def field_agreement(outputs: Sequence[AgentGraphOutput]) -> list[FieldAgreement]:
    """N회 출력을 필드별 일치도로 바꾼다. 처음 나타난 순서를 유지한다."""
    per_run = [output_fields(output) for output in outputs]
    names: dict[str, None] = {}
    for run in per_run:
        for name in run:
            names.setdefault(name, None)
    return [
        FieldAgreement(field=name, values=tuple(run.get(name, MISSING) for run in per_run))
        for name in names
    ]


def unstable_fields(outputs: Sequence[AgentGraphOutput]) -> list[FieldAgreement]:
    """흔들린 필드만. 안정된 필드를 함께 내면 정작 볼 줄이 묻힌다."""
    return [item for item in field_agreement(outputs) if not item.stable]
