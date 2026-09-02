# ==============================================================================
# [파일 설명]
# 수집·자산·판정 저장소 — CollectionRun·Asset·AssetRelationship·MetricSummary·
# RuleEvaluation. (Issue #60)
# ==============================================================================

from __future__ import annotations

from datetime import datetime
from typing import Optional, Sequence

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from schemas.api.assets import AssetType, RelationType
from schemas.assets import MetricSummary as MetricSummaryContract
from schemas.collections import CollectionRunStatus
from schemas.rules import RuleEvaluationResult

from .. import mappers, models

# --- CollectionRun -------------------------------------------------------------


def start_collection_run(
    db: Session,
    *,
    account_id: str,
    region: str,
    mode: str,
    lookback_days: int,
    period_seconds: int,
) -> models.CollectionRun:
    run = models.CollectionRun(
        account_id=account_id,
        region=region,
        mode=mode,
        lookback_days=lookback_days,
        period_seconds=period_seconds,
    )
    db.add(run)
    db.flush()
    return run


def finish_collection_run(
    db: Session,
    collection_run_id: str,
    status: CollectionRunStatus,
    *,
    finished_at: datetime,
    error_summary: Optional[str] = None,
) -> bool:
    """IN_PROGRESS인 실행만 종료 상태로 전이한다."""
    result = db.execute(
        update(models.CollectionRun)
        .where(
            models.CollectionRun.collection_run_id == collection_run_id,
            models.CollectionRun.status == CollectionRunStatus.IN_PROGRESS,
        )
        .values(status=status, finished_at=finished_at, error_summary=error_summary)
    )
    return result.rowcount == 1


def latest_collection_run(db: Session) -> Optional[models.CollectionRun]:
    """가장 최근에 시작된 수집 실행 — 목록 응답의 collection_status 원천. (Issue #68)"""
    return db.execute(
        select(models.CollectionRun)
        .order_by(models.CollectionRun.started_at.desc())
        .limit(1)
    ).scalar_one_or_none()


def last_finished_collection_at(db: Session) -> Optional[datetime]:
    """마지막으로 종료된 수집 시각 — 목록 응답의 last_collected_at 원천. (Issue #68)"""
    return db.execute(
        select(func.max(models.CollectionRun.finished_at))
    ).scalar_one()


# --- Asset ---------------------------------------------------------------------


def get_asset_by_arn(db: Session, arn: str) -> Optional[models.Asset]:
    return db.execute(
        select(models.Asset).where(models.Asset.arn == arn)
    ).scalar_one_or_none()


def list_assets(
    db: Session, *, asset_type: Optional[AssetType] = None
) -> list[models.Asset]:
    stmt = select(models.Asset).order_by(models.Asset.arn)
    if asset_type is not None:
        stmt = stmt.where(models.Asset.asset_type == asset_type)
    return list(db.execute(stmt).scalars())


def upsert_asset(
    db: Session,
    *,
    arn: str,
    asset_type: AssetType,
    resource_id: str,
    account_id: str,
    region: str,
    spec: dict,
    collection_run_id: str,
    collected_at: datetime,
    name: Optional[str] = None,
    state: Optional[str] = None,
) -> models.Asset:
    """arn 기준 upsert. 수집 회차마다 최신 관측으로 덮어쓴다(이력은 MetricSummary·
    RuleEvaluation이 회차 단위로 보존)."""
    asset = get_asset_by_arn(db, arn)
    if asset is None:
        asset = models.Asset(
            arn=arn,
            asset_type=asset_type,
            resource_id=resource_id,
            account_id=account_id,
            region=region,
            spec=spec,
            last_collection_run_id=collection_run_id,
            collected_at=collected_at,
            name=name,
            state=state,
        )
        db.add(asset)
    else:
        asset.asset_type = asset_type
        asset.resource_id = resource_id
        asset.account_id = account_id
        asset.region = region
        asset.spec = spec
        asset.last_collection_run_id = collection_run_id
        asset.collected_at = collected_at
        asset.name = name
        asset.state = state
    db.flush()
    return asset


# --- AssetRelationship ---------------------------------------------------------


def replace_relationships(
    db: Session,
    source_asset_id: str,
    items: Sequence[tuple[RelationType, str]],
    *,
    collection_run_id: str,
) -> int:
    """이번 수집 관측으로 연결관계를 전량 교체한다(스냅샷 의미론)."""
    db.execute(
        delete(models.AssetRelationship).where(
            models.AssetRelationship.source_asset_id == source_asset_id
        )
    )
    for relation_type, target_arn in items:
        db.add(
            models.AssetRelationship(
                source_asset_id=source_asset_id,
                relation_type=relation_type,
                target_arn=target_arn,
                collection_run_id=collection_run_id,
            )
        )
    db.flush()
    return len(items)


def list_relationships_by_target(
    db: Session, target_arn: str
) -> list[models.AssetRelationship]:
    """역방향 조회 — 이 자산을 가리키는 연결(토폴로지 맵)."""
    return list(
        db.execute(
            select(models.AssetRelationship).where(
                models.AssetRelationship.target_arn == target_arn
            )
        ).scalars()
    )


