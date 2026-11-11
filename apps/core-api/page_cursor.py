# ==============================================================================
# [파일 설명]  담당: 김승철 (BE)
# 최신순 기록 목록의 쪽 넘김 — 감사 기록(GET /audit-events) · 플랫폼 활동 로그(GET /platform-events)가 함께 쓴다.
#
#   - 커서는 직전 쪽 마지막 행의 (occurred_at, 행 ID) 를 감싼 불투명 문자열이다. FE 는 해석하지 않는다.
#     깨졌거나 시각에 시간대가 없거나 ID 가 UUID 가 아니면 422 REQUEST_VALIDATION_FAILED.
#   - 질의 시각(since · until)은 시간대가 있어야 한다 — 기록은 UTC 이고 해석을 서버가 추측하지 않는다.
# ==============================================================================

from __future__ import annotations

import base64
import binascii
import json
from datetime import datetime
from typing import Optional

from exceptions import ApiError
from identifiers import canonical_id
from schemas.api.errors import ErrorCode


def encode(occurred_at: datetime, row_id: str) -> str:
    raw = json.dumps([occurred_at.isoformat(), row_id])
    return base64.urlsafe_b64encode(raw.encode()).rstrip(b"=").decode()


def decode(cursor: str) -> tuple[datetime, str]:
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        occurred_at, row_id = json.loads(raw)
        occurred_at = datetime.fromisoformat(occurred_at)
        row_id = canonical_id(row_id)
    except (binascii.Error, UnicodeDecodeError, ValueError, TypeError, AttributeError):
        raise ApiError(ErrorCode.REQUEST_VALIDATION_FAILED) from None
    if occurred_at.tzinfo is None or row_id is None:
        raise ApiError(ErrorCode.REQUEST_VALIDATION_FAILED)
    return occurred_at, row_id


def aware(value: Optional[datetime]) -> Optional[datetime]:
    """질의 시각 — 시간대가 없으면 422."""
    if value is not None and value.tzinfo is None:
        raise ApiError(ErrorCode.REQUEST_VALIDATION_FAILED)
    return value
