# ==============================================================================
# [파일 설명]  담당: 안성일 (BE · T-16)
# 고객 구독 계약 처리 잡(Post-MVP T-16 · #528) — 주기마다 subscription_workflow.process_subscription_cycle 을
# 한 번 부른다(모의 Marketplace 의 활성화 · 갱신 · 만료 → 이벤트 소비 · 투영). main.py lifespan 이 다른
# 스케줄러와 함께 기동 · 종료한다.
#
#   - 기동 스위치는 SUBSCRIPTION_SCHEDULER_ENABLED 하나다(config.SubscriptionSettings). SCAN_ENABLED ·
#     DISPATCH_ENABLED · 새 구매 스위치(SUBSCRIPTION_MOCK_MARKETPLACE_ENABLED)와 독립이다 — 저장된 계약은
#     새 구매를 닫아도, 수집 · 실행을 꺼도, 테넌트가 정지돼도 기간대로 처리된다.
#   - 대상은 매 주기 DB 에서 고르고 쓰기는 revision 조건부 UPDATE · 이벤트 잠금(SKIP LOCKED)이다 — 재기동 ·
#     겹친 실행 · 다른 프로세스가 같은 전이를 두 번 하지 않는다. 프로세스 안 겹침은 max_instances=1 이 막는다.
#   - 이벤트 훑기 위치(subscription_workflow.EventSweep)는 기동 때 하나 만들어 주기마다 같은 것을 넘긴다 — 한 주기는
#     상한 건수까지만 소비하고 다음 주기가 그 뒤를 이어 받는다. 재기동하면 처음부터 다시 훑는다.
#   - AWS 를 부르지 않는다. 시각은 여기서 읽어(UTC) 워크플로에 넘긴다 — 날짜는 워크플로가 KST 로 바꾼다.
# ==============================================================================

from __future__ import annotations

import logging
from datetime import UTC, datetime
from functools import partial

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from config import SubscriptionSettings, get_subscription_settings
from db.session import get_session_factory
from subscription_workflow import CycleReport, EventSweep, process_subscription_cycle

logger = logging.getLogger("vigilantis.subscriptions")

JOB_ID = "subscription_cycle"


def run_subscription_cycle(settings: SubscriptionSettings, sweep: EventSweep) -> CycleReport:
    """잡 본체 — 주기마다 세션 1개를 만들고 닫는다. 세션 팩토리는 실행 시점에 읽는다(lru_cache 재설정 대응)."""
    with get_session_factory()() as db:
        return process_subscription_cycle(db, settings=settings, sweep=sweep, now=datetime.now(UTC))


def start_subscription_scheduler() -> AsyncIOScheduler | None:
    """main 의 lifespan 에서 기동한다. SUBSCRIPTION_SCHEDULER_ENABLED=false 면 기동하지 않고 None 이다."""
    settings = get_subscription_settings()
    if not settings.SUBSCRIPTION_SCHEDULER_ENABLED:
        logger.info("subscription scheduler disabled: SUBSCRIPTION_SCHEDULER_ENABLED=false")
        return None
    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(
        partial(run_subscription_cycle, settings, EventSweep()),
        trigger=IntervalTrigger(seconds=settings.SUBSCRIPTION_DUE_INTERVAL_SECONDS),
        id=JOB_ID,
        name="고객 구독 계약 처리(모의 Marketplace 활성화 · 갱신 · 만료 · 투영)",
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )
    scheduler.start()
    logger.info("subscription_scheduler_started", extra={
        "interval_seconds": settings.SUBSCRIPTION_DUE_INTERVAL_SECONDS,
        "mock_marketplace_enabled": settings.SUBSCRIPTION_MOCK_MARKETPLACE_ENABLED,
        "mock_activation_outcome": settings.SUBSCRIPTION_MOCK_ACTIVATION_OUTCOME,
    })
    return scheduler
