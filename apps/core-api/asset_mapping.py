# ==============================================================================
# [파일 설명]
# 자산 ORM → AssetItem 변환. 목록 API와 Detection 스냅샷이 같은 표기를 쓴다.
# 두 소비자가 공유하므로 스케줄러가 Router 내부를 import하지 않도록 분리한다.
# db/mappers.py는 ORM ↔ 내부 계약 변환만 맡고 외부 API DTO는 다루지 않는다.
# 공개 DTO인 AssetItem의 공유 변환은 그 경계 밖의 이 모듈에서 맡는다.
# ==============================================================================

from __future__ import annotations

# _PRIMARY_TYPES·_RULE_TARGET_TYPES는 계약의 판정 대상 정의를 단일 원천으로
# 재사용한다. 여기서 재정의하면 계약 개정 시 목록과 스냅샷의 표기가 어긋난다.
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
