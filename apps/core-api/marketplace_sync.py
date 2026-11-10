# ==============================================================================
# [파일 설명]  담당: 김세혁 (BE · Marketplace 실연동)
# 실제 AWS Marketplace 동기화 흐름(ADR-0012 2차 개정) — 등록 랜딩 처리 · 계약 변경 알림 수신 · 권리 끝 재확인.
# 판정은 marketplace.py, 투영은 subscription_workflow.reconcile_tenant_subscription 이 한다.
#
#   - 상품 코드(MARKETPLACE_PRODUCT_CODE)가 비어 있으면 아무것도 하지 않는다. 스케줄러도 뜨지 않는다.
#   - 알림은 EventBridge(`aws.agreement-marketplace`) → SQS 다. 2026-07-15 이후 판매자 등록이면 SNS 알림을 쓸 수
#     없다(AWS 문서). 알림 내용은 「어느 계정을 다시 읽을지」로만 쓰고, 권리는 GetEntitlements 로 다시 읽는다.
#   - 메시지는 처리에 성공했거나 처리할 대상이 아닐 때만 지운다. 재확인이 실패하면 남겨 두어 SQS 가 다시 준다.
#   - 권리 끝 시각이 지난 테넌트는 알림이 없어도 주기마다 다시 읽는다 — 갱신 알림을 놓쳐 거짓으로 멈추지 않게.
#   - 트랜잭션: 계정 1개마다 commit 한다(한 계정의 장애가 다른 계정을 막지 않는다).
# ==============================================================================

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any

import marketplace
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from config import MarketplaceSettings, get_marketplace_settings, get_subscription_settings
from db.repositories import marketplace as repo
from db.repositories import tenants as tenants_repo
from db.session import get_session_factory
from services.aws.settings_notifications import platform_client
from sqlalchemy.orm import Session
from subscription_workflow import reconcile_tenant_subscription

logger = logging.getLogger("vigilantis.marketplace")

JOB_ID = "marketplace_cycle"
EVENT_SOURCE = "aws.agreement-marketplace"
#: 다시 읽을 계정을 알려 주는 이벤트. 계약형 상품은 License Updated 하나만 반응하면 되지만(AWS 문서), 계약 생성 ·
#: 변경 · 종료도 같은 재확인으로 받는다 — 재확인은 멱등이다.
_REFRESH_DETAIL_TYPES = (
    "License Updated",
    "Purchase Agreement Created",
    "Purchase Agreement Amended",
    "Purchase Agreement Ended",
)
#: 한 주기에 다시 읽는 만료 계정 수 · 받는 메시지 묶음 수의 상한.
DUE_BATCH = 50
RECEIVE_ROUNDS = 5

ClientFactory = Callable[[str, str], Any]


def default_client(service: str, region: str):
    return platform_client(service, region, connect_timeout=3.0, read_timeout=10.0)


# --- 계정 1개 재확인 ----------------------------------------------------------------


def refresh_account(
    db: Session,
    account_id: str,
    *,
    settings: MarketplaceSettings,
    clients: ClientFactory,
    now: datetime,
    resolved: marketplace.ResolvedCustomer | None = None,
) -> str | None:
    """그 계정의 권리를 다시 읽어 사본을 맞추고, 그 계정의 테넌트가 있으면 투영한다 → 테넌트 ID(없으면 None).
    commit 하지 않는다. AWS 오류는 그대로 던진다(호출부가 되돌리고 다음에 다시 한다)."""
    grants = marketplace.fetch_grants(
        account_id,
        settings=settings,
        client=clients("marketplace-entitlement", settings.MARKETPLACE_ENTITLEMENT_REGION),
    )
    marketplace.apply_grants(
        db,
        account_id,
        grants,
        product_code=settings.MARKETPLACE_PRODUCT_CODE.strip(),
        now=now,
        resolved=resolved,
    )
    tenant_id = tenants_repo.tenant_id_for_account(db, account_id)
    if tenant_id is not None:
        reconcile_tenant_subscription(db, tenant_id, now=now)
    return tenant_id


