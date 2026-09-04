# ==============================================================================
# [파일 설명]  담당: 안성일 (AI/Guardrail · Architect)
# 요약 3줄이 구조화 필드로 이미 나가는 값을 되읽는지, 첫 줄이 입력을 인용하는지 봅니다.
# (Issue #243)
#
# 결함 체크리스트(summary_defects.md) 5번(구조화 값 되읽기)과 1번(근거 사실 없음)의
# **어휘 수준 근사**다. 값이 문장에 그대로 있는지만 보므로 인용("평균 CPU 4.9%라 후보로
# 잡혔다")과 되읽기("verdict는 COST_CANDIDATE입니다")를 가르지 못한다 — 합격 조건이
# 아니라 진단값이며, 판을 바꿨을 때 방향(v0 대비 줄었는가)을 보는 데 쓴다. v0 실측
# (gpt-5.6-luna low · 60회)은 1줄의 COST_CANDIDATE 42회 · health_score 42회 · verdict 19회였다.
#
# 되읽기 대상은 화면이 아니라 계약이다(summary_defects.md §5의 판정 기준) —
#   AssetItem(schemas/api/assets.py)      verdict · skip_reason_code · health_score · evaluation_status
#   RecommendationItem(schemas/api/incidents.py)  runbook_id · target_arn · display_parameters
# 필드 **이름**도 센다 — "health_score는 5"처럼 이름을 문장에 쓰는 것 자체가 되읽기의
# 표지다. health_score의 값(정수 하나)은 세지 않는다 — 숫자 하나는 다른 인용과 구분되지
# 않아 오탐이 된다.
#
# 근거 인용은 factcheck.py와 같은 토큰 규칙으로 본다 — 첫 줄에 입력의 식별자·날짜·수치·
# 인스턴스 타입 중 하나라도 있으면 인용이다. 규칙을 두 벌 두면 한쪽만 고쳐져 갈린다.
# ==============================================================================

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from schemas.agents import AgentGraphOutput
from schemas.runbook_parameters import build_display_parameters

from .factcheck import _tokens, allowed_tokens

_FIELD_NAMES = (
    "health_score",
    "verdict",
    "skip_reason_code",
    "evaluation_status",
    "runbook_id",
    "target_arn",
)
_EVALUATION_KEYS = ("verdict", "skip_reason_code", "evaluation_status")

# factcheck.py와 같은 ASCII 경계 — 한글 조사가 붙은 자리("COST_CANDIDATE로")에서도 서고,
# 짧은 숫자 값("2")이 "2026" 안에서 걸리지 않는다
_L = r"(?<![0-9A-Za-z_.\-])"
_R = r"(?![0-9A-Za-z_\-])"


@dataclass(frozen=True)
class ReadbackResult:
    """줄별로 되읽은 토큰. 개수만 남기면 어느 값을 되읽었는지 사람이 볼 수 없다."""

    hits: tuple[tuple[str, ...], ...] = ()

    @property
    def lines_with_readback(self) -> int:
        return sum(1 for line in self.hits if line)

    @property
    def any(self) -> bool:
        return self.lines_with_readback > 0


def structured_tokens(payload: Mapping[str, Any], output: AgentGraphOutput) -> dict[str, str]:
    """되읽기로 셀 토큰 → 출처 라벨. 페이로드의 판정값과 후보의 카드 값이다."""
    tokens: dict[str, str] = {name: "field_name" for name in _FIELD_NAMES}
    asset = payload.get("asset", {})
    for key in _EVALUATION_KEYS:
        value = asset.get(key)
        if value:
            tokens[str(value)] = f"asset.{key}"
    for evidence in payload.get("evidences", ()):
        evaluation = evidence.get("content", {}).get("evaluation")
        if not isinstance(evaluation, Mapping):
            continue
        for key in _EVALUATION_KEYS:
            value = evaluation.get(key)
            if value:
                tokens.setdefault(str(value), f"evidence.{key}")
    for candidate in output.candidates:
        tokens[candidate.runbook_id.value] = "candidate.runbook_id"
        tokens[candidate.target_arn] = "candidate.target_arn"
        for key, value in build_display_parameters(candidate.parameters).items():
            tokens.setdefault(value, f"candidate.display_parameters.{key}")
    return tokens


_NUMERIC = re.compile(r"[0-9]+(?:\.[0-9]+)?")


def _contains(line: str, token: str) -> bool:
    if _NUMERIC.fullmatch(token):
        # 숫자 값(min_size "1" 같은 표시 파라미터)은 숫자·소수점 경계로 본다 — ASCII 낱말
        # 경계만 쓰면 "1.5%" 안의 1이 걸린다(v1 2차 실측에서 25회 오탐)
        return re.search(r"(?<![0-9.])" + re.escape(token) + r"(?![0-9.])", line) is not None
    return re.search(_L + re.escape(token) + _R, line) is not None


def check_readback(payload: Mapping[str, Any], output: AgentGraphOutput) -> ReadbackResult:
    """요약 각 줄에 그대로 나타난 구조화 토큰. FAILED(요약 없음)는 빈 결과다."""
    tokens = structured_tokens(payload, output)
    return ReadbackResult(
        hits=tuple(
            tuple(sorted(token for token in tokens if _contains(line, token)))
            for line in output.summary_lines
        )
    )


def cites_input(payload: Mapping[str, Any], line: str) -> bool:
    """줄 하나가 입력의 값(식별자·날짜·인스턴스 타입·수치)을 하나라도 인용하는가."""
    allowed_ids, allowed_dates, allowed_types, allowed_numbers = allowed_tokens(payload)
    found_ids, found_dates, found_types, found_numbers = _tokens(line)
    allowed_date_digits = [re.sub(r"[^0-9]", "", token) for token in allowed_dates]
    return bool(
        found_ids & allowed_ids
        or found_types & allowed_types
        or found_numbers & allowed_numbers
        or any(
            re.sub(r"[^0-9]", "", token) in pool
            for token in found_dates
            for pool in allowed_date_digits
        )
    )


def observation_cites_input(payload: Mapping[str, Any], summary_lines: Sequence[str]) -> bool:
    """첫 줄(observation)이 입력을 인용하는가 — 결함 1의 근사. 요약이 없으면 False."""
    return bool(summary_lines) and cites_input(payload, summary_lines[0])
