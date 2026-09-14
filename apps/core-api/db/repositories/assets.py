# ==============================================================================
# [파일 설명]
# 수집·자산·판정 저장소 — CollectionRun·Asset·AssetRelationship·MetricSummary·
# RuleEvaluation. (Issue #60)
# ==============================================================================

from __future__ import annotations

from datetime import datetime
from typing import Collection, Optional, Sequence

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


def latest_collection_run_per_region(
    db: Session, regions: Optional[Sequence[str]] = None
) -> list[models.CollectionRun]:
    """리전별로 가장 최근에 시작된 수집 실행을 1건씩. (Issue #231)

    C4(#222)로 수집이 리전 단위 독립 트랜잭션이 되면서 한 사이클에 CollectionRun 이
    리전 수만큼 생긴다. 전역 최신 1행만 보면 리전1 FAILED → 리전2 SUCCESS 순서일 때
    실패가 성공에 가려진다 — 화면이 READY 로 뜬다.

    ``regions`` 를 주면 그 리전들로 범위를 좁힌다(관제 대상 = 설정된 리전). 수집 대상에서
    빠진 리전의 옛 run 이 영구히 그 리전의 최신으로 남아 collection_status 를 붙잡던
    문제(#261)를 막는다. 세 필드(collection_status·items·last_collected_at)를 **같은
    리전 스코프로 함께 좁혀야** 응답 안에서 범위가 갈리지 않는다(PR #259 리뷰, 안성일).
    ``regions=None`` 이면 전 리전(수집기 등 내부 호출용).
    """
    stmt = (
        select(models.CollectionRun)
        .distinct(models.CollectionRun.region)  # DISTINCT ON — 리전별 첫 행
        .order_by(models.CollectionRun.region, models.CollectionRun.started_at.desc())
    )
    if regions is not None:
        stmt = stmt.where(models.CollectionRun.region.in_(regions))
    return list(db.execute(stmt).scalars().all())


def last_finished_collection_at(
    db: Session, regions: Optional[Sequence[str]] = None
) -> Optional[datetime]:
    """마지막으로 종료된 수집 시각 — 목록 응답의 last_collected_at 원천. (Issue #68)

    ``regions`` 를 주면 그 리전들로 좁힌다 — collection_status·items 와 같은 스코프를
    유지하기 위함이다(#261). ``None`` 이면 전 리전.
    """
    stmt = select(func.max(models.CollectionRun.finished_at))
    if regions is not None:
        stmt = stmt.where(models.CollectionRun.region.in_(regions))
    return db.execute(stmt).scalar_one()


# --- Asset ---------------------------------------------------------------------


def get_asset_by_arn(db: Session, arn: str) -> Optional[models.Asset]:
    return db.execute(
        select(models.Asset).where(models.Asset.arn == arn)
    ).scalar_one_or_none()


