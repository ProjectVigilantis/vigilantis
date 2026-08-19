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
    latest_run = assets_repo.latest_collection_run(db)
    if latest_run is None:
        # 수집 이력이 전혀 없음 — 계약상 목록·last_collected_at도 비어 있어야 한다
        return AssetsResponse(collection_status=CollectionStatus.NOT_COLLECTED)

    relationships: dict[str, list[models.AssetRelationship]] = defaultdict(list)
    for rel in assets_repo.list_all_relationships(db):
        relationships[rel.source_asset_id].append(rel)
    evaluations = assets_repo.latest_rule_evaluation_by_asset(db)

    items = [
        _to_item(asset, relationships.get(asset.asset_id, []), evaluations.get(asset.asset_id))
        for asset in assets_repo.list_assets(db)
    ]
    return AssetsResponse(
        collection_status=_COLLECTION_STATUS[latest_run.status],
        last_collected_at=assets_repo.last_finished_collection_at(db),
        items=items,
    )
