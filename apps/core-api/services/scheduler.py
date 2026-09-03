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

# 파이프라인 잡 전용 PostgreSQL advisory lock 키(고정 64bit 정수).
# 다중 워커/레플리카에서 같은 tick 이 파이프라인을 겹쳐 돌리지 않게 한다 — 프로세스 내
# 겹침은 build_scheduler 의 max_instances=1 이, 프로세스 간 겹침은 이 락이 막는다. (#277)
# 값은 이 잡 전용으로 고정한 임의 상수다("vig_scan" ASCII, 다른 advisory lock 과 비충돌).
_ADVISORY_LOCK_KEY = 0x7669675F7363616E


def run_pipeline() -> dict:
    """collector → rule_engine 1회 실행. 스케줄러 잡이자 수동 호출 진입점.

    다중 워커/레플리카에서 같은 tick 이 파이프라인을 겹쳐 돌리지 않도록 PostgreSQL
    세션 레벨 advisory lock 을 전용 커넥션에 잡고 실행한다. 락을 못 잡으면(다른 프로세스가
    이미 실행 중) 이 tick 을 건너뛴다({"skipped": True}). 끝나면 락을 해제하고 커넥션을
    반납한다. 프로세스 내 겹침은 max_instances=1, 프로세스 간 겹침은 이 락이 막는다. (#277)
    """
    from sqlalchemy import text

    from db.session import get_engine, get_session_factory
    from services.collector import collect_and_store
    from services.rule_engine import run_rule_engine

    # AUTOCOMMIT — 세션 레벨 advisory lock 은 트랜잭션이 아니라 커넥션에 매이므로,
    # 장시간 열린 트랜잭션을 남기지 않고 커넥션이 살아있는 동안 락을 유지한다.
    lock_conn = get_engine().connect().execution_options(isolation_level="AUTOCOMMIT")
    try:
        acquired = lock_conn.execute(
            text("SELECT pg_try_advisory_lock(:k)"), {"k": _ADVISORY_LOCK_KEY}
        ).scalar()
        if not acquired:
            logger.info("scan pipeline skipped: 다른 프로세스가 이미 실행 중(advisory lock 미획득)")
            return {"skipped": True}

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
    finally:
        try:
            lock_conn.execute(
                text("SELECT pg_advisory_unlock(:k)"), {"k": _ADVISORY_LOCK_KEY}
            )
        except Exception:
            logger.exception("advisory unlock 실패 — 커넥션 반납으로 세션 종료 시 해제됨")
        lock_conn.close()



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


def start_scheduler() -> AsyncIOScheduler:
    """main의 lifespan에서 기동(#67에서 배선). 스케줄러를 구성·기동해 반환한다."""
    scheduler = build_scheduler()
    scheduler.start()
    logger.info("scheduler started: job=%s interval=%ss", JOB_ID, _interval_seconds())
    return scheduler
