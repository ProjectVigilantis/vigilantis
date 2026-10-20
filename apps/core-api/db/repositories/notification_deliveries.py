"""#493 PR2 채널별 발송 상태 저장소. commit하지 않는다 — 트랜잭션은 dispatcher가 소유한다.

- 시각(활성화·재시도·임대·관문)은 전부 DB now()로 쓰고 비교한다.
- 상태 전이는 조건부 UPDATE다. 종결 상태(SUCCEEDED·FAILED·BLOCKED)를 고르는 전이는 없다.
- 결과 확정(settle)은 claim_token·IN_FLIGHT가 맞을 때만 반영한다. 회수가 토큰을 지우므로 회수 뒤의
  늦은 결과는 막히고, 임대가 지났어도 회수 전에 돌아온 결과는 반영한다.
- notification_events(사건 사실)는 읽기만 한다.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from datetime import timedelta

from sqlalchemy import case, delete, exists, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from db import models

_D = models.NotificationDelivery
_R = models.NotificationRoute
_L = models.NotificationLane
_E = models.NotificationEvent
_NO_SYNC = {"synchronize_session": False}


def _after(seconds: float):
    return func.now() + timedelta(seconds=seconds)


# --- 현재 바인딩·관문 ---------------------------------------------------------


def ensure_lane(db: Session, channel: str) -> None:
    db.execute(
        insert(_L)
        .values(channel=channel, available_at=func.now(), cooldown_until=func.now())
        .on_conflict_do_nothing(index_elements=[_L.channel])
    )


def sync_routes(db: Session, routes: Iterable) -> None:
    """설정 바인딩을 현재 바인딩으로 맞춘다.

    같은 fingerprint·tenant는 epoch·activated_at을 유지한다. 바뀐 바인딩은 새 epoch와
    DB 시각 activated_at을 받고, 설정에서 빠진 바인딩은 지운다(재등록 시 새 epoch).
    routes는 aws_account_id·channel·tenant_id·fingerprint 속성을 가진 값이다.
    """
    keys = set()
    for route in routes:
        keys.add((route.aws_account_id, route.channel))
        stmt = insert(_R).values(
            aws_account_id=route.aws_account_id,
            channel=route.channel,
            tenant_id=route.tenant_id,
            fingerprint=route.fingerprint,
            route_epoch=str(uuid.uuid4()),
            activated_at=func.now(),
        )
        db.execute(stmt.on_conflict_do_update(
            index_elements=[_R.aws_account_id, _R.channel],
            set_={
                "tenant_id": stmt.excluded.tenant_id,
                "fingerprint": stmt.excluded.fingerprint,
                "route_epoch": stmt.excluded.route_epoch,
                "activated_at": stmt.excluded.activated_at,
            },
            where=or_(
                _R.fingerprint != stmt.excluded.fingerprint,
                _R.tenant_id != stmt.excluded.tenant_id,
            ),
        ))
    for account, channel in db.execute(select(_R.aws_account_id, _R.channel)).all():
        if (account, channel) not in keys:
            db.execute(delete(_R).where(_R.aws_account_id == account, _R.channel == channel))


def current_routes(db: Session, channel: str, accounts: Iterable[str]) -> dict[str, _R]:
    rows = db.scalars(select(_R).where(_R.channel == channel, _R.aws_account_id.in_(list(accounts))))
    return {row.aws_account_id: row for row in rows}


def get_route(db: Session, aws_account_id: str, channel: str) -> _R | None:
    return db.scalars(
        select(_R).where(_R.aws_account_id == aws_account_id, _R.channel == channel)
    ).first()


def acquire_lane(db: Session, channel: str, token: str, hold_seconds: float) -> bool:
    """간격 관문과 공급자 대기가 모두 지났으면 이 claim이 hold_seconds 동안(또는 해제 때까지) 잡는다."""
    result = db.execute(
        update(_L)
        .where(
            _L.channel == channel,
            _L.available_at <= func.now(),
            _L.cooldown_until <= func.now(),
        )
        .values(available_at=_after(hold_seconds), holder_token=token)
        .execution_options(**_NO_SYNC)
    )
    return result.rowcount == 1


def release_lane(
    db: Session, channel: str, token: str, *, interval_seconds: float, cooldown_seconds: float,
) -> None:
    """공급자 대기는 해제자와 무관하게 최댓값으로만 늘린다(줄이지 않는다).

    간격 관문은 보유자만 지금부터 interval 뒤에 연다. 보유를 잃은 늦은 결과는 현재 보유자의
    간격 관문을 건드리지 않는다.
    """
    held = _L.holder_token == token
    db.execute(
        update(_L)
        .where(_L.channel == channel)
        .values(
            available_at=case((held, _after(interval_seconds)), else_=_L.available_at),
            holder_token=case((held, None), else_=_L.holder_token),
            cooldown_until=func.greatest(_L.cooldown_until, _after(cooldown_seconds)),
        )
        .execution_options(**_NO_SYNC)
    )


# --- 발견·생성 ----------------------------------------------------------------


def undelivered_events(db: Session, channel: str, limit: int) -> list[_E]:
    """이 채널 행이 없는 사건(anti-join). cursor가 없어 늦게 commit된 사건도 찾는다."""
    missing = ~exists().where(_D.event_id == _E.event_id, _D.channel == channel)
    return list(db.scalars(
        select(_E).where(missing).order_by(_E.created_at, _E.event_id).limit(limit)
    ))


def new_delivery(
    *,
    event_id: str,
    channel: str,
    blocked_reason: str | None,
    tenant_id: str | None,
    aws_account_id: str | None,
    route_fingerprint: str | None,
    route_epoch: str | None,
) -> dict:
    pending = blocked_reason is None
    return {
        "delivery_id": str(uuid.uuid4()),
        "event_id": event_id,
        "channel": channel,
        "state": "PENDING" if pending else "BLOCKED",
        "blocked_reason": blocked_reason,
        "tenant_id": tenant_id,
        "aws_account_id": aws_account_id,
        "route_fingerprint": route_fingerprint,
        "route_epoch": route_epoch,
        "next_attempt_at": func.now() if pending else None,
        "finished_at": None if pending else func.now(),
    }


def insert_deliveries(db: Session, rows: list[dict]) -> int:
    """동시 발견자끼리는 ON CONFLICT DO NOTHING으로 (사건, 채널)당 1행만 남는다."""
    if not rows:
        return 0
    stmt = (
        insert(_D)
        .values(rows)
        .on_conflict_do_nothing(index_elements=[_D.event_id, _D.channel])
        .returning(_D.delivery_id)
    )
    return len(db.execute(stmt).all())


def list_by_event(db: Session, event_id: str) -> list[_D]:
    return list(db.scalars(select(_D).where(_D.event_id == event_id).order_by(_D.channel)))


# --- claim·회수·확정 ----------------------------------------------------------


def lock_next_due(db: Session, channel: str, *, due_only: bool) -> _D | None:
    """PENDING 1건을 잠근다. due_only=False는 기한 전 행도 고른다(보내지 않는 정리 전용)."""
    conditions = [_D.channel == channel, _D.state == "PENDING"]
    if due_only:
        conditions.append(_D.next_attempt_at <= func.now())
    return db.scalars(
        select(_D)
        .where(*conditions)
        .order_by(_D.next_attempt_at, _D.created_at)
        .limit(1)
        .with_for_update(skip_locked=True)
    ).first()


def lock_expired(db: Session, channel: str, limit: int) -> list[_D]:
    return list(db.scalars(
        select(_D)
        .where(_D.channel == channel, _D.state == "IN_FLIGHT", _D.lease_expires_at <= func.now())
        .order_by(_D.lease_expires_at)
        .limit(limit)
        .with_for_update(skip_locked=True)
    ))


def start_attempt(db: Session, delivery_id: str, token: str, lease_seconds: float) -> None:
    db.execute(
        update(_D)
        .where(_D.delivery_id == delivery_id, _D.state == "PENDING")
        .values(
            state="IN_FLIGHT",
            attempts=_D.attempts + 1,
            claim_token=token,
            lease_expires_at=_after(lease_seconds),
            next_attempt_at=None,
            updated_at=func.now(),
        )
        .execution_options(**_NO_SYNC)
    )


def block_pending(db: Session, delivery_id: str, reason: str) -> None:
    db.execute(
        update(_D)
        .where(_D.delivery_id == delivery_id, _D.state == "PENDING")
        .values(
            state="BLOCKED",
            blocked_reason=reason,
            next_attempt_at=None,
            finished_at=func.now(),
            updated_at=func.now(),
        )
        .execution_options(**_NO_SYNC)
    )


def exhaust_pending(db: Session, delivery_id: str, max_attempts: int) -> bool:
    """시도 한도에 이른 PENDING을 claim 없이 FAILED(EXHAUSTED)로 닫는다. 마지막 범주·코드는 유지한다."""
    result = db.execute(
        update(_D)
        .where(_D.delivery_id == delivery_id, _D.state == "PENDING", _D.attempts >= max_attempts)
        .values(
            state="FAILED",
            final_reason="EXHAUSTED",
            last_category=func.coalesce(_D.last_category, "UNKNOWN"),
            next_attempt_at=None,
            finished_at=func.now(),
            updated_at=func.now(),
        )
        .execution_options(**_NO_SYNC)
    )
    return result.rowcount == 1


def succeeded(receipt_kind: str, receipt_ref: str | None) -> dict:
    return {
        "state": "SUCCEEDED",
        "receipt_kind": receipt_kind,
        "receipt_ref": receipt_ref,
        "finished_at": func.now(),
    }


def retry(category: str, error_code: str | None, delay_seconds: float) -> dict:
    return {
        "state": "PENDING",
        "next_attempt_at": _after(delay_seconds),
        "last_category": category,
        "last_error_code": error_code,
    }


def failed(final_reason: str, category: str, error_code: str | None) -> dict:
    return {
        "state": "FAILED",
        "final_reason": final_reason,
        "last_category": category,
        "last_error_code": error_code,
        "finished_at": func.now(),
    }


def _close_attempt(db: Session, conditions: tuple, outcome: dict) -> bool:
    result = db.execute(
        update(_D)
        .where(_D.state == "IN_FLIGHT", *conditions)
        .values(claim_token=None, lease_expires_at=None, updated_at=func.now(), **outcome)
        .execution_options(**_NO_SYNC)
    )
    return result.rowcount == 1


def settle(db: Session, delivery_id: str, token: str, outcome: dict) -> bool:
    """전송 결과 반영. 토큰·IN_FLIGHT가 하나라도 다르면 반영하지 않는다(False).

    임대 만료는 조건이 아니다. 회수(recover_expired)·재claim이 토큰을 지우거나 바꾸므로 회수된
    시도의 결과는 토큰에서 막힌다. 회수와 겹치면 행 잠금을 먼저 잡은 쪽 하나만 반영된다 — 회수가
    먼저면 기다린 이 UPDATE가 회수 commit 뒤 조건을 다시 평가해 막히고(그 사건은 다시 보낼 수
    있다 — 최소 1회 전송), 결과가 먼저면 회수가 SKIP LOCKED로 건너뛴다.
    """
    return _close_attempt(db, (_D.delivery_id == delivery_id, _D.claim_token == token), outcome)


def recover_expired(db: Session, delivery_id: str, outcome: dict) -> bool:
    """임대가 만료된 시도를 닫는다(결과 미상). 그 사이 결과가 확정됐으면 False."""
    return _close_attempt(
        db, (_D.delivery_id == delivery_id, _D.lease_expires_at <= func.now()), outcome,
    )