def list_assets(
    db: Session,
    *,
    asset_type: Optional[AssetType] = None,
    regions: Optional[Sequence[str]] = None,
    include_absent: bool = False,
) -> list[models.Asset]:
    """자산 목록. ``regions`` 를 주면 그 리전들로 좁힌다 — collection_status·
    last_collected_at 과 같은 스코프를 유지하기 위함이다(#261, ix_assets_region 활용).
    ``None`` 이면 전 리전.

    **소멸 자산(``absent_since`` 가 찍힌 행)은 기본으로 제외한다**(#332). 실물이 없는
    자산을 돌려주면 화면에 유령이 섞이는 데서 끝나지 않고, 판정이 낡은 메트릭으로 다시
    돌아 조치 후보까지 올라간다(rule_engine 은 이 함수의 결과를 판정 대상으로 읽는다).
    이력·감사 목적으로 소멸분까지 봐야 하면 ``include_absent=True`` 로 부른다."""
    stmt = select(models.Asset).order_by(models.Asset.arn)
    if asset_type is not None:
        stmt = stmt.where(models.Asset.asset_type == asset_type)
    if regions is not None:
        stmt = stmt.where(models.Asset.region.in_(regions))
    if not include_absent:
        stmt = stmt.where(models.Asset.absent_since.is_(None))
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
    RuleEvaluation이 회차 단위로 보존).

    이 함수가 불렸다는 것은 곧 **이번 회차에 관측됐다**는 뜻이므로 소멸 표시를 해제한다
    (#332) — 자산이 지워졌다 같은 ARN 으로 되살아나는 경우도 이 경로로 돌아온다."""
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
        asset.absent_since = None
    db.flush()
    return asset


def mark_absent_assets(
    db: Session,
    *,
    region: str,
    observed_arns: Collection[str],
    absent_at: datetime,
) -> list[str]:
    """이번 회차가 관측하지 못한 ``region`` 의 자산에 소멸 표시를 찍고 그 ARN 을 돌려준다.

    **호출 조건은 부르는 쪽이 지킨다** — 이 함수는 회차 상태를 보지 않는다. 수집이
    일부만 성공한 회차(PARTIAL)에서 부르면 못 본 것과 사라진 것을 구분하지 못해 멀쩡한
    자산이 화면에서 사라진다. collector 는 **SUCCESS 로 마감되는 회차에서만** 부른다
    (#221 이 세운 경계를 그대로 쓴다).

    스코프가 리전인 이유는 수집 단위가 리전이기 때문이다 — 다른 리전의 자산은 이번
    회차의 관측 범위 밖이라 판단 근거가 없다.

    이미 표시된 자산은 건드리지 않는다. 처음 사라진 시각을 보존해야 "언제부터 없었나"
    가 남고, 매 회차 값이 갱신되면 그 정보가 사라진다.

    ``observed_arns`` 가 비어 있으면 그 리전의 자산이 전부 표시된다. 그것이 맞는
    동작이다 — SUCCESS 로 마감된 회차가 아무것도 못 봤다면 AWS 쪽이 실제로 비어 있다.
    """
    stmt = select(models.Asset).where(
        models.Asset.region == region,
        models.Asset.absent_since.is_(None),
    )
    if observed_arns:
        stmt = stmt.where(models.Asset.arn.notin_(list(observed_arns)))

    # UPDATE 문 대신 ORM 객체에 값을 넣는다 — 같은 세션이 들고 있는 자산 객체가 바로
    # 최신이 되어, 호출부가 expire_all 왕복 없이 이어서 읽어도 어긋나지 않는다
    # (upsert_asset 과 같은 방식). 대상은 한 리전의 자산이라 건수가 작다.
    vanished = list(db.execute(stmt).scalars())
    for asset in vanished:
        asset.absent_since = absent_at
    db.flush()
    return [asset.arn for asset in vanished]


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


def list_relationships_by_source(
    db: Session, source_asset_id: str
) -> list[models.AssetRelationship]:
    """지정 자산의 정방향 관계만 조회한다."""
    return list(
        db.execute(
            select(models.AssetRelationship)
            .where(models.AssetRelationship.source_asset_id == source_asset_id)
            .order_by(
                models.AssetRelationship.relation_type,
                models.AssetRelationship.target_arn,
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


def get_metric_summary_for_run(
    db: Session, *, asset_arn: str, collection_run_id: str
) -> Optional[models.MetricSummary]:
    """자산 1건의 **그 회차** 메트릭 요약. 없으면 None.

    회차를 인자로 받는 것이 이 함수의 요점이다 — Intake가 나르는 판정·자산 스냅샷과
    같은 회차의 관측만 Incident 근거가 되어야 한다(schemas/intake.py 계약 원칙).
    자산별 최신 요약을 돌려주는 fresh_ec2_metric_summaries와 다른 자리다: 그쪽은
    CloudWatch 재조회를 건너뛸지 판단하는 신선도 기준이라 회차를 묻지 않는다.

    (asset_id, collection_run_id)에 유니크 제약이 있어(db/models.py MetricSummary)
    결과는 0건 또는 1건이다.
    """
    return db.execute(
        select(models.MetricSummary)
        .join(models.Asset, models.Asset.asset_id == models.MetricSummary.asset_id)
        .where(
            models.Asset.arn == asset_arn,
            models.MetricSummary.collection_run_id == collection_run_id,
        )
    ).scalar_one_or_none()


def fresh_ec2_metric_summaries(
    db: Session, *, region: str, not_older_than: datetime
) -> tuple[Optional[datetime], dict[str, MetricSummaryContract]]:
    """리전의 EC2 자산별 최신 메트릭 요약 중 not_older_than 이후 **창** 의 것만 돌려준다.

    collector 가 CloudWatch 재조회를 건너뛸지 판단하는 근거다(#255) — 스캔 주기가 메트릭
    입자(METRIC_PERIOD_SECONDS)보다 짧으면 같은 값을 반복해서 받아오게 되기 때문이다.
    반환 = (재사용 대상 창의 끝, {resource_id: 요약}). 해당 행이 없으면 (None, {}).
    창의 끝을 함께 주는 이유는 persist 가 이 요약을 **원본 창** 으로 적재해야 해서다.

    **신선도 기준은 window_end 이지 collected_at 이 아니다.** 재사용한 요약도 회차마다
    새 행으로 복사되면서 collected_at 에는 그 회차 시각이 찍힌다. collected_at 으로 재면
    복사본이 매번 "방금 수집됨"이 되어 기준이 무한히 연장되고, 최초 조회 이후 CloudWatch 를
    영영 다시 부르지 않는다 — 메트릭이 그 시점에 얼어붙는다. window_end 는 재사용해도
    원본 값이 보존되므로 **실제로 CloudWatch 를 부른 시각** 을 가리킨다.
    (PR #257 리뷰에서 안성일 지적)
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
            models.MetricSummary.window_end >= not_older_than,
        )
        .order_by(
            models.MetricSummary.window_end.desc(),
            models.MetricSummary.collected_at.desc(),
        )
    ).all()

    out: dict[str, MetricSummaryContract] = {}
    window_end: Optional[datetime] = None
    for resource_id, row in rows:
        if resource_id in out:
            continue  # window_end 내림차순이라 첫 행이 가장 최근 조회분이다
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
