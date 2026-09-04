# ==============================================================================
# [파일 설명]  담당: 김세혁 / 김승철
# APScheduler 기반 주기 스캔 스케줄러입니다. (MVP에서 Step Functions/Fargate 대체)
# collector→rule_engine 파이프라인을 주기적으로 실행합니다.
#
# 구현: run_pipeline(수집→정형화→적재→판정) 잡을 IntervalTrigger 로 등록한다.
#   FastAPI 시작 시 start_scheduler() 를 호출하면 된다(main.py 배선은 별도).
#   실행 간격은 CollectorSettings.SCAN_INTERVAL_SECONDS(기본 300초, gt=0 검증).
# ==============================================================================

from __future__ import annotations

import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from config import get_collector_settings

logger = logging.getLogger("vigilantis.scheduler")

JOB_ID = "finops_secops_scan"


def run_pipeline() -> dict:
    """collector → rule_engine 1회 실행. 스케줄러 잡이자 수동 호출 진입점."""
    from db.session import get_session_factory
    from services.collector import collect_and_store
    from services.rule_engine import run_rule_engine

    store = collect_and_store()  # 수집→정형화→assets/metric_summaries upsert
    session_factory = get_session_factory()
    db = session_factory()
    try:
        judged = run_rule_engine(db)  # RuleEvaluation 적재
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
    summary = {"stored": store, "verdicts": judged["counts"]}
    logger.info("scan pipeline done: %s", summary)
    return summary



def _interval_seconds() -> int:
    """스캔 주기(초). 검증된 설정에서만 읽는다 — 생짜 os.getenv 는 0·음수·비정수를
    잡지 못해 잘못된 값이 잡 등록 시점까지 흘러갔다(#255)."""
    return get_collector_settings().SCAN_INTERVAL_SECONDS


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


def start_scheduler() -> "AsyncIOScheduler | None":
    """main의 lifespan에서 기동. 스케줄러를 구성·기동해 반환한다.

    SCAN_ENABLED=false 면 기동하지 않고 None 을 돌려준다 — 테스트가 앱을 띄울 때 실제
    수집·판정 스캔이 도는 것을 막는다(dispatcher.start_dispatcher 와 같은 결)."""
    if not get_collector_settings().SCAN_ENABLED:
        logger.info("scan scheduler disabled: SCAN_ENABLED=false")
        return None
    scheduler = build_scheduler()
    scheduler.start()
    logger.info("scheduler started: job=%s interval=%ss", JOB_ID, _interval_seconds())
    return scheduler
