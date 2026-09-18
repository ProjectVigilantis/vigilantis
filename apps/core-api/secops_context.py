# ==============================================================================
# [파일 설명]
# SecOps 모의 위협 접수 시 자산·직접 SG/NACL 관계 사본을 조립합니다. (Issue #350)
# 접수 때 확보한 자료를 보존하며, 분석 시점의 최신 Inventory로 대체하지 않습니다.
# 저장·전달 계약: datasets/secops-log-corpus/MVP_CONNECTION.md
# ==============================================================================

from __future__ import annotations

from datetime import UTC, datetime

from asset_mapping import to_asset_item
from db.repositories import assets as assets_repo
from schemas.api.assets import AssetType, RelationType
from schemas.evidence import (
    DetectionAssetSnapshot,
    SecOpsContextIssue,
    SecOpsEvidenceContext,
)
from schemas.mock_logs import MockSshLogEvidence
from sqlalchemy.orm import Session


class SecOpsContextLimitExceeded(ValueError):
    """직접 SG/NACL 관계가 MVP 근거 저장 상한을 넘은 접수 거부."""


def _unavailable(row) -> str | None:
    if row is None:
        return "not_collected"
    if row.absent_since is not None:
        return "absent"
    if not row.last_collection_run_id:
        return "missing_collection"
    return None


def capture_secops_context(
    db: Session, target_arn: str, log_evidence: MockSshLogEvidence | None,
) -> SecOpsEvidenceContext:
    rows = assets_repo.read_secops_context_rows(db, target_arn)
    captured_at = datetime.now(UTC)
    source = rows[0][0] if rows else None
    status = _unavailable(source)
    if status:
        return SecOpsEvidenceContext(captured_at=captured_at, target_status=status,
                                     log_evidence=log_evidence)
    if len(rows) > 64:
        raise SecOpsContextLimitExceeded("MVP SecOps context exceeds 64 direct SG/NACL relationships")
    selected, related, issues = [], [], []
    expected_types = {RelationType.SECURED_BY: AssetType.SG, RelationType.PROTECTED_BY: AssetType.NACL}
    for _, relation, target in rows:
        if relation is None:
            continue
        reason = _unavailable(target)
        if relation.collection_run_id != source.last_collection_run_id:
            reason = "relation_run_mismatch"
        elif reason is None and (
            source.asset_type is not AssetType.EC2
            or target.asset_type is not expected_types[relation.relation_type]
            or target.account_id != source.account_id or target.region != source.region
        ):
            reason = "type_or_scope_mismatch"
        if reason:
            issues.append(SecOpsContextIssue(target_arn=relation.target_arn, reason=reason))
            continue
        selected.append(relation)
        related.append(DetectionAssetSnapshot(
            collection_run_id=target.last_collection_run_id,
            asset=to_asset_item(target, [], None),
        ))
    return SecOpsEvidenceContext(
        captured_at=captured_at, target_status="available",
        target=DetectionAssetSnapshot(collection_run_id=source.last_collection_run_id,
                                      asset=to_asset_item(source, selected, None)),
        related_assets=related, relation_issues=issues, log_evidence=log_evidence,
    )
