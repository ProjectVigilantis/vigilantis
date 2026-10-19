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

from schemas.asset_roles import ROLE_ISOLATION, ROLE_TAG_KEY

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
    SKIP_LOW_UTIL = "SKIP_LOW_UTIL"                    # 다운사이징 부적합 — 스파이크 · 이미 한 번 줄인 인스턴스(#453)
    SKIP_WHITELISTED = "SKIP_WHITELISTED"              # 화이트리스트(예: default SG)
    SKIP_ACTIVE = "SKIP_ACTIVE"                        # 정상 가동(낭비 아님)
    SKIP_UNSUPPORTED_STATE = "SKIP_UNSUPPORTED_STATE"  # 판정 보류 상태 — EBS: available/in-use 외(#276) · EC2: running 외(#453)


# EC2 판정 대상 상태. 나머지(pending·stopping·stopped·shutting-down·terminated·미상)는 보류다(#453).
EC2_JUDGEABLE_STATES = ("running",)


def _is_prod(tags: dict) -> bool:
    """운영 자산 여부. 인식 태그 키(PROD_TAG_KEYS)의 값이 PROD_TAG_VALUES 에
    정확히 들어맞으면 True. 키·값 모두 대소문자 무시, 값은 부분일치 아님."""
    for key, value in (tags or {}).items():
        if key.lower() in PROD_TAG_KEYS and str(value).strip().lower() in PROD_TAG_VALUES:
            return True
    return False


def evaluate_ec2(cpu_avg: Optional[float], cpu_max: Optional[float], cpu_datapoints: Optional[int],
                 tags: dict | None = None, *, state: Optional[str],
                 already_rightsized: bool = False) -> tuple[Verdict, Optional[SkipReason], Optional[float]]:
    """EC2 1대 판정 → (verdict, skip_reason, health_score). 판정 우선순위대로 검사.

    - **상태가 먼저다(#453).** `running` 이 아니면 지표를 보지 않고 SKIP_UNSUPPORTED_STATE 다.
      종료(`terminated`·`shutting-down`)된 인스턴스는 CloudWatch 가 지난 지표를 보관하므로
      14일 창에서 관측치 48개·저활성을 그대로 넘겨 후보가 된다 — 그 후보는 승인 화면까지
      올라간 뒤 실행에서야 깨진다(가드레일 ④가 존재를 검사하지 않는다 · ADR-0006 이월 8행).
      `stopped`·`stopping` 도 보류한다 — RIGHTSIZING 실행이 정지 → 타입 변경 → **기동**
      순서라 꺼 둔 인스턴스를 켜 버리고, 정지 구간은 관측치가 비어 정지 자체가 저활성으로
      읽힌다. 미상(`None`)은 EBS 와 같이 보류한다(fail-safe). `evaluate_ebs` 와 같은 코드다.
    - **이미 한 번 줄인 인스턴스는 다시 후보가 되지 않는다(#453 · #434 5-2).** 지표가 인스턴스
      ID 기준이라 타입을 바꿔도 창에 옛 타입의 낮은 CPU 가 남고, 목표 타입 규칙은 매번 현재
      타입을 기준으로 다시 1/4 를 내리므로 스캔마다 연쇄로 내려간다. 저활성이어도
      SKIP_LOW_UTIL(다운사이징 부적합)로 보류한다 — 누적 하강이 1회분(1/4)에서 멈춘다.
      정상 가동(SKIP_ACTIVE)은 그대로 정상 가동이다. 무엇을 「이미 줄였다」로 볼지는
      호출부(`run_rule_engine`)가 실행 기록으로 정한다.
    """
    tags = tags or {}
    health = round(cpu_avg, 2) if cpu_avg is not None else None

    if (state or "").lower() not in EC2_JUDGEABLE_STATES:
        return Verdict.SKIP, SkipReason.SKIP_UNSUPPORTED_STATE, health
    if cpu_datapoints is None or cpu_datapoints < MIN_DATAPOINTS:
        return Verdict.SKIP, SkipReason.SKIP_INSUFFICIENT_DATA, health
    if _is_prod(tags):
        return Verdict.SKIP, SkipReason.SKIP_PROD_PROTECTED, health
    if cpu_avg is not None and cpu_avg < IDLE_CPU_AVG:
        if cpu_max is not None and cpu_max >= SPIKE_CPU_MAX:
            return Verdict.SKIP, SkipReason.SKIP_LOW_UTIL, health   # 스파이크: 최대 CPU 높음
        if already_rightsized:
            return Verdict.SKIP, SkipReason.SKIP_LOW_UTIL, health   # 이미 한 번 줄였다: 연쇄 하강 방지
        return Verdict.COST_CANDIDATE, None, health                 # 저활성 → 다운사이징 후보
    return Verdict.SKIP, SkipReason.SKIP_ACTIVE, health             # 정상 가동


