# ==============================================================================
# [파일 설명]  담당: 김세혁 / 김승철
# APScheduler 기반 주기 스캔 스케줄러입니다. (MVP에서 Step Functions/Fargate 대체)
# collector→rule_engine 파이프라인을 주기적으로 실행합니다.
#
# 구현: run_pipeline(수집→정형화→적재→판정) 잡을 IntervalTrigger 로 등록한다.
#   FastAPI 시작 시 start_scheduler() 를 호출하면 된다(main.py 배선은 별도).
#   실행 간격은 SCAN_INTERVAL_SECONDS(기본 300초). config 확정 시 주입부 교체. (TODO: config)
# ==============================================================================

from __future__ import annotations

import logging
import os

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

logger = logging.getLogger("vigilantis.scheduler")

JOB_ID = "finops_secops_scan"


def run_pipeline() -> dict:
    """collector → rule_engine 1회 실행. 스케줄러 잡이자 수동 호출 진입점.
    수집·적재는 collector 가 자체 세션으로, 판정은 여기서 세션을 열어 수행한다."""
    from db.session import SessionLocal
    from services.collector import collect_and_store
    from services.rule_engine import run_rule_engine

    store = collect_and_store()  # 수집→정형화→assets upsert
    db = SessionLocal()
    try:
        judged = run_rule_engine(db)  # assets 판정(verdict/skip_reason)
    finally:
        db.close()
    summary = {"stored": store, "verdicts": judged["counts"]}
    logger.info("scan pipeline done: %s", summary)
    return summary


def _interval_seconds() -> int:
    # TODO(config): get_settings().scan_interval_seconds 로 교체
    return int(os.getenv("SCAN_INTERVAL_SECONDS", "300"))


def build_scheduler() -> AsyncIOScheduler:
    """스케줄러를 구성하고 파이프라인 잡을 등록한다(기동은 하지 않음)."""
    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(
        run_pipeline,
        trigger=IntervalTrigger(seconds=_interval_seconds()),
        id=JOB_ID,
        name="FinOps/SecOps 자산 스캔 파이프라인",
        max_instances=1,       # 이전 실행이 안 끝났으면 겹쳐 돌지 않음
        coalesce=True,         # 밀린 실행은 1회로 합침
        replace_existing=True,
    )
    return scheduler


def start_scheduler() -> AsyncIOScheduler:
    """main의 lifespan에서 기동(#67에서 배선). 스케줄러를 구성·기동해 반환한다."""
    scheduler = build_scheduler()
    scheduler.start()
    logger.info("scheduler started: job=%s interval=%ss", JOB_ID, _interval_seconds())
    return scheduler
