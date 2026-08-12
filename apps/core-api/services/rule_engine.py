# ==============================================================================
# [파일 설명]  담당: 김승철 (Data & Rule Engine)
# 사전 필터링 Rule Evaluator입니다. Idle EC2/미사용 SG를 판별하고, 정상 자산은
# Skip 사유 코드와 함께 DB에 적재해 불필요한 LLM 호출을 절감합니다.
#
# 구현: collector 가 적재한 assets 테이블을 읽어 각 자산에 verdict + skip_reason
#   (+ health_score) 을 매긴다. 낭비/위협 후보(COST_CANDIDATE/THREAT/UNUSED)만
#   이후 AI 추론 단계로 넘긴다.
#
# 임계치는 seed_localstack 의 기대 판정(정답지)을 재현하도록 맞췄다.
#   실 계정 데이터 확보 후 한 번 더 보정할 것. (LOCALSTACK.md 한계 항목 참고)
# ==============================================================================

from __future__ import annotations

from enum import Enum
from typing import Optional

# ----- 임계치 (실 계정 데이터로 재보정 대상) -----
IDLE_CPU_AVG = 5.0        # 평균 CPU 이 값 미만이면 저활성 후보
SPIKE_CPU_MAX = 40.0      # 평균은 낮아도 최대가 이 값 이상이면 스파이크 → 다운사이징 부적합
MIN_DATAPOINTS = 48       # 최소 관측치(약 2일). 미만이면 데이터부족
PROD_HINTS = ("prod", "production")


class Verdict(str, Enum):
    COST_CANDIDATE = "COST_CANDIDATE"  # 낭비 후보(EC2 다운사이징) → AI
    THREAT = "THREAT"                  # SG 전체개방 → 보안/AI
    UNUSED = "UNUSED"                  # 미사용 SG(미부착) → 정리 후보
    SKIP = "SKIP"                      # 제외(사유는 skip_reason)


class SkipReason(str, Enum):
    SKIP_INSUFFICIENT_DATA = "SKIP_INSUFFICIENT_DATA"  # 관측치 부족(신규 등)
    SKIP_PROD_PROTECTED = "SKIP_PROD_PROTECTED"        # 운영 자산 보호
    SKIP_LOW_UTIL = "SKIP_LOW_UTIL"                    # 스파이크 등으로 다운사이징 부적합
    SKIP_WHITELISTED = "SKIP_WHITELISTED"              # 화이트리스트(예: default SG)
    SKIP_ACTIVE = "SKIP_ACTIVE"                        # 정상 가동(낭비 아님)


def _is_prod(name: Optional[str], tags: dict) -> bool:
    hay = (name or "").lower()
    if any(h in hay for h in PROD_HINTS):
        return True
    env = str(tags.get("Environment", tags.get("env", ""))).lower()
    return any(h in env for h in PROD_HINTS)


def evaluate_ec2(cpu_avg: Optional[float], cpu_max: Optional[float], cpu_datapoints: Optional[int],
                 name: Optional[str], tags: dict | None = None) -> tuple[Verdict, Optional[SkipReason], Optional[float]]:
    """EC2 1대 판정 → (verdict, skip_reason, health_score). 판정 우선순위대로 검사."""
    tags = tags or {}
    health = round(cpu_avg, 2) if cpu_avg is not None else None

    if cpu_datapoints is None or cpu_datapoints < MIN_DATAPOINTS:
        return Verdict.SKIP, SkipReason.SKIP_INSUFFICIENT_DATA, health
    if _is_prod(name, tags):
        return Verdict.SKIP, SkipReason.SKIP_PROD_PROTECTED, health
    if cpu_avg is not None and cpu_avg < IDLE_CPU_AVG:
        if cpu_max is not None and cpu_max >= SPIKE_CPU_MAX:
            return Verdict.SKIP, SkipReason.SKIP_LOW_UTIL, health   # 스파이크: 최대 CPU 높음
        return Verdict.COST_CANDIDATE, None, health                 # 저활성 → 다운사이징 후보
    return Verdict.SKIP, SkipReason.SKIP_ACTIVE, health             # 정상 가동


def evaluate_sg(name: Optional[str], attached: Optional[bool],
                open_to_world: Optional[bool]) -> tuple[Verdict, Optional[SkipReason]]:
    """SG 1개 판정 → (verdict, skip_reason)."""
    if (name or "").lower() == "default":
        return Verdict.SKIP, SkipReason.SKIP_WHITELISTED   # default SG 는 삭제/변경 불가
    if open_to_world:
        return Verdict.THREAT, None                        # 22/3389 등 전체개방
    if attached is False:
        return Verdict.UNUSED, None                        # 미부착(미사용 후보)
    return Verdict.SKIP, SkipReason.SKIP_ACTIVE


def run_rule_engine(db) -> dict:
    """assets 테이블 전체를 읽어 판정 결과(verdict/skip_reason/health_score)를 기록한다.
    반환: verdict 별 집계 + 각 자산 판정 목록."""
    from sqlalchemy import select

    from db.models import Asset

    assets = db.execute(select(Asset)).scalars().all()
    results = []
    counts: dict[str, int] = {}
    for a in assets:
        if a.asset_type == "EC2":
            tags = (a.attributes or {}).get("tags", {})
            verdict, skip, health = evaluate_ec2(a.cpu_avg, a.cpu_max, a.cpu_datapoints, a.name, tags)
            a.health_score = health
        else:  # SG
            verdict, skip = evaluate_sg(a.name, a.attached, a.open_to_world)
        a.verdict = verdict.value
        a.skip_reason = skip.value if skip else None
        counts[verdict.value] = counts.get(verdict.value, 0) + 1
        results.append({"name": a.name, "type": a.asset_type,
                        "verdict": a.verdict, "skip_reason": a.skip_reason})
    db.commit()
    return {"counts": counts, "results": results}
