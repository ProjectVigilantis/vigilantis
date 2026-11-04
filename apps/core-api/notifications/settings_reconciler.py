"""T-7 이메일 연결 관측과 은퇴 자원 정리 — 알림 런타임(NOTIFICATION_ENABLED)과 함께만 돈다.

단계마다 짧은 트랜잭션이고 SDK 호출 중에는 세션을 열어 두지 않는다.

이메일(세대 1개 = 관리 토픽 1개): SETUP_PENDING → CreateTopic → Subscribe(email, 실제 구독 ARN) →
CONFIRMATION_PENDING → (수신자 확인) → READY. 한 claim 은 한 단계이고 그 SDK 호출 직전에 하나씩 잡는다(확인
판정은 구독 속성 조회와 토픽의 구독 목록 — 저장한 구독 ARN 이 목록에 확인된 구독으로 있어야 READY 다).
결과는 세대 · 임대 토큰 CAS 와 임대 만료(잠금을 기다린 뒤의 DB 시계)로 막는다 — 그 사이 주소가 바뀌거나 재시도로
세대가 바뀌었거나 임대가 끝났으면 늦은 결과는 버려지고, 은퇴한 세대의 토픽은 저장 때 넣은 정리 대기열이 임대 만료
뒤 지운다(확인 대기 구독도 함께).
READY 는 주기적으로 구독을 다시 보며, 해지 · 소실이면 SETUP_FAILED 로 내리고 같은 트랜잭션에서 라우트를 뺀다.
READY 가 될 때 라우트가 새 epoch 로 생기므로 그 전 사건은 NOT_ACTIVATED 다(소급 발송 없음).
실패 코드는 고정 문자열이고 주소 · ARN · 원문 오류는 로그에 남기지 않는다.

정리: 대기열에서 차례가 된 자원을 지운다. 없는 자원은 끝난 것으로 보고, 실패는 backoff 로 다시 한다.
"""

from __future__ import annotations

import logging
import os
import threading
import uuid
from collections.abc import Callable, Mapping
from datetime import timedelta

from db.repositories import notification_deliveries as deliveries_repo
from db.repositories import tenant_settings as settings_repo
from services.aws.settings_notifications import SettingsProviderError, topic_name
from sqlalchemy import func
from sqlalchemy.orm import Session

from .dispatcher import _join, _log_failure
from .settings_source import sync_tenant

logger = logging.getLogger("vigilantis.notifications.settings")

SETUP_RETRY_EXHAUSTED = "SETUP_RETRY_EXHAUSTED"
PROVIDER_REJECTED = "PROVIDER_REJECTED"
CONFIRMATION_EXPIRED = "CONFIRMATION_EXPIRED"
SUBSCRIPTION_REMOVED = "SUBSCRIPTION_REMOVED"
SUBSCRIPTION_MISMATCH = "SUBSCRIPTION_MISMATCH"