def list_all_relationships(db: Session) -> list[models.AssetRelationship]:
    """전 자산의 정방향 연결 일괄 조회 — 목록 응답 조립용. 자산별 반복 질의를
    피하고 호출부가 source_asset_id로 묶는다. (Issue #68)"""
    return list(
        db.execute(
            select(models.AssetRelationship).order_by(
                models.AssetRelationship.source_asset_id,
                models.AssetRelationship.relation_type,
                models.AssetRelationship.target_arn,
            )
        ).scalars()
    )


# --- MetricSummary -------------------------------------------------------------


def add_metric_summary(
    db: Session,
    *,
    asset_id: str,
    collection_run_id: str,
    summary: MetricSummaryContract,
    window_start: datetime,
    window_end: datetime,
    collected_at: datetime,
) -> models.MetricSummary:
    row = models.MetricSummary(
        asset_id=asset_id,
        collection_run_id=collection_run_id,
        cpu_datapoints=summary.cpu_datapoints,
        cpu_avg=summary.cpu_avg,
        cpu_max=summary.cpu_max,
        net_in_avg=summary.net_in_avg,
        net_out_avg=summary.net_out_avg,
        window_start=window_start,
        window_end=window_end,
        collected_at=collected_at,
    )
    db.add(row)
    db.flush()
    return row


def fresh_ec2_metric_summaries(
    db: Session, *, region: str, not_older_than: datetime
) -> tuple[Optional[datetime], dict[str, MetricSummaryContract]]:
    """리전의 EC2 자산별 최신 메트릭 요약 중 not_older_than 이후에 수집된 것만 돌려준다.

    collector 가 CloudWatch 재조회를 건너뛸지 판단하는 근거다(#255) — 스캔 주기가 메트릭
    입자(METRIC_PERIOD_SECONDS)보다 짧으면 같은 값을 반복해서 받아오게 되기 때문이다.
    반환 = (재사용 대상 창의 끝, {resource_id: 요약}). 해당 행이 없으면 (None, {}).
    창의 끝을 함께 주는 이유는 persist 가 이 요약을 **원본 창** 으로 적재해야 해서다.
    """
    rows = db.execute(
        select(models.Asset.resource_id, models.MetricSummary)
        .join(
            models.MetricSummary,
            models.MetricSummary.asset_id == models.Asset.asset_id,
        )
        .where(
            models.Asset.region == region,
            models.Asset.asset_type == AssetType.EC2,
            models.MetricSummary.collected_at >= not_older_than,
        )
        .order_by(models.MetricSummary.collected_at.desc())
    ).all()

    out: dict[str, MetricSummaryContract] = {}
    window_end: Optional[datetime] = None
    for resource_id, row in rows:
        if resource_id in out:
            continue  # collected_at 내림차순이라 첫 행이 최신이다
        out[resource_id] = MetricSummaryContract(
            cpu_datapoints=row.cpu_datapoints,
            cpu_avg=row.cpu_avg,
            cpu_max=row.cpu_max,
            net_in_avg=row.net_in_avg,
            net_out_avg=row.net_out_avg,
        )
        if window_end is None or row.window_end > window_end:
            window_end = row.window_end
    return window_end, out


# --- RuleEvaluation ------------------------------------------------------------


def add_rule_evaluation(
    db: Session, contract: RuleEvaluationResult
) -> models.RuleEvaluation:
    """계약의 asset_arn을 asset_id로 해석해 저장한다. 자산 미존재 시 LookupError."""
    asset = get_asset_by_arn(db, contract.asset_arn)
    if asset is None:
        raise LookupError(f"자산이 없습니다: {contract.asset_arn}")
    row = mappers.new_rule_evaluation(contract, asset.asset_id)
    db.add(row)
    db.flush()
    return row


def latest_rule_evaluation(
    db: Session, asset_id: str
) -> Optional[models.RuleEvaluation]:
    return db.execute(
        select(models.RuleEvaluation)
        .where(models.RuleEvaluation.asset_id == asset_id)
        .order_by(models.RuleEvaluation.evaluated_at.desc())
        .limit(1)
    ).scalar_one_or_none()


def latest_rule_evaluation_by_asset(db: Session) -> dict[str, models.RuleEvaluation]:
    """자산별 최신 판정 1건 일괄 조회(PostgreSQL DISTINCT ON) — 자산마다
    latest_rule_evaluation()을 반복 호출하는 N+1을 피한다. (Issue #68)"""
    rows = db.execute(
        select(models.RuleEvaluation)
        .distinct(models.RuleEvaluation.asset_id)
        .order_by(
            models.RuleEvaluation.asset_id,
            models.RuleEvaluation.evaluated_at.desc(),
            models.RuleEvaluation.rule_evaluation_id.desc(),
        )
    ).scalars()
    return {row.asset_id: row for row in rows}