# --- 등록 랜딩 ------------------------------------------------------------------------


@dataclass(frozen=True)
class Registration:
    """등록 랜딩 결과. 테넌트가 없으면 가입 화면, 있으면 구독 화면으로 보낸다."""

    accepted: bool
    tenant_id: str | None = None
    reason: str | None = None


def register(
    db: Session,
    token: str,
    *,
    settings: MarketplaceSettings,
    clients: ClientFactory | None = None,
    now: datetime | None = None,
) -> Registration:
    """구매자 브라우저가 보낸 등록 토큰을 처리한다. 토큰이 맞으면 그 계정의 권리를 바로 읽어 둔다.

    테넌트는 여기서 만들지 않는다. 고객이 가입 · 연결 확인(T-4)을 마쳐 같은 AWS 계정의 테넌트가 생기면, 그
    뒤의 재확인이 계정 ID 로 권리를 잇는다. 연결 확인은 그 계정의 역할에 실제로 들어가 봐야 통과하므로, 남의
    계정 ID 를 적어 남의 권리를 가져갈 수 없다.
    """
    now = now or datetime.now(UTC)
    clients = clients or default_client
    try:
        resolved = marketplace.resolve_customer(
            token,
            settings=settings,
            client=clients("meteringmarketplace", settings.MARKETPLACE_METERING_REGION),
        )
    except marketplace.MarketplaceRejected as exc:
        logger.warning("marketplace_registration_rejected", extra={"reason": exc.reason})
        return Registration(accepted=False, reason=exc.reason)
    try:
        tenant_id = refresh_account(
            db, resolved.aws_account_id, settings=settings, clients=clients, now=now, resolved=resolved
        )
        db.commit()
    except Exception:
        db.rollback()
        # 토큰은 맞았다 — 권리는 다음 알림 · 재확인이 다시 읽는다. 고객에게는 가입을 이어 가게 한다
        logger.exception(
            "marketplace_registration_refresh_failed",
            extra={"aws_account_id": resolved.aws_account_id},
        )
        tenant_id = tenants_repo.tenant_id_for_account(db, resolved.aws_account_id)
    logger.info(
        "marketplace_registration_accepted",
        extra={"aws_account_id": resolved.aws_account_id, "tenant_id": tenant_id},
    )
    return Registration(accepted=True, tenant_id=tenant_id)


# --- 알림 수신 ------------------------------------------------------------------------


def account_of_event(body: str, *, product_code: str) -> str | None:
    """SQS 메시지 본문(EventBridge 이벤트) → 다시 읽을 고객 계정. 대상이 아니면 None."""
    try:
        event = json.loads(body)
    except (TypeError, ValueError):
        return None
    if not isinstance(event, dict) or event.get("source") != EVENT_SOURCE:
        return None
    detail_type = event.get("detail-type") or ""
    if not any(detail_type.startswith(prefix) for prefix in _REFRESH_DETAIL_TYPES):
        return None
    detail = event.get("detail") or {}
    product = (detail.get("product") or {}).get("code")
    if product is not None and product != product_code:
        return None
    account = (detail.get("acceptor") or {}).get("accountId")
    if not isinstance(account, str) or len(account) != 12 or not account.isdigit():
        return None
    return account


@dataclass
class CycleReport:
    received: int = 0
    refreshed: int = 0
    ignored: int = 0
    failed: int = 0
    due_refreshed: int = 0


def _refresh_committed(db: Session, account_id: str, **kwargs) -> bool:
    try:
        refresh_account(db, account_id, **kwargs)
        db.commit()
        return True
    except Exception:
        db.rollback()
        logger.exception("marketplace_refresh_failed", extra={"aws_account_id": account_id})
        return False


