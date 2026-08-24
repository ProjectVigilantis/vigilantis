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

# 운영(prod) 자산 인식 기준 (미해결 #4, 규칙 소유자 확정 2026-08-24)
#   - 키: 대소문자 무시 비교(소문자로 저장)
#   - 값: 소문자 정확일치 — 부분 문자열 매칭 금지(product-service류 오탐 방지, #81)
PROD_TAG_KEYS = ("environment", "env", "stage", "tier")
PROD_TAG_VALUES = ("prod", "production", "prd")


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


def _is_prod(tags: dict) -> bool:
    """운영 자산 여부. 인식 태그 키(PROD_TAG_KEYS)의 값이 PROD_TAG_VALUES 에
    정확히 들어맞으면 True. 키·값 모두 대소문자 무시, 값은 부분일치 아님."""
    for key, value in (tags or {}).items():
        if key.lower() in PROD_TAG_KEYS and str(value).strip().lower() in PROD_TAG_VALUES:
            return True
    return False


def evaluate_ec2(cpu_avg: Optional[float], cpu_max: Optional[float], cpu_datapoints: Optional[int],
                 name: Optional[str], tags: dict | None = None) -> tuple[Verdict, Optional[SkipReason], Optional[float]]:
    """EC2 1대 판정 → (verdict, skip_reason, health_score). 판정 우선순위대로 검사."""
    tags = tags or {}
    health = round(cpu_avg, 2) if cpu_avg is not None else None

    if cpu_datapoints is None or cpu_datapoints < MIN_DATAPOINTS:
        return Verdict.SKIP, SkipReason.SKIP_INSUFFICIENT_DATA, health
    if _is_prod(tags):
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


def run_rule_engine(db, collection_run_id: str | None = None) -> dict:
    """assets 및 metric_summaries 테이블을 읽어 RuleEvaluation 결과(RuleEvaluationResult 계약)를 기록한다.
    반환: verdict 별 집계 + 각 자산 판정 목록.
    """
    from datetime import datetime, timezone

    from sqlalchemy import select

    from db import models
    from db.repositories import assets as assets_repo
    from schemas.api.assets import (
        AssetType,
        EvaluationStatus,
        SkipReasonCode,
    )
    from schemas.api.assets import (
        Verdict as ApiVerdict,
    )
    from schemas.rules import RuleEvaluationResult

    assets = assets_repo.list_assets(db)
    results = []
    counts: dict[str, int] = {}
    now = datetime.now(timezone.utc)

    for a in assets:
        run_id = collection_run_id or a.last_collection_run_id
        if not run_id:
            continue

        if a.asset_type == AssetType.EC2:
            metric_row = db.execute(
                select(models.MetricSummary).where(
                    models.MetricSummary.asset_id == a.asset_id,
                    models.MetricSummary.collection_run_id == run_id,
                )
            ).scalar_one_or_none()

            cpu_avg = metric_row.cpu_avg if metric_row else None
            cpu_max = metric_row.cpu_max if metric_row else None
            cpu_dp = metric_row.cpu_datapoints if metric_row else None

            tags = (a.spec or {}).get("tags", {})
            verdict, skip, health = evaluate_ec2(cpu_avg, cpu_max, cpu_dp, a.name, tags)
            health_int = int(round(health)) if health is not None else None
        elif a.asset_type == AssetType.SG:
            attached = (a.spec or {}).get("attached")
            open_to_world = bool((a.spec or {}).get("open_to_world"))
            verdict, skip = evaluate_sg(a.name, attached, open_to_world)
            health_int = None
        else:
            continue

        api_verdict = ApiVerdict(verdict.value)
        api_skip = SkipReasonCode(skip.value) if skip else None

        contract = RuleEvaluationResult(
            asset_arn=a.arn,
            collection_run_id=run_id,
            evaluation_status=EvaluationStatus.COMPLETED,
            verdict=api_verdict,
            health_score=health_int,
            skip_reason_code=api_skip,
            reason=f"{a.asset_type.value} rule evaluation: verdict={api_verdict.value}",
            evaluated_at=now,
        )

        existing_eval = db.execute(
            select(models.RuleEvaluation).where(
                models.RuleEvaluation.asset_id == a.asset_id,
                models.RuleEvaluation.collection_run_id == run_id,
            )
        ).scalar_one_or_none()

        if existing_eval is None:
            assets_repo.add_rule_evaluation(db, contract)
        else:
            existing_eval.evaluation_status = contract.evaluation_status.value
            existing_eval.verdict = contract.verdict.value if contract.verdict else None
            existing_eval.health_score = contract.health_score
            existing_eval.skip_reason_code = (
                contract.skip_reason_code.value if contract.skip_reason_code else None
            )
            existing_eval.reason = contract.reason
            existing_eval.evaluated_at = contract.evaluated_at

        counts[api_verdict.value] = counts.get(api_verdict.value, 0) + 1
        results.append(
            {
                "name": a.name,
                "type": a.asset_type.value if hasattr(a.asset_type, "value") else str(a.asset_type),
                "verdict": api_verdict.value,
                "skip_reason": api_skip.value if api_skip else None,
            }
        )

    return {"counts": counts, "results": results}

