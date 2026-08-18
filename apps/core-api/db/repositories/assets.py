# ==============================================================================
# [파일 설명]  담당: 안성일 (AI/Guardrail · Architect)
# 수집·자산·판정 저장소 — CollectionRun·Asset·AssetRelationship·MetricSummary·
# RuleEvaluation. (Issue #60)
# ==============================================================================

from __future__ import annotations

from datetime import datetime
from typing import Optional, Sequence

from sqlalchemy import delete, select, update
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