def receive_events(
    db: Session,
    *,
    settings: MarketplaceSettings,
    clients: ClientFactory,
    now: datetime,
    report: CycleReport,
) -> None:
    """큐에 쌓인 알림을 받아 계정별로 다시 읽는다. 같은 묶음에 같은 계정이 여러 번 오면 한 번만 읽는다."""
    queue = settings.MARKETPLACE_EVENTS_QUEUE_URL.strip()
    if not queue:
        return
    sqs = clients("sqs", settings.MARKETPLACE_EVENTS_REGION)
    product = settings.MARKETPLACE_PRODUCT_CODE.strip()
    for _ in range(RECEIVE_ROUNDS):
        messages = sqs.receive_message(QueueUrl=queue, MaxNumberOfMessages=10, WaitTimeSeconds=0).get(
            "Messages"
        ) or []
        if not messages:
            return
        report.received += len(messages)
        outcome: dict[str, bool] = {}
        done = []
        for message in messages:
            account = account_of_event(message.get("Body", ""), product_code=product)
            if account is None:
                report.ignored += 1
                done.append(message)
                continue
            if account not in outcome:
                ok = _refresh_committed(db, account, settings=settings, clients=clients, now=now)
                outcome[account] = ok
                if ok:
                    report.refreshed += 1
                else:
                    report.failed += 1
            if outcome[account]:
                done.append(message)
        for message in done:
            sqs.delete_message(QueueUrl=queue, ReceiptHandle=message["ReceiptHandle"])


def refresh_due(
    db: Session,
    *,
    settings: MarketplaceSettings,
    clients: ClientFactory,
    now: datetime,
    report: CycleReport,
) -> None:
    """권리 끝 시각이 지난 실제 Marketplace 투영을 다시 읽는다. 갱신됐으면 이어지고, 끝났으면 투영이 멈춘다."""
    accounts = repo.due_accounts(db, now=now, limit=DUE_BATCH)
    db.rollback()
    for account in accounts:
        if _refresh_committed(db, account, settings=settings, clients=clients, now=now):
            report.due_refreshed += 1
        else:
            report.failed += 1


def run_cycle(
    settings: MarketplaceSettings, clients: ClientFactory | None = None, *, now: datetime | None = None
) -> CycleReport:
    """잡 본체 — 알림 수신 → 권리 끝 재확인. 주기마다 세션 1개."""
    now = now or datetime.now(UTC)
    clients = clients or default_client
    report = CycleReport()
    with get_session_factory()() as db:
        try:
            receive_events(db, settings=settings, clients=clients, now=now, report=report)
        except Exception:
            db.rollback()
            logger.exception("marketplace_receive_failed")
        refresh_due(db, settings=settings, clients=clients, now=now, report=report)
    logger.info("marketplace_cycle", extra=asdict(report))
    return report


def start_marketplace_scheduler() -> AsyncIOScheduler | None:
    """main 의 lifespan 에서 기동한다. 상품 코드가 없거나 구독 스케줄러가 꺼져 있으면 기동하지 않는다."""
    settings = get_marketplace_settings()
    subscription = get_subscription_settings()
    if not settings.enabled or not subscription.SUBSCRIPTION_SCHEDULER_ENABLED:
        logger.info("marketplace scheduler disabled", extra={"product_code_set": settings.enabled})
        return None
    _ = settings.dimension_plans  # 잘못된 대응표는 기동에서 실패한다
    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(
        run_cycle,
        args=(settings,),
        trigger=IntervalTrigger(seconds=subscription.SUBSCRIPTION_DUE_INTERVAL_SECONDS),
        id=JOB_ID,
        name="AWS Marketplace 알림 수신 · 권리 끝 재확인",
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )
    scheduler.start()
    logger.info("marketplace_scheduler_started", extra={
        "events_queue_set": bool(settings.MARKETPLACE_EVENTS_QUEUE_URL.strip()),
    })
    return scheduler
