# ==============================================================================
# [파일 설명]
# GET /api/v1/assets — 자산 목록·연결관계·최신 판정 조회 라우터입니다. (Issue #68)
#
#   - 응답은 공개 계약 schemas.api.assets.AssetsResponse로만 직렬화한다.
#     계약 불변식(판정 상태 ↔ verdict/skip, spec ↔ asset_type)은 DTO가 검증한다.
#   - SQL은 db.repositories 경유 — 라우터는 응답 조립만 한다.
#   - 필터·페이지네이션 없음(전체 반환) — SSOT §API 계약.
# ==============================================================================

from __future__ import annotations

from collections import defaultdict
from typing import Optional

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

# _PRIMARY_TYPES·_RULE_TARGET_TYPES는 계약 모듈의 판정 대상 정의를 단일 원천으로
# 재사용한다 — 여기서 재정의하면 계약 개정 시 어긋난다
from schemas.api.assets import (
    _PRIMARY_TYPES,
    _RULE_TARGET_TYPES,
    AssetItem,
    AssetsResponse,
    CollectionStatus,
    EvaluationStatus,
    ResourceRole,
)
from schemas.collections import CollectionRunStatus

from config import get_aws_settings
from db import models
from db.repositories import assets as assets_repo
from db.session import get_db

router = APIRouter(prefix="/api/v1", tags=["assets"])

_COLLECTION_STATUS = {
    CollectionRunStatus.IN_PROGRESS: CollectionStatus.COLLECTING,
    CollectionRunStatus.SUCCESS: CollectionStatus.READY,
    CollectionRunStatus.PARTIAL: CollectionStatus.PARTIAL,
    CollectionRunStatus.FAILED: CollectionStatus.FAILED,
}

# 리전별 최신 run 을 하나로 접을 때의 우선순위 — 나쁜 쪽이 이긴다. (Issue #231)
# IN_PROGRESS 가 SUCCESS 보다 위인 이유: 아직 안 끝난 리전이 있는데 READY 로 확정하면
# 다음 순간 FAILED 로 뒤집힌다. 실패는 진행 중보다 위다 — 실패를 늦게 보여줄 이유가 없다.
_STATUS_SEVERITY = {
    CollectionRunStatus.SUCCESS: 0,
    CollectionRunStatus.IN_PROGRESS: 1,
    CollectionRunStatus.PARTIAL: 2,
    CollectionRunStatus.FAILED: 3,
}


def _worst_status(runs: list[models.CollectionRun]) -> CollectionRunStatus:
    """리전별 최신 run 들 중 가장 나쁜 상태. 빈 목록은 호출 전에 걸러야 한다."""
    return max((run.status for run in runs), key=_STATUS_SEVERITY.__getitem__)


def _configured_regions() -> list[str]:
    """관제 대상 리전 = 설정된 리전(AWS_REGIONS, 없으면 AWS_REGION). (#261)

    collection_status·items·last_collected_at 을 이 범위로 함께 좁혀 응답 안에서
    리전 범위가 갈리지 않게 한다. (테스트는 이 함수를 monkeypatch 로 대체한다)
    """
    return get_aws_settings().regions_list()


def _collection_status(
    runs: list[models.CollectionRun], regions: list[str]
) -> CollectionStatus:
    """설정 리전 스코프 안에서 collection_status 산출. (#261)

    설정 리전 중 아직 run 이 없는 리전이 있으면(최초 수집 대기·수집 중 모두 해당)
    '전체 관제 범위 수집 미완료'로 보아 COLLECTING 을 하한으로 깐다 — 기존 리전의
    SUCCESS 만으로 READY 를 주지 않는다. 단 IN_PROGRESS 는 심각도상 PARTIAL·FAILED
    아래라, 다른 리전의 실제 실패는 그대로 드러난다(안성일 확정).
    """
    worst = _worst_status(runs)
    covered = {run.region for run in runs}
    if not set(regions) <= covered:  # 아직 run 이 없는 설정 리전이 있음
        worst = max(
            worst, CollectionRunStatus.IN_PROGRESS, key=_STATUS_SEVERITY.__getitem__
        )
    return _COLLECTION_STATUS[worst]


def _to_item(
    asset: models.Asset,
    relationships: list[models.AssetRelationship],
    evaluation: Optional[models.RuleEvaluation],
) -> AssetItem:
    if asset.asset_type in _RULE_TARGET_TYPES:
        if evaluation is not None:
            evaluation_fields = {
                "evaluation_status": evaluation.evaluation_status,
                "verdict": evaluation.verdict,
                "health_score": evaluation.health_score,
                "skip_reason_code": evaluation.skip_reason_code,
            }
        else:
            # 판정 대상인데 판정 행이 아직 없음 — 계약상 PENDING
            evaluation_fields = {
                "evaluation_status": EvaluationStatus.PENDING,
                "verdict": None,
                "health_score": None,
                "skip_reason_code": None,
            }
    else:
        evaluation_fields = {
            "evaluation_status": EvaluationStatus.NOT_APPLICABLE,
            "verdict": None,
            "health_score": None,
            "skip_reason_code": None,
        }
    return AssetItem.model_validate(
        {
            "arn": asset.arn,
            "resource_id": asset.resource_id,
            "asset_type": asset.asset_type,
            "resource_role": (
                ResourceRole.PRIMARY
                if asset.asset_type in _PRIMARY_TYPES
                else ResourceRole.RUNBOOK_SUPPORT
            ),
            "name": asset.name,
            "account_id": asset.account_id,
            "region": asset.region,
            "state": asset.state,
            "spec": asset.spec,
            "relationships": [
                {"relation_type": rel.relation_type, "target_arn": rel.target_arn}
                for rel in relationships
            ],
            **evaluation_fields,
            "collected_at": asset.collected_at,
        }
    )


@router.get("/assets", response_model=AssetsResponse)
def get_assets(db: Session = Depends(get_db)) -> AssetsResponse:
    # 관제 대상 = 설정된 리전(AWS_REGIONS). 세 필드를 모두 이 범위로 좁혀 응답 안에서
    # 리전 범위가 갈리지 않게 한다 — 수집 대상서 빠진 리전의 옛 run 이 화면을 붙잡던
    # 문제를 막는다. (Issue #261, 안성일 확정) 리전별 최신 run 을 최악 상태로 접는
    # 것은 #231 그대로 — 리전 격리(C4) 이후 실패가 실행 순서에 가려지지 않게 한다.
    regions = _configured_regions()
    runs = assets_repo.latest_collection_run_per_region(db, regions=regions)
    if not runs:
        # 설정 리전 전체에 수집 이력이 없음 — 계약상 목록·last_collected_at도 비어야 한다
        return AssetsResponse(collection_status=CollectionStatus.NOT_COLLECTED)

    relationships: dict[str, list[models.AssetRelationship]] = defaultdict(list)
    for rel in assets_repo.list_all_relationships(db):
        relationships[rel.source_asset_id].append(rel)
    evaluations = assets_repo.latest_rule_evaluation_by_asset(db)

    items = [
        _to_item(asset, relationships.get(asset.asset_id, []), evaluations.get(asset.asset_id))
        for asset in assets_repo.list_assets(db, regions=regions)
    ]
    return AssetsResponse(
        collection_status=_collection_status(runs, regions),
        last_collected_at=assets_repo.last_finished_collection_at(db, regions=regions),
        items=items,
    )
