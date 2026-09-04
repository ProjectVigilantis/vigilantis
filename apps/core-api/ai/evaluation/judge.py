# ==============================================================================
# [파일 설명]  담당: 안성일 (AI/Guardrail · Architect)
# 요약 3줄을 LLM 판정자에게 묻는 요청·출력 모델·채점입니다. (Issue #243)
#
# 판정 둘을 서로 다른 호출로 나눈다 — 보여 주는 것이 다르기 때문이다.
#   ① 복원 대조 — 화면 조합(요약 3줄 + 제안 카드)**만** 보고 자산의 판정과 판정을 가른
#      값을 복원하게 한 뒤 골든과 대조한다. 입력·정답을 보여 주면 읽기가 아니라 베끼기가
#      된다. 정답지가 판정자 품질을 판정하므로 사람 보정이 필요 없고, LLM은 케이스를
#      기억하지 않아 매 판 다시 쓸 수 있다(사람은 첫 판 뒤에 기억으로 답한다).
#   ② 결함 판정 — 결함 체크리스트 2·3·4번(단정/추정 · 진단↔조치 · 같은 말). 2번은 무엇이
#      입력 사실인지 알아야 매길 수 있어 **압축 사실표**를 함께 준다. 전체 페이로드가
#      아니라 사실표인 것은 정보는 같고 토큰만 8배이기 때문이다. 이 판정에는 정답지가
#      없으므로 사람 보정 1회가 판정자 품질을 잰다(docs/AI_SUMMARY_BASELINE.md).
#   결함 1(근거 없음)·5(되읽기)는 어휘 수준 근사가 코드로 선다(readback.py).
#
# 판정자는 처방을 내지 않는다 — 라벨과 이유만. 판정과 처방을 한 호출이 내면 판정이
# 처방에 끌린다. 이유를 받는 것은 사람이 보정할 때 어느 표현 때문인지 보기 위해서다.
#
# 판정자 모델은 생성 모델과 같은 확정 모델(config.py 기본값)을 쓴다. 같은 계열이 자기
# 산출을 후하게 매기는 편향은 사람 보정이 직접 재므로 별도 모델을 들이지 않는다.
#
# 판정 프롬프트도 판이다 — judge_fingerprint()를 스냅샷에 적는다. CI 게이트는 아니다
# (프로덕션 경로 밖). 바뀌면 이전 판정과 비교할 수 없다는 표시다.
# ==============================================================================

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict

from ai.model_client import AIModelRequest
from schemas.agents import RunbookCandidateDraft
from schemas.api.assets import SkipReasonCode, Verdict
from schemas.evidence import EvidenceType
from schemas.runbook_parameters import build_display_parameters

from .factcheck import _NUMBER, _normalize_number

JUDGE_VERSION = "v1"

RESTORATION_SYSTEM_PROMPT = (
    "너는 관제 화면에 뜬 AI 요약 3줄과 조치 카드만 보고 원래 자산의 상태를 복원한다. "
    "입력 데이터는 볼 수 없다. 요약과 카드에 적힌 내용만 근거로 아래를 채우고, 적혀 있지 "
    "않은 값은 null로 둔다.\n"
    "verdict: 이 자산이 받은 규칙 판정. verdict_options 중 하나. 요약에서 알 수 없으면 null.\n"
    "skip_reason_code: 판정이 SKIP일 때의 사유 코드. skip_reason_options 중 하나. SKIP이 "
    "아니거나 알 수 없으면 null.\n"
    "cpu_avg: 요약이 말한 관측 기간의 평균 CPU 사용률. 적힌 표기 그대로 옮긴다.\n"
    "cpu_max: 요약이 말한 최대 CPU 사용률.\n"
    "cpu_datapoints: 요약이 말한 관측 데이터포인트 수(측정 건수).\n"
    "other_values: 그 밖에 요약이 판정 근거로 든 값(태그·관측 기간·네트워크 등)을 name과 "
    "value로 옮긴다."
)

DEFECT_SYSTEM_PROMPT = (
    "너는 관제자에게 나가는 AI 요약 3줄을 결함 목록으로 점검한다. 좋은 요약을 정의하지 "
    "않는다 — 관제자의 승인·차단 결정을 막는 결함만 찾는다. fact_sheet(입력이 확정한 값), "
    "summary_lines(요약 3줄), proposal_cards(조치 카드)를 받는다. 각 결함에 flagged와 "
    "reason을 낸다. reason은 요약의 몇 번째 줄의 어느 표현 때문인지 한 문장으로 쓴다.\n"
    "defect_2 단정과 추정이 안 갈린다: fact_sheet에 있는 값은 단정으로, 거기 없는 해석·"
    "추측은 추정으로 구분돼 있어야 한다. 추측을 단정 어조로 썼거나 확정 사실을 추정처럼 "
    "흐렸으면 flagged.\n"
    "defect_3 진단과 조치가 안 이어진다: 세 번째 줄의 결론이 1·2줄의 근거와 진단에서 따라 "
    "나와야 하고, proposal_cards의 조치를 설명해야 한다. 1·2줄에 없는 축의 조치를 권하거나, "
    "카드에 없는 조치를 권하거나, 왜 그 조치인지가 없으면 flagged. 카드가 비어 있으면 왜 "
    "조치가 없는지가 있어야 한다.\n"
    "defect_4 세 줄이 같은 말이다: 세 줄이 각각 새 정보를 실어야 한다. 두 줄 이상이 같은 "
    "사실을 표현만 바꿔 되풀이하면 flagged."
)


