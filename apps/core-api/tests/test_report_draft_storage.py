"""인시던트 보고서 최신 생성본 보관(#623) — 저장소 계약, 정리 잡, 수명주기.

  ① 저장소(PostgreSQL): 교체는 1행 유지 · 오래된 생성은 새 보관본을 덮지 못함 · 호출자 rollback 이 이전 보관본을
     남김 · 고객사 정확 일치(None 포함) · 만료 경계 · 만료 행만 삭제 · 인시던트 삭제 CASCADE · DB 불변식.
  ② 정리 잡: 만료만 지우고 commit, 실패는 rollback, 세션은 항상 닫힘.
  ③ 기동: 기본 켜짐 · 즉시 1회 · 매시간 · 스위치로 끔 · 실제 lifespan 이 띄우고 내림.
"""

from __future__ import annotations

import asyncio
import threading
from datetime import UTC, datetime, timedelta

import pytest
import report_draft_cleanup
from config import Settings, get_settings
from db import models
from db.repositories import report_drafts as repo
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

T0 = datetime(2026, 11, 18, 0, 0, tzinfo=UTC)


def _save(db, incident, *, as_of=T0, generated_at=None, md=b"# md", js=b"{}", tenant_id=None):
    return repo.save_latest(
        db,
        incident_id=incident.incident_id,
        tenant_id=tenant_id,
        as_of=as_of,
        generated_at=generated_at or as_of,
        markdown=md,
        json_body=js,
    )


def _count(db):
    return db.execute(select(func.count()).select_from(models.IncidentReportDraft)).scalar_one()


def _get(db, incident, *, tenant_id=None, now=T0):
    return repo.get_latest(db, incident_id=incident.incident_id, tenant_id=tenant_id, now=now)


# --- ① 저장소 ------------------------------------------------------------------------


def test_save_then_get_returns_exact_bytes_and_seven_day_expiry(db, make_incident):
    incident = make_incident(db)
    md, js = b"# \xed\x95\x9c\xea\xb8\x80\r\n", b'{"a": 1}\n'
    assert _save(db, incident, md=md, js=js) is True

    row = _get(db, incident)
    assert bytes(row.markdown) == md and bytes(row.json_body) == js
    assert row.expires_at == T0 + timedelta(days=7)
    assert row.generated_at == T0 and row.as_of == T0


def test_replace_keeps_one_row_with_the_newer_generation(db, make_incident):
    incident = make_incident(db)
    assert _save(db, incident, as_of=T0, md=b"old") is True
    assert _save(db, incident, as_of=T0 + timedelta(hours=1), md=b"new", js=b"[1]") is True

    assert _count(db) == 1
    row = _get(db, incident, now=T0 + timedelta(hours=1))
    assert bytes(row.markdown) == b"new" and bytes(row.json_body) == b"[1]"
    assert row.expires_at == T0 + timedelta(hours=1) + timedelta(days=7)


def test_older_as_of_never_overwrites_a_newer_generation(db, make_incident):
    incident = make_incident(db)
    assert _save(db, incident, as_of=T0 + timedelta(hours=1), generated_at=T0, md=b"new") is True
    # 나중에 끝났어도 as_of 가 오래된 생성은 진다
    assert _save(db, incident, as_of=T0, generated_at=T0 + timedelta(hours=2), md=b"old") is False
    assert bytes(_get(db, incident).markdown) == b"new"


def test_equal_as_of_is_broken_by_generated_at(db, make_incident):
    incident = make_incident(db)
    assert _save(db, incident, generated_at=T0 + timedelta(minutes=5), md=b"later") is True
    assert _save(db, incident, generated_at=T0 + timedelta(minutes=1), md=b"earlier") is False
    assert bytes(_get(db, incident).markdown) == b"later"
    assert _save(db, incident, generated_at=T0 + timedelta(minutes=9), md=b"latest") is True
    assert bytes(_get(db, incident).markdown) == b"latest"


def test_rolled_back_replacement_keeps_the_previous_saved_generation(db, make_incident):
    incident = make_incident(db)
    assert _save(db, incident, md=b"kept") is True
    db.commit()

    assert _save(db, incident, as_of=T0 + timedelta(hours=1), md=b"failed") is True
    db.rollback()  # 호출자가 되돌린다 — 저장소는 commit · rollback 을 하지 않는다

    assert bytes(_get(db, incident).markdown) == b"kept"


def test_get_requires_exact_tenant_match_including_none(db, make_incident, make_tenant):
    tenant, other = make_tenant(), make_tenant()
    owned, orphan = make_incident(db), make_incident(db)
    owned.tenant_id = tenant
    db.flush()
    _save(db, owned, tenant_id=tenant)
    _save(db, orphan, tenant_id=None)

    assert _get(db, owned, tenant_id=tenant) is not None
    assert _get(db, owned, tenant_id=other) is None
    assert _get(db, owned, tenant_id=None) is None
    assert _get(db, orphan, tenant_id=None) is not None
    assert _get(db, orphan, tenant_id=tenant) is None