def evaluate_ebs(state: Optional[str],
                 attached_instance_ids: Optional[list[str]]) -> tuple[Verdict, Optional[SkipReason]]:
    """EBS 볼륨 1개 판정 → (verdict, skip_reason). (#276 정책 확정)

    - 정리 후보(UNUSED): **state == "available" 이면서 부착 인스턴스가 없음** — 유일한 삭제 후보.
    - SKIP_ACTIVE: in-use 이거나 부착된 볼륨(정상 가동). available + 부착 같은 모순 조합은 부착을 우선.
    - SKIP_UNSUPPORTED_STATE: creating·deleting·error·deleted·미상(null·기타) — available/in-use 가
      아닌 상태. 삭제 후보가 아니므로 UNUSED 로 보지 않고, "정상 가동" 오해를 주는 SKIP_ACTIVE 대신
      판정 보류 코드로 처리한다(오삭제 방지 + 관제 화면·AI 요약 정확성).
    """
    s = (state or "").lower()
    if not attached_instance_ids and s == "available":
        return Verdict.UNUSED, None
    if attached_instance_ids or s == "in-use":
        return Verdict.SKIP, SkipReason.SKIP_ACTIVE
    return Verdict.SKIP, SkipReason.SKIP_UNSUPPORTED_STATE


def _is_isolation_sg(tags: dict | None) -> bool:
    """격리용 SG 여부 — 자리 태그(schemas.asset_roles)가 키·값 모두 정확히 맞을 때만 True.
    이름이나 규칙 수로 추정하지 않는다(#359)."""
    return (tags or {}).get(ROLE_TAG_KEY) == ROLE_ISOLATION


def evaluate_sg(name: Optional[str], attached: Optional[bool],
                open_to_world: Optional[bool],
                tags: dict | None = None) -> tuple[Verdict, Optional[SkipReason]]:
    """SG 1개 판정 → (verdict, skip_reason). 판정 우선순위대로 검사."""
    if (name or "").lower() == "default":
        return Verdict.SKIP, SkipReason.SKIP_WHITELISTED   # default SG 는 삭제/변경 불가
    if open_to_world:
        return Verdict.THREAT, None                        # 22/3389 등 전체개방
    # 격리용 SG 는 EC2_ISOLATE 전까지 미부착이 정상이라 UNUSED 로 두면 첫 회차부터 삭제 후보다.
    # 위협(전체개방) 뒤에 둔다 — 태그가 빼는 것은 "미사용이니 지우자" 하나뿐이고, 격리 SG 가
    # 전체개방이면 그건 진짜 위협이라 태그로 가리지 않는다(#359).
    # 미부착 UNUSED보다 먼저 격리 역할을 제외하며, 부착 후에도 SKIP_WHITELISTED를 유지한다.
    if _is_isolation_sg(tags):
        return Verdict.SKIP, SkipReason.SKIP_WHITELISTED
    if attached is False:
        return Verdict.UNUSED, None                        # 미부착(미사용 후보)
    return Verdict.SKIP, SkipReason.SKIP_ACTIVE


