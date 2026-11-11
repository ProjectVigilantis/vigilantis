# ==============================================================================
# [파일 설명]  담당: 김승철 (BE)
# 플랫폼 활동 로그의 「따로 남기기」 — 거절 · 실패를 요청 트랜잭션과 다른 짧은 트랜잭션으로 남긴다. (#589)
#
#   - 요청 트랜잭션이 되돌려져도 「거절됐다 · 실패했다」는 남아야 한다 — 그래서 자기 세션을 열고 commit 한다.
#     (상태 변경의 기록은 db/repositories/platform_events.record_platform_event 를 같은 트랜잭션에서 부른다.)
#   - DB 쓰기가 실패해도 예외를 올리지 않는다 — 이미 거절 · 실패 중인 응답을 막지 않는다. 대신 앱 로그
#     (stdout → CloudWatch)에 행동 · 결과 · 예외 종류만 남긴다(details 원문은 싣지 않는다).
#   - 계약 위반(행동 · 결과 · 행위자의 짝이 틀림)은 삼키지 않고 그대로 올린다 — 호출부 결함이라 그 경로를
#     지나는 테스트 하나면 드러난다. 삼키면 테스트에서도 운영에서도 기록이 소리 없이 빠진다(PR 리뷰).
# ==============================================================================

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any, Optional

from db.repositories.platform_events import EventActor, build_platform_event
from schemas.platform_events import PlatformEventAction, PlatformEventOutcome
from sqlalchemy.orm import Session

_logger = logging.getLogger("vigilantis.platform_events")


def record_detached(
    session_factory: Callable[[], Session],
    action: PlatformEventAction,
    actor: EventActor,
    *,
    outcome: PlatformEventOutcome,
    tenant_id: Optional[str] = None,
    target_id: Optional[str] = None,
    details: Optional[Mapping[str, Any]] = None,
    session_id: Optional[str] = None,
    occurred_at: Optional[datetime] = None,
) -> bool:
    """한 행을 따로 남긴다. 남겼으면 True, DB 쓰기가 실패하면 앱 로그에 남기고 False.

    계약 위반은 세션을 열기 전에 ValueError(pydantic)로 올린다."""
    row = build_platform_event(
        action=action,
        actor=actor,
        outcome=outcome,
        tenant_id=tenant_id,
        target_id=target_id,
        details=details,
        session_id=session_id,
        occurred_at=occurred_at,
    )
    try:
        with session_factory() as session:
            session.add(row)
            session.commit()
        return True
    except Exception as exc:  # noqa: BLE001 — 로그 쓰기 실패가 응답을 막지 않는다
        _logger.error(
            "platform_event_write_failed",
            extra={
                "event_action": getattr(action, "value", str(action)),
                "event_outcome": getattr(outcome, "value", str(outcome)),
                "error_type": type(exc).__name__,
            },
        )
        return False
