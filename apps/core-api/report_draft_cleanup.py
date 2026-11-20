# ==============================================================================
# [파일 설명]
# 만료된 인시던트 보고서 보관본(#623) 정리 잡. main.py lifespan 이 다른 스케줄러와 함께 기동 · 종료한다.
#
#   - 기동 스위치는 REPORT_DRAFT_CLEANUP_ENABLED 하나다. 수집 · 디스패치 · 구독 · 알림과 독립이다.
#   - 기동 즉시 1회 돌고 이후 매시간 돈다(고정 주기) — 내려가 있던 동안 만료된 행도 다음 성공 정리에서 지워진다.
#   - 만료된 보관본의 조회 차단은 저장소(get_latest)가 즉시 한다. 이 잡은 행 삭제만 맡는다.
#   - 주기마다 세션 1개 · 트랜잭션 1개. 실패하면 되돌리고 세션을 닫으며, 예외는 잡 밖으로 내보내지 않는다.
# ==============================================================================

from __future__ import annotations

import logging
from datetime import UTC, datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from config import get_settings
from db.repositories import report_drafts
from db.session import get_session_factory

logger = logging.getLogger("vigilantis.report_drafts")

JOB_ID = "report_draft_cleanup"
INTERVAL_SECONDS = 3600


def run_cleanup(now: datetime | None = None) -> int:
    """만료된 보관본을 지우고 지운 수를 돌려준다. 실패하면 되돌리고 0 을 돌려준다(다음 주기가 다시 한다)."""
    db = None
    try:
        db = get_session_factory()()
        deleted = report_drafts.delete_expired(db, now=now or datetime.now(UTC))
        db.commit()
    except Exception as exc:  # noqa: BLE001 — 정리 실패는 안전 로그를 남기고 다음 주기에 재시도한다.
        _rollback_and_close(db)
        _log_failure("cleanup", exc)
        return 0
    try:
        db.close()
    except Exception as exc:  # noqa: BLE001 — commit 뒤 close 실패도 원문 노출 없이 기록한다.
        _log_failure("close", exc)
    logger.info("report_draft_cleanup", extra={"deleted": deleted})
    return deleted


def _rollback_and_close(db) -> None:
    if db is None:
        return
    for step in (db.rollback, db.close):
        try:
            step()
        except Exception as exc:  # noqa: BLE001 — rollback 실패가 close를 막지 않도록 각각 격리한다.
            _log_failure("teardown", exc)


def _log_failure(stage: str, exc: Exception) -> None:
    # 예외 메시지 · traceback 에는 접속 정보나 보관본 내용이 섞일 수 있어 남기지 않는다 — 고정 이벤트와 예외 클래스명만.
    logger.error("report_draft_cleanup_failed", extra={"stage": stage, "error_type": type(exc).__name__})


def start_report_draft_cleanup() -> AsyncIOScheduler | None:
    """main 의 lifespan 에서 기동한다. REPORT_DRAFT_CLEANUP_ENABLED=false 면 기동하지 않고 None 이다."""
    if not get_settings().REPORT_DRAFT_CLEANUP_ENABLED:
        logger.info("report draft cleanup disabled: REPORT_DRAFT_CLEANUP_ENABLED=false")
        return None
    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(
        run_cleanup,
        trigger=IntervalTrigger(seconds=INTERVAL_SECONDS),
        id=JOB_ID,
        name="만료된 인시던트 보고서 보관본 정리",
        next_run_time=datetime.now(UTC),  # 기동 즉시 1회
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )
    scheduler.start()
    logger.info("report_draft_cleanup_started", extra={"interval_seconds": INTERVAL_SECONDS})
    return scheduler