def _rightsized_instance_arns(db) -> set[str]:
    """한 번이라도 크기를 바꾼(또는 바꿨을 수 있는) EC2 의 ARN — `evaluate_ec2` 의 already_rightsized.

    RIGHTSIZING 실행 중 **`FAILED` 만 뺀다.** `FAILED` 는 크기를 바꾸지 못하고 끝난 것이라
    다시 제안해도 연쇄가 아니다. 나머지는 모두 「이미 줄였다」로 본다.
    - `SUCCESS` — 줄였다.
    - `ROLLED_BACK`·`ROLLBACK_INITIATED`·`ROLLBACK_FAILED` — 줄였다가 되돌렸거나 되돌리는 중이다.
      원복(REVERT_SIZE)이 성공하면 원본이 `ROLLED_BACK` 으로 닫히고(workflows
      `_ORIGIN_STATUS_AFTER_ROLLBACK`), 자식이 성공했는지가 원본의 성공 여부를 가리지 않는다
      (성공 뒤 수동 원복 · 실패 뒤 자동 원복 둘 다 이 상태다). 되돌린 인스턴스를 다시 제안하면
      같은 제안 → 원복이 반복된다.
    - `UNVERIFIED` — 자산이 바뀌었을 수 있다(Issue #249).
    - `IN_PROGRESS` — 지금 바꾸는 중이다.
    """
    from sqlalchemy import select

    from db import models
    from schemas.api.actions import ExecutionStatus
    from schemas.runbooks import RunbookId

    rows = db.execute(
        select(models.ActionExecution.target_arn).where(
            models.ActionExecution.runbook_id == RunbookId.RUNBOOK_EC2_RIGHTSIZING,
            models.ActionExecution.status != ExecutionStatus.FAILED,
        )
    ).scalars()
    return set(rows)


def run_rule_engine(db, collection_run_id: str | None = None) -> dict:
    """assets 및 metric_summaries 테이블을 읽어 RuleEvaluation 결과(RuleEvaluationResult 계약)를 기록한다.
    반환: verdict 별 집계 + 표시 요약 + 이번 호출의 typed 판정 목록(Intake 조립용).
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

    # 소멸 자산은 list_assets 가 기본으로 제외한다(#332) — 실물이 없는 자산을 여기서
    # 판정하면 마지막 관측 회차의 낡은 메트릭으로 후보가 서고, 그 후보가 승인 화면까지
    # 올라간 뒤 실행에서야 깨진다.
    # 종료·정지 인스턴스도 여기서는 빼지 않는다 — 목록에는 남기고 판정만 보류한다(#453 ⓒ).
    # 거른 것과 AWS 응답에서 사라진 것(absent_since)을 같은 칸에 섞지 않기 위해서다.
    assets = assets_repo.list_assets(db)
    rightsized_arns = _rightsized_instance_arns(db)
    results = []
    evaluations: list[RuleEvaluationResult] = []
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
            verdict, skip, health = evaluate_ec2(
                cpu_avg, cpu_max, cpu_dp, tags,
                state=a.state, already_rightsized=a.arn in rightsized_arns,
            )
            health_int = int(round(health)) if health is not None else None
        elif a.asset_type == AssetType.SG:
            attached = (a.spec or {}).get("attached")
            open_to_world = bool((a.spec or {}).get("open_to_world"))
            tags = (a.spec or {}).get("tags", {})
            verdict, skip = evaluate_sg(a.name, attached, open_to_world, tags)
            health_int = None
        elif a.asset_type == AssetType.EBS:
            # EBS 는 판정 대상(_RULE_TARGET_TYPES). 분기를 두지 않으면 판정행이 없어
            # 조회단이 영구 PENDING 을 부여한다. state 는 Asset 행 컬럼(available/in-use 등).
            attached_ids = (a.spec or {}).get("attached_instance_ids") or []
            verdict, skip = evaluate_ebs(a.state, attached_ids)
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

        evaluations.append(contract)
        counts[api_verdict.value] = counts.get(api_verdict.value, 0) + 1
        results.append(
            {
                "name": a.name,
                "type": a.asset_type.value if hasattr(a.asset_type, "value") else str(a.asset_type),
                "verdict": api_verdict.value,
                "skip_reason": api_skip.value if api_skip else None,
            }
        )

    return {"counts": counts, "results": results, "evaluations": evaluations}

