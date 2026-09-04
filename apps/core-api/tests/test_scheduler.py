# ==============================================================================
# [파일 설명]
# 스케줄러 파이프라인 잡의 다중 프로세스 중복 실행 방지(advisory lock) 통합 검증. (#277)
#
#   - 프로세스 내 겹침은 APScheduler max_instances=1 이 막는다(test_scheduler_interval
#     계열과 별개). 이 파일은 **프로세스 간** 겹침 — 두 커넥션(=두 프로세스 모사)이 같은
#     tick 을 잡을 때 실질 실행이 1회뿐인지 PostgreSQL advisory lock 으로 검증한다.
#   - collect_and_store·run_rule_engine 는 스텁으로 대체 — AWS/LocalStack 불필요.
#     get_engine·get_session_factory 를 일회용 테스트 DB(pg_engine)로 고정한다.
# ==============================================================================

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker


def _try_lock(engine, key) -> tuple:
    """다른 프로세스 모사 — 별도 커넥션에서 세션 레벨 락을 시도한다."""
    conn = engine.connect().execution_options(isolation_level="AUTOCOMMIT")
    got = conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": key}).scalar()
    return conn, got


def _bind_scheduler_to_test_db(monkeypatch, pg_engine):
    import db.session as db_session

    monkeypatch.setattr(db_session, "get_engine", lambda: pg_engine)
    monkeypatch.setattr(db_session, "get_session_factory", lambda: sessionmaker(bind=pg_engine))


def _stub_pipeline(monkeypatch, called):
    monkeypatch.setattr(
        "services.collector.collect_and_store",
        lambda: (called.__setitem__("collect", called["collect"] + 1), {"stored": 1})[1],
    )
    monkeypatch.setattr(
        "services.rule_engine.run_rule_engine",
        lambda db: (called.__setitem__("judge", called["judge"] + 1), {"counts": {"SKIP": 1}})[1],
    )


def test_run_pipeline_skips_when_lock_held(pg_engine, monkeypatch):
    """다른 프로세스가 이미 락을 쥐고 있으면 이 tick 은 파이프라인을 돌리지 않고 건너뛴다."""
    from services import scheduler

    _bind_scheduler_to_test_db(monkeypatch, pg_engine)
    called = {"collect": 0, "judge": 0}
    _stub_pipeline(monkeypatch, called)

    holder, got = _try_lock(pg_engine, scheduler._ADVISORY_LOCK_KEY)
    assert got is True  # 다른 프로세스가 선점
    try:
        result = scheduler.run_pipeline()
    finally:
        holder.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": scheduler._ADVISORY_LOCK_KEY})
        holder.close()

    assert result == {"skipped": True}
    assert called["collect"] == 0  # 락 미획득 → 수집·판정 미실행(중복 방지)
    assert called["judge"] == 0


def test_run_pipeline_runs_and_releases_when_lock_free(pg_engine, monkeypatch):
    """락이 비어 있으면 파이프라인을 1회 실행하고, 끝나면 락을 해제한다(다음 tick 이 잡을 수 있다)."""
    from services import scheduler

    _bind_scheduler_to_test_db(monkeypatch, pg_engine)
    called = {"collect": 0, "judge": 0}
    _stub_pipeline(monkeypatch, called)

    result = scheduler.run_pipeline()

    assert called["collect"] == 1 and called["judge"] == 1
    assert result == {"stored": {"stored": 1}, "verdicts": {"SKIP": 1}}

    # 락이 해제됐어야 한다 — 같은 키를 다시 잡을 수 있어야 한다
    conn, got = _try_lock(pg_engine, scheduler._ADVISORY_LOCK_KEY)
    try:
        assert got is True  # 실행 후 해제 확인
    finally:
        conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": scheduler._ADVISORY_LOCK_KEY})
        conn.close()


def test_two_ticks_do_not_overlap(pg_engine, monkeypatch):
    """한 프로세스가 실행 중(락 보유)일 때 다른 tick 이 겹쳐 들어와도 건너뛰고, 실질 실행은 1회뿐이다."""
    from services import scheduler

    _bind_scheduler_to_test_db(monkeypatch, pg_engine)
    called = {"collect": 0, "judge": 0}

    # collect_and_store 안에서 '겹친 tick' 을 모사 — 진행 중 재진입 시 건너뛰는지 본다
    def _collect_then_reenter():
        called["collect"] += 1
        reentrant = scheduler.run_pipeline()  # 락 보유 중 재진입
        assert reentrant == {"skipped": True}  # 겹친 tick 은 건너뜀
        return {"stored": 1}

    monkeypatch.setattr("services.collector.collect_and_store", _collect_then_reenter)
    monkeypatch.setattr(
        "services.rule_engine.run_rule_engine",
        lambda db: (called.__setitem__("judge", called["judge"] + 1), {"counts": {}})[1],
    )

    result = scheduler.run_pipeline()
    assert result["verdicts"] == {}
    assert called["collect"] == 1  # 재진입분은 락에 막혀 collect 재실행 안 됨
    assert called["judge"] == 1


def test_lock_released_when_pipeline_raises(pg_engine, monkeypatch):
    """파이프라인이 예외로 끝나도 락은 finally 에서 풀린다 — 다음 tick 이 잡을 수 있어야
    한 프로세스의 실패가 스캔을 영구히 막지 않는다(#277 완료조건 4, #281 리뷰: 김세혁)."""
    from services import scheduler

    _bind_scheduler_to_test_db(monkeypatch, pg_engine)

    def _boom():
        raise RuntimeError("collect boom")

    monkeypatch.setattr("services.collector.collect_and_store", _boom)

    with pytest.raises(RuntimeError, match="collect boom"):
        scheduler.run_pipeline()

    # 예외에도 락이 해제됐어야 한다 — 같은 키를 다시 잡을 수 있다
    conn, got = _try_lock(pg_engine, scheduler._ADVISORY_LOCK_KEY)
    try:
        assert got is True
    finally:
        conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": scheduler._ADVISORY_LOCK_KEY})
        conn.close()