class DecidingValue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    value: str


class RestorationOutput(BaseModel):
    """복원 대조 판정자의 출력. 정답지와 대조하는 것은 score_restoration()이다.

    가른 값은 자유 목록이 아니라 **이름 붙은 슬롯**으로 받는다. 값 집합으로 받으면 요약이
    health_score 5를 "평균 CPU 5%"로 잘못 붙여도 5가 입력 어딘가에 있어 통과한다 — #237
    사실 정합성 검사의 한계 그대로다(#243 §선행 계측의 한계). 슬롯이면 cpu_avg 자리에
    4.9가 있어야 복원이다.
    """

    model_config = ConfigDict(extra="forbid")

    verdict: Optional[str]
    skip_reason_code: Optional[str]
    cpu_avg: Optional[str]
    cpu_max: Optional[str]
    cpu_datapoints: Optional[str]
    other_values: list[DecidingValue]


class DefectFlag(BaseModel):
    model_config = ConfigDict(extra="forbid")

    flagged: bool
    reason: str


class DefectJudgement(BaseModel):
    """결함 체크리스트 2·3·4번 판정. 번호는 summary_defects.md의 것이다."""

    model_config = ConfigDict(extra="forbid")

    defect_2: DefectFlag
    defect_3: DefectFlag
    defect_4: DefectFlag


def judge_material() -> str:
    sections = (
        ("restoration_system_prompt", RESTORATION_SYSTEM_PROMPT),
        ("defect_system_prompt", DEFECT_SYSTEM_PROMPT),
        (
            "restoration_output_schema",
            json.dumps(RestorationOutput.model_json_schema(), ensure_ascii=False, sort_keys=True),
        ),
        (
            "defect_output_schema",
            json.dumps(DefectJudgement.model_json_schema(), ensure_ascii=False, sort_keys=True),
        ),
    )
    return "\n".join(f"[{name}]\n{body}" for name, body in sections)


def judge_fingerprint() -> str:
    return hashlib.sha256(judge_material().encode("utf-8")).hexdigest()


def proposal_cards(candidates: Sequence[RunbookCandidateDraft]) -> list[dict[str, Any]]:
    """화면의 제안 카드와 같은 조합 — 런북·대상·표시 파라미터. 표시본은 서버가 만드는
    것과 같은 함수로 만든다(schemas/runbook_parameters.py)."""
    return [
        {
            "runbook_id": candidate.runbook_id.value,
            "target_arn": candidate.target_arn,
            "display_parameters": build_display_parameters(candidate.parameters),
        }
        for candidate in candidates
    ]


def restoration_request(
    summary_lines: Sequence[str], cards: Sequence[Mapping[str, Any]]
) -> AIModelRequest:
    """복원 대조 요청. 자산·근거·정답은 싣지 않는다 — 화면 조합뿐이다."""
    return AIModelRequest(
        system_prompt=RESTORATION_SYSTEM_PROMPT,
        user_payload={
            "summary_lines": list(summary_lines),
            "proposal_cards": [dict(card) for card in cards],
            "verdict_options": [item.value for item in Verdict],
            "skip_reason_options": [item.value for item in SkipReasonCode],
        },
    )


def _metric_content(payload: Mapping[str, Any]) -> Optional[Mapping[str, Any]]:
    for evidence in payload.get("evidences", ()):
        if evidence.get("evidence_type") == EvidenceType.METRIC.value:
            return evidence.get("content")
    return None


def _rule_evaluation(payload: Mapping[str, Any]) -> Optional[Mapping[str, Any]]:
    """RULE 근거의 evaluation. 없으면 최상위 rule_evaluation(중복 제거가 안 걸린 경우)."""
    for evidence in payload.get("evidences", ()):
        if evidence.get("evidence_type") == EvidenceType.RULE.value:
            return evidence.get("content", {}).get("evaluation")
    return payload.get("rule_evaluation")