def test_get_is_denied_exactly_at_expiry(db, make_incident):
    incident = make_incident(db)
    _save(db, incident)
    expiry = T0 + timedelta(days=7)
    assert _get(db, incident, now=expiry - timedelta(microseconds=1)) is not None
    assert _get(db, incident, now=expiry) is None
    assert _get(db, incident, now=expiry + timedelta(seconds=1)) is None


def test_delete_expired_removes_only_rows_at_or_past_expiry(db, make_incident):
    old, edge, fresh = make_incident(db), make_incident(db), make_incident(db)
    _save(db, old, generated_at=T0 - timedelta(days=8), as_of=T0 - timedelta(days=8))
    _save(db, edge, generated_at=T0 - timedelta(days=7), as_of=T0 - timedelta(days=7))
    _save(db, fresh, generated_at=T0 - timedelta(days=6), as_of=T0 - timedelta(days=6))

    assert repo.delete_expired(db, now=T0) == 2
    assert _count(db) == 1
    assert _get(db, fresh, now=T0) is not None


def test_deleting_the_incident_removes_its_artifact_only(db, make_incident):
    gone, kept = make_incident(db), make_incident(db)
    _save(db, gone)
    _save(db, kept)

    db.delete(gone)
    db.flush()

    assert _count(db) == 1
    assert _get(db, kept) is not None


def test_database_rejects_retention_other_than_seven_days_and_oversize_output(db, make_incident):
    incident = make_incident(db)
    db.add(
        models.IncidentReportDraft(
            incident_id=incident.incident_id,
            as_of=T0,
            generated_at=T0,
            expires_at=T0 + timedelta(days=8),
            markdown=b"x",
            json_body=b"x",
        )
    )
    with pytest.raises(IntegrityError):
        db.flush()
    db.rollback()

    big = make_incident(db)
    with pytest.raises(IntegrityError):
        _save(db, big, md=b"x" * (2 * 1024 * 1024 + 1))
    db.rollback()


# --- ② 정리 잡 -----------------------------------------------------------------------


class _Session:
    """정리 잡이 쓴 호출을 기록하는 세션 대역."""

    def __init__(self, fail=False):
        self.calls, self.fail = [], fail

    def commit(self):
        self.calls.append("commit")

    def rollback(self):
        self.calls.append("rollback")

    def close(self):
        self.calls.append("close")


def test_cleanup_job_deletes_expired_keeps_fresh_and_commits(db, make_incident, monkeypatch):
    expired, fresh = make_incident(db), make_incident(db)
    now = datetime.now(UTC)
    _save(db, expired, as_of=now - timedelta(days=8), generated_at=now - timedelta(days=8))
    _save(db, fresh, as_of=now - timedelta(days=1), generated_at=now - timedelta(days=1))
    monkeypatch.setattr(report_draft_cleanup, "get_session_factory", lambda: lambda: db)

    assert report_draft_cleanup.run_cleanup() == 1

    remaining = db.execute(select(models.IncidentReportDraft.incident_id)).scalars().all()
    assert remaining == [fresh.incident_id]


def test_cleanup_job_rolls_back_closes_and_swallows_failure(monkeypatch):
    session = _Session()
    monkeypatch.setattr(report_draft_cleanup, "get_session_factory", lambda: lambda: session)

    def boom(db, *, now):
        raise RuntimeError("db down")

    monkeypatch.setattr(report_draft_cleanup.report_drafts, "delete_expired", boom)

    assert report_draft_cleanup.run_cleanup() == 0
    assert session.calls == ["rollback", "close"]


SECRET = "postgresql://user:s3cr3t-pw@host/db # artifact-body"


def _failure_logs(caplog):
    return [r for r in caplog.records if r.getMessage() == "report_draft_cleanup_failed"]


def _assert_no_leak(caplog):
    for record in caplog.records:
        assert record.exc_info is None
        assert "s3cr3t" not in record.getMessage() and "artifact-body" not in str(record.__dict__)


def test_cleanup_failure_log_is_a_fixed_event_without_exception_text(monkeypatch, caplog):
    session = _Session()
    monkeypatch.setattr(report_draft_cleanup, "get_session_factory", lambda: lambda: session)

    def boom(db, *, now):
        raise RuntimeError(SECRET)

    monkeypatch.setattr(report_draft_cleanup.report_drafts, "delete_expired", boom)
    with caplog.at_level("INFO", logger="vigilantis.report_drafts"):
        assert report_draft_cleanup.run_cleanup() == 0

    [record] = _failure_logs(caplog)
    assert record.error_type == "RuntimeError" and record.stage == "cleanup"
    assert not [r for r in caplog.records if r.getMessage() == "report_draft_cleanup"]
    _assert_no_leak(caplog)


