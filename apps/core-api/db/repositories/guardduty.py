# ==============================================================================
# [파일 설명]  담당: 김승철 (API 계약 · BE)
# GuardDuty 탐지 읽기 커서 저장소 — guardduty_finding_cursors (#588 C).
#
#   - commit하지 않는다. 트랜잭션은 호출부(guardduty_ingress)가 소유한다.
#   - 시각은 호출부가 넘긴다(시계를 읽지 않는다).
# ==============================================================================

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session

from db import models


def get_cursor(db: Session, *, tenant_id: str, region: str) -> Optional[datetime]:
    """다음 회차가 읽기 시작할 갱신 시각. 처음 읽는 리전이면 None."""
    row = db.get(models.GuardDutyFindingCursor, (tenant_id, region))
    return None if row is None else row.updated_since


def set_cursor(db: Session, *, tenant_id: str, region: str, updated_since: datetime) -> None:
    """커서를 옮긴다. 뒤로 가지 않는다 — 더 이른 값이 오면 그대로 둔다."""
    row = db.get(models.GuardDutyFindingCursor, (tenant_id, region))
    if row is None:
        db.add(models.GuardDutyFindingCursor(
            tenant_id=tenant_id, region=region, updated_since=updated_since,
        ))
    elif updated_since > row.updated_since:
        row.updated_since = updated_since
    db.flush()
