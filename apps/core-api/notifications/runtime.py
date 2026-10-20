"""앱 수명주기와 발송 자원의 연결. 비활성 앱은 수신처·비밀값을 읽지 않는다."""

from __future__ import annotations

import logging

_logger = logging.getLogger("vigilantis.notifications")


class NotificationRuntime:
    def __init__(self, dispatcher, senders) -> None:
        self.dispatcher = dispatcher
        self.senders = senders
        self._closed = False

    async def start(self) -> None:
        try:
            await self.dispatcher.start()
        except Exception:
            await self.stop()
            raise RuntimeError("NOTIFICATION_START_FAILED") from None

    async def stop(self) -> None:
        if self._closed:
            return
        try:
            await self.dispatcher.stop()
        finally:
            self._closed = True
            for sender in self.senders.values():
                try:
                    sender.close()
                except Exception:
                    # 예외에는 비밀 목적지나 SDK 원문이 들어갈 수 있다.
                    _logger.warning("notification_sender_close_failed")


def build_notification_runtime(settings) -> NotificationRuntime:
    """실제 연결은 명시적 활성화 후에만 만든다. 자동 migration은 하지 않는다."""
    if not settings.NOTIFICATION_ENABLED:
        raise ValueError("NOTIFICATION_DISABLED")
    if not settings.NOTIFICATION_ROUTES_FILE:
        raise ValueError("NOTIFICATION_ROUTES_REQUIRED")
    if settings.NOTIFICATION_MAX_BACKOFF_SECONDS < settings.NOTIFICATION_BACKOFF_SECONDS:
        raise ValueError("NOTIFICATION_BACKOFF_INVALID")
    # 연결·응답 제한에 자격증명 갱신과 결과 저장 여유를 더한다.
    minimum_lease = 2 * (
        settings.NOTIFICATION_CONNECT_TIMEOUT_SECONDS
        + settings.NOTIFICATION_READ_TIMEOUT_SECONDS
    ) + 10
    if settings.NOTIFICATION_LEASE_SECONDS < minimum_lease:
        raise ValueError("NOTIFICATION_LEASE_TOO_SHORT")

    from db.session import get_session_factory
    from notifications.channels import SlackSender, SnsSender
    from notifications.dispatcher import DeliveryDispatcher
    from notifications.routing import RouteRegistry, validate_public_base_url

    public_base_url = validate_public_base_url(settings.NOTIFICATION_PUBLIC_BASE_URL)
    registry = RouteRegistry.from_file(settings.NOTIFICATION_ROUTES_FILE)
    senders = {}
    try:
        channels = {route.channel for route in registry.routes}
        timeout_options = {
            "connect_timeout": settings.NOTIFICATION_CONNECT_TIMEOUT_SECONDS,
            "read_timeout": settings.NOTIFICATION_READ_TIMEOUT_SECONDS,
        }
        if "SLACK" in channels:
            senders["SLACK"] = SlackSender(**timeout_options)
        if "SNS" in channels:
            senders["SNS"] = SnsSender(**timeout_options)
        dispatcher = DeliveryDispatcher(
            get_session_factory(),
            registry,
            senders,
            public_base_url=public_base_url,
            poll_seconds=settings.NOTIFICATION_POLL_SECONDS,
            batch_size=settings.NOTIFICATION_BATCH_SIZE,
            max_attempts=settings.NOTIFICATION_MAX_ATTEMPTS,
            backoff_seconds=settings.NOTIFICATION_BACKOFF_SECONDS,
            max_backoff_seconds=settings.NOTIFICATION_MAX_BACKOFF_SECONDS,
            lease_seconds=settings.NOTIFICATION_LEASE_SECONDS,
        )
    except Exception:
        for sender in senders.values():
            try:
                sender.close()
            except Exception:
                _logger.warning("notification_sender_close_failed")
        raise RuntimeError("NOTIFICATION_CONFIGURATION_FAILED") from None
    return NotificationRuntime(dispatcher, senders)