class SettingsReconciler:
    def __init__(
        self,
        session_factory: Callable[[], Session],
        resources,
        *,
        poll_seconds: float = 5.0,
        batch_size: int = 10,
        lease_seconds: float = 120.0,
        confirm_poll_seconds: float = 60.0,
        ready_check_seconds: float = 900.0,
        max_setup_attempts: int = 8,
        backoff_seconds: float = 2.0,
        max_backoff_seconds: float = 300.0,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self._sessions = session_factory
        self._resources = resources
        self._poll_seconds = poll_seconds
        self._batch_size = batch_size
        self._lease_seconds = lease_seconds
        self._confirm_poll_seconds = confirm_poll_seconds
        self._ready_check_seconds = ready_check_seconds
        self._max_setup_attempts = max_setup_attempts
        self._backoff_seconds = backoff_seconds
        self._max_backoff_seconds = max_backoff_seconds
        self._environ = os.environ if environ is None else environ
        self._stopping = threading.Event()
        self._thread: threading.Thread | None = None

    # --- 수명주기 ---------------------------------------------------------------

    async def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("SETTINGS_RECONCILER_ALREADY_STARTED")
        self._stopping.clear()
        self._thread = threading.Thread(
            target=self._run, name="notification-settings", daemon=True,
        )
        self._thread.start()

    async def stop(self) -> None:
        """진행 중 단계의 결과 저장까지 기다린다(취소돼도 끝까지)."""
        self._stopping.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            await _join([thread])

    def close(self) -> None:
        self._resources.close()

    def _run(self) -> None:
        while not self._stopping.is_set():
            try:
                self.run_once()
            except Exception as exc:  # noqa: BLE001 — 한 회차 실패가 스레드를 끝내지 않는다
                _log_failure("notification_settings_loop_failed", None, exc)
            self._stopping.wait(self._poll_seconds)

    def run_once(self) -> int:
        return self.reconcile_emails() + self.run_cleanups()

    def _backoff(self, attempts: int) -> float:
        return min(self._backoff_seconds * 2 ** max(attempts - 1, 0), self._max_backoff_seconds)

    # --- 이메일 -----------------------------------------------------------------

    def reconcile_emails(self) -> int:
        """한 단계씩 — 임대는 SDK 호출 직전에 하나만 잡는다(묶음 임대가 차례를 기다리며 만료되지 않게)."""
        processed = 0
        while processed < self._batch_size and not self._stopping.is_set():
            claim = self._claim_next_email()
            if claim is None:
                break
            values, delay = self._observe(claim)
            self._apply(claim, values, delay)
            processed += 1
        return processed

    def _claim_next_email(self):
        with self._sessions() as db:
            claim = None
            for tenant_id in settings_repo.due_email_tenants(db, 1):
                claim = settings_repo.claim_email(
                    db, tenant_id, str(uuid.uuid4()), self._lease_seconds,
                )
            db.commit()
        return claim

    def _failed(self, code: str) -> tuple[dict, float | None]:
        return {
            "email_status": "SETUP_FAILED", "email_failure_code": code, "email_attempts": 0,
        }, None

    def _observe(self, claim) -> tuple[dict, float | None]:
        """SDK 1회 → (바꿀 칸, 다음 관측까지 초 또는 None=멈춤)."""
        resources = self._resources
        try:
            if claim.status == "SETUP_PENDING":
                if claim.topic_arn is None:
                    arn = resources.create_topic(
                        region=claim.region, name=topic_name(claim.tenant_id, claim.generation),
                    )
                    return {"email_topic_arn": arn, "email_attempts": 0}, 0
                subscription = resources.subscribe_email(
                    topic_arn=claim.topic_arn, address=claim.address,
                )
                return {
                    "email_subscription_arn": subscription,
                    "email_status": "CONFIRMATION_PENDING",
                    "email_attempts": 0,
                }, self._confirm_poll_seconds
            state = resources.subscription_state(
                subscription_arn=claim.subscription_arn, topic_arn=claim.topic_arn,
                address=claim.address,
            )
        except SettingsProviderError as exc:
            return self._provider_failure(claim, exc)
        if state == "CONFIRMED":
            return {"email_status": "READY", "email_attempts": 0}, self._ready_check_seconds
        if state == "PENDING" and claim.status == "CONFIRMATION_PENDING":
            return {"email_attempts": 0}, self._confirm_poll_seconds
        if state == "MISSING":
            return self._failed(
                CONFIRMATION_EXPIRED if claim.status == "CONFIRMATION_PENDING" else SUBSCRIPTION_REMOVED
            )
        return self._failed(SUBSCRIPTION_MISMATCH)

    def _provider_failure(self, claim, exc: SettingsProviderError) -> tuple[dict, float | None]:
        logger.warning("notification_email_setup_step_failed", extra={
            "status": claim.status, "category": exc.category, "error_code": exc.code,
        })
        if exc.category == "NOT_FOUND" and claim.status == "SETUP_PENDING":
            # 토픽이 밖에서 지워졌다 — 같은 세대 이름으로 다시 만든다
            return {"email_topic_arn": None, "email_attempts": claim.attempts + 1}, 0
        if exc.category == "PERMANENT" and claim.status != "READY":
            return self._failed(PROVIDER_REJECTED)
        attempts = claim.attempts + 1
        if claim.status == "SETUP_PENDING" and attempts >= self._max_setup_attempts:
            return self._failed(SETUP_RETRY_EXHAUSTED)
        # 확인 대기 · READY 의 일시 오류는 상태를 바꾸지 않는다(READY 라우트를 관측 실패로 빼지 않는다)
        return {"email_attempts": attempts}, self._backoff(attempts)

    def _apply(self, claim, values: dict, delay: float | None) -> None:
        with self._sessions() as db:
            settings_repo.lock_notification_config(db)
            record = settings_repo.lock_email_claim(db, claim)
            # 세대 · 토큰이 같아도 임대가 끝났으면 버린다 — 잠금을 기다린 뒤의 DB 시계로 본다. 만료된 임대는
            # 다시 잡혀(새 토큰) 그 단계를 다시 한다
            if record is None or not settings_repo.lease_alive(db, record):
                db.rollback()
                logger.info("notification_email_result_fenced", extra={
                    "status": claim.status, "reason": "STALE" if record is None else "LEASE_EXPIRED",
                })
                return
            was_ready = record.email_status == "READY"
            for name, value in values.items():
                setattr(record, name, value)
            if record.email_status == "SETUP_FAILED":
                record.email_next_check_at = None
            else:
                record.email_next_check_at = func.clock_timestamp() + timedelta(seconds=delay or 0)
            record.email_checked_at = func.clock_timestamp()
            settings_repo.release_email_lease(record)
            db.flush()
            if (record.email_status == "READY") != was_ready:
                account = deliveries_repo.tenant_accounts(db, (claim.tenant_id,))[claim.tenant_id]
                sync_tenant(db, claim.tenant_id, account, record, self._environ)
            db.commit()
        logger.info("notification_email_status_observed", extra={
            "previous": claim.status, "status": values.get("email_status", claim.status),
            "failure_code": values.get("email_failure_code"),
        })

    # --- 정리 --------------------------------------------------------------------

    def run_cleanups(self) -> int:
        """한 건씩 — 임대는 삭제 직전에 잡는다. 결과는 claim 때의 attempts 로만 막는다: 성공한 삭제는 임대가
        지났어도 사실이고(재claim 되면 attempts 가 달라 반영되지 않는다), 준비 비밀은 claim 순간부터 저장에 쓰이지 않는다."""
        processed = 0
        while processed < self._batch_size and not self._stopping.is_set():
            with self._sessions() as db:
                claims = settings_repo.claim_cleanups(db, 1, self._lease_seconds)
                db.commit()
            if not claims:
                break
            self._clean(claims[0])
            processed += 1
        return processed

    def _clean(self, claim) -> None:
        error = None
        try:
            if claim.resource_kind == "SECRET":
                self._resources.delete_secret(region=claim.region, name=claim.resource_name)
            else:
                self._resources.delete_topic(region=claim.region, name=claim.resource_name)
        except SettingsProviderError as exc:
            error = exc
        with self._sessions() as db:
            # 관리 접두 밖 이름은 지우지 않고 대기열에서만 뺀다(외부 자원 보호)
            if error is None or error.code.endswith("_name_not_owned"):
                settings_repo.finish_cleanup(db, claim)
            else:
                settings_repo.retry_cleanup(
                    db, claim, delay_seconds=self._backoff(claim.attempts),
                    error_code=error.code,
                )
            db.commit()
        logger.info(
            "notification_settings_cleanup_done" if error is None
            else "notification_settings_cleanup_retry",
            extra={
                "resource_kind": claim.resource_kind, "purpose": claim.purpose,
                "attempts": claim.attempts,
                "error_code": None if error is None else error.code,
            },
        )
