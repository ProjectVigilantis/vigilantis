# ==============================================================================
# [파일 설명]  담당: 김승철 (API 계약 · BE)
# GuardDuty 탐지 읽기 커서 저장소 — guardduty_finding_cursors (#588 C).
#
#   - commit하지 않는다. 트랜잭션은 호출부(guardduty_ingress)가 소유한다.
#   - 시각은 호출부가 넘긴다(시계를 읽지 않는다).
#   - 커서를 옮기는 규칙(언제 순회가 끝났고 어디까지 옮겨도 되는가)은 호출부가 정한다. 여기는 그대로 적고,
#     updated_since 가 뒤로 가지 않는 것만 지킨다.
# ==============================================================================

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session

from db import models


@dataclass(frozen=True)
class PassProgress:
    """회차 한도에서 끊긴 목록 순회의 이어 읽을 자리."""

    next_token: str
    started_at: datetime
    watermark: Optional[datetime] = None
    hold: Optional[datetime] = None


@dataclass(frozen=True)
class Cursor:
    updated_since: datetime
    #: 끊긴 순회가 없으면 None — 다음 회차는 updated_since 부터 새 순회를 시작한다
    progress: Optional[PassProgress] = None


def get_cursor(db: Session, *, tenant_id: str, region: str) -> Optional[Cursor]:
    """다음 회차가 읽을 자리. 처음 읽는 리전이면 None."""
    row = db.get(models.GuardDutyFindingCursor, (tenant_id, region))
    if row is None:
        return None
    progress = None
    if row.pass_next_token is not None:
        progress = PassProgress(
            next_token=row.pass_next_token, started_at=row.pass_started_at,
            watermark=row.pass_watermark, hold=row.pass_hold,
        )
    return Cursor(updated_since=row.updated_since, progress=progress)


def save_cursor(
    db: Session, *, tenant_id: str, region: str, updated_since: datetime,
    progress: Optional[PassProgress] = None,
) -> None:
    """커서를 적는다. updated_since 는 뒤로 가지 않는다 — 더 이른 값이 오면 그대로 둔다.

    progress 가 없으면 순회가 끝난 것이다 — 이어 읽을 자리를 비운다."""
    row = db.get(models.GuardDutyFindingCursor, (tenant_id, region))
    if row is None:
        row = models.GuardDutyFindingCursor(
            tenant_id=tenant_id, region=region, updated_since=updated_since,
        )
        db.add(row)
    elif updated_since > row.updated_since:
        row.updated_since = updated_since
    row.pass_next_token = None if progress is None else progress.next_token
    row.pass_started_at = None if progress is None else progress.started_at
    row.pass_watermark = None if progress is None else progress.watermark
    row.pass_hold = None if progress is None else progress.hold
    db.flush()
