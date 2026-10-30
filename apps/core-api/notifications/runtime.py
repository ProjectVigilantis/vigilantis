"""앱 수명주기와 발송 자원의 연결. 비활성 앱은 수신처·비밀값을 읽지 않는다.

비활성(NOTIFICATION_ENABLED=false)이면 런타임을 만들지 않는다 — 발송 · T-7 이메일 연결 관측 · 설정 자원
정리 · 관리 Slack 비밀 읽기가 모두 없다. 활성이면 고객 설정(DB)이 수신처 원천이고 두 채널 발신기를 항상
두므로, 새로 켠 채널도 재기동 없이 쓴다. 발신기 · 공급자 클라이언트는 첫 사용 때 AWS 에 붙는다.
"""

from __future__ import annotations

import logging

_logger = logging.getLogger("vigilantis.notifications")


class NotificationRuntime:
    def __init__(self, dispatcher, senders, reconciler=None) -> None:
        self.dispatcher = dispatcher
        self.senders = senders
        self.reconciler = reconciler
        self._closed = False

    async def start(self) -> None:
        try:
            await self.dispatcher.start()
            if self.reconciler is not None:
                await self.reconciler.start()
        except Exception:
            await self.stop()
            raise RuntimeError("NOTIFICATION_START_FAILED") from None

    async def stop(self) -> None:
        if self._closed:
            return
        try:
            try:
                if self.reconciler is not None:
                    await self.reconciler.stop()
            finally:
                await self.dispatcher.stop()
        finally:
            self._closed = True
            for sender in self.senders.values():
                try:
                    sender.close()
                except Exception:
                    # 예외에는 비밀 목적지나 SDK 원문이 들어갈 수 있다.
                    _logger.warning("notification_sender_close_failed")
            if self.reconciler is not None:
                try:
                    self.reconciler.close()
                except Exception:
                    _logger.warning("notification_sender_close_failed")


def warn_blocked_routes(registry) -> None:
    """선언됐지만 막힌 (계정, 채널)을 시작 시 한 번씩 경고한다. 라우팅이 이미 계산한 결과만 쓴다.

    계정·채널·사유 코드만 남긴다. 채널을 일부러 뺀 계정(CHANNEL_NOT_CONFIGURED)은 막힌 항목이 아니라 경고하지 않는다.
    """
    for (account, channel), reason in sorted(registry.blocked.items()):
        _logger.warning("notification_route_blocked", extra={
            "aws_account_id": account, "channel": channel or None, "reason": reason,
        })


def validate_notification_timing(settings) -> None:
    """backoff·lease 교차 규칙. 런타임 기동과 배포 스크립트가 같은 규칙을 쓴다."""
    if settings.NOTIFICATION_MAX_BACKOFF_SECONDS < settings.NOTIFICATION_BACKOFF_SECONDS:
        raise ValueError("NOTIFICATION_BACKOFF_INVALID")
    # 연결·응답 제한에 자격증명 갱신과 결과 저장 여유를 더한다.
    minimum_lease = 2 * (
        settings.NOTIFICATION_CONNECT_TIMEOUT_SECONDS
        + settings.NOTIFICATION_READ_TIMEOUT_SECONDS
    ) + 10
    if settings.NOTIFICATION_LEASE_SECONDS < minimum_lease:
        raise ValueError("NOTIFICATION_LEASE_TOO_SHORT")


def build_notification_runtime(settings) -> NotificationRuntime:
    """실제 연결은 명시적 활성화 후에만 만든다. 자동 migration은 하지 않는다.

    NOTIFICATION_ROUTES_FILE 은 선택이다 — 있으면 기동 때 아직 정하지 않은 고객 채널로만 가져온다
    (notifications.settings_source). 만드는 동안 DB · AWS 에 붙지 않는다.
    """
    if not settings.NOTIFICATION_ENABLED:
        raise ValueError("NOTIFICATION_DISABLED")
    validate_notification_timing(settings)

    from db.session import get_session_factory
    from notifications.channels import ManagedSlackSender, SlackSender, SnsSender
    from notifications.dispatcher import DeliveryDispatcher
    from notifications.routing import RouteRegistry, validate_public_base_url
    from notifications.settings_reconciler import SettingsReconciler
    from notifications.settings_source import SettingsRouteSource
    from services.aws.settings_notifications import SettingsResources

    public_base_url = validate_public_base_url(settings.NOTIFICATION_PUBLIC_BASE_URL)
    legacy = None
    if settings.NOTIFICATION_ROUTES_FILE:
        legacy = RouteRegistry.from_file(settings.NOTIFICATION_ROUTES_FILE)
        warn_blocked_routes(legacy)
    senders = {}
    resources = None
    try:
        timeout_options = {
            "connect_timeout": settings.NOTIFICATION_CONNECT_TIMEOUT_SECONDS,
            "read_timeout": settings.NOTIFICATION_READ_TIMEOUT_SECONDS,
        }
        resources = SettingsResources(**timeout_options)
        senders["SLACK"] = ManagedSlackSender(
            SlackSender(**timeout_options), resources.read_slack_secret,
        )
        senders["SNS"] = SnsSender(**timeout_options)
        sessions = get_session_factory()
        dispatcher = DeliveryDispatcher(
            sessions,
            SettingsRouteSource(legacy),
            senders,
            public_base_url=public_base_url,
            poll_seconds=settings.NOTIFICATION_POLL_SECONDS,
            batch_size=settings.NOTIFICATION_BATCH_SIZE,
            max_attempts=settings.NOTIFICATION_MAX_ATTEMPTS,
            backoff_seconds=settings.NOTIFICATION_BACKOFF_SECONDS,
            max_backoff_seconds=settings.NOTIFICATION_MAX_BACKOFF_SECONDS,
            lease_seconds=settings.NOTIFICATION_LEASE_SECONDS,
        )
        reconciler = SettingsReconciler(
            sessions,
            resources,
            poll_seconds=max(settings.NOTIFICATION_POLL_SECONDS, 5.0),
            lease_seconds=settings.NOTIFICATION_LEASE_SECONDS,
            backoff_seconds=settings.NOTIFICATION_BACKOFF_SECONDS,
            max_backoff_seconds=settings.NOTIFICATION_MAX_BACKOFF_SECONDS,
        )
    except Exception:
        for sender in senders.values():
            try:
                sender.close()
            except Exception:
                _logger.warning("notification_sender_close_failed")
        if resources is not None:
            resources.close()
        raise RuntimeError("NOTIFICATION_CONFIGURATION_FAILED") from None
    return NotificationRuntime(dispatcher, senders, reconciler)