def test_session_acquisition_failure_is_logged_safely_and_swallowed(monkeypatch, caplog):
    def factory():
        raise RuntimeError(SECRET)

    monkeypatch.setattr(report_draft_cleanup, "get_session_factory", lambda: factory)
    with caplog.at_level("INFO", logger="vigilantis.report_drafts"):
        assert report_draft_cleanup.run_cleanup() == 0

    [record] = _failure_logs(caplog)
    assert record.error_type == "RuntimeError"
    _assert_no_leak(caplog)


def test_teardown_failures_are_logged_safely_and_do_not_escape(monkeypatch, caplog):
    class BadSession(_Session):
        def rollback(self):
            raise RuntimeError(SECRET)

        def close(self):
            self.calls.append("close")
            raise RuntimeError(SECRET)

    session = BadSession()
    monkeypatch.setattr(report_draft_cleanup, "get_session_factory", lambda: lambda: session)
    monkeypatch.setattr(
        report_draft_cleanup.report_drafts, "delete_expired", lambda db, *, now: (_ for _ in ()).throw(ValueError(SECRET)))
    with caplog.at_level("INFO", logger="vigilantis.report_drafts"):
        assert report_draft_cleanup.run_cleanup() == 0

    assert session.calls == ["close"]
    assert {r.stage for r in _failure_logs(caplog)} == {"cleanup", "teardown"}
    _assert_no_leak(caplog)


def test_close_failure_after_commit_is_logged_and_deletion_is_still_reported(monkeypatch, caplog):
    class CloseFails(_Session):
        def close(self):
            raise RuntimeError(SECRET)

    session = CloseFails()
    monkeypatch.setattr(report_draft_cleanup, "get_session_factory", lambda: lambda: session)
    monkeypatch.setattr(report_draft_cleanup.report_drafts, "delete_expired", lambda db, *, now: 2)
    with caplog.at_level("INFO", logger="vigilantis.report_drafts"):
        assert report_draft_cleanup.run_cleanup() == 2

    [record] = _failure_logs(caplog)
    assert record.stage == "close"
    _assert_no_leak(caplog)


def test_cleanup_job_closes_the_session_after_success(monkeypatch):
    session = _Session()
    monkeypatch.setattr(report_draft_cleanup, "get_session_factory", lambda: lambda: session)
    monkeypatch.setattr(report_draft_cleanup.report_drafts, "delete_expired", lambda db, *, now: 3)

    assert report_draft_cleanup.run_cleanup() == 3
    assert session.calls == ["commit", "close"]


# --- ③ 기동 · 수명주기 ----------------------------------------------------------------


@pytest.fixture()
def cleanup_env(monkeypatch):
    def configure(value: str) -> None:
        monkeypatch.setenv("REPORT_DRAFT_CLEANUP_ENABLED", value)
        get_settings.cache_clear()

    yield configure
    get_settings.cache_clear()


def test_cleanup_is_on_by_default(monkeypatch):
    monkeypatch.delenv("REPORT_DRAFT_CLEANUP_ENABLED", raising=False)
    assert Settings(_env_file=None).REPORT_DRAFT_CLEANUP_ENABLED is True


def test_disabled_switch_does_not_start(cleanup_env):
    cleanup_env("false")
    assert report_draft_cleanup.start_report_draft_cleanup() is None


def test_enabled_switch_starts_an_hourly_job_that_runs_at_once(cleanup_env, monkeypatch):
    cleanup_env("true")
    ran = threading.Event()
    monkeypatch.setattr(report_draft_cleanup, "run_cleanup", lambda: ran.set())

    async def scenario():
        scheduler = report_draft_cleanup.start_report_draft_cleanup()
        try:
            job = scheduler.get_job(report_draft_cleanup.JOB_ID)
            assert job.trigger.interval == timedelta(hours=1)
            assert job.max_instances == 1 and job.coalesce is True
            assert await asyncio.to_thread(ran.wait, 5)
        finally:
            scheduler.shutdown(wait=False)
        await asyncio.sleep(0)
        assert not scheduler.running

    asyncio.run(scenario())


def test_real_lifespan_starts_and_stops_the_cleanup_job(cleanup_env, monkeypatch):
    import main
    from fastapi.testclient import TestClient

    cleanup_env("true")
    monkeypatch.setattr(report_draft_cleanup, "run_cleanup", lambda: 0)
    started = []
    real_start = main.start_report_draft_cleanup

    def recording_start():
        scheduler = real_start()
        started.append(scheduler)
        return scheduler

    monkeypatch.setattr(main, "start_report_draft_cleanup", recording_start)
    with TestClient(main.create_app()) as client:
        [scheduler] = started
        assert scheduler is not None and scheduler.running
        assert scheduler.get_job(report_draft_cleanup.JOB_ID) is not None
        assert client.get("/health").status_code == 200
    assert not scheduler.running