def fact_sheet(payload: Mapping[str, Any]) -> dict[str, Any]:
    """입력이 확정한 값의 압축본. 결함 2(단정/추정)의 대조 상대다.

    페이로드가 확정한 값은 빠짐없이 싣는다 — v0 1·2차 판정에서 evaluation_status·규칙
    평가(RULE 근거)·가용 영역·인스턴스 ID를 빼먹었더니, 요약이 그 값을 쓴 것을 판정자가
    "확정되지 않은 사실을 단정했다"로 오탐했다. 빠진 값은 곧 오탐이다.
    """
    asset = payload.get("asset", {})
    spec = asset.get("spec", {})
    metric = _metric_content(payload)
    rule = _rule_evaluation(payload)
    return {
        "asset_type": asset.get("asset_type"),
        "arn": asset.get("arn"),
        "resource_id": asset.get("resource_id"),
        "name": asset.get("name"),
        "instance_type": spec.get("instance_type"),
        "availability_zone": spec.get("availability_zone"),
        "vpc_id": spec.get("vpc_id"),
        "subnet_id": spec.get("subnet_id"),
        "private_ip": spec.get("private_ip"),
        "state": asset.get("state"),
        "region": asset.get("region"),
        "account_id": asset.get("account_id"),
        "collected_at": asset.get("collected_at"),
        "tags": spec.get("tags", {}),
        "relationships": list(asset.get("relationships", ())),
        "evaluation_status": asset.get("evaluation_status"),
        "verdict": asset.get("verdict"),
        "skip_reason_code": asset.get("skip_reason_code"),
        "health_score": asset.get("health_score"),
        "rule_evaluation": (
            {
                "verdict": rule.get("verdict"),
                "reason": rule.get("reason"),
                "evaluated_at": rule.get("evaluated_at"),
            }
            if rule
            else None
        ),
        "metric": (
            {
                "metric_name": metric.get("metric_name"),
                "window_start": metric.get("window_start"),
                "window_end": metric.get("window_end"),
                "summary": metric.get("summary"),
            }
            if metric
            else None
        ),
    }


def defect_request(
    payload: Mapping[str, Any],
    summary_lines: Sequence[str],
    cards: Sequence[Mapping[str, Any]],
) -> AIModelRequest:
    return AIModelRequest(
        system_prompt=DEFECT_SYSTEM_PROMPT,
        user_payload={
            "fact_sheet": fact_sheet(payload),
            "summary_lines": list(summary_lines),
            "proposal_cards": [dict(card) for card in cards],
        },
    )


def expected_deciding_values(payload: Mapping[str, Any]) -> dict[str, str]:
    """골든이 담은 "판정을 가른 값" — rule_engine.evaluate_ec2의 입력이다.

    cpu_avg(IDLE_CPU_AVG 문턱) · cpu_max(SPIKE_CPU_MAX 문턱, 없으면 검사를 건너뛰므로
    기대하지 않는다) · cpu_datapoints(MIN_DATAPOINTS 문턱). 태그는 넣지 않는다 — prod
    정확일치 정책은 "왜 보호 대상이 아닌가"라 요약이 담을 이유가 없고, 골든 6건 중 4건이
    그 축 말고는 수치까지 같아 복원 결과가 같게 나오는 것이 예상 결과다(#243).
    """
    metric = _metric_content(payload)
    summary = (metric or {}).get("summary", {})
    expected: dict[str, str] = {}
    for name in ("cpu_avg", "cpu_max", "cpu_datapoints"):
        value = summary.get(name)
        if value is not None:
            expected[name] = _normalize_number(str(value))
    return expected


@dataclass(frozen=True)
class RestorationScore:
    verdict_ok: bool
    skip_reason_ok: bool
    restored: tuple[str, ...]
    missing: tuple[str, ...]
    # 골든에 없는 값이 슬롯에 채워진 경우(A7의 cpu_max처럼). 요약이 지어냈거나 판정자가
    # 짐작한 것이라 실패로 세지는 않지만 사람이 볼 자리다
    unexpected: tuple[str, ...] = ()

    @property
    def expected(self) -> int:
        return len(self.restored) + len(self.missing)


_SLOTS = ("cpu_avg", "cpu_max", "cpu_datapoints")


def _numbers_in(text: str) -> set[str]:
    return {_normalize_number(match.group(0)) for match in re.finditer(_NUMBER, text)}


def score_restoration(output: RestorationOutput, payload: Mapping[str, Any]) -> RestorationScore:
    """판정자가 슬롯에 옮긴 값을 골든과 대조한다 — 슬롯이 곧 귀속이다."""
    asset = payload.get("asset", {})
    expected = expected_deciding_values(payload)
    filled = {name: _numbers_in(getattr(output, name) or "") for name in _SLOTS}

    verdict_ok = (output.verdict or "").strip().upper() == str(asset.get("verdict") or "").upper()
    expected_skip = asset.get("skip_reason_code")
    actual_skip = (output.skip_reason_code or "").strip().upper() or None
    skip_reason_ok = actual_skip == (str(expected_skip).upper() if expected_skip else None)
    restored = tuple(name for name, value in expected.items() if value in filled[name])
    missing = tuple(name for name in expected if name not in restored)
    unexpected = tuple(name for name in _SLOTS if name not in expected and filled[name])
    return RestorationScore(
        verdict_ok=verdict_ok,
        skip_reason_ok=skip_reason_ok,
        restored=restored,
        missing=missing,
        unexpected=unexpected,
    )
