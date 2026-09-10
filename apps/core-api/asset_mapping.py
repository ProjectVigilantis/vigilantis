"""자산 ORM → AssetItem 변환. 목록 API와 Detection 스냅샷이 같은 표기를 쓴다."""

from __future__ import annotations

from schemas.api.assets import (
    _PRIMARY_TYPES,
    _RULE_TARGET_TYPES,
    AssetItem,
    EvaluationStatus,
    ResourceRole,
)
from schemas.rules import RuleEvaluationResult

from db import models


def to_asset_item(
    asset: models.Asset,
    relationships: list[models.AssetRelationship],
    evaluation: models.RuleEvaluation | RuleEvaluationResult | None,
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
