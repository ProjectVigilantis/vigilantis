"""#493 PR2: 채널 중립 사건 → (사건, 채널) 발송 행 → 외부 전송.

단계마다 짧은 별도 트랜잭션이며, 외부 호출 중에는 세션·트랜잭션을 열어 두지 않는다.

initialize  설정 바인딩을 notification_routes에 맞춘다. 같은 fingerprint는 epoch·activated_at을
            유지하고, 바인딩 변경이나 제거 후 재등록은 새 epoch와 DB 시각 activated_at을 받는다.
discover    채널 행이 없는 사건을 created_at 순으로 batch_size만큼 찾아 행을 만든다(anti-join,
            cursor 없음, ON CONFLICT DO NOTHING). 라우트 없음·충돌·발신기 없음·바인딩 불일치,
            created_at 또는 occurred_at이 activated_at보다 이른 사건은 BLOCKED로 끝난다.
            설정을 고치거나 더해도 BLOCKED를 되살리거나 과거 사건을 보내지 않는다.
process_one 만료 임대 회수 → PENDING 1건 정리 또는 claim → 전송 → 결과 확정. claim 전에
            현재 max_attempts에 이른 행은 보내지 않고 FAILED(EXHAUSTED)로 닫고(마지막 범주 유지,
            없으면 UNKNOWN), 발신기가 없는 채널의 행은 기한과 무관하게 BLOCKED(CHANNEL_NOT_CONFIGURED)로
            닫는다. 보내기 직전에 행의 바인딩 스냅숏, DB 현재 바인딩(epoch), 설정 해석, 사건 계정이
            모두 같아야 한다. 하나라도 다르면 BLOCKED(ROUTE_CHANGED)이며 다른 수신처로 옮겨 보내지 않는다.
            채널 스레드는 발신기가 없어도 돈다 — 채널을 뺀 뒤 재기동해도 남은 PENDING·IN_FLIGHT를
            외부 호출·새 시도 없이 정리한다.

복구: 시도 수는 claim 때 센다. claim 뒤나 공급자 수락 뒤 결과 저장 전에 멈춘 시도는 임대 만료 후
UNKNOWN으로 회수해 재시도하고, max_attempts에 이르면 FAILED(EXHAUSTED)로 닫는다. 따라서 같은 사건이
최대 max_attempts번 전송될 수 있다(정확히 1회 보장 없음). 결과는 claim_token·IN_FLIGHT·미만료 임대가
모두 맞을 때만 반영하고, 종결(SUCCEEDED·FAILED·BLOCKED)은 되돌리지 않는다. 재전송 기능은 없다.

Slack: 모든 처리 프로세스가 notification_lanes의 Slack 관문 1행을 공유한다. 간격 관문(available_at)은
호출 중 임대 길이만큼 닫히고 보유자가 결과 확정 뒤 1초 간격으로 연다. 429 Retry-After는 cooldown_until에
따로 남아 최댓값으로만 늘며(임대를 잃은 늦은 결과 포함), 보유자 해제·재기동으로 줄지 않는다.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from db.repositories import notification_deliveries as deliveries_repo
from db.repositories import notification_events as events_repo
from notifications.contracts import CHANNELS, Channel, EventSnapshot, Route, SendResult
from notifications.routing import CHANNEL_NOT_CONFIGURED
from sqlalchemy.orm import Session

logger = logging.getLogger("vigilantis.notifications.dispatcher")

ROUTE_CHANGED = "ROUTE_CHANGED"
NOT_ACTIVATED = "NOT_ACTIVATED"
LEASE_EXPIRED = "lease_expired"
SENDER_RAISED = "sender_raised"
RESULT_MISSING = "result_missing"
# 채널별 최소 호출 간격(초). 이 채널만 전역 관문을 쓴다.
PACED_CHANNELS: Mapping[str, float] = {"SLACK": 1.0}
_JOIN_POLL_SECONDS = 0.05


@dataclass(frozen=True)
class _Claim:
    delivery_id: str
    channel: Channel
    token: str
    attempts: int
    event: EventSnapshot
    route: Route


_CLOSED = object()  # 보내지 않고 종결(BLOCKED·EXHAUSTED)한 claim 결과


def _log_failure(name: str, channel: str | None, exc: BaseException) -> None:
    # 예외 문자열·traceback에는 SQL·파라미터·공급자 원문이 섞일 수 있어 형식만 남긴다.
    cause = getattr(exc, "orig", None)
    logger.error(name, extra={
        "channel": channel,
        "error_type": type(exc).__name__,
        "sqlstate": getattr(cause, "sqlstate", None),
    })


async def _join(threads: list[threading.Thread]) -> None:
    """스레드가 모두 끝난 뒤에만 돌아온다. 그 사이 취소되면 끝난 뒤 취소를 다시 올린다."""
    cancelled = False
    while any(thread.is_alive() for thread in threads):
        try:
            await asyncio.sleep(_JOIN_POLL_SECONDS)
        except asyncio.CancelledError:
            cancelled = True
    if cancelled:
        raise asyncio.CancelledError


class DeliveryDispatcher:
    def __init__(
        self,
        session_factory: Callable[[], Session],
        registry,
        senders: Mapping[str, object],
        *,
        public_base_url: str,
        poll_seconds: float = 1,
        batch_size: int = 20,
        max_attempts: int = 5,
        backoff_seconds: float = 2,
        max_backoff_seconds: float = 300,
        lease_seconds: float = 60,
    ) -> None:
        self._sessions = session_factory
        self._registry = registry
        self._senders = dict(senders)
        self._public_base_url = public_base_url
        self._poll_seconds = poll_seconds
        self._batch_size = batch_size
        self._max_attempts = max_attempts
        self._backoff_seconds = backoff_seconds
        self._max_backoff_seconds = max_backoff_seconds
        self._lease_seconds = lease_seconds
        self._stopping = threading.Event()
        self._threads: list[threading.Thread] = []

    # --- 수명주기 ---------------------------------------------------------------

    async def start(self) -> None:
        """바인딩 등록 뒤 채널마다 스레드 1개를 띄운다. 실패 시 띄운 스레드를 모두 내린다."""
        if self._threads:
            raise RuntimeError("NOTIFICATION_DISPATCHER_ALREADY_STARTED")
        self._stopping.clear()
        failures: list[BaseException] = []

        def initialize() -> None:
            try:
                self.initialize()
            except Exception as exc:  # noqa: BLE001 — 형식만 기록하고 고정 문구로 올린다
                failures.append(exc)

        init = threading.Thread(target=initialize, name="notification-initialize", daemon=True)
        init.start()
        await _join([init])
        if failures:
            _log_failure("notification_dispatcher_initialize_failed", None, failures.pop())
            raise RuntimeError("NOTIFICATION_DISPATCHER_INITIALIZE_FAILED")
        try:
            for channel in CHANNELS:
                worker = threading.Thread(
                    target=self._run, args=(channel,),
                    name=f"notification-{channel.lower()}", daemon=True,
                )
                worker.start()
                self._threads.append(worker)
        except BaseException:
            await self.stop()
            raise
        logger.info("notification_dispatcher_started", extra={
            "channels": sorted(self._senders), "poll_seconds": self._poll_seconds,
        })

    async def stop(self) -> None:
        """새 claim을 멈추고 진행 중인 전송의 결과 저장까지 기다린다(취소돼도 끝까지 기다린다)."""
        self._stopping.set()
        try:
            await _join(self._threads)
        finally:
            self._threads = []

    def _run(self, channel: Channel) -> None:
        while not self._stopping.is_set():
            try:
                self.discover(channel)
                for _ in range(self._batch_size):
                    if self._stopping.is_set() or not self.process_one(channel):
                        break
            except Exception as exc:  # noqa: BLE001 — 한 회차 실패가 채널 스레드를 끝내지 않는다
                _log_failure("notification_delivery_loop_failed", channel, exc)
            self._stopping.wait(self._poll_seconds)

    # --- 결정적 단계(테스트·스레드 공용) -------------------------------------------

    def initialize(self) -> None:
        with self._sessions() as db:
            for channel in PACED_CHANNELS:
                deliveries_repo.ensure_lane(db, channel)
            deliveries_repo.sync_routes(db, self._registry.routes)
            db.commit()

    def discover(self, channel: Channel | None = None) -> int:
        """채널마다 최대 batch_size개 사건의 행을 만든다. 만든 행 수를 돌려준다."""
        created = 0
        for current in CHANNELS if channel is None else (channel,):
            with self._sessions() as db:
                events = [
                    EventSnapshot.from_row(row)
                    for row in deliveries_repo.undelivered_events(db, current, self._batch_size)
                ]
                if not events:
                    continue
                bindings = deliveries_repo.current_routes(
                    db, current, {e.aws_account_id for e in events if e.aws_account_id},
                )
                rows = [self._materialize(event, current, bindings) for event in events]
                created += deliveries_repo.insert_deliveries(db, rows)
                db.commit()
        return created

    def _materialize(self, event: EventSnapshot, channel: Channel, bindings) -> dict:
        decision = self._registry.resolve(event, channel)
        route = decision.route
        binding = bindings.get(event.aws_account_id) if route is not None else None
        if route is None:
            reason = decision.blocked_reason
        elif channel not in self._senders:
            reason = CHANNEL_NOT_CONFIGURED
        elif (
            binding is None
            or binding.fingerprint != route.fingerprint
            or binding.tenant_id != route.tenant_id
        ):
            reason = ROUTE_CHANGED
        elif event.created_at < binding.activated_at or event.occurred_at < binding.activated_at:
            reason = NOT_ACTIVATED
        else:
            reason = None
        bound = reason is None or reason == NOT_ACTIVATED
        return deliveries_repo.new_delivery(
            event_id=event.event_id,
            channel=channel,
            blocked_reason=reason,
            tenant_id=route.tenant_id if bound else None,
            aws_account_id=event.aws_account_id,
            route_fingerprint=route.fingerprint if bound else None,
            route_epoch=binding.route_epoch if bound else None,
        )

    def process_one(self, channel: Channel) -> bool:
        """만료 임대를 회수하고 PENDING 1건을 처리한다. 할 일이 없거나 관문이 닫혀 있으면 False.

        발신기가 없는 채널은 회수·종결만 하고 보내지 않는다(claim은 발신기가 있을 때만 생긴다).
        """
        self._recover(channel)
        claim = self._claim(channel)
        if claim is None:
            return False
        if claim is _CLOSED:
            return True
        try:
            result = self._senders[channel].send(claim.event, claim.route, self._public_base_url)
        except Exception as exc:  # noqa: BLE001 — 전송 여부를 알 수 없다
            _log_failure("notification_sender_raised", channel, exc)
            result = SendResult(failure_category="UNKNOWN", error_code=SENDER_RAISED)
        self._finish(claim, result)
        return True

    def _recover(self, channel: Channel) -> None:
        with self._sessions() as db:
            expired = [
                (row.delivery_id, row.attempts)
                for row in deliveries_repo.lock_expired(db, channel, self._batch_size)
            ]
            for delivery_id, attempts in expired:
                deliveries_repo.recover_expired(
                    db, delivery_id, self._retry_or_exhaust(attempts, "UNKNOWN", LEASE_EXPIRED, 0),
                )
            db.commit()
        for delivery_id, attempts in expired:
            logger.warning("notification_delivery_lease_expired", extra={
                "delivery_id": delivery_id, "channel": channel, "attempts": attempts,
            })

    def _claim(self, channel: Channel):
        with self._sessions() as db:
            has_sender = channel in self._senders
            # 보내지 않는 정리는 관문·기한을 기다릴 이유가 없다
            row = deliveries_repo.lock_next_due(db, channel, due_only=has_sender)
            if row is None:
                return None
            delivery_id, attempts = row.delivery_id, row.attempts
            if attempts >= self._max_attempts:
                # 재기동으로 한도가 낮아져도 현재 한도를 넘겨 보내지 않는다(시도 수 증가 없음)
                deliveries_repo.exhaust_pending(db, delivery_id, self._max_attempts)
                db.commit()
                logger.warning("notification_delivery_exhausted", extra={
                    "delivery_id": delivery_id, "channel": channel, "attempts": attempts,
                })
                return _CLOSED
            if not has_sender:
                reason = CHANNEL_NOT_CONFIGURED
            else:
                event = EventSnapshot.from_row(events_repo.get(db, row.event_id))
                decision = self._registry.resolve(event, channel)
                current = deliveries_repo.get_route(db, row.aws_account_id, channel)
                reason = _binding_problem(row, event, decision, current)
            if reason is not None:
                deliveries_repo.block_pending(db, delivery_id, reason)
                db.commit()
                logger.warning("notification_delivery_blocked", extra={
                    "delivery_id": delivery_id, "channel": channel, "reason": reason,
                })
                return _CLOSED
            token = str(uuid.uuid4())
            if channel in PACED_CHANNELS and not deliveries_repo.acquire_lane(
                db, channel, token, self._lease_seconds,
            ):
                db.rollback()
                return None
            deliveries_repo.start_attempt(db, delivery_id, token, self._lease_seconds)
            db.commit()
        return _Claim(delivery_id, channel, token, attempts + 1, event, decision.route)

    def _finish(self, claim: _Claim, result: SendResult) -> None:
        cooldown = result.retry_after_seconds or 0.0
        if result.succeeded:
            outcome = deliveries_repo.succeeded(
                result.receipt_kind, result.receipt_ref[:256] if result.receipt_ref else None,
            )
        else:
            category = result.failure_category or "UNKNOWN"
            code = (result.error_code or RESULT_MISSING)[:64]
            outcome = (
                deliveries_repo.failed("PERMANENT", category, code)
                if category == "PERMANENT"
                else self._retry_or_exhaust(claim.attempts, category, code, cooldown)
            )
        with self._sessions() as db:
            applied = deliveries_repo.settle(db, claim.delivery_id, claim.token, outcome)
            interval = PACED_CHANNELS.get(claim.channel)
            if interval is not None:
                deliveries_repo.release_lane(
                    db, claim.channel, claim.token,
                    interval_seconds=interval, cooldown_seconds=cooldown,
                )
            db.commit()
        logger.info(
            "notification_delivery_settled" if applied else "notification_delivery_result_fenced",
            extra={
                "delivery_id": claim.delivery_id, "channel": claim.channel,
                "attempts": claim.attempts, "state": outcome["state"],
                "category": result.failure_category, "error_code": result.error_code,
            },
        )

    def _retry_or_exhaust(
        self, attempts: int, category: str, code: str | None, retry_after: float,
    ) -> dict:
        if attempts >= self._max_attempts:
            return deliveries_repo.failed("EXHAUSTED", category, code)
        delay = min(self._backoff_seconds * 2 ** (attempts - 1), self._max_backoff_seconds)
        # 공급자가 요구한 대기(Retry-After)는 상한으로 자르지 않는다 — 일찍 다시 보내지 않는다.
        return deliveries_repo.retry(category, code, max(delay, retry_after))


def _binding_problem(row, event: EventSnapshot, decision, current) -> str | None:
    """보내기 직전 대조: 행 스냅숏 = DB 현재 바인딩(epoch) = 설정 해석 = 사건 계정."""
    if decision.route is None:
        return decision.blocked_reason
    route = decision.route
    same = (
        current is not None
        and current.route_epoch == row.route_epoch
        and current.fingerprint == row.route_fingerprint == route.fingerprint
        and current.tenant_id == row.tenant_id == route.tenant_id
        and event.aws_account_id == row.aws_account_id == route.aws_account_id
    )
    return None if same else ROUTE_CHANGED
